"""SW Factory 도메인 모델.

파이프라인 전 구간에서 오가는 산출물의 타입 정의.
모든 모델은 YAML round-trip 가능해야 한다 (to_dict / from_dict).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------
# 요구사항 / 유스케이스
# --------------------------------------------------------------------------

@dataclass
class Requirement:
    id: str                       # REQ-01
    text: str
    kind: str = "functional"      # functional | nonfunctional | constraint
    source: str = ""              # 출처 (문서, 인터뷰, 이슈번호)


@dataclass
class UseCase:
    id: str                       # UC-01
    title: str
    actor: str
    precondition: str = ""
    postcondition: str = ""
    main_flow: list[str] = field(default_factory=list)
    alt_flows: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)   # REQ-* 역추적


# --------------------------------------------------------------------------
# FSM — 자동화의 척추.
# 하나의 Transition == 하나의 Story == 하나의 테스트 케이스 == 하나의 DoD.
# --------------------------------------------------------------------------

@dataclass
class State:
    id: str                       # IDLE
    desc: str = ""
    initial: bool = False
    final: bool = False


@dataclass
class Event:
    id: str                       # INSERT_COIN
    desc: str = ""
    payload: dict[str, str] = field(default_factory=dict)   # {"amount": "int"}


@dataclass
class Rule:
    """결정표의 규칙 하나 — **좌표(src/dst)를 뺀 전이** 다.

    두 번째 도메인(로그 집계)을 FSM 으로 억지로 만들었더니 상태 7개가 전부
    `AWAITING_*` 였고, 핵심 규칙 7건은 출발·이벤트·도착이 모두 같은 자기 루프였다.
    상태 기계가 아니라 switch 문이었다 -- src/dst 가 정보를 하나도 안 날랐다.

    파이프라인이 구조적으로 쓰는 것은 다섯 가지뿐이다: id, test_name, usecase,
    내용(when/then), signature. 그 다섯이 살아 있으면
    **규칙 1개 = 티켓 1개 = 테스트 1개 = 완료 정의 1개** 대응도 그대로 산다.
    """
    id: str                       # R-001
    when: str = ""                # 조건 (guard 에 해당)
    then: str = ""                # 결과 (action 에 해당)
    usecase: str = ""             # UC-* 역추적
    notes: str = ""

    # 보존량 검사 등이 전이와 규칙을 같은 코드로 읽도록 이름을 맞춘다.
    @property
    def guard(self) -> str:
        return self.when

    @property
    def action(self) -> str:
        return self.then

    @property
    def test_name(self) -> str:
        return f"test_{self.id.lower().replace('-', '_')}"

    def signature(self) -> str:
        raw = f"{self.when}|{self.then}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def describe(self) -> str:
        """티켓 제목용 한 줄."""
        w = self.when.strip() or "(무조건)"
        return f"[{self.id}] {w[:60]}"

    def context_md(self) -> list[str]:
        """워커 프롬프트에 넣을 문맥."""
        return [f"- 조건: `{self.when or '(없음)'}`",
                f"- 결과: `{self.then or '(없음)'}`"]


@dataclass
class RuleTable:
    """결정표 — 상태 없는 도메인의 중간 형식. FSM 과 같은 자리에 선다.

    규칙의 순서가 의미를 갖는다: 위에서부터 첫 번째로 조건이 참인 규칙이 적용된다.
    (순서 무관이라고 선언하면 겹침 검사가 필요해지는데, 그 검사는 조건식을 평가할 수
    있어야 가능하다. 술어가 자유 변수인 이 단계에서는 순서 우선이 정직하다.)
    """
    project: str
    rules: list[Rule] = field(default_factory=list)

    # 파이프라인이 형식을 모르고 원자를 셀 수 있게 한다. FSM 쪽에도 같은 것이 있다.
    @property
    def atoms(self) -> list[Rule]:
        return self.rules

    def validate(self) -> list[str]:
        errs: list[str] = []
        for r in self.rules:
            if not isinstance(r.id, str) or not r.id.strip():
                errs.append(f"규칙 id가 문자열이 아님: {r.id!r}")
        if errs:
            return errs
        if not self.rules:
            errs.append("규칙이 하나도 없음")
        seen: set[str] = set()
        for r in self.rules:
            if r.id in seen:
                errs.append(f"{r.id}: 규칙 id 중복")
            seen.add(r.id)
            if not (r.when.strip() or r.then.strip()):
                errs.append(f"{r.id}: 조건도 결과도 비어 있음")
        return errs


@dataclass
class Transition:
    id: str                       # T-001
    src: str                      # from state id
    event: str                    # event id
    dst: str                      # to state id
    guard: str = ""               # 조건식 (자연어 또는 표현식)
    action: str = ""              # 부수효과
    usecase: str = ""             # UC-* 역추적
    notes: str = ""

    @property
    def test_name(self) -> str:
        """이 전이를 검증하는 테스트 함수 이름. DoD 판정의 앵커."""
        return f"test_{self.id.lower().replace('-', '_')}"

    def signature(self) -> str:
        """전이의 의미론적 지문. 바뀌면 티켓/테스트를 다시 만들어야 한다."""
        raw = f"{self.src}|{self.event}|{self.guard}|{self.dst}|{self.action}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def describe(self) -> str:
        return f"[{self.id}] {self.src} --{self.event}--> {self.dst}"

    def context_md(self) -> list[str]:
        return [f"- 출발: `{self.src}`",
                f"- 이벤트: `{self.event}`",
                f"- 도착: `{self.dst}`",
                f"- guard: `{self.guard or '(없음)'}`",
                f"- action: `{self.action or '(없음)'}`"]


@dataclass
class FSM:
    project: str
    states: list[State] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)

    @property
    def initial(self) -> str | None:
        for s in self.states:
            if s.initial:
                return s.id
        return None

    @property
    def atoms(self) -> list[Transition]:
        """형식 무관 원자 목록. RuleTable 에도 같은 것이 있다 --
        티켓화·1:1 검사·추적은 이걸 통해서만 원자를 만진다."""
        return self.transitions

    def validate(self) -> list[str]:
        """구조적 결함 목록. 비어 있어야 다음 단계로 넘어간다.

        여기서 막지 않으면 결함이 티켓 수십 개로 증식한다.
        """
        errs: list[str] = []

        # 식별자가 문자열이 아니면 파싱이 깨진 것이다. YAML은 OFF/NO/ON 을 불리언으로 읽으므로
        # 상태 이름이 통째로 사라질 수 있다. 티켓을 만들기 전에 여기서 잡는다.
        for label, items in (("상태", self.states), ("이벤트", self.events), ("전이", self.transitions)):
            for it in items:
                if not isinstance(it.id, str) or not it.id.strip():
                    errs.append(
                        f"{label} id가 문자열이 아님: {it.id!r} "
                        f"(YAML이 OFF/ON/NO/YES 를 불리언으로 읽었을 수 있음 — 따옴표 필요)")
        for t in self.transitions:
            for fld in ("src", "dst", "event"):
                v = getattr(t, fld)
                if not isinstance(v, str) or not v.strip():
                    errs.append(f"전이 {t.id!r}: {fld} 가 문자열이 아님 ({v!r})")
        if errs:
            return errs      # 식별자가 깨졌으면 나머지 검사는 의미가 없다

        sids = {s.id for s in self.states}
        eids = {e.id for e in self.events}

        inits = [s.id for s in self.states if s.initial]
        if len(inits) != 1:
            errs.append(f"initial 상태가 정확히 1개여야 함 (현재 {len(inits)}개: {inits})")
        if not sids:
            errs.append("상태가 하나도 없음")

        seen_t: set[str] = set()
        for t in self.transitions:
            if t.id in seen_t:
                errs.append(f"{t.id}: 전이 id 중복")
            seen_t.add(t.id)
            if t.src not in sids:
                errs.append(f"{t.id}: 미정의 출발 상태 '{t.src}'")
            if t.dst not in sids:
                errs.append(f"{t.id}: 미정의 도착 상태 '{t.dst}'")
            if t.event not in eids:
                errs.append(f"{t.id}: 미정의 이벤트 '{t.event}'")

        # 비결정성: 같은 (src, event)에 guard 없는 전이가 2개 이상이면 동작이 모호하다.
        bucket: dict[tuple[str, str], list[Transition]] = {}
        for t in self.transitions:
            bucket.setdefault((t.src, t.event), []).append(t)
        for (src, ev), ts in bucket.items():
            unguarded = [t for t in ts if not t.guard.strip()]
            if len(ts) > 1 and len(unguarded) > 1:
                ids = ", ".join(t.id for t in unguarded)
                errs.append(f"({src}, {ev}): guard 없는 전이가 {len(unguarded)}개 → 비결정적 [{ids}]")

        # 도달 불가 상태: 초기 상태에서 BFS
        if inits:
            reach = {inits[0]}
            changed = True
            while changed:
                changed = False
                for t in self.transitions:
                    if t.src in reach and t.dst not in reach:
                        reach.add(t.dst)
                        changed = True
            for s in self.states:
                if s.id not in reach:
                    errs.append(f"상태 '{s.id}': 초기 상태에서 도달 불가")

        # 막다른 상태: final이 아닌데 나가는 전이가 없음
        outgoing = {t.src for t in self.transitions}
        for s in self.states:
            if not s.final and s.id not in outgoing:
                errs.append(f"상태 '{s.id}': final이 아닌데 나가는 전이가 없음 (deadlock)")

        return errs


# --------------------------------------------------------------------------
# 티켓 (Jira 추상화)
# --------------------------------------------------------------------------

class TicketType(str, Enum):
    EPIC = "epic"
    STORY = "story"
    SUBTASK = "subtask"
    BUG = "bug"


class Status(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"          # 자동 처리 실패 → 사람 검토 대기
    DONE = "done"


@dataclass
class Ticket:
    key: str = ""                              # SWF-1 (tracker가 채움)
    type: str = TicketType.STORY.value
    title: str = ""
    description: str = ""
    status: str = Status.TODO.value
    parent: str | None = None
    labels: list[str] = field(default_factory=list)
    # --- 추적성 ---
    fsm_ref: str = ""                          # T-001
    usecase_ref: str = ""                      # UC-01
    signature: str = ""                        # Transition.signature()
    # --- DoD: 이 티켓이 끝났는지 기계가 판정하는 근거 ---
    dod_tests: list[str] = field(default_factory=list)   # pytest node id
    # --- 실행 이력 ---
    attempts: int = 0
    last_error: str = ""
    artifacts: list[str] = field(default_factory=list)   # 건드린 파일

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ticket":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
