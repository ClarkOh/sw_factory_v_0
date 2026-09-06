# Binance BTCUSDT Futures Trading System
## Software Specification — Principles, Use Cases, Architecture

## 1. 목적

본 시스템은 Binance의 BTCUSDT 선물 거래를 수동 명령과 자동화된 거래 전략을 통해 실행하고 관리하는 프로그램이다.

사용자는 다음 두 인터페이스를 통해 시스템을 사용할 수 있어야 한다.

1. 로컬 또는 서버의 명령어 창(Command Console)
2. 웹 기반 관리 화면(Web Dashboard)

시스템은 Binance API를 통해 주문 실행, 주문 상태 확인, 포지션 관리, 손절·익절 설정, 시장 데이터 수집을 수행한다.

향후에는 다음 자동화 파이프라인을 지원한다.

> 시장 데이터 수집 → 지표 계산 → 신호 계산 → 리스크 계산 → 예산 배분 → 거래 실행 → 결과 통지 및 기록

지표, 전략, 리스크 계산 방식, 예산 관리 정책, 알림 채널은 아직 확정되지 않았으므로 교체 가능한 모듈로 설계한다.

---

# 2. 핵심 원칙

## P-01. 시스템은 죽으면 안 된다

프로세스, 네트워크, 외부 API 또는 내부 모듈에 장애가 발생하더라도 시스템 전체가 중단되지 않아야 한다.

구체적인 요구사항은 다음과 같다.

- 처리되지 않은 예외 하나가 전체 프로세스를 종료시키지 않아야 한다.
- 각 주요 모듈은 서로 장애가 격리되어야 한다.
- 장애가 발생한 작업은 정책에 따라 자동으로 재시도해야 한다.
- WebSocket 연결이 끊어지면 자동으로 재연결해야 한다.
- Binance API 장애나 네트워크 단절 시 제한된 횟수와 간격으로 재시도해야 한다.
- 재시도 간격에는 exponential backoff와 jitter를 적용한다.
- 시스템 재시작 후에도 기존 주문, 포지션, 처리 중이던 명령을 복구해야 한다.
- 복구 시 로컬 상태만 신뢰하지 않고 Binance의 실제 주문 및 포지션 상태와 대조해야 한다.
- 반복적으로 실패하는 작업은 별도의 실패 상태로 격리하고 운영자에게 알려야 한다.
- 장애가 발생해도 명령어 창, 상태 조회 및 긴급 정지 기능은 가능한 한 유지되어야 한다.
- 시스템 상태를 `HEALTHY`, `DEGRADED`, `TRADING_PAUSED`, `RECOVERING`, `FAILED` 등으로 명확히 표시해야 한다.

"죽지 않는다"는 무조건 주문을 계속한다는 의미가 아니다. 상태가 불확실하거나 안전을 보장할 수 없으면 신규 거래를 중단하고 기존 포지션과 주문 상태를 확인할 수 있는 안전 모드로 전환한다.

## P-02. 거래 트랜잭션의 정합성을 반드시 보장한다

거래 명령은 금전과 포지션을 변경하는 트랜잭션으로 취급한다.

각 거래 명령은 다음 상태 흐름을 가져야 한다.

> RECEIVED → VALIDATED → APPROVED → SUBMITTED → ACKNOWLEDGED → FILLED/PARTIALLY_FILLED/CANCELED/REJECTED/EXPIRED

요구사항은 다음과 같다.

- 모든 거래 명령에는 시스템 내부의 고유한 `command_id`를 부여한다.
- Binance 주문에는 고유한 `client_order_id`를 사용한다.
- 동일한 명령을 재시도하더라도 중복 주문이 발생하지 않아야 한다.
- 주문 요청의 응답이 유실되거나 타임아웃이 발생했다고 해서 즉시 같은 주문을 다시 생성하면 안 된다.
- 결과가 불명확한 주문은 `client_order_id`를 이용하여 Binance에서 실제 상태를 먼저 조회한다.
- 주문 전송 전과 전송 후의 상태를 영구 저장소에 기록한다.
- 시스템 내부 상태와 Binance 상태가 다르면 reconciliation 절차를 수행한다.
- 부분 체결을 독립적인 상태로 처리해야 한다.
- 진입 주문과 손절·익절 주문 사이의 관계를 기록해야 한다.
- 진입 주문이 실패하거나 체결되지 않았다면 대응되는 손절·익절 주문을 잘못 생성해서는 안 된다.
- 포지션 수량보다 큰 reduce-only 청산 주문이 생성되지 않도록 검증해야 한다.
- 모든 금액과 수량 계산에는 부동소수점이 아닌 정확한 decimal 연산을 사용한다.
- Binance의 tick size, step size, 최소 주문 수량 및 최소 명목금액 규칙을 적용해야 한다.
- 저장된 로그만으로 명령 접수부터 최종 결과까지 재구성할 수 있어야 한다.

