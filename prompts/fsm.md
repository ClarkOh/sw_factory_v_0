당신은 시스템 모델러입니다. 아래 유스케이스를 **하나의** 유한상태기계로 통합하세요.

이 FSM은 이후 모든 자동화의 근거가 됩니다. 전이 하나가 티켓 하나, 테스트 하나가 됩니다.
따라서 전이는 "구현 가능하고 독립적으로 테스트 가능한 최소 단위"여야 합니다.

규칙:
- 상태는 시스템이 **기다리는 상황**이다. 동작 중인 절차가 아니다. (X: PROCESSING_PAYMENT / O: AWAITING_PAYMENT)
- **상태는 제품이 실제로 처하는 상황만 표현한다.** 명세가 미확정이거나 요구사항이 모호하다는 사실은
  제품의 상태가 아니라 문서의 문제다. `AWAITING_SPEC_DECISION` 같은 상태를 만들지 마라.
  미규정 사항은 그 전이의 notes 에 `[미규정 REQ-XX]` 로 적고, 문서에 적힌 범위까지만 모델링한다.
- 상태·이벤트 이름으로 `ON`, `OFF`, `YES`, `NO`, `NULL` 을 쓰지 마라. YAML이 불리언/널로 읽어
  이름이 사라진다. 부득이하면 `POWERED_OFF` 처럼 풀어 쓴다.
- initial: true 인 상태가 정확히 하나.
- 같은 (출발상태, 이벤트) 쌍이 여러 전이를 가지면 guard로 반드시 구분한다. guard 없는 중복은 비결정적이라 금지.
- 모든 상태는 초기 상태에서 도달 가능해야 한다.
- final이 아닌 상태에는 나가는 전이가 최소 하나 있어야 한다 (교착 금지).
- 유스케이스의 alt_flows(실패 경로)도 전이로 반드시 표현한다.
- guard/action은 짧은 표현식으로 쓴다. 구현 언어에 의존하지 않게.
- usecase 필드로 역추적을 남긴다.
- **guard와 action의 값은 반드시 큰따옴표로 감싼다.** 특히 `!`, `*`, `&`, `%` 로 시작하는 값
  (예: `guard: "!recognized"`)은 따옴표가 없으면 YAML 파싱이 깨진다. 빈 값은 `""` 로 쓴다.

출력: YAML **만**. 설명 금지.

```yaml
project: {project}
states:
  - id: IDLE
    desc: <이 상태에서 시스템이 무엇을 기다리는가>
    initial: true
    final: false
events:
  - id: INSERT_COIN
    desc: <언제 발생하는가>
    payload: {amount: int}
transitions:
  - id: T-001
    src: IDLE
    event: INSERT_COIN
    dst: HAS_CREDIT
    guard: "amount > 0"
    action: "credit += amount"
    usecase: UC-01
```

--- 유스케이스 ---
{usecases_yaml}
