"""검토 지적을 닫는 단계.

지적마다 **고칠 수 있는 역할이 다릅니다.** 아무나 아무거나 고치게 두면
이 파이프라인을 지탱하는 두 개의 무결성 규칙이 동시에 무너집니다:

  - 구현자는 테스트를 고칠 수 없다 (고치면 DoD가 자기인증이 된다)
  - 기계는 요구사항을 지어낼 수 없다 (지어내면 검증의 근거가 사라진다)

그래서 분류별로 라우팅합니다.
"""
from __future__ import annotations

from factory.context import Ctx, prompt
from factory.models import Status, Ticket
from factory.stages import build
from factory.worker.base import WorkerTask

# 테스트를 고쳐야 하는 지적 -- 구현 코드는 건드리지 못하게 한다.
TEST_SIDE = {"vacuous-test", "untraced-req"}

# 구현을 고쳐야 하는 지적 -- 테스트는 건드리지 못하게 한다.
CODE_SIDE = {"incomplete-guard"}

# 사람만 결정할 수 있는 것. 기계가 손대면 요구사항을 지어내는 셈이 된다.
HUMAN_ONLY = {"missing-rule", "guessed-spec"}

TEST_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(python *)", "Bash(pytest *)"]


def route(ticket: Ticket) -> str:
    """티켓 라벨에서 분류를 읽어 담당 역할을 정한다."""
    for label in ticket.labels:
        if label in TEST_SIDE:
            return "test"
        if label in CODE_SIDE:
            return "code"
        if label in HUMAN_ONLY:
            return "human"
    return "human"          # 모르면 사람에게. 기계가 넘겨짚지 않는다.


def open_findings(ctx: Ctx) -> list[Ticket]:
    """아직 닫히지 않은 검토 지적."""
    return [t for t in ctx.tracker.search(label="critic")
            if t.status not in (Status.DONE.value, Status.BLOCKED.value)]


def actionable(ctx: Ctx) -> list[Ticket]:
    """기계가 시도해 볼 수 있는 지적만."""
    return [t for t in open_findings(ctx) if route(t) in ("test", "code")]


def park_human_findings(ctx: Ctx) -> list[Ticket]:
    """사람 판단이 필요한 지적을 BLOCKED로 세워 둔다.

    닫지 않고 남겨 두면 루프가 영원히 안 끝난다. 버리는 게 아니라
    '기계 차례가 아님'을 명시하는 것이다 -- 보드에서 needs-human 으로 보인다.
    """
    parked = []
    for t in open_findings(ctx):
        if route(t) == "human":
            ctx.tracker.transition(
                t.key, Status.BLOCKED.value,
                "요구사항·명세 결정이 필요한 지적 -- 기계가 임의로 정할 수 없음")
            parked.append(t)
    return parked


def fix(ctx: Ctx, ticket: Ticket) -> bool:
    """지적 하나를 고친다. 반환: 전체 테스트가 통과하는가."""
    role = route(ticket)
    if role == "human":
        return False

    try:
        return _fix_attempts(ctx, ticket, role)
    except BaseException:
        _discard_worktree(ctx)
        raise


def _fix_attempts(ctx: Ctx, ticket: Ticket, role: str) -> bool:
    waivers_before = _count_waivers(ctx)
    for attempt in range(1, ctx.cfg.limits.implement_attempts + 1):
        ticket.attempts = attempt
        ctx.tracker.update(ticket)

        if role == "test":
            ok, note = _fix_tests(ctx, ticket, attempt)
        else:
            ok, note = _fix_code(ctx, ticket, attempt)

        if not ok:
            # 워커가 실행조차 안 됐으면 이 티켓의 잘못이 아니다. 다음 티켓도 똑같이 죽는다.
            # (WORK 루프엔 이 처리가 있었는데 여기 없어서, 한도 초과 뒤 티켓 5개가
            #  각각 3회씩 헛되이 재시도하고 잘못 BLOCKED 됐다.)
            from factory.worker.base import WorkerResult, WorkerUnavailable
            if WorkerResult(ok=False, error=note).is_outage():
                raise WorkerUnavailable(f"remediate:{ticket.key}: {note[:80]}")
            ctx.log(f"    시도 {attempt} 실패: {note[:160]}")
            continue

        outcome = build.run_tests(ctx)
        if outcome.passed:
            # 통과했다고 해소된 게 아니다. xfail/skip 을 붙여 초록을 만든 것은
            # 결함을 고친 게 아니라 결함에 이름표를 단 것이다.
            # (실제로 겪음: "fsync 가 구현도 검증도 안 됐다"는 지적이 fsync 를 넣는 대신
            #  xfail 테스트를 추가하는 것으로 닫혔다. 다음 사이클이 같은 결함을 다시 잡았다.)
            added = _count_waivers(ctx) - waivers_before
            if added > 0:
                ctx.log(f"    [WARN] xfail/skip 이 {added}개 늘었다 -- 결함을 덮은 것으로 보아 해소로 치지 않는다")
                ctx.tracker.comment(
                    ticket.key,
                    f"자동 수정이 xfail/skip {added}개를 추가해 통과시켰음. 실제 해소가 아니므로 사람 검토 필요.")
                return False
            ctx.log(f"    [OK] 전체 통과 (시도 {attempt})")
            return True
        ctx.log(f"    [FAIL] 테스트 실패 (시도 {attempt}/{ctx.cfg.limits.implement_attempts})")
        ticket.last_error = (outcome.failed_nodes or [""])[0]
        ctx.tracker.update(ticket)
    # 못 고쳤으면 작업트리를 마지막 커밋으로 되돌린다. 반쯤 고친 상태를 남기면
    # 다음 티켓이 깨진 테스트 위에서 시작하고, 워커가 중단되면 그대로 굳는다 (v4 에서 겪음).
    _discard_worktree(ctx)
    return False