외부 거래소를 포함한 완전한 ACID 트랜잭션은 불가능하므로, 영속 상태 머신, 멱등성, 재시도, 보상 처리 및 reconciliation을 통해 거래 정합성을 보장한다.

## P-03. 안전이 가용성보다 우선한다

다음 상황에서는 신규 자동 거래를 중단해야 한다.

- Binance 계정 상태를 확인할 수 없는 경우
- 현재 포지션을 확정할 수 없는 경우
- 주문 결과가 장시간 불명확한 경우
- 시장 데이터가 오래되었거나 데이터 정합성이 깨진 경우
- 시스템 시간과 거래소 시간의 차이가 허용 범위를 초과한 경우
- 리스크 한도 또는 예산 한도를 계산할 수 없는 경우
- 데이터베이스에 거래 상태를 안전하게 기록할 수 없는 경우

거래 중단 상태에서도 다음 기능은 가능해야 한다.

- 현재 상태 조회
- Binance와의 상태 재동기화
- 주문 및 포지션 조회
- 허용된 범위의 주문 취소
- 긴급 포지션 정리
- 시스템 상태 및 오류 로그 조회

## P-04. 모든 거래는 사전 검증을 통과해야 한다

수동 명령과 자동 전략 주문 모두 동일한 검증 절차를 거쳐야 한다.

검증 항목은 다음을 포함한다.

- 지원되는 심볼인지 확인
- LONG 또는 SHORT 방향 확인
- 주문 유형 확인
- 가격 및 수량 확인
- 레버리지 범위 확인
- 계정 잔고 및 사용 가능 증거금 확인
- 현재 포지션과 주문 상태 확인
- tick size 및 step size 확인
- 최소·최대 주문 조건 확인
- 최대 포지션 크기 확인
- 최대 레버리지 확인
- 일일 손실 한도 확인
- 중복 명령 여부 확인
- 시장 데이터의 최신성 확인
- 거래 중단 또는 긴급 정지 상태 확인

검증에 실패한 주문은 Binance로 전송하지 않으며, 실패 이유를 사용자에게 명확히 알려야 한다.

## P-05. 명령 인터페이스는 항상 접근 가능해야 한다

시스템은 별도의 명령어 창을 제공해야 한다.

명령어 창에서는 최소한 다음 기능을 제공한다.

- 주문 입력
- 주문 확인 및 취소
- 포지션 조회
- 잔고 조회
- 손절·익절 설정 및 변경
- 자동 거래 시작·중지
- 신규 거래 일시 중지
- Binance 상태와 재동기화
- 긴급 정지
- 시스템 상태 확인
- 최근 오류 및 명령 결과 조회

명령어 창은 거래 엔진과 직접 결합하지 않고 내부 Command API를 통해 동작해야 한다. 웹 화면도 동일한 Command API를 사용해야 한다.

## P-06. 관측성과 감사 가능성을 보장한다

다음 이벤트는 변경 불가능한 감사 기록으로 남겨야 한다.

- 사용자 명령 원문
- 명령을 입력한 사용자와 인터페이스
- 명령 접수 시각
- 검증 결과
- 리스크 계산 결과
- 예산 배분 결과
- Binance 요청과 응답
- 주문 상태 변경
- 체결 내역
- 포지션 변화
- 손절·익절 생성, 변경 및 취소
- 자동 전략의 신호와 판단 근거
- 오류, 재시도 및 복구 과정
- 운영자의 설정 변경
- 시스템 시작, 종료 및 재시작

API Secret과 인증 정보는 로그에 기록하지 않는다. 개인정보와 인증 관련 필드는 마스킹한다.

## P-07. 실거래와 테스트 환경을 분리한다

