# sw_factory

요구사항 문서 하나를 넣으면 유스케이스 → FSM → 티켓 → 테스트 → 구현 → 커밋까지
스스로 굴러가는 소프트웨어 공장.

```
requirements.md
   │  ELICIT      워커: 원자 요구사항으로 분해
   ▼
requirements.yaml
   │  USECASE     워커: 액터/흐름/인수조건
   ▼
usecases.yaml
   │  MODEL       워커: 하나의 FSM으로 통합 + 구조 검증 + 자동 수리     [게이트: fsm]
   ▼
fsm.yaml  ◀────────────── 자동화의 척추
   │  TICKETIZE   코드: 유스케이스→Epic, 전이→Story (LLM 안 씀)         [게이트: ticketize]
   ▼
tickets   (backlog/tickets.yaml 또는 Jira)
   │  SPEC_TESTS  워커: FSM만 보고 테스트 작성 (구현 코드 안 보여줌)
   ▼
tests/test_fsm.py
   │  WORK        티켓 소진 루프 ─┐
   ▼                             │  DECOMPOSE → IMPLEMENT → TEST
git commits                      │       ↑                    │
   │  DRIVER      워커: 진입점    │       └── DEBUG ◀──────────┘ (실패 시, 상한 N회)
   ▼                             │                    │
app/__main__.py                  └────────────────────┴── 상한 초과 → BUG 티켓 + BLOCKED
   │  E2E_SPEC    워커: 유스케이스 → 시나리오 테스트 (FSM이 아니라 유스케이스에서!)
   ▼
tests/e2e/        │  E2E  실행 → 실패 시 DEBUG_E2E (상한 N회) → 초과 시 BUG 티켓
   ▼
push  [게이트: push]
```

---

## 이 설계를 지탱하는 일곱 가지 결정

### 1. FSM 전이 하나 = 티켓 하나 = 테스트 하나 = 완료 조건 하나

요구사항과 유스케이스는 자연어라 "끝났는지"를 기계가 판정할 수 없다.
FSM으로 떨어지는 순간 `(출발상태, 이벤트, 가드, 도착상태, 동작)` 튜플이 나오고,
이 튜플은 그대로 테스트 케이스가 된다.

> 티켓 SWF-8이 끝났다 ≡ `test_t_001` 이 통과한다

이 대응이 없으면 루프가 자동으로 못 돈다. 매 티켓마다 사람이 "이거 다 된 건가?"를 판정해야 한다.
추적성은 커밋 트레일러까지 이어진다:

```
$ git log --format='%(trailers:key=FSM-Transition,valueonly=true)'
T-004
```

### 2. 테스트는 구현자가 쓰지 않는다 — 그리고 그것을 강제한다

같은 워커가 코드와 테스트를 한 번에 쓰면 DoD는 자기인증이 되어 무의미해진다.
그래서 `SPEC_TESTS` 단계에서 **FSM만 보고** 테스트를 먼저 만든다. 구현 코드는 보여주지 않는다.

프롬프트로 "테스트 고치지 마세요"라고 부탁하는 것만으로는 부족하다. 해시로 강제한다:

- 구현/디버그 전에 `tests/**/test_*.py` 전체를 SHA-256으로 스냅샷
- 워커 실행 후 비교 → 달라졌으면 **되돌리고** 티켓에 위반 기록

워커가 "테스트가 틀렸다"고 판단하면 고치는 대신 `SPEC_CONFLICT: <이유>` 를 출력하고 멈춘다.
그 판단은 기계가 할 수 없으므로 사람 검토 큐로 간다.

### 3. 루프는 반드시 발산한다 — 발산할 때 어디로 갈지를 정해둔다

무한 재시도는 자동화가 아니라 폭주다. 세 겹의 상한:

| 상한 | 초과하면 |
|---|---|
| `implement_attempts` (기본 3) | BUG 티켓 생성 + 스토리 BLOCKED + 다음 티켓으로 |
| `e2e_attempts` (기본 2) | E2E BUG 티켓 (특정 티켓 탓으로 못 돌리므로 독립 이슈) |
| `worker_timeout_s` (기본 900) | 워커 강제 종료 |

`claude` CLI 2.1.251에는 `--max-turns` 가 없으므로 벽시계 타임아웃이 유일한 폭주 방지선이다.

### 4. 어디서 멈춰도 그 자리에서 재개된다

파이프라인 자체가 FSM이고 상태는 `.factory/state.json` 에 저장된다.
확정된 단계는 `done_phases` 에 기록되어 **다시 실행되지 않는다.**

