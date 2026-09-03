"""LLM이 뱉는 YAML의 흔한 파손을 복구한다.

모델은 `guard: !recognized` 처럼 쓴다. 사람 눈에는 멀쩡하지만 YAML에서 `!` 는 태그 문법이라
파서가 즉시 죽는다. `*`(별칭), `&`(앵커), `%`(디렉티브), 백틱도 같은 부류다.

프롬프트에서 따옴표를 치라고 지시해도 몇 번에 한 번은 빠뜨린다. 그때마다 비싼 호출을
통째로 버릴 수는 없으므로, 파싱 실패 시 한 번 고쳐 보고 그래도 안 되면 포기한다.
"""
from __future__ import annotations

import re

# 값의 첫 글자가 이것들이면 YAML이 특별하게 해석한다.
_SPECIAL_HEAD = ("!", "&", "*", "%", "`", "@")

# YAML 1.1은 이 토큰들을 불리언/널로 읽는다. 상태 이름이 OFF 면 id 가 False 가 되어
# 상태 이름이 통째로 사라진다. 실제로 겪은 사고다.
_BOOLISH = {
    "y", "yes", "n", "no", "true", "false", "on", "off",
    "null", "none", "~",
}

# 식별자가 들어가는 필드. 여기서는 불리언 해석이 항상 사고다.
# initial/final 같은 진짜 불리언 필드는 건드리면 안 되므로 화이트리스트로 간다.
_ID_KEYS = {"id", "src", "dst", "event", "usecase", "from", "to", "state", "parent"}

# 자유 서술이 들어가는 필드. 여기에 `: ` 가 섞이면 YAML이 매핑으로 착각해 파싱이 깨진다.
# "1번째 지적: 초기화 테스트가 약하다" 같은 제목은 모델이 흔히 쓴다.
_TEXT_KEYS = {
    "title", "desc", "description", "text", "evidence", "impact", "action", "notes",
    "source", "precondition", "postcondition", "guard", "summary", "reason", "message",
}
_COLON_SPACE = re.compile(r":(\s|$)")

# 시퀀스 항목:  `- 자유 서술`
# 검토자의 `checked:` 목록처럼 서술문이 통째로 항목이 되는 곳에서 `: ` 가 섞이면 깨진다.
_SEQ_ITEM = re.compile(r"^(?P<indent>\s*)-(?P<gap>[ \t]+)(?P<val>\S.*?)\s*$")
# `- id: T-001` 처럼 항목 자체가 매핑이면 건드리면 안 된다.
_SEQ_MAPPING = re.compile(r"^[A-Za-z_][\w.-]*:(\s|$)")


def _is_boolish(val: str) -> bool:
    return val.strip().lower() in _BOOLISH

# block 스타일:  key: value
_BLOCK = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w-]*):(?P<gap>[ \t]+)(?P<val>\S.*?)\s*$")

# flow 스타일 안의 특정 키:  {..., guard: !recognized, ...}
_FLOW = re.compile(r"(?P<key>\b(?:guard|action|desc|text|title|source)\s*:\s*)(?P<val>[!&*%`@][^,}\]\n]*)")

# flow 스타일 안의 식별자 필드:  {id: OFF, src: ON, ...}
_FLOW_ID = re.compile(
    r"(?P<key>\b(?:id|src|dst|event|usecase|from|to|state|parent)\s*:\s*)"
    r"(?P<val>[A-Za-z~][\w-]*)(?=\s*[,}\]]|\s*$)")


def _needs_quote(val: str) -> bool:
    if not val or val[0] in "\"'[{|>#":
        return False
    return val[0] in _SPECIAL_HEAD


def _seq_needs_quote(val: str) -> bool:
    """`- 자유 서술` 항목에 따옴표가 필요한가.

    `- id: T-001` 같은 매핑 항목은 절대 건드리면 안 된다.
    """
    if not val or val[0] in "\"'[{|>#&*!":
        return False
    if _SEQ_MAPPING.match(val):
        return False
    return bool(_COLON_SPACE.search(val))


def _quote(val: str) -> str:
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'


def repair(text: str) -> str:
    """따옴표가 빠진 특수문자 시작 스칼라를 감싼다. 그 외에는 손대지 않는다.

    결과는 yaml.safe_load 에만 넘기고 디스크에 쓰지 않는다. 줄바꿈이 LF로 정규화되는 것은
    그래서 문제가 되지 않는다.
    """
    out = []
    for line in text.splitlines():
        seq = _SEQ_ITEM.match(line)
        if seq and _seq_needs_quote(seq.group("val")):
            out.append(f"{seq.group('indent')}-{seq.group('gap')}{_quote(seq.group('val'))}")
            continue
        m = _BLOCK.match(line)
        if m:
            key, val = m.group("key"), m.group("val")
            unquoted = val[0] not in "\"'[{|>#" if val else False
            if (_needs_quote(val)
                    or (unquoted and key in _ID_KEYS and _is_boolish(val))
                    or (unquoted and key in _TEXT_KEYS and _COLON_SPACE.search(val))):
                line = f"{m.group('indent')}{key}:{m.group('gap')}{_quote(val)}"
            else:
                line = _fix_flow(line)
        else:
            line = _fix_flow(line)
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith(("\n", "\r")) else "")


def _fix_flow(line: str) -> str:
    """{id: OFF, src: ON} 같은 flow 스타일 안의 값도 같은 규칙으로 고친다."""
    line = _FLOW.sub(lambda mm: mm.group("key") + _quote(mm.group("val").rstrip()), line)
    return _FLOW_ID.sub(
        lambda mm: mm.group("key") + _quote(mm.group("val").strip())
        if _is_boolish(mm.group("val")) else mm.group(0),
        line,
    )
