"""진입점 생성 단계.

FSM은 시스템이 **무엇을 하는지**를 기술하지만 **어떻게 기동하는지**는 기술하지 않는다.
그래서 전이를 전부 구현해도 결과물은 라이브러리일 뿐 실행 가능한 프로그램이 아니다.

이 단계가 그 간극을 메운다. 생성물은 `app/__main__.py` 하나이며,
도메인 코드는 건드리지 않는다 -- 껍데기가 로직을 오염시키면 안 되기 때문이다.

왜 코드 생성이 아니라 워커인가: 생성자에 무엇을 넣어야 하는지(초기 재고, 가격표)는
FSM에 없다. 도메인을 알아야 하므로 기계적 변환으로는 안 된다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from factory.context import Ctx, prompt
from factory.worker.base import WorkerTask

DRIVER_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(python *)"]

# 공장이 생성하는 껍데기 파일. 도메인 코드가 아니므로 보호 대상에서 제외한다.
SHELL_FILES = ("__main__.py", "_fsm_spec.py")


def write_model_spec(ctx: Ctx) -> Path:
    """중간 형식에 맞는 명세 표. FSM 이면 전이표, 결정표면 규칙표."""
    from factory.models import RuleTable
    if isinstance(ctx.store.load_model(), RuleTable):
        return write_rules_spec(ctx)
    return write_fsm_spec(ctx)


def write_rules_spec(ctx: Ctx) -> Path:
    """`app/_rules_spec.py`. 전이표와 같은 이유로 기계 생성한다 --
    표를 모델에게 베껴 쓰게 하면 언젠가 틀리고, 틀린 표는 진단을 거짓말로 만든다."""
    table = ctx.store.load_rules()
    by_ref = {t.fsm_ref: t for t in ctx.tracker.search() if t.fsm_ref}
    rows = []
    for r in table.rules:
        tk = by_ref.get(r.id)
        rows.append("    {" + f"'id': {r.id!r}, 'when': {r.when!r}, 'then': {r.then!r}, "
                    f"'ticket': {(tk.key if tk else None)!r}, "
                    f"'status': {(tk.status if tk else 'unknown')!r}" + "},")
    body = f'"""결정표 명세 -- rules.yaml 에서 자동 생성. 직접 수정하지 마세요."""' + chr(10)
    body += f"PROJECT = {table.project!r}" + chr(10) + "RULES = [" + chr(10)
    body += chr(10).join(rows) + chr(10) + "]" + chr(10)
    out = ctx.cfg.repo / "app" / "_rules_spec.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return out


def write_fsm_spec(ctx: Ctx) -> Path:
    """`app/_fsm_spec.py` 를 fsm.yaml 에서 기계적으로 생성한다.

    왜 워커에게 안 맡기는가: 전이 33개짜리 표를 모델에게 베껴 쓰게 하면 언젠가 틀린다.
    그리고 이 표가 틀리면 진단이 거짓말을 한다 -- 진단은 정확하지 않으면 없느니만 못하다.
    fsm.yaml 에 이미 구조화된 데이터가 있으므로 그대로 옮기면 된다.
    """
    fsm = ctx.store.load_fsm()

    # 전이별 티켓 상태를 같이 심는다. 이게 있어야 "아직 안 지었다"와
    # "지었는데 guard가 거짓이다"를 구별할 수 있다 -- 사용자 입장에선 완전히 다른 상황이다.
    by_fsm = {t.fsm_ref: t for t in ctx.tracker.search() if t.fsm_ref}

    rows = []
    for t in fsm.transitions:
        tk = by_fsm.get(t.id)
        rows.append(
            "    {"
            f"'id': {t.id!r}, 'src': {t.src!r}, 'event': {t.event!r}, 'dst': {t.dst!r}, "
            f"'guard': {t.guard!r}, "
            f"'ticket': {(tk.key if tk else None)!r}, 'status': {(tk.status if tk else 'unknown')!r}"
            "},"
        )
    body = f'''"""FSM 명세 표 -- fsm.yaml 에서 자동 생성. 직접 수정하지 마세요.

