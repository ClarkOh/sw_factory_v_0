"""6~8단계: 테스트 생성 → 구현 → 테스트 실행 → 디버그.

무결성 규칙 하나가 이 구간 전체를 지탱한다:
    **구현하는 워커는 테스트를 고칠 수 없다.**
프롬프트로 부탁하는 것만으로는 부족하므로 해시 스냅샷으로 강제한다.
이것이 없으면 워커가 테스트를 통과하도록 테스트를 고쳐버리고, DoD는 자기인증이 되어 무의미해진다.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from factory.context import Ctx, prompt
from factory.models import Ticket
from factory.worker.base import WorkerTask

IMPL_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(python *)", "Bash(pytest *)", "Bash(ls*)", "Bash(cat*)", "Bash(mkdir*)",
]
SPEC_CONFLICT = "SPEC_CONFLICT:"


@dataclass
class TestOutcome:
    passed: bool
    output: str
    collected: int = 0
    failed_nodes: list[str] | None = None


# --------------------------------------------------------------------------
# 테스트 보호
# --------------------------------------------------------------------------

def _test_files(ctx: Ctx) -> list[Path]:
    d = ctx.cfg.repo / "tests"
    return sorted(d.rglob("test_*.py")) if d.exists() else []


def snapshot_tests(ctx: Ctx) -> dict[str, str]:
    return {
        str(p.relative_to(ctx.cfg.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in _test_files(ctx)
    }


def restore_if_tampered(ctx: Ctx, before: dict[str, str], saved: dict[str, bytes]) -> list[str]:
    """구현 워커가 테스트를 건드렸으면 되돌리고 위반 목록을 반환한다."""
    violations = []
    for rel, digest in before.items():
        p = ctx.cfg.repo / rel
        now = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        if now != digest:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(saved[rel])
            violations.append(rel)
    return violations


# --------------------------------------------------------------------------
# 6단계: 명세로부터 테스트 생성 (구현 이전, 구현과 격리)
# --------------------------------------------------------------------------

def spec_tests(ctx: Ctx, force: bool = False) -> Path:
    """FSM만 보고 테스트를 작성한다. 구현 코드는 워커에게 주지 않는다.

    지금은 전부 실패하는 게 정상이다 — 그게 red 상태의 정의다.
    """
    test_path = ctx.cfg.repo / "tests" / "test_fsm.py"
    if test_path.exists() and not force:
        ctx.log(f"테스트 이미 존재 → 재생성 생략 ({test_path.name})")
        return test_path

    iface = ctx.cfg.repo / "INTERFACE.md"
    res = ctx.worker.run(WorkerTask(
        prompt=prompt(
            "spec_test",
            fsm_yaml=ctx.store.fsm_yaml.read_text(encoding="utf-8"),
            test_path="tests/test_fsm.py",
            interface=iface.read_text(encoding="utf-8") if iface.exists()
                      else "(INTERFACE.md 없음 — FSM의 상태/이벤트 이름을 그대로 쓰는 인터페이스를 가정하고,\n"
                           "  사용한 공개 인터페이스를 테스트 파일 상단 docstring에 명시하세요)",
        ),
        cwd=ctx.cfg.repo,
        allowed_tools=["Read", "Write", "Glob", "Grep"],
        timeout=ctx.cfg.limits.gen_timeout_s,
        label="spec_test",
    ))
    if not res.ok:
        from factory.worker.base import WorkerUnavailable
        if res.infra_failure():
            raise WorkerUnavailable(f"테스트 생성 실패: {res.error}")
        raise RuntimeError(f"테스트 생성 실패: {res.error}")
    if not test_path.exists():
        raise RuntimeError(f"워커가 테스트 파일을 만들지 않았습니다: {test_path}")

    # 생성된 테스트가 **지금의** FSM과 1:1인지 확인한다. 이전 세대의 테스트가 저장소에
    # 남아 있으면 워커가 그걸 베끼고, 그 결과 존재하지 않는 전이의 테스트가 DoD 가 되어
    # 티켓 수십 개가 SPEC_CONFLICT 로 죽는다 (v4 에서 21건). 여기서 막는다.
    expected = {t.test_name for t in ctx.store.load_fsm().transitions}
    actual = set(_test_names(test_path))
    if expected != actual:
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        raise RuntimeError(
            "생성된 테스트가 현재 FSM과 맞지 않습니다 -- 이전 세대 산출물을 베꼈을 가능성. "
            f"누락 {len(missing)}: {missing[:5]} / 잉여 {len(extra)}: {extra[:5]}")

    out = run_tests(ctx)
    ctx.log(f"테스트 생성 완료: {out.collected}개 수집, FSM 전이 {len(expected)}개와 1:1 (현재 red — 정상)")
    return test_path


def _test_names(path: Path) -> list[str]:
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_t_")]


# --------------------------------------------------------------------------
# 7단계: 구현
# --------------------------------------------------------------------------

def implement(ctx: Ctx, ticket: Ticket, subtasks: list[Ticket]) -> tuple[bool, str]:
    """티켓을 구현한다. 반환: (성공여부, 메모)."""
    before = snapshot_tests(ctx)
    saved = {rel: (ctx.cfg.repo / rel).read_bytes() for rel in before}

    fsm = ctx.store.load_fsm()
    tr = next((t for t in fsm.transitions if t.id == ticket.fsm_ref), None)
    body = ticket.description
    if subtasks:
        body += "\n\n**서브태스크**\n" + "\n".join(
            f"{i}. {s.title} — {s.description}" for i, s in enumerate(subtasks, 1)
        )

    conv = ctx.cfg.repo / "CONVENTIONS.md"
    res = ctx.worker.run(WorkerTask(
        prompt=prompt(
            "implement",
            key=ticket.key, title=ticket.title, description=body,
            tid=tr.id if tr else ticket.fsm_ref,
            src=tr.src if tr else "?", dst=tr.dst if tr else "?",
            event=tr.event if tr else "?",
            guard=(tr.guard or "(없음)") if tr else "?",
            action=(tr.action or "(없음)") if tr else "?",
            dod_tests="\n".join(ticket.dod_tests) or "(지정 없음)",
            conventions=conv.read_text(encoding="utf-8") if conv.exists() else "(CONVENTIONS.md 없음)",
        ),
        cwd=ctx.cfg.repo,
        allowed_tools=IMPL_TOOLS,
        timeout=ctx.cfg.limits.worker_timeout_s,
        label=f"implement:{ticket.key}",
    ))

    violations = restore_if_tampered(ctx, before, saved)
    if violations:
        ctx.log(f"  ⚠ 테스트 무단 수정 감지 → 되돌림: {', '.join(violations)}")
        ctx.tracker.comment(ticket.key, f"위반: 구현 워커가 테스트를 수정하려 함 → 되돌림 ({violations})")

    if not res.ok:
        return False, res.error
    if SPEC_CONFLICT in res.text:
        note = res.text.split(SPEC_CONFLICT, 1)[1].strip().splitlines()[0]
        return False, f"{SPEC_CONFLICT} {note}"
    return True, res.text[-1500:]


# --------------------------------------------------------------------------
# 8단계: 테스트 실행 / 디버그
# --------------------------------------------------------------------------

def run_tests(ctx: Ctx, nodes: list[str] | None = None) -> TestOutcome:
    """pytest 실행. nodes가 있으면 그 테스트만."""
    cmd = ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    cmd += nodes if nodes else ["tests"]
    proc = subprocess.run(
        cmd, cwd=str(ctx.cfg.repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # "FAILED tests/x.py::test_y - msg" 에서 node id 는 두 번째 토큰이다. 첫 토큰은 그냥 "FAILED".
    failed = [
        ln.split()[1] for ln in out.splitlines()
        if (ln.startswith("FAILED ") or ln.startswith("ERROR ")) and len(ln.split()) > 1
    ]
    collected = out.count("::") if nodes else _count_collected(out)
    return TestOutcome(
        passed=proc.returncode == 0, output=out[-6000:],
        collected=collected, failed_nodes=failed,
    )


def _count_collected(out: str) -> int:
    """pytest 요약에서 테스트 개수를 센다.

    첫 숫자만 집으면 수집 오류 시의 "1 error" 를 개수로 착각해 로그가 거짓말을 한다.
    """
    import re
    m = re.search(r"(\d+) tests? collected", out)
    if m:
        return int(m.group(1))
    # 마지막 요약 줄만 본다 -- 본문에 흩어진 "1 error" 를 중복 집계하지 않도록.
    for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
        counts = re.findall(r"(\d+) (?:passed|failed|error|skipped)", line)
        if counts:
            return sum(int(n) for n in counts)
    return 0


def debug(ctx: Ctx, ticket: Ticket, outcome: TestOutcome, attempt: int, history: list[str]) -> tuple[bool, str]:
    """실패 출력을 주고 고치게 한다. 테스트 보호는 구현 때와 동일하게 적용."""
    before = snapshot_tests(ctx)
    saved = {rel: (ctx.cfg.repo / rel).read_bytes() for rel in before}

    res = ctx.worker.run(WorkerTask(
        prompt=prompt(
            "debug",
            attempt=attempt, max_attempts=ctx.cfg.limits.implement_attempts,
            dod_tests="\n".join(ticket.dod_tests),
            output=outcome.output,
            history="\n---\n".join(history[-2:]) or "(첫 시도)",
        ),
        cwd=ctx.cfg.repo,
        allowed_tools=IMPL_TOOLS,
        timeout=ctx.cfg.limits.worker_timeout_s,
        label=f"debug:{ticket.key}:{attempt}",
    ))

    violations = restore_if_tampered(ctx, before, saved)
    if violations:
        ctx.log(f"  ⚠ 디버그 중 테스트 무단 수정 → 되돌림: {', '.join(violations)}")

    if not res.ok:
        return False, res.error
    if SPEC_CONFLICT in res.text:
        return False, f"{SPEC_CONFLICT} {res.text.split(SPEC_CONFLICT, 1)[1].strip().splitlines()[0]}"
    return True, res.text[-1500:]


# --------------------------------------------------------------------------
# E2E 시나리오 테스트 생성
#
# 단위 테스트는 FSM에서, E2E는 유스케이스에서 나온다. 출처가 달라야 서로를 검증한다.
# 둘 다 FSM에서 뽑으면 같은 오해를 두 번 하고 두 번 통과할 뿐이다.
# --------------------------------------------------------------------------

def spec_e2e(ctx: Ctx, force: bool = False) -> Path:
    """유스케이스로부터 `tests/e2e/test_scenarios.py` 를 생성한다."""
    e2e_path = ctx.cfg.repo / "tests" / "e2e" / "test_scenarios.py"
    if e2e_path.exists() and not force:
        ctx.log(f"E2E 시나리오 이미 존재 -> 재생성 생략 ({e2e_path.name})")
        return e2e_path

    # 단위 테스트와 구현은 이 단계에서 바뀌면 안 된다.
    before = snapshot_tests(ctx)
    saved = {rel: (ctx.cfg.repo / rel).read_bytes() for rel in before}

    e2e_path.parent.mkdir(parents=True, exist_ok=True)
    res = ctx.worker.run(WorkerTask(
        prompt=prompt(
            "e2e_spec",
            e2e_path="tests/e2e/test_scenarios.py",
            usecases_yaml=ctx.store.usecases_yaml.read_text(encoding="utf-8"),
            fsm_yaml=ctx.store.fsm_yaml.read_text(encoding="utf-8"),
        ),
        cwd=ctx.cfg.repo,
        allowed_tools=["Read", "Write", "Glob", "Grep", "Bash(python *)"],
        timeout=ctx.cfg.limits.gen_timeout_s,
        label="e2e_spec",
    ))
    if not res.ok:
        from factory.worker.base import WorkerUnavailable
        if res.infra_failure():
            raise WorkerUnavailable(f"E2E 시나리오 생성 실패: {res.error}")
        raise RuntimeError(f"E2E 시나리오 생성 실패: {res.error}")

    # 기존 단위 테스트를 건드렸으면 되돌린다 (E2E를 통과시키려고 단위 테스트를
    # 느슨하게 만드는 것이 가장 흔한 부정행위다).
    violations = [rel for rel in before
                  if rel != "tests/e2e/test_scenarios.py"
                  and _digest(ctx.cfg.repo / rel) != before[rel]]
    for rel in violations:
        (ctx.cfg.repo / rel).write_bytes(saved[rel])
    if violations:
        ctx.log(f"  [WARN] 단위 테스트 수정 감지 -> 되돌림: {', '.join(violations)}")

    if not e2e_path.exists():
        raise RuntimeError(f"워커가 E2E 시나리오를 만들지 않았습니다: {e2e_path}")

    n = _count_collected(run_tests(ctx, nodes=["tests/e2e"]).output)
    ctx.log(f"E2E 시나리오 생성: {n}개")
    return e2e_path


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
