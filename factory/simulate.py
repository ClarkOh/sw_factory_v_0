"""FSM 모델 검사 — 구현 전에 명세를 실행해 본다.

**왜.** 설계 단계(요구사항·유스케이스·FSM)는 전체 토큰의 0.2% 인데 나머지 99.8% 의 내용을
결정한다. 여기서 틀리면 그 뒤 수십만 토큰이 잘못된 것을 만드는 데 쓰인다.
실제로 v4 는 명세 수준 모순을 구현 42건 만든 뒤에야 발견해 통째로 폐기했다.

**어떻게.** FSM 은 구현이 없어도 그래프로서 실행 가능하다. 문제는 guard 가
`canMakeChange`, `stockAfterDecrement(drink)` 같은 **도메인 술어**라 평가할 수 없다는 것인데 --
평가할 필요가 없다. **술어를 자유 변수로 두고 양쪽 가지를 모두 탐색하면 된다.**
`canMakeChange` 의 의미를 몰라도 다음을 확인할 수 있다:

  - 원칙 불변식 (그래프 도달성 질의)
  - 유스케이스 재생 (선언된 이벤트 열이 FSM 에서 밟히는가)
  - 양방향 커버리지 (안 쓰이는 전이 / 전이 없는 유스케이스 단계)
  - 이벤트 완결성 (정의되지 않아 조용히 무시되는 (상태, 이벤트) 조합)

**한계.** 술어를 자유 변수로 두면 실제로는 불가능한 경로도 '가능'으로 나온다.
그래서 결과는 결함 단정이 아니라 **위반 후보** 다. 수리 루프나 사람에게 넘긴다.
그리고 이 검사는 명세가 *자기 자신과* 맞는지만 본다. 현실과 맞는지는 여전히 사람 몫이다.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from factory.models import FSM, UseCase

# 돈이 고객에게 나가는 동작. 원칙 P1(회수 가능성) 검사의 앵커.
REFUND_WORDS = ("refund", "pay_out", "payout", "return_", "returncoin", "eject",
                "반환", "지급", "돌려")

# 사람이 장부를 실측값으로 고치는 동작. 원칙 P3(장부는 주장이다) 검사의 앵커.
ADJUST_WORDS = ("adjust", "correct", "set_stock", "reconcile", "collect", "조정", "정정")

# 세대마다 표기가 달랐다. v6/v7 은 `credit = 0`, v8 은 `credit := 0` 을 썼고
# 보관액은 `unreturnedBalance` / `deposit` / `ledger.credit` 로 제각각이었다.
# **검사가 표기를 하나로 가정하면 다음 세대에서 조용히 통과한다.** 실제로 그렇게 뚫렸다 --
# 지적 0건이 '깨끗하다'가 아니라 '검사가 안 돌았다'였다.
_ASSIGN = r"(?::=|=)"                 # `x = 0` 과 `x := 0` 을 함께. `==` 은 안 걸린다.
_CREDIT = r"(?:[\w.]*\.)?credit"      # `credit` 과 `ledger.credit` 을 함께.

# 실물이 존재해 장부와 어긋날 수 있는 것만 P3 대상이다.
# 일련번호·매출 누계처럼 물리 대응물이 없는 값은 '실측 대조'가 성립하지 않는다.
PHYSICAL_LEDGERS = ("stock", "coin", "재고", "동전", "held", "retained", "보관")


@dataclass
class Finding:
    check: str                  # 어떤 검사에서 나왔는가
    principle: str              # 어느 원칙인가 (없으면 "")
    detail: str
    refs: list[str] = field(default_factory=list)
    # FSM 을 고쳐서 해소되는 지적인가. 다른 산출물(유스케이스 등)의 문제면 False --
    # 수리 루프에 넘기면 고칠 수 없는 것을 상한까지 시도하며 토큰만 태운다.
    fixable_by_fsm: bool = True

    def __str__(self) -> str:
        p = f"[{self.principle}] " if self.principle else ""
        r = f" ({', '.join(self.refs[:6])})" if self.refs else ""
        return f"{p}{self.detail}{r}"


# --------------------------------------------------------------------------
# 그래프 기본기
# --------------------------------------------------------------------------

def _outgoing(fsm: FSM) -> dict[str, list]:
    g = defaultdict(list)
    for t in fsm.transitions:
        g[t.src].append(t)
    return g


def _reachable_from(fsm: FSM, start: str) -> set[str]:
    """술어를 자유 변수로 본다 -- 모든 가지를 열어놓고 걷는다."""
    g = _outgoing(fsm)
    seen, stack = {start}, [start]
    while stack:
        for t in g.get(stack.pop(), []):
            if t.dst not in seen:
                seen.add(t.dst)
                stack.append(t.dst)
    return seen


def _has_word(text: str, words) -> bool:
    low = (text or "").lower()
    return any(w in low for w in words)


# --------------------------------------------------------------------------
# 검사 1: 원칙 불변식
# --------------------------------------------------------------------------

def check_refund_reachable(fsm: FSM) -> list[Finding]:
    """P1. 잔액을 보유할 수 있는 모든 상태에서 반환 전이가 도달 가능한가.

    잔액 보유 가능 상태 = 잔액을 늘리는 전이의 도착 상태, 또는 그런 상태에서
    잔액을 비우지 않고 갈 수 있는 상태.
    """
    out = []
    # 잔액을 늘리는 전이가 도착시키는 상태들
    holding = {t.dst for t in fsm.transitions
               if re.search(rf"{_CREDIT}\s*\+=|balance\s*\+=|누적.*\+", t.action or "")}
    if not holding:
        return []                     # 잔액 개념이 없는 FSM

    for state in sorted(holding):
        # 이 상태에서 도달 가능한 모든 상태의 전이 중 반환 동작이 있는가
        reach = _reachable_from(fsm, state)
        refunds = [t for t in fsm.transitions
                   if t.src in reach and _has_word(t.action, REFUND_WORDS)]
        if not refunds:
            out.append(Finding(
                "원칙 불변식", "P1",
                f"상태 '{state}' 에서 잔액을 보유할 수 있으나, 거기서 도달 가능한 어떤 전이도 "
                f"돈을 고객에게 돌려주지 않는다", [state]))
    return out


def check_ledger_adjustable(fsm: FSM) -> list[Finding]:
    """P3. 장부에 담긴 양을 사람이 실측값으로 고칠 경로가 있는가."""
    out = []
    # action 에서 갱신되는 장부성 변수를 뽑는다
    ledgers = set()
    for t in fsm.transitions:
        for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\s*\[[^\]]*\]\s*[-+]=|\b([a-z_][a-z0-9_]*)\s*[-+]=",
                             (t.action or "").lower()):
            name = m.group(1) or m.group(2)
            if name in ("credit", "balance"):
                continue              # 잔액은 거래 중 값이지 장부가 아니다
            if not any(w in name for w in PHYSICAL_LEDGERS):
                continue              # 실물이 없으면 맞출 대상도 없다
            ledgers.add(name)
    for name in sorted(ledgers):
        fixers = [t for t in fsm.transitions
                  if name in (t.action or "").lower() and _has_word(t.event + " " + t.action, ADJUST_WORDS)]
        if not fixers:
            out.append(Finding(
                "원칙 불변식", "P3",
                f"'{name}' 은 장부인데 관리자가 실측값으로 고칠 경로가 없다 — "
                f"실물과 어긋나면 영영 못 맞춘다", [name]))
    return out


def check_fault_stops_selling(fsm: FSM) -> list[Finding]:
    """P4. 하드웨어 실패를 감지한 전이가 판매를 계속하는 상태로 가는가."""
    out = []
    # '실패'는 하드웨어가 응답하지 않은 것이다. 무조작 타임아웃(고객이 가만히 있음)은
    # 정상 흐름이므로 제외한다 -- 안 그러면 IDLE_TIMEOUT 이 전부 고장으로 잡힌다.
    fail_events = [e.id for e in fsm.events
                   if re.search(r"(dispense|vend|motor|hardware|배출).*(timeout|fail|error|jam)"
                                r"|(timeout|fail|error|jam).*(dispense|vend|motor|배출)",
                                e.id, re.I)]
    selling = {t.src for t in fsm.transitions
               if re.search(r"insert.?coin|코인", t.event, re.I)
               and re.search(r"credit\s*\+=|누적", t.action or "")}
    for t in fsm.transitions:
        if t.event in fail_events and t.dst in selling:
            out.append(Finding(
                "원칙 불변식", "P4",
                f"{t.id}: 실패 이벤트 '{t.event}' 인데 판매 가능 상태 '{t.dst}' 로 간다 — "
                f"다음 고객도 같은 실패를 겪는다", [t.id]))
    return out


def check_every_transition_persists(fsm: FSM) -> list[Finding]:
    """P5. 상태를 바꾸는 전이가 저장하는가. 저장 개념이 없는 FSM 이면 건너뛴다."""
    save = re.compile(r"save\(|persist\(|저장", re.I)
    if not any(save.search(t.action or "") for t in fsm.transitions):
        return []
    missing = [t for t in fsm.transitions
               if t.src != t.dst and t.action and not save.search(t.action)]
    return [Finding("원칙 불변식", "P5",
                    f"{t.id}: 상태를 바꾸면서 저장하지 않는다 — 여기서 전원이 끊기면 소실된다", [t.id])
            for t in missing]


def _states_that_can_hold_credit(fsm: FSM) -> set[str]:
    """고객 돈이 남은 채로 들어올 수 있는 상태.

    전원이 꺼진 기계를 켜면서 `credit = 0` 하는 것은 **초기화**지 돈을 버리는 게 아니다.
    잃을 돈이 없는 자리까지 지적하면 검사가 소음이 되고, 소음이 되면 아무도 안 본다.

    잔액을 늘리는 전이의 도착지에서 출발해서, 잔액을 비우지 않는 전이를 따라 퍼뜨린다.
    단, **잔액을 쳐다보지도 않는 상태로는 퍼지지 않는다** -- 전원 꺼짐이 그렇다.
    그런 자리로 돈을 들고 넘어가는 것 자체가 결함이고, 그건 아래에서 따로 지적한다.
    """
    live = _credit_aware_states(fsm)
    # 씨앗은 거르지 않는다 -- 잔액을 늘리는 전이의 도착지는 정의상 돈이 있는 자리다.
    # 거르는 것은 전파뿐이다.
    holds = {t.dst for t in fsm.transitions
             if re.search(rf"\b{_CREDIT}\s*\+=", t.action or "")}
    changed = True
    while changed:
        changed = False
        for t in fsm.transitions:
            if t.src in holds and t.dst in live and t.dst not in holds \
                    and not re.search(rf"\b{_CREDIT}\s*{_ASSIGN}\s*0", t.action or "") \
                    and not _guard_pins_credit_zero(t.guard or ""):
                holds.add(t.dst)
                changed = True
    return holds


def _guard_pins_credit_zero(guard: str) -> bool:
    """guard 가 잔액을 0 으로 못박는가.

    `credit == 0` 뿐 아니라 `credit - change - price(drink) == 0` 처럼 **산술식**으로
    못박는 경우까지 본다. v8 이 정확히 그렇게 썼다 -- 남는 돈이 있으면 다른 갈래로
    보내고, 이 갈래는 잔액이 0 일 때만 탄다.

    이걸 못 읽으면 옳게 만든 설계를 지적하게 된다. guard 는 대부분 평가할 수 없는
    도메인 술어지만, **잔액 자신에 대한 산술 제약은 평가할 수 있다.**
    """
    for conj in re.split(r"&&|\|\||\band\b", guard or ""):
        if re.search(rf"\b{_CREDIT}\b", conj) and re.search(r"==\s*0\b", conj):
            return True
    return False


def _credit_aware_states(fsm: FSM) -> set[str]:
    """잔액이 살아 있는 상태 -- 나가는 전이 중 잔액을 읽거나 쓰는 것이 있는 자리.

    전원 꺼짐에서 나가는 전이는 잔액을 *덮어쓸* 뿐 읽지 않는다. 거기의 `credit` 은
    장부가 아니라 꺼진 메모리다. 그래서 보관액(deposit)이라는 별도 장부가 존재한다.
    """
    reads = re.compile(rf"\b{_CREDIT}\b")
    writes_zero = re.compile(rf"\b{_CREDIT}\s*{_ASSIGN}\s*0")
    out = set()
    for t in fsm.transitions:
        g, a = t.guard or "", t.action or ""
        if reads.search(g) or reads.search(writes_zero.sub("", a)):
            out.add(t.src)
    return out


def check_ledger_balance(fsm: FSM) -> list[Finding]:
    """P2/ACID-C. 전이 하나 안에서 보존량의 수지가 맞는가.

    **왜 전이 단위인가.** 요구사항에 "전이가 끝난 시점의 장부는 항상 등식을 만족한다"고
    써 두고도 아무도 검사하지 않았다. 그 결과 v7 은 이렇게 만들어졌다:

        T-025 SELECT_DRINK   credit = 0; refund(credit - price)   <- 700원이 사라지고
        T-041 DISPENSE_DONE  sales += price                       <- 여기서 나타난다

    두 전이를 합치면 균형이 맞지만, **그 사이 상태에서는 장부가 깨져 있다.**
    거기서 정전이나 배출 실패가 나면 그 700원이 증발한다.
    실제로 검토(Critic)가 두 버전에 걸쳐 증상을 여섯 번 신고했으나 원인을 묶지 못했다.

    다른 검사들과 달리 이것은 **경로가 아니라 한 걸음**을 본다.
    보존량은 전이를 건너 흐르면 안 된다 -- 그게 원자성의 의미다.
    """
    out = []
    holds = _states_that_can_hold_credit(fsm)
    aware = _credit_aware_states(fsm)
    abandoned: dict[tuple[str, str], list[str]] = {}
    for t in fsm.transitions:
        a = t.action or ""
        # guard 가 잔액 0 을 요구하면 그 전이에서는 **잃을 돈이 없다.**
        # v7 T-053 이 정확히 그렇게 막아 두었다 -- 그걸 못 읽으면 옳은 설계를 지적하게 된다.
        # 다만 아래 매출 규칙에는 면제가 아니다. 거기서 잔액 0 은 오히려 증거다 --
        # 물건값을 받기도 전에 돈이 이미 어딘가로 갔다는 뜻이니까.
        guard_zero = _guard_pins_credit_zero(t.guard or "")
        pays = [x.strip() for x in
                re.findall(r"\b(?:refund|pay|pay_change|pay_out|payout)\(([^)]*)\)", a)]
        sales = re.findall(r"sales\s*\+=\s*([\w()]+)", a)
        # 잔액을 **다른 장부로 옮겨 담은** 경우. 그 장부의 이름을 검사가 알 필요는 없다 --
        # v6 은 `unreturnedBalance`, v7 은 `deposit` 이었다. 이름을 박아 두면 버전마다 깨진다.
        moved = re.findall(rf"(\w+)\s*\+=\s*{_CREDIT}\b", a)
        zeroed = bool(re.search(rf"\b{_CREDIT}\s*{_ASSIGN}\s*0", a))
        reduced = bool(re.search(rf"{_CREDIT}\s*-=", a))

        # 잔액이 남은 채 들어올 수 없는 상태에서의 `credit = 0` 은 초기화지 손실이 아니다.
        if zeroed and t.src in holds and not guard_zero:
            full = any(p == "credit" or p == "" for p in pays)
            partial = [p for p in pays if p and p != "credit"]
            if not full and not sales and not moved:
                where = f"일부({partial[0]})만 지급하고" if partial else "지급·매출·이월 어느 것도 없이"
                out.append(Finding(
                    "장부 수지", "P2",
                    f"{t.id}: 잔액을 비우면서 {where} 나머지의 행선지가 없다 — "
                    f"이 전이 뒤의 '{t.dst}' 에서 장부가 깨져 있다", [t.id]))
        if sales and not zeroed and not reduced:
            out.append(Finding(
                "장부 수지", "P2",
                f"{t.id}: 매출({sales[0]})을 잡는데 대응하는 잔액 차감이 이 전이에 없다 — "
                f"돈이 앞선 전이에서 이미 사라졌다는 뜻이다", [t.id]))

        # 잔액을 든 채, 잔액이라는 개념이 없는 자리로 넘어간다.
        # v7 의 T-008 (POWER_OFF, action 없음) 이 이것이다. v6 은 같은 자리에서
        # `unreturnedBalance += credit` 로 옮겨 담아 두었다 -- 그래서 안 걸린다.
        if t.src in holds and t.dst not in holds and t.dst not in aware \
                and not guard_zero and not (pays or sales or moved or zeroed or reduced):
            # 같은 사건의 여러 갈래는 한 결함이다. 네 건으로 세면 심각도가 부풀고,
            # 고치는 사람은 네 번 같은 판단을 다시 한다.
            abandoned.setdefault((t.event, t.dst), []).append(t.id)

    for (event, dst), ids in abandoned.items():
        many = f" ({len(ids)}개 출발 상태에서 모두)" if len(ids) > 1 else ""
        out.append(Finding(
            "장부 수지", "P2",
            f"{', '.join(ids)}: 잔액을 든 채 '{event}' 로 '{dst}' 에 넘어가는데 "
            f"그 상태에는 잔액이라는 개념이 없다{many} — "
            f"돈을 별도 장부로 옮겨 담거나 돌려주고 가야 한다", ids))
    return out


def check_stock_balance(fsm: FSM) -> list[Finding]:
    """재고도 같다. 실물을 내보내는 전이와 장부를 줄이는 전이가 갈라지면 안 된다."""
    out = []
    # 실물이 나가는 동작만. `log_late_dispense` · `show_pending_dispense` 처럼
    # 이름에 dispense 가 들어갈 뿐인 것을 잡으면 지적이 전부 소음이 된다.
    gives_re = re.compile(r"\b(?:complete_dispense|finish_dispense|deliver|vend|eject_product)\s*\(", re.I)
    cuts_re = re.compile(r"stock\[[^\]]*\]\s*-=")
    for t in fsm.transitions:
        a = t.action or ""
        if gives_re.search(a) and not cuts_re.search(a):
            out.append(Finding(
                "장부 수지", "P3",
                f"{t.id}: 실물을 내보내면서 재고 장부를 줄이지 않는다", [t.id]))
    return out


# --------------------------------------------------------------------------
# 검사 2: 유스케이스 재생
# --------------------------------------------------------------------------

_EVENT_TAG = re.compile(r"\[event:\s*([A-Z_][A-Z0-9_]*)\s*\]")


def _event_sequence(steps: list) -> list[str]:
    """유스케이스 단계에서 이벤트 열을 뽑는다.

    두 형태를 받는다:  {step: ..., event: X}  또는  "1. ... [event: X]"
    """
    seq = []
    for s in steps:
        if isinstance(s, dict) and s.get("event"):
            seq.append(str(s["event"]))
        elif isinstance(s, str):
            m = _EVENT_TAG.search(s)
            if m:
                seq.append(m.group(1))
    return seq


def replay_usecases(fsm: FSM, usecases: list[UseCase]) -> list[Finding]:
    """각 유스케이스의 이벤트 열이 FSM 에서 실제로 밟히는가.

    술어는 자유 변수이므로 '어떤 가지로든 갈 수 있으면' 통과로 본다.
    그래도 못 밟으면 FSM 이 그 유스케이스를 구현하지 않은 것이다.
    """
    out = []
    g = _outgoing(fsm)
    tagged = 0

    # 유스케이스는 **사전조건이 만족된 상태**에서 시작하지 초기 상태에서 시작하지 않는다.
    # UC-01 의 "코인 투입"은 전원이 켜진 뒤의 이야기다. 사전조건은 산문이라 기계가 못 읽으므로,
    # 초기 상태에서 도달 가능한 모든 상태를 출발점 후보로 둔다.
    # (초기 상태를 강요했더니 v7 에서 유스케이스 18개가 전부 거짓 양성으로 나왔다.)
    origins = _reachable_from(fsm, fsm.initial) if fsm.initial else {s.id for s in fsm.states}

    for uc in usecases:
        seq = _event_sequence(uc.main_flow)
        if not seq:
            continue
        tagged += 1
        cur = set(origins)
        for i, ev in enumerate(seq, 1):
            nxt = {t.dst for s in cur for t in g.get(s, []) if t.event == ev}
            if not nxt:
                out.append(Finding(
                    "유스케이스 재생", "",
                    f"{uc.id} 의 {i}번째 이벤트 '{ev}' 를 어느 상태에서도 처리할 수 없다 "
                    f"(그때까지 도달한 상태: {', '.join(sorted(cur)) or '없음'})",
                    [uc.id, ev]))
                break
            cur = nxt
    if usecases and not tagged:
        out.append(Finding(
            "유스케이스 재생", "",
            "어떤 유스케이스에도 이벤트 표시가 없어 재생할 수 없다 — "
            "각 단계에 `event:` 를 적어야 기계가 검사할 수 있다",
            fixable_by_fsm=False))          # 유스케이스 쪽 문제라 FSM 수리로는 못 고친다
    return out


# --------------------------------------------------------------------------
# 검사 3·4: 커버리지와 완결성
# --------------------------------------------------------------------------

def check_coverage(fsm: FSM, usecases: list[UseCase]) -> list[Finding]:
    """어떤 유스케이스에도 안 걸리는 전이 / 전이가 없는 이벤트."""
    out = []
    used_uc = {t.usecase for t in fsm.transitions if t.usecase}
    for uc in usecases:
        if uc.id not in used_uc:
            out.append(Finding("커버리지", "",
                               f"{uc.id} '{uc.title}' 를 참조하는 전이가 하나도 없다", [uc.id]))
    declared = {e.id for e in fsm.events}
    used_ev = {t.event for t in fsm.transitions}
    for e in sorted(declared - used_ev):
        out.append(Finding("커버리지", "", f"이벤트 '{e}' 를 쓰는 전이가 없다", [e]))
    return out


# --------------------------------------------------------------------------
# 선언된 보존량
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```\s*보존\s*\n(.*?)```", re.S)


@dataclass
class Conserved:
    """원칙 문서가 선언한 보존 관계. 두 형태가 있다.

    `등식: A = B + C` -- **닫힌 계**. 셋 다 같은 전이에서 함께 움직여야 한다.
    읽은 줄 수처럼 바깥에서 새로 생기지 않는 양이 여기 해당한다.

    `감소대응: A -> B, C, payout` -- **열린 계**. A 는 바깥에서 들어올 수 있고
    (고객이 코인을 넣는다) 그때는 짝이 필요 없다. 다만 **줄어들 때는** 반드시
    B·C 로 옮겨가거나 payout 처럼 계를 떠나야 한다.
    돈이 이 형태다. 처음에 등식으로 쓰려다 거짓 양성 13건을 얻고 알았다.
    """
    kind: str                  # "등식" | "감소대응"
    lhs: str
    rhs: list[str]
    raw: str

    @property
    def names(self) -> list[str]:
        return [self.lhs, *self.rhs]


def load_conserved(principles_md: str) -> list[Conserved]:
    """원칙 문서의 ```보존 블록을 읽는다.

    **왜 문서에 두는가.** 처음엔 `credit` · `sales` · `stock` 을 검사 코드에 손으로
    박아넣었다. 그래서 도메인이 로그 집계로 바뀌자 검사가 통째로 눈을 감았다 --
    원칙 문서에 보존 등식을 셋이나 적어두었는데도 지적 0건이었다.
    **무엇이 보존량인지는 도메인이 아는 것이지 검사기가 아는 것이 아니다.**

        ```보존
        등식: read_lines = event_count + dropped
        ```
    """
    out = []
    for block in _FENCE.findall(principles_md or ""):
        for line in block.splitlines():
            line = line.strip()
            for kind, sep in (("등식", "="), ("감소대응", "->")):
                if not line.startswith(kind + ":"):
                    continue
                body = line.split(":", 1)[1].strip()
                if sep not in body:
                    continue
                lhs, rhs = body.split(sep, 1)
                terms = [t.strip().rstrip("()") for t in re.split(r"[+＋,·]", rhs)
                         if t.strip()]
                if lhs.strip() and terms:
                    out.append(Conserved(kind, lhs.strip(), terms, body))
    return out


def _states_holding(fsm: FSM, name: str) -> set[str]:
    """그 양이 0보다 클 수 있는 상태. `_states_that_can_hold_credit` 의 일반형.

    이게 없으면 **이미 0인 자리의 방어적 초기화** 를 결함으로 신고한다.
    v8 의 T-075 · T-076 이 그랬다 -- 관리 모드를 나오며 `credit := 0` 하는데,
    거기 도달할 때 잔액은 이미 0이다. 잃을 것이 없는 자리는 지적하지 않는다.
    """
    inc = rf"\b{re.escape(name)}\b\s*(?:\[[^\]]*\]|\([^)]*\))?\s*\+="
    zero = rf"\b{re.escape(name)}\b\s*(?:\[[^\]]*\]|\([^)]*\))?\s*{_ASSIGN}\s*0"
    holds = {t.dst for t in fsm.transitions if re.search(inc, t.action or "")}
    changed = True
    while changed:
        changed = False
        for t in fsm.transitions:
            if t.src in holds and t.dst not in holds \
                    and not re.search(zero, t.action or "") \
                    and not _guard_pins_zero(t.guard or "", name):
                holds.add(t.dst)
                changed = True
    return holds


def _guard_pins_zero(guard: str, name: str) -> bool:
    """guard 가 그 양을 0 으로 못박는 갈래인가. 산술식도 읽는다."""
    for conj in re.split(r"&&|\|\||\band\b", guard or ""):
        if re.search(rf"\b{re.escape(name)}\b", conj) and re.search(r"==\s*0\b", conj):
            return True
    return False


def _moves(action: str, name: str) -> bool:
    """이 전이가 그 양을 건드리는가. 배열 첨자와 점 표기까지 같이 본다."""
    pat = rf"\b{re.escape(name)}\b\s*(?:\[[^\]]*\]|\.\w+)?\s*(?:\+=|-=|{_ASSIGN})"
    return bool(re.search(pat, action or ""))


def check_conservation(model, ids: list[Conserved]) -> list[Finding]:
    """선언된 등식을 **한 전이 안에서** 깨는 곳을 찾는다.

    등식이 두 전이에 걸쳐서만 맞으면, 그 사이 상태에서는 장부가 깨져 있다.
    거기서 정전이나 실패가 나면 차이가 그대로 사라진다 -- 자판기의 -700 이 그랬다.
    """
    # FSM 이든 결정표든 원자의 내용(guard/action)만 본다. 좌표(src/dst)는
    # 도달성 면제에만 쓰이므로, 좌표가 없는 형식에서는 면제 없이 전부 검사한다.
    atoms = model.atoms
    has_coords = bool(atoms) and hasattr(atoms[0], "src")
    out = []
    holding: dict[str, set] = {}
    actions = " ; ".join(t.action or "" for t in atoms)
    for c in ids:
        # **선언이 틀린 것과 설계가 틀린 것을 구분한다.** 이름이 어느 전이에도
        # 없으면 그건 모든 전이가 등식을 깬 게 아니라 내가 이름을 잘못 적은 것이다.
        # 이 구분이 없으면 오타 하나가 지적 수십 건으로 둔갑한다 -- 실제로 그랬다.
        missing = [n for n in c.names
                   if not re.search(rf"\b{re.escape(n)}\b", actions)]
        if missing:
            out.append(Finding(
                "선언 오류", "",
                f"원칙에 적은 `{c.raw}` 에서 {' · '.join(missing)} 은(는) "
                f"어떤 전이에도 나오지 않는다 — 이름이 FSM 과 다르거나 "
                f"모델이 그 양을 아예 안 만들었다", [], fixable_by_fsm=False))
            continue

        for t in atoms:
            a = t.action or ""
            where = getattr(t, "dst", "") or t.id
            right = [r for r in c.rhs if _moves(a, r)]
            if c.kind == "등식":
                left = _moves(a, c.lhs)
                if left and not right:
                    out.append(Finding(
                        "보존 등식", "",
                        f"{t.id}: '{c.lhs}' 이 움직이는데 대응하는 "
                        f"{' · '.join(c.rhs)} 변화가 이 전이에 없다 — "
                        f"`{c.raw}` 이 '{where}' 에서 깨진다", [t.id]))
                elif right and not left:
                    out.append(Finding(
                        "보존 등식", "",
                        f"{t.id}: {' · '.join(right)} 이 움직이는데 '{c.lhs}' 는 "
                        f"그대로다 — `{c.raw}` 이 '{t.dst}' 에서 깨진다", [t.id]))
            else:                                   # 감소대응
                if has_coords and t.src not in holding.setdefault(
                        c.lhs, _states_holding(model, c.lhs)):
                    continue                        # 잃을 것이 없는 자리
                if _guard_pins_zero(t.guard or "", c.lhs):
                    continue                        # 설계자가 이미 막아 둔 갈래
                drops = bool(re.search(rf"\b{re.escape(c.lhs)}\b\s*-=", a)) or \
                    bool(re.search(rf"\b{re.escape(c.lhs)}\b\s*{_ASSIGN}\s*0", a))
                # 계를 떠나는 호출도 대응으로 친다: payout(...), refund(...)
                left_system = any(re.search(rf"\b{re.escape(r)}\s*\(", a) for r in c.rhs)
                if drops and not right and not left_system:
                    out.append(Finding(
                        "보존 등식", "",
                        f"{t.id}: '{c.lhs}' 이 줄어드는데 "
                        f"{' · '.join(c.rhs)} 어디로도 가지 않는다 — "
                        f"그만큼이 '{where}' 에서 사라진다", [t.id]))
    return out


# --------------------------------------------------------------------------
# 전체 실행
# --------------------------------------------------------------------------

CHECKS = [
    ("원칙 P1 · 돈 회수 가능성", lambda f, u: check_refund_reachable(f)),
    ("원칙 P3 · 장부 정정 가능성", lambda f, u: check_ledger_adjustable(f)),
    ("원칙 P4 · 고장 시 판매 중단", lambda f, u: check_fault_stops_selling(f)),
    ("원칙 P5 · 전이마다 저장", lambda f, u: check_every_transition_persists(f)),
    ("원칙 P2 · 장부 수지", lambda f, u: check_ledger_balance(f) + check_stock_balance(f)),
    ("유스케이스 재생", replay_usecases),
    ("커버리지", check_coverage),
]


def run_all(fsm: FSM, usecases: list[UseCase], principles: str = "") -> list[Finding]:
    """`principles` 는 원칙 문서 원문. 거기 선언된 보존 등식이 검사 대상에 더해진다.

    빠뜨려도 나머지 검사는 그대로 돈다 -- 다만 그 프로젝트의 보존량은 아무도 안 본다.
    """
    out = []
    for _name, fn in CHECKS:
        try:
            out.extend(fn(fsm, usecases))
        except Exception as exc:            # 검사 하나가 죽어도 나머지는 돈다
            out.append(Finding("검사 오류", "", f"{_name} 실행 실패: {exc}"))
    try:
        out.extend(check_conservation(fsm, load_conserved(principles)))
    except Exception as exc:
        out.append(Finding("검사 오류", "", f"보존 등식 실행 실패: {exc}"))
    return out
