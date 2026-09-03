당신은 요구사항 분석가입니다. 아래 요구사항 문서를 원자적 요구사항으로 분해하세요.

규칙:
- 한 항목은 하나만 주장한다. "그리고/또한"으로 두 가지를 말하면 쪼갠다.
- 검증 가능해야 한다. "빠르게"가 아니라 "3초 이내".
- 문서에 없는 내용을 지어내지 않는다. 애매하면 kind를 constraint로 두고 text에 [모호] 표시.

출력: YAML **만**. 설명 금지.

```yaml
requirements:
  - id: REQ-01
    text: <검증 가능한 한 문장>
    kind: functional | nonfunctional | constraint
    source: <문서에서 근거가 된 구절>
```

--- 요구사항 문서 ---
{requirements_md}