이게 없으면: 게이트에서 멈췄다 재개할 때 FSM을 다시 생성하고,
LLM은 같은 입력에 다른 답을 내므로 **사람이 승인한 FSM과 실제로 쓰이는 FSM이 달라진다.**
(실제로 겪은 버그다. `tests/test_loop.py::test_gate_resume_does_not_regenerate_artifact` 참고)

### 5. FSM은 *동작*을 기술하지 *기동 방법*을 기술하지 않는다

전이를 33개 다 구현해도 결과물은 **진입점 없는 라이브러리**다. `main`도, CLI도 없다.
FSM 어디에도 "이 프로그램을 어떻게 켜는가"가 없기 때문이다.

`DRIVER` 단계가 그 간극을 메운다. FSM과 실제 구현을 읽고 `app/__main__.py` 를 만든다:

```bash
$ cd projects/vending_machine/repo && python -m app
=== vending_machine FSM REPL ===
사용 가능한 이벤트 (입력 형식: EVENT key=value key=value):
  INSERT_COIN        amount=<int> recognized=<bool>
  SELECT_DRINK       drink=<id>
  ...
> INSERT_COIN amount=500 recognized=true
  IDLE --INSERT_COIN--> HAS_CREDIT
    state=HAS_CREDIT credit=500 buttons=[COFFEE, WATER]
```

두 가지 규칙이 붙어 있다:

- **도메인 코드는 건드리지 않는다.** 껍데기를 만들다 로직을 고치면 통과했던 DoD의 근거가
  조용히 무너진다. `app/*.py`(진입점 제외)를 스냅샷해 두고 바뀌면 되돌린다.
- **아무 일도 안 일어났을 때 그 이유를 정확히 구별한다.** 이게 이 단계의 핵심이다.
  "전이 없음" 한마디로 뭉뚱그리면 정반대 상황이 같은 말로 보고되어 진단이 쓸모없어진다:

  | 상황 | 뜻 | 드라이버 출력 |
  |---|---|---|
  | 명세에도 없는 조합 | 사용자가 잘못된 이벤트를 보냄 | `명세에도 없는 조합입니다` |
  | 명세엔 있고 티켓 미완료 | 공장이 아직 안 지음 | `아직 구현되지 않았습니다 -> T-012(SWF-19/todo)` |
  | 명세에 있고 티켓 완료 | guard 조건이 거짓 (정상) | `T-016 는 구현되어 있습니다. guard 조건이 거짓입니다` |

  구별의 근거인 `app/_fsm_spec.py`(전이 표 + 티켓 상태)는 **fsm.yaml과 트래커에서 코드로
  생성한다.** 전이 33개짜리 표를 모델에게 베껴 쓰게 하면 언젠가 틀리고,
  틀린 진단은 없는 진단보다 나쁘다. 티켓 상태를 담으므로 `driver`/`demo` 를 부를 때마다
  다시 쓴다 -- 낡은 진단도 같은 이유로 위험하다.

### 6. 단위 테스트는 FSM에서, E2E는 유스케이스에서 나온다

둘 다 FSM에서 뽑으면 같은 오해를 두 번 하고 두 번 통과할 뿐이다. **출처가 달라야 서로를 검증한다.**

| | 출처 | 검증 대상 |
|---|---|---|
| `tests/test_fsm.py` | fsm.yaml | 전이 **하나**가 맞는가 |
| `tests/e2e/test_scenarios.py` | usecases.yaml | 전이의 **조합**이 업무 결과를 내는가 |

E2E가 잡아야 하는 것은 "전이 하나하나는 맞는데 이어 붙이면 틀리는" 버그다.
그래서 여러 이벤트를 순서대로 흘려보내고 마지막에 관찰 가능한 업무 결과
(배출된 음료, 반환된 금액, 남은 재고)를 단언한다. 중간 상태 이름만 확인하면 단위 테스트의 중복이다.

이 계층이 실제로 값을 하는지는 **돌연변이 검사**로 확인했다:

```
진입점(app/__main__.py)만 망가뜨림
  → 단위 테스트 33/33 통과 (눈치채지 못함)
  → E2E 실패                (잡아냄)
```

E2E는 유스케이스에서 나오므로, **FSM 전이로 표현되지 않는 제약 요구사항도 검증 대상이 된다.**
자판기 예제의 "외부 네트워크에 연결하지 않는다"(UC-07)는 전이가 하나도 없어 에픽이 비어 있었는데,
E2E 단계가 AST로 `socket`/`urllib`/`requests` import 부재를 검사하는 테스트를 만들어 메웠다.