시스템은 최소한 다음 실행 모드를 제공해야 한다.

- `PAPER`: 거래소 주문 없이 내부에서 모의 체결
- `TESTNET`: Binance 테스트넷 연결
- `LIVE`: 실제 Binance 계정에서 거래

현재 실행 모드를 CLI와 웹 화면에 항상 명확하게 표시한다.

`LIVE` 모드는 별도의 설정과 명시적인 확인 없이는 활성화할 수 없어야 한다. 기본 실행 모드는 `PAPER` 또는 `TESTNET`으로 한다.

---

# 3. 용어 및 거래 입력 모델

## 3.1 기본 용어

- `symbol`: 거래 대상. 초기 범위는 `BTCUSDT`
- `side`: 주문 방향. `BUY` 또는 `SELL`
- `position_side`: 포지션 방향. `LONG` 또는 `SHORT`
- `order_type`: 주문 방식
- `quantity`: BTC 기준 주문 수량
- `notional`: USDT 기준 주문 명목금액
- `price`: 지정가 주문 가격
- `leverage`: 레버리지 배수
- `time_in_force`: GTC, IOC, FOK 등 주문 유효 방식
- `reduce_only`: 기존 포지션 감소만 허용하는 주문 여부
- `stop_loss`: 손절 조건
- `take_profit`: 익절 조건
- `bid`: 현재 최고 매수호가
- `ask`: 현재 최저 매도호가

`bid`와 `ask`는 일반적으로 사용자가 주문 방향으로 직접 입력하는 값이 아니라 시장 호가 정보다. 사용자는 `BUY/SELL`, `LONG/SHORT`, 주문 유형과 가격 조건을 입력하고 시스템이 현재 bid/ask를 참고하여 주문을 실행한다.

## 3.2 지원할 주문 방식

초기 주문 방식은 다음을 포함한다.

- Market order
- Limit order
- Stop Market order
- Stop Limit order
- Take Profit Market order
- Take Profit Limit order
- Trailing Stop order
- Post-only 성격의 지정가 주문
- Reduce-only 주문
- 전체 또는 일부 포지션 청산
- 주문 취소
- 미체결 주문 일괄 취소

실제 Binance Futures API에서 지원하는 정확한 주문 타입과 파라미터에 맞춰 adapter에서 변환한다.

---

# 4. 유즈케이스

## UC-01. 수동으로 신규 포지션을 생성한다

사용자는 CLI 또는 웹 화면에서 다음 정보를 입력한다.

- symbol
- LONG 또는 SHORT
- 주문 방식
- 수량 또는 투자할 USDT 금액
- 레버리지
- 지정 가격 또는 가격 계산 방식
- 선택적인 손절·익절 조건

처리 흐름:

1. 시스템이 명령을 접수하고 `command_id`를 생성한다.
2. 명령의 문법과 필수 항목을 확인한다.
3. 최신 계정, 포지션, 주문 및 시장 상태를 확인한다.
4. 리스크 및 예산 규칙을 적용한다.
5. Binance 거래 규칙에 맞게 가격과 수량을 보정한다.
6. 예상 주문 내용을 사용자에게 제시한다.
7. 확인이 필요한 모드에서는 사용자의 승인을 받는다.
8. 주문을 Binance에 전송한다.
9. 주문 접수 및 체결 상태를 추적한다.
10. 최종 결과를 CLI와 웹 화면에 표시한다.
11. 모든 과정을 감사 로그에 기록한다.

예시 명령 개념:

```text
trade BTCUSDT long market notional=500USDT leverage=3
trade BTCUSDT short limit price=65000 quantity=0.01 leverage=2
```

정확한 명령 문법은 별도 Command Specification에서 확정한다.

## UC-02. 포지션에 손절과 익절을 설정한다

사용자는 기존 포지션에 다음 조건 중 하나로 손절·익절을 지정할 수 있다.

- 절대 가격
- 진입가 대비 비율
- 예상 손익률
- 포지션의 일부 또는 전체 수량

처리 흐름:

1. 대상 포지션이 실제로 존재하는지 Binance에서 확인한다.
2. 현재 포지션 방향과 수량을 확인한다.
3. 손절·익절 가격 방향이 유효한지 검증한다.
4. Binance 가격 단위에 맞게 값을 보정한다.
5. reduce-only 또는 close-position 성격의 보호 주문을 생성한다.
6. 생성된 주문 ID를 원래 포지션 및 진입 주문과 연결한다.
7. 보호 주문의 상태를 지속적으로 추적한다.
8. 포지션 수량이 변경되면 보호 주문 수량도 정책에 따라 조정한다.
9. 포지션이 종료되면 남아 있는 불필요한 보호 주문을 취소한다.

예시:

```text
set stoploss BTCUSDT position=long price=62000 close=100%
set takeprofit BTCUSDT position=long percent=5 close=50%
```

## UC-03. 주문을 조회하거나 취소한다

사용자는 다음 작업을 수행할 수 있다.

- 현재 미체결 주문 조회
- 특정 주문 조회
- 특정 주문 취소
- 심볼의 모든 미체결 주문 취소
- 명령 ID를 이용한 처리 상태 조회

취소 요청도 거래 트랜잭션으로 기록하며 Binance에서 최종 취소 여부를 확인해야 한다.

## UC-04. 포지션과 계정 상태를 조회한다

사용자는 다음 정보를 확인할 수 있다.

- USDT 지갑 잔고
- 사용 가능 잔고
- 사용 중인 증거금
- 현재 포지션
- 포지션 방향과 수량
- 진입 평균가격
- 시장가격 및 청산가격
- 미실현 손익
- 실현 손익
- 현재 레버리지
- 현재 미체결 주문
- 손절·익절 설정 상태
- 시스템이 계산한 총자산과 투자자산

## UC-05. 시장 과거 데이터를 수집한다

시스템은 정해진 주기에 따라 Binance에서 historical market data를 수집한다.

초기 대상 데이터:

- Candlestick/Kline
- Open, High, Low, Close
- Volume
- Trade count
- Funding rate
- Mark price
- Index price
- Open interest
- 필요 시 aggregate trades

요구사항:

- 여러 시간 주기를 지원해야 한다.
- 마지막으로 정상 수집한 위치부터 이어서 수집해야 한다.
- 중복 데이터는 동일 키로 병합해야 한다.
- 누락된 구간을 탐지하고 다시 수집해야 한다.
- 데이터의 출처, 시간 및 수집 시각을 기록해야 한다.
- 미완성 캔들과 확정된 캔들을 구분해야 한다.

대상 시간 주기와 데이터 보존 기간은 추후 확정한다.

## UC-06. WebSocket으로 실시간 시장 데이터를 수신한다

시스템은 Binance WebSocket을 통해 다음 정보를 구독한다.

- Order book
- Best bid/ask
- Trades 또는 aggregate trades
- Mark price
- Kline update
- 사용자 주문 및 계정 이벤트

Order book 처리 요구사항:

1. REST API로 초기 snapshot을 가져온다.
2. WebSocket delta event를 순서대로 적용한다.
3. sequence gap을 확인한다.
4. 누락이나 순서 오류가 발견되면 현재 order book을 폐기한다.
5. snapshot부터 다시 동기화한다.
6. 마지막 정상 수신 시각과 데이터 최신성을 관리한다.

오래되었거나 불완전한 order book을 자동 거래 판단에 사용하면 안 된다.

## UC-07. 자동 거래를 수행한다

자동 거래는 다음 파이프라인으로 구성한다.

1. Data Collection
2. Indicator Calculation
3. Signal Calculation
4. Risk Calculation
5. Budget Allocation
6. Trade Planning
7. Trade Execution
8. Position and Order Monitoring
9. Notification
10. Audit and Reporting

각 단계의 입력과 출력은 명시적인 데이터 모델을 사용하며 개별적으로 교체하거나 비활성화할 수 있어야 한다.

자동 거래 명령도 수동 거래와 동일한 검증, 리스크 제한, 멱등성 및 감사 기록을 적용한다.

자동화 주기, 지표, 신호 생성 방식, 전략 및 리스크 정책은 추후 확정한다.

## UC-08. 자동 거래를 시작하거나 중지한다

사용자는 CLI와 웹에서 다음 동작을 수행할 수 있다.

- 자동 거래 시작
- 자동 거래 일시 중지
- 신규 진입만 중지
- 기존 포지션 관리만 계속
- 전략별 활성화 및 비활성화
- 긴급 정지

자동 거래를 중지해도 기존 손절·익절 주문을 임의로 제거하면 안 된다.

