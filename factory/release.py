"""릴리스 판정 — 루프를 끝내는 자리.

**왜 필요한가.** 검토(Critic)는 언제나 무언가를 찾는다. 명세는 무한히 정밀해질 수 있고,
좋은 검토자는 항상 지적을 낸다. 실제로 v1~v6 매 버전 8~37건이 나왔고 0으로 수렴한 적이 없다.
**기계에 게이트를 걸면 영원히 출시하지 못한다.**

그래서 판정 기준을 뒤집는다:

    요구사항 문서는 유한하고 사람이 쓴다. 검토 지적은 무한하고 기계가 낸다.
    그러므로 **판정은 문서를 기준으로 한다.**

이 관점에서 v6 은 "미완성"이 아니었다. **요구사항-v6 을 정확히 구현한 완성품** 이고,
거기에 v7 요구사항 후보 12건이 딸린 상태였다.

실제 제품이 출시되는 방식이 이렇다 -- 버그가 0이라서 나가는 게 아니라,
**분류되지 않은 항목이 0이라서** 나간다.

**판정은 순수 코드다.** 모델이 재량으로 판정하면 자기 게이트를 자기가 여는 셈이 된다.
심각도의 정의도 `principles.md` 에 고정돼 있어야 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.models import Status, TicketType

SEVERITY_IN_TITLE = re.compile(r"\[검토/(high|medium|low)\]")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"[{'x' if self.passed else ' '}] {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class Verdict:
    checks: list[Check] = field(default_factory=list)
    known_issues: list[dict] = field(default_factory=list)

    @property
    def released(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def severity_of(ticket) -> str:
    m = SEVERITY_IN_TITLE.search(ticket.title or "")
    return m.group(1) if m else "medium"


def judge(ctx) -> Verdict:
    """릴리스 가능한가. 여섯 항목 전부 통과해야 한다."""
    from factory.stages import build

    v = Verdict()
    tracker, store = ctx.tracker, ctx.store

    # 1. 범위 완료 -- 모든 스토리가 끝났는가
    stories = tracker.search(type=TicketType.STORY.value)
    unfinished = [t for t in stories if t.status != Status.DONE.value]
    v.checks.append(Check(
        "모든 스토리 완료", not unfinished,
        f"{len(stories) - len(unfinished)}/{len(stories)}"
        + (f" — 미완: {', '.join(t.key for t in unfinished[:5])}" if unfinished else "")))

    # 2. 요구사항 추적 -- 검증까지 도달하지 못한 요구사항이 있는가
    untraced = _untraced_requirements(ctx)
    reqs = store.load_requirements()
    v.checks.append(Check(
        "모든 요구사항이 테스트에 도달", not untraced,
        f"{len(reqs) - len(untraced)}/{len(reqs)}"
        + (f" — 미도달: {', '.join(untraced[:6])}" if untraced else "")))

    # 3. 건전성 -- 전체 스위트가 초록인가
    outcome = build.run_tests(ctx)
    v.checks.append(Check(
        "전체 테스트 통과", outcome.passed,
        f"{outcome.collected}개" if outcome.passed else f"실패: {(outcome.failed_nodes or [])[:3]}"))

    # 4. 면제 통제 -- xfail/skip 은 승인된 것만
    waivers = _waivers(ctx)
    approved = _approved_waivers(ctx)
    unapproved = [w for w in waivers if w not in approved]
    v.checks.append(Check(
        "승인되지 않은 xfail/skip 없음", not unapproved,
        f"{len(waivers)}건 중 승인 {len(approved)}"
        + (f" — 미승인: {', '.join(unapproved[:3])}" if unapproved else "")))

    # 5. 심각도 게이트 -- 여기가 루프를 끊는 자리다
    open_findings = [t for t in tracker.search(label="critic")
                     if t.status != Status.DONE.value]
    highs = _dedupe([t for t in open_findings if severity_of(t) == "high"])
    v.checks.append(Check(
        "열린 high 지적 없음", not highs,
        f"{len(highs)}건" + (f" — {', '.join(t.key for t in highs[:5])}" if highs else "")))

    # 6. 조립 확인 -- 진입점이 실제로 도는가
    ok, out = _entrypoint_runs(ctx)
    v.checks.append(Check("진입점 실행 확인", ok, "" if ok else out[:120]))

    # 이월 목록 -- 릴리스 노트가 되고, 다음 버전 요구사항 후보가 된다
    for t in _dedupe(open_findings):
        sev = severity_of(t)
        if sev != "high":
            v.known_issues.append({"key": t.key, "severity": sev,
                                   "title": re.sub(r"^\[검토/\w+\]\s*", "", t.title)})
    v.known_issues.sort(key=lambda d: {"medium": 0, "low": 1}.get(d["severity"], 2))
    return v


# --------------------------------------------------------------------------

def _dedupe(tickets: list) -> list:
    """같은 결함을 다른 문장으로 다시 신고한 것을 합친다.

    검토가 사이클마다 같은 것을 다시 찾으면 7건으로 보이는 게 실은 3건이다.
    참조(refs)에 같은 전이·요구사항이 겹치면 같은 결함으로 본다.
    """
    out, seen = [], []
    for t in tickets:
        refs = set(re.findall(r"\b(?:T-\d+|REQ-\d+|UC-\d+)\b", t.description or ""))
        if refs and any(len(refs & s) >= 2 for s in seen):
            continue
        seen.append(refs)
        out.append(t)
    return out


def _untraced_requirements(ctx) -> list[str]:
    """어떤 통과하는 테스트에도 도달하지 못한 요구사항.

    요구사항 -> 유스케이스 -> 전이 -> DoD 테스트 사슬을 따라간다.
    """
    reqs = {r.id for r in ctx.store.load_requirements()}
    if not reqs:
        return []
    ucs = ctx.store.load_usecases()
    model = ctx.store.load_model()

    uc_of_req = {}
    for u in ucs:
        for r in u.requirements:
            uc_of_req.setdefault(r, []).append(u.id)

    covered_uc = {t.usecase for t in model.atoms if t.usecase}
    done_uc = {t.usecase_ref for t in ctx.tracker.search(type=TicketType.STORY.value)
               if t.status == Status.DONE.value and t.usecase_ref}

    untraced = []
    for r in sorted(reqs):
        owners = uc_of_req.get(r, [])
        if not owners:
            untraced.append(r)                      # 유스케이스까지도 못 감
        elif not (set(owners) & covered_uc & done_uc):
            untraced.append(r)                      # 전이나 완료된 구현까지 못 감
    return untraced


def _waivers(ctx) -> list[str]:
    """테스트를 면제하는 표시의 위치 목록."""
    out = []
    d = ctx.cfg.repo / "tests"
    if not d.exists():
        return out
    pat = re.compile(r"@pytest\.mark\.(xfail|skip)|pytest\.(skip|xfail)\(")
    for p in sorted(d.rglob("test_*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                out.append(f"{p.relative_to(ctx.cfg.repo)}:{i}")
    return out


def _approved_waivers(ctx) -> set[str]:
    """`.factory/approved_waivers.txt` 에 사람이 적어 둔 것. 한 줄에 하나."""
    p = ctx.cfg.workspace / ".factory" / "approved_waivers.txt"
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def _entrypoint_runs(ctx) -> tuple[bool, str]:
    from factory.stages import entry

    if not (ctx.cfg.repo / "app" / "__main__.py").exists():
        return False, "app/__main__.py 가 없다"
    return entry.smoke_run(ctx)


# --------------------------------------------------------------------------

def render(v: Verdict, project: str) -> str:
    """판정서. 그대로 릴리스 노트로 쓸 수 있게."""
    lines = [f"# 릴리스 판정: {project}", ""]
    lines.append("**출시 가능**" if v.released else f"**출시 차단** — {len(v.blockers)}개 항목")
    lines += ["", "## 판정 항목", ""] + [f"- {c}" for c in v.checks]
    if v.known_issues:
        lines += ["", "## 알려진 이슈 (이월)", "",
                  "출시를 막지 않는다. 다음 버전 요구사항 후보다.", ""]
        for d in v.known_issues:
            lines.append(f"- `{d['key']}` **{d['severity']}** {d['title']}")
    return "\n".join(lines) + "\n"