생성 중에는 **기존 단위 테스트를 수정할 수 없다.** E2E를 통과시키려고 단위 테스트를
느슨하게 만드는 것이 가장 흔한 부정행위다.

### 7. 회귀 기준선은 "전체 스위트"가 아니라 "지금까지 초록이던 것"

전이 33개 중 4개만 구현된 시점에 전체 스위트를 돌리면 항상 빨간색이라 신호가 되지 않는다.
완료된 티켓의 DoD 테스트만 `green_nodes` 에 누적하고, 새 티켓마다 그것만 다시 돌린다.
깨지면 회귀 → BUG 티켓.

---

## 사용법

```bash
cd sw_factory

# 1. 프로젝트 준비: requirements.md 와 factory.yaml
#    projects/vending_machine/ 을 복사해서 시작하는 게 빠르다

# 2. 실행 (게이트에서 멈춘다)
PYTHONPATH=. python -m factory.cli -c projects/내프로젝트/factory.yaml run

# 3. artifacts/fsm.yaml 을 눈으로 검토
#    상태 이름이 "기다리는 상황"인가? 놓친 예외 경로는 없는가?

# 4. 승인하고 재개
PYTHONPATH=. python -m factory.cli -c projects/내프로젝트/factory.yaml approve fsm
PYTHONPATH=. python -m factory.cli -c projects/내프로젝트/factory.yaml run

# 상태 확인
python -m factory.cli -c ... status   # 어느 단계인지, 통계
python -m factory.cli -c ... board    # 티켓 보드, BLOCKED/needs-human 요약
python -m factory.cli -c ... push     # 수동 푸시
python -m factory.cli -c ... reset --hard   # 산출물까지 초기화
```

### 만들어진 프로그램 실행하기

```bash
# 진입점 생성 (파이프라인의 DRIVER 단계가 자동으로 하지만, 따로도 부를 수 있다)
python -m factory.cli -c projects/내프로젝트/factory.yaml driver [--force]

# 대화형
cd projects/내프로젝트/repo && python -m app

# 비대화형 (이벤트 스크립트: 한 줄에 하나씩 `EVENT key=value`)
python -m app --script events.txt
python -m factory.cli -c ... demo --script events.txt   # 공장 쪽에서 부르기
```

전이가 아직 다 구현되지 않은 상태에서도 실행됩니다. 미구현 이벤트는 무시되고
`(전이 없음)` 으로 표시되므로, **어디까지 지어졌는지가 실행만 해도 보입니다.**

**처음 돌릴 때는 `limits.max_tickets_per_run: 2` 로 두세요.** 전이 33개짜리 FSM이면
티켓 33개가 생기고, 무제한으로 두면 한 번에 수십 번의 워커 호출이 나갑니다.
상한에 걸리면 WORK 단계에 머물며, 다시 `run` 하면 이어서 처리합니다.

---

## 구조

```
factory/
  models.py        Requirement / UseCase / FSM / Transition / Ticket
                   FSM.validate() — 비결정성·도달불가·교착·미정의 참조를 티켓이 되기 전에 잡는다
  config.py        factory.yaml (게이트, 상한)
  artifacts.py     단계 간 산출물 직렬화 + LLM 응답에서 YAML 추출
  yamlfix.py       `guard: !recognized` 같은 파손 복구 (YAML에서 ! 는 태그 문법)
  scaffold.py      대상 저장소 초기 구조 (conftest.py 없으면 생성된 테스트가 import에서 깨진다)
  context.py       Ctx + 프롬프트 로더
  orchestrator.py  파이프라인 FSM. 게이트, 재개, 회귀 기준선, 백로그 승격
  cli.py           run / status / board / approve / driver / demo / push / reset

  tracker/         base.py(ABC) → local.py(YAML 파일) / jira.py(어댑터 자리)
  worker/          base.py(ABC) → headless.py(claude -p) / scripted.py(토큰 안 쓰는 가짜)
  stages/          analysis(1-3) planning(4-5) build(6-8) entry(진입점+진단표) deliver(9-11)

prompts/           단계별 프롬프트. 코드 수정 없이 여기만 고쳐서 튜닝한다
tests/test_loop.py 공장 루프 자체의 테스트 — LLM 없이 제어 흐름만 검증
```

### 왜 워커와 트래커가 추상화되어 있나