def _count_waivers(ctx: Ctx) -> int:
    """테스트를 면제하는 표시(xfail/skip)의 개수.

    이게 늘었다는 것은 검증 범위가 좁아졌다는 뜻이다. 수정의 결과로는 절대 늘면 안 된다.
    """
    import re
    total = 0
    d = ctx.cfg.repo / "tests"
    if d.exists():
        for p in d.rglob("test_*.py"):
            total += len(re.findall(r"@pytest\.mark\.(xfail|skip)|pytest\.(skip|xfail)\(",
                                    p.read_text(encoding="utf-8")))
    return total


def _discard_worktree(ctx: Ctx) -> None:
    """추적 중인 파일만 되돌린다.

    `git clean` 은 쓰지 않는다 -- 커밋되지 않은 생성물까지 지운다.
    실제로 E2E_SPEC 이 만든 시나리오 50개가 첫 수정 실패에서 통째로 날아갔다.
    (근본 대책은 생성 직후 커밋하는 것이고, 이건 두 번째 방어선이다.)
    """
    from factory.stages.deliver import git
    git(ctx, "checkout", "--", ".")


def _fix_tests(ctx: Ctx, ticket: Ticket, attempt: int) -> tuple[bool, str]:
    """테스트를 고친다. 구현 코드는 스냅샷으로 보호한다."""
    before = {p: p.read_bytes() for p in (ctx.cfg.repo / "app").rglob("*.py")}
    res = ctx.worker.run(WorkerTask(
        prompt=prompt("remediate_test", finding=ticket.description,
                      title=ticket.title, attempt=attempt),
        cwd=ctx.cfg.repo, allowed_tools=TEST_TOOLS,
        timeout=ctx.cfg.limits.worker_timeout_s,
        label=f"remediate_test:{ticket.key}:{attempt}",
    ))
    changed = [p for p, body in before.items() if not p.exists() or p.read_bytes() != body]
    for p in changed:
        p.write_bytes(before[p])
    if changed:
        names = ", ".join(p.name for p in changed)
        ctx.log(f"    [WARN] 구현 코드 수정 감지 -> 되돌림: {names}")
        ctx.tracker.comment(ticket.key, f"위반: 테스트 수정 역할이 구현을 고치려 함 ({names})")
    return (res.ok, res.text[-1200:] if res.ok else res.error)


def _fix_code(ctx: Ctx, ticket: Ticket, attempt: int) -> tuple[bool, str]:
    """구현을 고친다. 테스트는 기존 보호 장치를 그대로 쓴다."""
    before = build.snapshot_tests(ctx)
    saved = {rel: (ctx.cfg.repo / rel).read_bytes() for rel in before}
    res = ctx.worker.run(WorkerTask(
        prompt=prompt("remediate_code", finding=ticket.description,
                      title=ticket.title, attempt=attempt),
        cwd=ctx.cfg.repo, allowed_tools=build.IMPL_TOOLS,
        timeout=ctx.cfg.limits.worker_timeout_s,
        label=f"remediate_code:{ticket.key}:{attempt}",
    ))
    violations = build.restore_if_tampered(ctx, before, saved)
    if violations:
        ctx.log(f"    [WARN] 테스트 무단 수정 -> 되돌림: {', '.join(violations)}")
    if res.ok and build.SPEC_CONFLICT in res.text:
        return False, res.text.split(build.SPEC_CONFLICT, 1)[1].strip().splitlines()[0]
    return (res.ok, res.text[-1200:] if res.ok else res.error)
