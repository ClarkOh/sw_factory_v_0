"""4~5단계: FSM → 티켓 → 서브태스크.

ticketize는 LLM을 쓰지 않는다. FSM이 이미 구조화돼 있으므로 기계적 변환이면 충분하고,
여기에 모델을 넣으면 재실행마다 결과가 흔들려 멱등성이 깨진다.
"""
from __future__ import annotations

from factory.context import Ctx, ask_yaml, prompt
from factory.models import FSM, Status, Ticket, TicketType, UseCase


def ticketize(ctx: Ctx) -> list[Ticket]:
    """FSM → Epic(유스케이스별) + Story(전이별).

    멱등: signature로 기존 티켓을 찾아 재사용한다. 두 번 돌려도 티켓이 두 배가 되지 않는다.
    변경 감지: 전이 내용이 바뀌면 해당 Story를 todo로 되돌리고 're-spec' 라벨을 붙인다.
    """
    model = ctx.store.load_model()
    ucs = {u.id: u for u in ctx.store.load_usecases()}
    created, reopened, reused = [], [], []

    # --- Epic: 유스케이스 하나당 하나 ---
    epic_of: dict[str, str] = {}
    for uc_id, uc in ucs.items():
        existing = next(
            (t for t in ctx.tracker.search(type=TicketType.EPIC.value) if t.usecase_ref == uc_id),
            None,
        )
        if existing:
            epic_of[uc_id] = existing.key
            reused.append(existing.key)
            continue
        e = ctx.tracker.create(Ticket(
            type=TicketType.EPIC.value,
            title=f"[{uc_id}] {uc.title}",
            description=_epic_body(uc),
            usecase_ref=uc_id,
            labels=["auto", "usecase"],
        ))
        epic_of[uc_id] = e.key
        created.append(e.key)

    # --- Story: 전이 하나당 하나 ---
    for tr in model.atoms:
        sig = tr.signature()
        prev = next(
            (t for t in ctx.tracker.search(type=TicketType.STORY.value) if t.fsm_ref == tr.id),
            None,
        )
        if prev is not None:
            if prev.signature == sig:
                reused.append(prev.key)
                continue
            # 명세가 바뀌었다 → 이미 done이어도 다시 해야 한다.
            prev.signature = sig
            prev.title = _story_title(tr)
            prev.description = _story_body(tr, ucs.get(tr.usecase))
            prev.status = Status.TODO.value
            prev.attempts = 0
            if "re-spec" not in prev.labels:
                prev.labels.append("re-spec")
            ctx.tracker.update(prev)
            ctx.tracker.comment(prev.key, f"FSM 전이 {tr.id} 명세 변경 감지 → 재작업 대상으로 되돌림")
            reopened.append(prev.key)
            continue

        s = ctx.tracker.create(Ticket(
            type=TicketType.STORY.value,
            title=_story_title(tr),
            description=_story_body(tr, ucs.get(tr.usecase)),
            parent=epic_of.get(tr.usecase),
            fsm_ref=tr.id,
            usecase_ref=tr.usecase,
            signature=sig,
            labels=["auto", "transition"],
            dod_tests=[f"tests/test_fsm.py::{tr.test_name}"],
        ))
        created.append(s.key)

    ctx.log(f"티켓: 신규 {len(created)} / 재작업 {len(reopened)} / 기존유지 {len(reused)}")
    if reopened:
        ctx.log(f"  재작업: {', '.join(reopened)}")
    return ctx.tracker.search()