긴급 정지의 동작은 설정에 따라 구분한다.

- 신규 주문만 차단
- 미체결 진입 주문 취소
- 모든 미체결 주문 취소
- 기존 포지션 유지
- 기존 포지션 시장가 청산

포지션 시장가 청산은 별도의 강한 확인 절차를 요구한다.

## UC-09. 거래 결과를 알림으로 전달한다

시스템은 다음 이벤트를 정해진 수신자에게 전달한다.

- 주문 접수
- 주문 체결 및 부분 체결
- 주문 거절 또는 실패
- 포지션 진입 및 종료
- 손절 또는 익절 실행
- 리스크 한도 초과
- 자동 거래 중단
- 데이터 수집 장애
- Binance 연결 장애
- 시스템 복구 결과
- 일간 또는 주간 거래 요약

초기 알림 채널 후보:

- Email
- SNS 또는 메신저: 추후 결정

알림 실패가 거래 실행을 실패로 변경해서는 안 된다. 알림은 별도 큐에서 재시도하며 실패 내역을 기록한다.

## UC-10. 웹 대시보드에서 상태를 확인한다

웹 화면은 최소한 다음 정보를 제공한다.

### Overview

- 시스템 실행 상태
- Binance 연결 상태
- WebSocket 연결 상태
- 현재 실행 모드
- 자동 거래 활성 상태
- 최근 데이터 수신 시각
- 현재 총자산
- 거래에 배정된 투자자산
- 사용 가능 잔고
- 실현·미실현 손익

### Positions and Orders

- 현재 포지션
- 진입가격과 시장가격
- 청산가격
- 레버리지
- 손절·익절 상태
- 미체결 주문
- 최근 체결 내역

### Charts

- BTCUSDT 가격 차트
- 주문 및 체결 위치
- 손절·익절 가격
- 총자산 변화
- 투자자산 변화
- 실현·미실현 손익 변화
- 전략별 성과

### Commands

- 거래 명령 입력
- 주문 취소
- 손절·익절 설정
- 자동 거래 시작 및 중지
- 긴급 정지
- 명령 처리 과정 및 결과 표시

### Activity and Logs

- 최근 사용자 명령
- 전략 판단
- 주문 상태 변화
- 경고 및 오류
- 시스템 복구 내역
- 알림 전송 결과

## UC-11. 시스템 재시작 후 상태를 복구한다

시스템이 시작되면 다음 복구 절차를 수행한다.

1. 데이터베이스에서 마지막 시스템 상태를 읽는다.
2. Binance 서버 시간을 확인한다.
3. 계정 잔고를 조회한다.
4. 실제 포지션을 조회한다.
5. 미체결 주문을 조회한다.
6. 최근 체결 내역을 조회한다.
7. 처리 중이거나 결과가 불명확한 명령을 확인한다.
8. 로컬 상태와 Binance 상태를 비교한다.
9. 차이를 reconciliation 기록으로 남긴다.
10. 보호 주문의 존재와 수량을 검증한다.
11. 시장 데이터를 다시 동기화한다.
12. 안전 조건을 모두 충족한 경우에만 자동 거래를 재개한다.

자동 거래의 자동 재개 여부는 설정 가능해야 하며 기본값은 수동 확인 후 재개로 한다.

---

# 5. 논리 아키텍처

```text
┌───────────────────────────────────────────────────────────────┐
│                       User Interfaces                         │
│                                                               │
│   Command Console                 Web Dashboard                │
└───────────────┬───────────────────────────┬───────────────────┘
                │                           │
                └───────────┬───────────────┘
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                    Command/API Gateway                        │
│ Authentication · Authorization · Validation · Command ID      │
└────────────────────────────┬──────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────┐
│                    Application Services                       │
│                                                               │
│ Command Service · Account Service · Position Service          │
│ Order Service · Risk Service · Budget Service                 │
│ Automation Orchestrator · Notification Service                │
└───────────┬────────────────┬───────────────────┬──────────────┘
            │                │                   │
            ▼                ▼                   ▼
┌─────────────────┐ ┌──────────────────┐ ┌─────────────────────┐
│ Trading Engine  │ │ Strategy Engine  │ │ Market Data Service │
│                 │ │                  │ │                     │
│ Order State     │ │ Indicators       │ │ REST Collector      │
│ Machine         │ │ Signals          │ │ WebSocket Client    │
│ Reconciliation  │ │ Risk Calculation │ │ Order Book Builder  │
│ Position Guard  │ │ Budget Allocation│ │ Data Validation     │
└────────┬────────┘ └─────────┬────────┘ └──────────┬──────────┘
         │                    │                     │
         └────────────────────┴───────────┬─────────┘
                                          ▼
┌───────────────────────────────────────────────────────────────┐
│                  Binance Futures Adapter                      │
│ REST API · WebSocket API · User Data Stream · Rate Limiting   │
└────────────────────────────┬──────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────┐
│                    Binance Futures                            │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│                   Persistence & Messaging                     │
│                                                               │
│ Transaction DB · Time-Series Data · Audit Log · Event Queue    │
└───────────────────────────────────────────────────────────────┘
```