`ScriptedWorker` 로 **LLM 없이** 루프 전체를 돌릴 수 있다. 이 분리가 없으면
"루프가 잘못됐나, 모델이 이상한 답을 했나"를 구별할 수 없어 디버깅이 불가능해진다.
`tests/test_loop.py` 는 16개 시나리오를 약 1분에, 토큰 0으로 검증한다.

`LocalTracker` → `JiraTracker` 도 마찬가지다. 파이프라인 코드는 Jira를 모른다.

---

## Jira 붙이기

`factory/tracker/jira.py` 에 인터페이스가 준비되어 있다. 채울 것:

1. 인증 — Cloud는 `JIRA_URL`/`JIRA_EMAIL`/`JIRA_TOKEN` + Basic + `/rest/api/3`,
   DC는 `JIRA_URL`/`JIRA_TOKEN`(PAT) + Bearer + `/rest/api/2`
2. `STATUS_MAP` / `TYPE_MAP` — Jira는 status 이름과 transition id가 프로젝트마다 다르므로
   하드코딩할 수 없다. 대상 프로젝트의 워크플로를 보고 채워야 한다.
3. `TRACE_FIELDS` — `fsm_ref` / `signature` 를 커스텀 필드에 심을지 결정.
   심어두면 Jira에서 JQL로 "T-017 관련 이슈 전부"를 뽑을 수 있다.
4. `search()` → JQL 번역

그 다음 `factory.yaml` 에서 `tracker: jira`, `jira_project_key: SWF` 로 바꾸면 끝이다.
파이프라인 코드는 한 줄도 바뀌지 않는다.

---

## LLM 출력을 YAML로 받을 때 겪은 일

산출물 형식으로 YAML을 택한 이유는 사람이 읽고 고치기 쉬워서다. 그 대가를 한 세션에서 네 번 치렀다.
전부 **유효한 YAML이라 파싱 오류가 안 나거나, 모델이 자연스럽게 쓰는 문장이 YAML 문법과 충돌**한 경우다.

| 모델이 쓴 것 | YAML이 읽은 것 | 결과 |
|---|---|---|
| `guard: !recognized` | 태그 | 파싱 실패 |
| `id: OFF` | 불리언 `false` | **오류 없이** 상태 이름 소실 |
| `title: 1번째 지적: 테스트가 약하다` | 중첩 매핑 | 파싱 실패 |
| `description: ... (유스케이스 1b: 누적 ...)` | 중첩 매핑 | 파싱 실패 |

세 겹으로 막는다:

1. `factory/yamlfix.py` — 파싱 **전에 항상** 돌린다. 오류 시 폴백이 아니다 (두 번째 사례는 오류가 안 난다).
   식별자 필드의 불리언 토큰, 자유 서술 필드의 `: `, 특수문자 시작 값만 따옴표로 감싼다.
2. `FSM.validate()` — 식별자가 문자열이 아니면 티켓이 되기 전에 거부.
3. `context.ask_yaml()` — 원문을 `.factory/raw/`에 남기고, 실패하면 오류를 되돌려주며 한 번 더 묻는다.
   **YAML을 받는 모든 단계는 이걸 써야 한다.** `test_no_stage_bypasses_ask_yaml` 이 우회를 잡는다.

새 필드를 추가하면 `_TEXT_KEYS` 나 `_ID_KEYS` 에 넣어야 한다. 안 넣으면 다섯 번째 사례가 된다.

## 언제 끝나는가 — 릴리스 판정

검토(Critic)는 언제나 무언가를 찾는다. 명세는 무한히 정밀해질 수 있고, 좋은 검토자는 항상 지적을 낸다.
실제로 매 버전 8~37건이 나왔고 0으로 수렴한 적이 없다. **기계에 게이트를 걸면 영원히 출시하지 못한다.**

그래서 판정 기준을 뒤집는다:

> 요구사항 문서는 **유한하고 사람이 쓴다.** 검토 지적은 **무한하고 기계가 낸다.**
> 그러므로 판정은 문서를 기준으로 한다.

이 관점에서 **검토 지적은 "결함"이 아니라 "다음 버전 요구사항 후보"** 다.
버그가 0이라서 출시하는 게 아니라, **분류되지 않은 항목이 0이라서** 출시한다.

`RELEASE` 단계가 여섯 항목을 판정한다. **순수 코드다** — 모델이 재량으로 판정하면
자기 게이트를 자기가 여는 셈이 된다.