def decompose(ctx: Ctx, story: Ticket) -> list[Ticket]:
    """Story → 서브태스크. 이미 쪼개져 있으면 그대로 쓴다(멱등)."""
    existing = ctx.tracker.search(type=TicketType.SUBTASK.value, parent=story.key)
    if existing:
        return existing

    model = ctx.store.load_model()
    tr = next((t for t in model.atoms if t.id == story.fsm_ref), None)
    if tr is None:
        raise RuntimeError(f"{story.key}: 모델에 원자 {story.fsm_ref} 가 없습니다")
    uc = next((u for u in ctx.store.load_usecases() if u.id == tr.usecase), None)

    data = ask_yaml(
        ctx, f"decompose:{story.key}",
        prompt(
            "decompose",
            tid=tr.id, atom_md="\n".join(tr.context_md()),
            usecase=_epic_body(uc) if uc else "(연결된 유스케이스 없음)",
            test_code=_read_dod_tests(ctx, story),
        ),
        cwd=ctx.cfg.repo,
        tools=["Read", "Glob", "Grep"],
    )
    subs = data.get("subtasks", []) or []
    out = []
    for i, s in enumerate(subs[:5], 1):
        out.append(ctx.tracker.create(Ticket(
            type=TicketType.SUBTASK.value,
            title=f"{story.fsm_ref}.{i} {s.get('title', '')}",
            description=s.get("description", ""),
            parent=story.key,
            fsm_ref=story.fsm_ref,
            usecase_ref=story.usecase_ref,
            labels=["auto", "subtask"],
        )))
    ctx.log(f"  {story.key} → 서브태스크 {len(out)}건")
    return out


# --- 본문 생성 ---

def _story_title(tr) -> str:
    return tr.describe()


def _story_body(tr, uc: UseCase | None) -> str:
    lines = [f"원자 `{tr.id}` 구현", ""] + tr.context_md()
    if uc:
        lines += ["", f"유스케이스: [{uc.id}] {uc.title}"]
        if uc.acceptance:
            lines += ["", "인수 조건:"] + [f"- {a}" for a in uc.acceptance]
    lines += ["", f"**완료 조건: `{tr.test_name}` 통과**"]
    return "\n".join(lines)


def _render_step(s) -> str:
    """유스케이스 단계는 문자열이거나 {step, event} 다. 둘 다 사람이 읽게 찍는다."""
    if isinstance(s, dict):
        text = s.get("step") or s.get("text") or ""
        ev = s.get("event")
        return f"{text}" + (f"  `[{ev}]`" if ev else "")
    return str(s)


def _epic_body(uc: UseCase) -> str:
    lines = [
        f"**액터** {uc.actor}",
        f"**사전조건** {uc.precondition}",
        f"**사후조건** {uc.postcondition}",
        "", "**주 흐름**",
    ] + [f"- {_render_step(s)}" for s in uc.main_flow]
    if uc.alt_flows:
        lines += ["", "**대안/예외 흐름**"] + [f"- {_render_step(s)}" for s in uc.alt_flows]
    if uc.acceptance:
        lines += ["", "**인수 조건**"] + [f"- {s}" for s in uc.acceptance]
    return "\n".join(lines)


def _read_dod_tests(ctx: Ctx, ticket: Ticket) -> str:
    """DoD로 지정된 **그 테스트 함수만** 잘라서 돌려준다.

    파일 전체를 넣으면 전이가 늘수록 프롬프트가 선형으로 커진다.
    전이 42개 시점에 Windows 명령줄 상한(32767자)을 넘겨 파이프라인이 죽은 적이 있다.
    워커에게 필요한 건 자기 티켓의 테스트 하나뿐이므로, 그것만 보낸다.
    """
    import ast

    chunks: list[str] = []
    for node in ticket.dod_tests:
        rel, _, func = node.partition("::")
        path = ctx.cfg.repo / rel
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        snippet = _extract_func(src, func) if func else None
        if snippet is None:
            # 함수를 못 찾으면 파일 전체 대신 앞부분만 (프롬프트 폭발 방지)
            snippet = src[:4000] + ("\n# ...(생략)" if len(src) > 4000 else "")
        chunks.append(f"# {node}\n{_module_preamble(src)}\n{snippet}")
    return "\n\n".join(chunks) if chunks else "(테스트 파일이 아직 생성되지 않았습니다)"


def _extract_func(src: str, name: str) -> str | None:
    """모듈 소스에서 함수 하나의 원문을 잘라낸다."""
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    lines = src.splitlines()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            start = min([n.lineno] + [d.lineno for d in n.decorator_list]) - 1
            return "\n".join(lines[start:n.end_lineno])
    return None


def _module_preamble(src: str) -> str:
    """import와 모듈 수준 상수 — 테스트가 무엇에 의존하는지 워커가 알아야 한다."""
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    lines = src.splitlines()
    keep: list[str] = []
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign)):
            keep.extend(lines[n.lineno - 1:n.end_lineno])
    return "\n".join(keep[:60])