---

# 6. 주요 컴포넌트

## 6.1 Command Console

운영자가 시스템을 관리하기 위한 항상 가동되는 명령어 인터페이스다.

역할:

- 거래 명령 입력
- 상태 조회
- 주문 및 포지션 관리
- 자동 거래 제어
- 복구 명령 실행
- 긴급 정지
- 명령 결과 실시간 표시

CLI가 종료되더라도 거래 엔진이 함께 종료되지 않도록 별도 프로세스 또는 독립 클라이언트로 설계한다.

## 6.2 Web Dashboard

브라우저 기반의 관제 및 명령 인터페이스다.

역할:

- 자산, 주문, 포지션 및 손익 시각화
- 가격과 거래 기록 그래프 제공
- 명령 입력 및 결과 추적
- 자동 거래 상태 관리
- 오류와 알림 확인

웹 화면은 데이터베이스를 직접 수정하지 않고 Command/API Gateway만 사용해야 한다.

## 6.3 Command/API Gateway

모든 수동 및 자동 명령의 단일 진입점이다.

역할:

- 사용자 인증과 권한 확인
- 입력 형식 검증
- 명령 ID 생성
- 중복 명령 차단
- 명령 영속화
- 명령 처리 상태 조회
- CLI, 웹, 자동화 모듈에 동일한 API 제공

## 6.4 Trading Engine

거래의 상태와 정합성을 관리하는 핵심 컴포넌트다.

역할:

- 주문 상태 머신
- 멱등적인 주문 실행
- 주문 생성, 변경 및 취소
- 부분 체결 처리
- 포지션 상태 관리
- 손절·익절 연결 관리
- Binance 상태와 내부 상태의 reconciliation
- 불명확한 거래 결과 복구

Trading Engine 이외의 모듈은 Binance에 거래 주문을 직접 전송할 수 없어야 한다.

## 6.5 Binance Futures Adapter

Binance 관련 구현을 시스템 내부 도메인과 분리한다.

역할:

- API 요청 및 응답 변환
- 인증과 서명
- 서버 시간 동기화
- rate limit 관리
- REST 호출
- WebSocket 연결
- User Data Stream 관리
- Binance 오류 코드를 내부 오류 모델로 변환
- 재시도 가능 오류와 불가능 오류 분류

## 6.6 Market Data Service

과거 데이터와 실시간 데이터를 수집하고 정합성을 검증한다.

하위 구성:

- Historical Data Collector
- WebSocket Client
- Order Book Builder
- Gap Detector
- Market Data Normalizer
- Data Freshness Monitor

거래 전략은 Binance 원본 메시지를 직접 사용하지 않고 정규화되고 검증된 시장 데이터만 사용한다.

## 6.7 Strategy Engine

향후 확정될 전략을 플러그인 형태로 실행한다.

하위 단계:

- Indicator Calculator
- Signal Generator
- Risk Calculator
- Budget Allocator
- Trade Planner

각 전략 출력에는 다음 정보가 포함되어야 한다.

- 전략 ID와 버전
- 입력 데이터 기준 시각
- 사용한 지표
- 생성된 신호
- 판단 근거
- 제안 포지션 방향
- 제안 수량 또는 예산
- 손절·익절 제안
- 신뢰도 또는 점수
- 만료 시각

전략은 직접 주문을 실행하지 않고 Trade Proposal만 생성한다. 실제 주문은 Risk Service와 Trading Engine을 통과해야 한다.