드라이버가 "이 이벤트가 무시된 이유"를 정확히 말하기 위해 쓴다:
명세에 없는 조합인가, 명세에는 있는데 구현이 아직 없는가.
"""

PROJECT = {fsm.project!r}
INITIAL = {fsm.initial!r}

TRANSITIONS = [
{chr(10).join(rows)}
]

EVENTS = {{
{chr(10).join(f"    {e.id!r}: {e.payload!r}," for e in fsm.events)}
}}


def expected(src, event):
    """명세가 이 (상태, 이벤트)에 대해 정의한 전이들. 없으면 빈 리스트."""
    return [t for t in TRANSITIONS if t["src"] == src and t["event"] == event]


def diagnose(src, event, moved):
    """이벤트가 아무 일도 일어나지 않았을 때 그 이유를 한 줄로 설명한다.

    세 가지를 구별한다. 뭉뚱그리면 진단이 쓸모없어진다:
      1) 명세에도 없다        -> 사용자가 잘못된 이벤트를 보냈다
      2) 명세에 있고 미완료    -> 공장이 아직 안 지었다 (티켓 번호를 알려준다)
      3) 명세에 있고 완료됨    -> 구현은 있고 guard 조건이 거짓이다
    """
    if moved:
        return ""
    cand = expected(src, event)
    if not cand:
        return f"명세에도 없는 조합입니다 ({{src}} + {{event}}) - 입력을 확인하세요"

    pending = [t for t in cand if t["status"] != "done"]
    if len(pending) == len(cand):
        parts = ", ".join(
            f"{{t['id']}}({{t['ticket'] or '티켓없음'}}/{{t['status']}})" for t in pending
        )
        return f"아직 구현되지 않았습니다 -> {{parts}}"

    done = [t for t in cand if t["status"] == "done"]
    if pending:
        return (
            f"구현됨 {{', '.join(t['id'] for t in done)}} / 미구현 "
            f"{{', '.join(t['id'] for t in pending)}} - guard 조건을 확인하세요"
        )
    return (
        f"{{', '.join(t['id'] for t in done)}} 는 구현되어 있습니다. "
        f"guard 조건이 거짓입니다 (guard: {{cand[0]['guard'] or '없음'}})"
    )
'''
    target = ctx.cfg.repo / "app" / "_fsm_spec.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def generate_driver(ctx: Ctx, force: bool = False) -> Path:
    """`app/__main__.py` 를 만들고 실제로 실행되는지 확인한다."""
    target = ctx.cfg.repo / "app" / "__main__.py"

    # 명세 표는 티켓 상태를 담으므로 진행에 따라 낡는다. 진입점을 재생성하지 않더라도
    # 항상 다시 쓴다 -- 낡은 진단은 없는 진단보다 나쁘다.
    spec = write_model_spec(ctx)
    n = len(ctx.store.load_model().atoms)
    ctx.log(f"  모델 명세 표 갱신: app/{spec.name} (원자 {n}개)")

    if target.exists() and not force:
        ctx.log(f"진입점 이미 존재 -> 재생성 생략 ({target.name})")
        return target

    # 도메인 코드는 이 단계에서 바뀌면 안 된다. 바뀌면 되돌린다.
    before = _snapshot_domain(ctx)

    iface = ctx.cfg.repo / "INTERFACE.md"
    res = ctx.worker.run(WorkerTask(
        prompt=prompt(
            "driver",
            fsm_yaml=ctx.store.fsm_yaml.read_text(encoding="utf-8"),
            interface=iface.read_text(encoding="utf-8") if iface.exists() else "(INTERFACE.md 없음)",
        ),
        cwd=ctx.cfg.repo,
        allowed_tools=DRIVER_TOOLS,
        timeout=ctx.cfg.limits.gen_timeout_s,
        label="driver",
    ))
    if not res.ok:
        from factory.worker.base import WorkerUnavailable
        if res.infra_failure():
            raise WorkerUnavailable(f"진입점 생성 실패: {res.error}")
        raise RuntimeError(f"진입점 생성 실패: {res.error}")

    reverted = _restore_domain(ctx, before)
    if reverted:
        ctx.log(f"  진입점 생성 중 도메인 코드 수정 감지 -> 되돌림: {', '.join(reverted)}")

    if not target.exists():
        raise RuntimeError(f"워커가 진입점을 만들지 않았습니다: {target}")

    ok, out = smoke_run(ctx)
    if not ok:
        ctx.log(f"  [WARN] 진입점이 실행되지 않습니다:\n{out[:800]}")
    else:
        ctx.log(f"진입점 생성 완료: `python -m app` 실행 확인")
    return target


def smoke_run(ctx: Ctx, events: list[str] | None = None) -> tuple[bool, str]:
    """빈 스크립트를 물려 비대화형으로 한 번 돌려본다.

    생성만 하고 실행을 확인하지 않으면 '실행 가능한 프로그램'이라는 주장이 검증되지 않는다.
    """
    script = ctx.cfg.workspace / ".factory" / "smoke.txt"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("\n".join(events or []) + "\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            ["python", "-m", "app", "--script", str(script)],
            cwd=str(ctx.cfg.repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "타임아웃 -- 비대화형 모드에서 입력을 기다리는 것으로 보입니다"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


# --- 도메인 코드 보호 ---

def _domain_files(ctx: Ctx) -> list[Path]:
    d = ctx.cfg.repo / "app"
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*.py") if p.name not in SHELL_FILES)


def _snapshot_domain(ctx: Ctx) -> dict[str, bytes]:
    return {str(p.relative_to(ctx.cfg.repo)): p.read_bytes() for p in _domain_files(ctx)}


def _restore_domain(ctx: Ctx, before: dict[str, bytes]) -> list[str]:
    changed = []
    for rel, body in before.items():
        p = ctx.cfg.repo / rel
        if not p.exists() or p.read_bytes() != body:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
            changed.append(rel)
    return changed
