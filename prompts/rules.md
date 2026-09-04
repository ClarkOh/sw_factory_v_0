당신은 명세 설계자입니다. 유스케이스를 **결정표**로 옮기세요.

프로젝트: {project}

--- 유스케이스 ---
{usecases_yaml}

## 결정표란

규칙의 목록입니다. 각 규칙은 `when`(조건)과 `then`(결과)의 쌍이고,
**위에서부터 첫 번째로 조건이 참인 규칙이 적용됩니다.** 순서가 의미를 갖습니다.

상태 기계가 아닙니다. "지금 무슨 상태인가"라는 질문이 없는 도메인 —
입력을 읽어 분류·변환·집계하는 프로그램 — 을 위한 형식입니다.
상태를 지어내지 마세요. `AWAITING_*` 같은 이름이 필요해 보인다면
그건 상태가 아니라 루프 안의 위치이고, 이 표에는 들어가지 않습니다.

## 규칙

- `when` 은 입력 한 단위(예: 로그 한 줄, 요청 하나)에 대한 술어입니다.
  평가 불가능한 도메인 함수를 써도 됩니다: `valid_iso8601(fields[0])`.
- `then` 은 그 입력에 대한 처리입니다. 장부를 움직이면 반드시 함께 적으세요:
  `read_lines += 1; dropped += 1; count_reason(drop_reasons, invalid_timestamp)`.
- 모든 규칙에 `usecase` 로 출처(UC-*)를 답니다. 출처 없는 규칙은 요구사항에 없는
  발명이고, 규칙이 없는 유스케이스는 구현되지 않을 요구사항입니다.
- 규칙 하나가 티켓 하나, 테스트 하나가 됩니다. 한 규칙에 두 가지 일을 넣지 마세요.

## 출력 (YAML만)

```yaml
project: {project}
rules:
  - id: R-001
    when: "is_blank(line)"
    then: "read_lines += 1; dropped += 1; count_reason(drop_reasons, empty_line)"
    usecase: UC-03
  - id: R-002
    when: "count(fields) < 3"
    then: "..."
    usecase: UC-03
```
