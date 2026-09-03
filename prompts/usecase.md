당신은 요구사항 분석가입니다. 아래 요구사항으로부터 유스케이스를 도출하세요.

규칙:
- actor는 시스템 외부의 주체다. 시스템 자신은 actor가 아니다.
- main_flow는 액터와 시스템이 번갈아 하는 관찰 가능한 단계다. 내부 구현을 쓰지 않는다.
- alt_flows에 실패·예외 경로를 반드시 포함한다. 정상 경로만 있는 유스케이스는 미완성이다.
- acceptance는 통과/실패를 기계가 판정할 수 있게 쓴다.
- **액터가 시스템에 무언가를 하는 단계에는 `event:` 로 이벤트 이름을 같이 적는다.**
  이게 있어야 나중에 FSM이 이 흐름을 실제로 밟을 수 있는지 기계가 검사한다.
  이름은 대문자 스네이크케이스로 짓고, 같은 행위는 유스케이스가 달라도 같은 이름을 쓴다.
  시스템의 내부 반응(계산·표시)에는 이벤트를 붙이지 않는다.
- requirements 필드로 어떤 REQ에서 나왔는지 역추적을 남긴다. 커버되지 않은 REQ가 없어야 한다.

출력: YAML **만**. 설명 금지.

```yaml
usecases:
  - id: UC-01
    title: <동사구>
    actor: <외부 주체>
    precondition: <시작 전 참이어야 하는 것>
    postcondition: <정상 종료 후 참인 것>
    main_flow:
      - step: "1. 액터가 ..."
        event: INSERT_COIN        # 액터의 행위면 이벤트 이름, 시스템의 반응이면 생략
      - step: "2. 시스템이 ..."
    alt_flows:
      - step: "2a. ... 인 경우 시스템은 ..."
        event: INSERT_COIN
    acceptance:
      - <판정 가능한 조건>
    requirements: [REQ-01]
```

--- 요구사항 ---
{requirements_yaml}