1. 모든 스토리 완료
2. 모든 요구사항이 통과하는 테스트에 도달 (요구사항→유스케이스→전이→DoD 사슬 추적)
3. 전체 테스트 통과
4. 승인되지 않은 `xfail`/`skip` 없음 (`.factory/approved_waivers.txt` 에 사람이 적은 것만 허용)
5. **열린 high 지적 없음** ← 루프를 끊는 자리
6. 진입점 실행 확인

심각도의 정의는 `principles.md` 에 산다 — 검토자가 임의로 매기면 안 되기 때문이다.
`high` = 고객이 돈을 잃는다 / 영업 불능. medium·low 는 알려진 이슈로 이월된다.

같은 결함을 다른 문장으로 다시 신고한 것은 참조(`T-*`/`REQ-*`/`UC-*`)가 겹치면 합친다.
v6 에서 high 7건이 실제로는 4건이었다.

## 명세를 구현 전에 실행한다 — SIMULATE

설계 단계(요구사항·유스케이스·FSM)는 전체 토큰의 **0.2%** 인데 나머지 99.8% 의 내용을 결정한다.
여기서 틀리면 그 뒤 수십만 토큰이 잘못된 것을 만드는 데 쓰인다. v4 는 명세 수준 모순을
구현 42건 만든 뒤 발견해 통째로 폐기했다.

FSM 은 구현이 없어도 그래프로서 실행할 수 있다. guard 가 `canMakeChange` 같은 도메인 술어라
평가할 수 없는 것이 문제인데 — **평가할 필요가 없다. 술어를 자유 변수로 두고 양쪽 가지를
모두 탐색하면 된다.** 의미를 몰라도 다음이 확인된다:

- **원칙 불변식** — "잔액을 보유할 수 있는 모든 상태에서 반환 전이가 도달 가능한가" (그래프 질의)
- **유스케이스 재생** — 선언된 이벤트 열을 FSM 이 실제로 밟는가
- **커버리지** — 어떤 유스케이스에도 안 걸리는 전이 / 전이가 없는 이벤트

과거 버전으로 검증했다. v3·v4·v5 의 "배출 실패 후 판매 상태 복귀"(P4 위반)를 전부 잡는다 —
사람이 v6 에서야 발견한 결함이다. v6 에서는 "원자적 저장 유스케이스를 참조하는 전이가 없다"를
잡았는데, 검토가 3사이클 돌려 찾은 `fsync` 결함의 상류 원인이다.

**결과는 결함 단정이 아니라 위반 후보다.** 술어를 자유 변수로 두면 실제로는 불가능한 경로도
나온다. 그래서 수리 프롬프트에 "타당하지 않으면 고치지 말고 이유를 적으라"고 넣었다.
그리고 노이즈가 많으면 아무도 안 읽는다 — v6 첫 실행의 16건 중 12건이 거짓 양성이라
검사를 좁혔고, 2건으로 줄었다.

## 비용은 어떻게 보는가

**절대 토큰 수는 예산으로 쓸 수 없다.** 전이 10개짜리의 1억과 500개짜리의 1억은 다른 의미다.
두 가지로만 쓴다.

1. **프로젝트 안** — 같은 단계 중앙값 대비 배수로 폭주 감지 (`MeteredWorker`).
   큰 프로젝트를 막지 않으면서 한 티켓이 유독 헤매는 것만 걸린다.
2. **같은 프로젝트 반복** — 도메인이 고정된 통제된 실험. `baselines/` 에 얼려 두고 비교한다.
   공장 변경의 효과를 재는 유일한 척도다.

`factory cost` 가 단계별 집계와 단위당 지표를 낸다. 기록은 `MeteredWorker` 라는
**단일 관문**에서 한다 — 단계마다 붙이면 반드시 빠뜨리는 곳이 생긴다 (`ask_yaml` 에서 배웠다).

## 아직 없는 것

- **JiraTracker 구현체** — 인터페이스만 있다 (`NotImplementedError`)
- **병렬 처리** — 티켓을 하나씩 순차 처리한다. 전이는 대부분 독립적이라 병렬화 여지가 크지만,
  같은 파일을 동시에 고치는 충돌 처리가 먼저 필요하다 (git worktree 격리)
- **INTERFACE.md 자동 갱신** — 구현이 공개 인터페이스를 바꿔도 문서가 따라가지 않는다.
  테스트가 명세인 구조에서는 인터페이스 드리프트가 곧 SPEC_CONFLICT로 나타난다.
- **비용 추적** — `WorkerResult.cost_usd` 를 받아오지만 집계하지 않는다.