## 6.8 Risk Service

모든 거래 명령에 대해 독립적인 최종 안전 검사를 수행한다.

예정된 정책:

- 거래당 최대 예산
- 최대 총 포지션
- 최대 레버리지
- 최대 일일 손실
- 최대 연속 손실
- 최대 drawdown
- 동시 주문 수 제한
- 데이터 stale 기준
- 비정상 변동성 차단
- 청산가격과의 최소 거리
- stop-loss 필수 여부

전략 모듈이 허용했다고 해도 Risk Service가 거부한 주문은 실행할 수 없다.

## 6.9 Budget Service

전체 자산 중 거래에 사용할 수 있는 금액을 관리한다.

역할:

- 총자산 계산
- 투자 가능 자산 계산
- 전략별 예산 배정
- 이미 사용된 증거금 반영
- 예약된 주문 금액 반영
- 실현 및 미실현 손익 반영
- 예산 초과 주문 차단

예산 정책은 추후 확정하되 거래 실행과 독립된 모듈로 유지한다.

## 6.10 Notification Service

거래 및 시스템 이벤트를 외부 채널로 전달한다.

역할:

- 템플릿 기반 메시지 생성
- Email 전달
- 추후 선정할 SNS 또는 메신저 전달
- 비동기 전송
- 실패 재시도
- 중복 알림 방지
- 전송 결과 기록

## 6.11 Persistence

데이터 특성에 따라 저장 영역을 구분한다.

### Transaction Database

- 명령
- 주문
- 체결
- 포지션
- 계정 snapshot
- 전략 실행
- 리스크 결정
- 예산 배정
- 알림 상태
- 시스템 설정

### Time-Series Storage

- Kline
- Order book snapshot 또는 요약
- Best bid/ask
- Mark price
- Funding rate
- 자산 및 손익 시계열
- 계산된 지표

### Audit Log

- 사용자 및 시스템의 모든 중요 행위
- 기존 레코드를 수정하는 대신 변경 이벤트를 추가
- 동일한 correlation ID를 이용한 전체 흐름 추적

## 6.12 Event Queue

다음 작업을 거래 실행 흐름과 분리한다.

- 시장 데이터 처리
- 지표 계산
- 알림 전송
- 웹 실시간 갱신
- 보고서 생성
- 실패 작업 재처리

거래 상태 변경 이벤트는 최소 한 번 전달될 수 있다고 가정한다. 모든 소비자는 동일 이벤트를 반복 수신해도 결과가 중복되지 않도록 멱등적으로 구현한다.

---

# 7. 핵심 데이터 흐름

## 7.1 수동 거래

```text
CLI/Web
  → Command Gateway
  → Command 영속화
  → Validation
  → Risk Check
  → Budget Check
  → Trading Engine
  → Binance Adapter
  → Binance
  → Order Update
  → Transaction DB/Audit Log
  → CLI/Web Result
  → Notification
```

## 7.2 자동 거래

```text
Historical/WebSocket Market Data
  → Data Validation
  → Indicator Calculation
  → Signal Calculation
  → Risk Calculation
  → Budget Allocation
  → Trade Proposal
  → Final Risk Check
  → Trading Engine
  → Binance
  → Monitoring
  → Record/Notification/Dashboard
```

## 7.3 장애 복구

```text
System Restart
  → Local Pending State Load
  → Binance Account/Order/Position Query
  → State Comparison
  → Reconciliation
  → Protective Order Validation
  → Market Data Resynchronization
  → Operator Report
  → Manual or Policy-Based Trading Resume
```

---

# 8. 배포 아키텍처 원칙

초기에는 운영 복잡도를 줄이기 위해 모듈형 모놀리스로 구현할 수 있다. 다만 다음 경계는 코드와 실행 책임 차원에서 명확히 분리한다.

- User Interface
- Command/API
- Trading
- Market Data
- Strategy
- Risk and Budget
- Notification
- Persistence
- Binance Adapter

권장 프로세스 구성:

1. Core Trading Service
2. Market Data Worker
3. Automation Worker
4. Notification Worker
5. Web API and Dashboard
6. Command Console

Core Trading Service는 단일 인스턴스가 주문 권한을 소유하도록 시작한다. 고가용성을 위해 여러 인스턴스를 실행할 경우 분산 락 또는 leader election을 적용하여 동시에 두 인스턴스가 동일 명령을 실행하지 않도록 한다.

