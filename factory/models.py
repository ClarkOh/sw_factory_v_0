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