각 프로세스는 독립적으로 재시작할 수 있어야 하며 프로세스 관리자 또는 컨테이너 오케스트레이터를 통해 자동 재시작한다.

---

# 9. 보안 원칙

- Binance API Secret을 소스 코드나 설정 파일에 평문으로 저장하지 않는다.
- 환경별 secret 저장소를 사용한다.
- 가능하다면 출금 권한이 없는 API Key를 사용한다.
- Binance API Key에 IP 접근 제한을 적용한다.
- 웹과 CLI 명령에는 사용자 인증과 권한 검사를 적용한다.
- 조회 권한, 거래 권한, 설정 변경 권한, 긴급 청산 권한을 분리한다.
- 중요한 거래 및 설정 변경에는 추가 확인 또는 재인증을 적용한다.
- 로그와 오류 메시지에서 인증 정보를 제거한다.
- 모든 외부 통신은 암호화된 연결을 사용한다.

---

# 10. 초기 범위와 미정 사항

## 초기 범위

- Binance USDT-M Futures
- BTCUSDT
- One-way 또는 Hedge Mode 중 하나를 명시적으로 선택
- 수동 거래 명령
- Market 및 Limit 주문
- 손절 및 익절
- 포지션·주문·잔고 조회
- 과거 Kline 데이터 수집
- WebSocket order book 및 사용자 주문 이벤트
- 거래 및 감사 기록
- 기본 웹 대시보드
- PAPER, TESTNET, LIVE 환경 분리
- Email 알림을 위한 인터페이스
- 자동화 파이프라인의 확장 가능한 골격

## 추후 결정 사항

- One-way Mode 또는 Hedge Mode
- Cross Margin 또는 Isolated Margin
- 사용할 지표
- 신호 계산 알고리즘
- 자동 거래 전략
- 리스크 한도
- 예산 배분 규칙
- 자동화 실행 주기
- SNS 또는 메신저 알림 채널
- historical data 보존 기간
- order book 저장 수준
- 웹 사용자와 권한 체계
- 배포 환경
- 사용 언어와 프레임워크
- 데이터베이스 종류
- 시스템 단일 인스턴스 또는 고가용성 구성

미정 사항은 임의로 비즈니스 규칙을 확정하지 말고 설정 가능하거나 교체 가능한 인터페이스로 구현한다.

---

# 11. Claude Code 구현 지시사항

다음 규칙에 따라 시스템을 설계하고 구현한다.

1. 한 번에 전체 시스템을 구현하지 말고 도메인 모델, 인터페이스, 테스트 가능한 단위로 나눈다.
2. 구현 전에 각 단계의 산출물과 acceptance criteria를 제시한다.
3. 실거래 기능보다 PAPER 및 TESTNET 모드를 먼저 구현한다.
4. 거래 상태 머신과 멱등성 처리를 최우선으로 구현한다.
5. 주문 실행 전후에 반드시 상태를 영속화한다.
6. Binance API 응답을 받지 못한 경우 주문 실패로 단정하지 말고 reconciliation을 수행한다.
7. 모든 외부 연동은 adapter 인터페이스 뒤에 배치한다.
8. 전략 모듈이 Binance 주문 API를 직접 호출하지 못하게 한다.
9. CLI와 웹은 동일한 Command API를 사용한다.
10. 모든 수동 및 자동 거래에 동일한 Risk Service를 적용한다.
11. 각 장애 시나리오에 대한 자동화 테스트를 작성한다.
12. 시스템 재시작, 응답 유실, 중복 명령, 부분 체결, WebSocket 재연결 및 order book gap을 반드시 테스트한다.
13. 로그에는 command ID, correlation ID, client order ID를 포함한다.
14. API Key와 Secret이 로그, 오류 또는 테스트 결과에 노출되지 않게 한다.
15. 미정인 전략과 정책을 임의로 확정하지 말고 인터페이스와 설정 항목으로 남긴다.
16. LIVE 모드는 명시적 설정과 확인 절차 없이는 실행되지 않게 한다.
17. 구현 중 거래 정합성이나 안전성에 영향을 주는 모호한 사항을 발견하면 임의 결정하지 말고 질문 목록으로 보고한다.
