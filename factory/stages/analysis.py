"""1~3단계: 요구사항 → 유스케이스 → FSM.

이 구간의 출력은 전부 '문서'다. 코드는 아직 한 줄도 없다.
여기서 나온 결함은 뒤에서 티켓 수십 개로 증식하므로, FSM 검증이 가장 중요한 관문이다.
"""
from __future__ import annotations

from factory.context import Ctx, ask_yaml, prompt
from factory.models import FSM, Event, Requirement, State, Transition, UseCase

READ_ONLY = ["Read", "Glob", "Grep"]


def _principles(ctx: Ctx) -> str:
    """원칙 문서 원문. 여기 선언된 보존 등식을 모델 검사가 읽는다."""
    p = ctx.store.principles_md
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _ask(ctx: Ctx, label: str, body: str) -> dict:
    """분석 단계는 파일을 쓰지 않는다. 읽기 전용 도구로 묻고 YAML만 받는다."""
    return ask_yaml(ctx, label, body, cwd=ctx.cfg.workspace, tools=READ_ONLY)


def elicit(ctx: Ctx) -> list[Requirement]:
    """requirements.md → 원자적 요구사항 목록."""
    src = ctx.store.requirements_md
    if not src.exists():
        raise FileNotFoundError(f"요구사항 문서가 없습니다: {src}")
    d = _ask(ctx, "elicit", prompt("elicit", requirements_md=src.read_text(encoding="utf-8")))
    reqs = [Requirement(**r) for r in d.get("requirements", [])]
    if not reqs:
        raise RuntimeError("요구사항이 하나도 도출되지 않았습니다")
    ctx.store.save_requirements(reqs)
    ctx.log(f"요구사항 {len(reqs)}건 → {ctx.store.requirements_yaml}")
    return reqs


def derive_usecases(ctx: Ctx) -> list[UseCase]:
    """요구사항 → 유스케이스. 커버리지(모든 REQ가 어딘가에 쓰였는가)를 경고한다."""
    d = _ask(ctx, "usecase", prompt(
        "usecase",
        requirements_yaml=ctx.store.requirements_yaml.read_text(encoding="utf-8"),
    ))
    ucs = [UseCase(**u) for u in d.get("usecases", [])]
    if not ucs:
        raise RuntimeError("유스케이스가 하나도 도출되지 않았습니다")

    covered = {r for u in ucs for r in u.requirements}
    missing = [r.id for r in ctx.store.load_requirements() if r.id not in covered]
    if missing:
        ctx.log(f"  ⚠ 어떤 유스케이스에도 안 잡힌 요구사항: {', '.join(missing)}")

    ctx.store.save_usecases(ucs)
    ctx.log(f"유스케이스 {len(ucs)}건 → {ctx.store.usecases_yaml}")
    return ucs


def model(ctx: Ctx):
    """MODEL 단계. 중간 형식은 factory.yaml 의 `model_form` 이 정한다.

    이 선택이 곧 최소한의 ARCHITECT 다: 도메인의 원자가 무엇인지 사람이 한 줄로
    선언한다. 반응형이면 전이(fsm), 상태 없는 변환·분류면 규칙(rules).
    """
    if ctx.cfg.model_form == "rules":
        return model_rules(ctx)
    return model_fsm(ctx)


def model_rules(ctx: Ctx):
    """유스케이스 → 결정표. FSM 쪽과 같은 수리 루프를 돈다."""
    from factory.models import Rule, RuleTable

    d = _ask(ctx, "rules", prompt(
        "rules",
        project=ctx.cfg.project,
        usecases_yaml=ctx.store.usecases_yaml.read_text(encoding="utf-8"),
    ))
    table = _to_rules(d, ctx.cfg.project)

    for attempt in range(1, ctx.cfg.limits.fsm_repair_attempts + 1):
        errs = table.validate()
        if not errs:
            break
        ctx.store.save_rules(table)
        d = _ask(ctx, f"rules_repair:{attempt}", prompt(
            "fsm_repair",           # 결함 목록을 주고 고치게 하는 틀은 같다
            errors="\n".join(f"- {e}" for e in errs),
            fsm_yaml=ctx.store.rules_yaml.read_text(encoding="utf-8"),
        ))
        table = _to_rules(d, ctx.cfg.project)
    else:
        raise RuntimeError("결정표 구조 수리 상한 초과: " + "; ".join(table.validate()[:5]))

    table = _simulate_rules_and_repair(ctx, table)
    ctx.store.save_rules(table)
    ctx.log(f"결정표: 규칙 {len(table.rules)}개 → {ctx.store.rules_yaml}")
    return table


def _to_rules(d: dict, project: str):
    from factory.models import Rule, RuleTable
    rules = []
    for i, r in enumerate(d.get("rules", []) or [], 1):
        if not isinstance(r, dict):
            continue
        rules.append(Rule(
            id=str(r.get("id") or f"R-{i:03d}"),
            when=str(r.get("when") or r.get("guard") or ""),
            then=str(r.get("then") or r.get("action") or ""),
            usecase=str(r.get("usecase") or ""),
            notes=str(r.get("notes") or "")))
    return RuleTable(project=project, rules=rules)


def _simulate_rules_and_repair(ctx: Ctx, table):
    """결정표에 돌릴 수 있는 검사: 보존 등식과 유스케이스 커버리지.

    도달성·재생 같은 그래프 검사는 좌표가 없으니 성립하지 않는다.
    보존 등식은 원자의 내용만 보므로 형식을 가리지 않는다 -- 그래서 살아남는다.
    """
    from factory import simulate

    ucs = ctx.store.load_usecases()
    for attempt in range(1, ctx.cfg.limits.fsm_repair_attempts + 1):
        findings = list(simulate.check_conservation(
            table, simulate.load_conserved(_principles(ctx))))
        covered = {r.usecase for r in table.rules if r.usecase}
        for u in ucs:
            if u.id not in covered:
                findings.append(simulate.Finding(
                    "커버리지", "", f"{u.id} '{u.title}' 를 참조하는 규칙이 하나도 없다",
                    [u.id]))
        fixable = [f for f in findings if f.fixable_by_fsm]
        for f in findings[:10]:
            ctx.log(f"    - {f}")
        if not fixable:
            return table
        ctx.log(f"  모델 검사: {len(fixable)}건 수리 시도 {attempt}/{ctx.cfg.limits.fsm_repair_attempts}")
        ctx.store.save_rules(table)
        d = _ask(ctx, f"rules_sim_repair:{attempt}", prompt(
            "fsm_sim_repair",
            findings="\n".join(f"- {f}" for f in fixable),
            fsm_yaml=ctx.store.rules_yaml.read_text(encoding="utf-8"),
        ))
        table = _to_rules(d, ctx.cfg.project)
    ctx.log("  모델 검사: 진전 없음 -- 게이트에서 사람이 판단합니다")
    return table


def model_fsm(ctx: Ctx) -> FSM:
    """유스케이스 → FSM. 구조 검증에 걸리면 결함을 되먹여 자동 수리한다.

    수리 상한을 넘기면 예외로 멈춘다 — 잘못된 FSM으로 티켓을 만드는 것보다 낫다.
    """
    d = _ask(ctx, "fsm", prompt(
        "fsm",
        project=ctx.cfg.project,
        usecases_yaml=ctx.store.usecases_yaml.read_text(encoding="utf-8"),
    ))
    fsm = _to_fsm(d, ctx.cfg.project)

    for attempt in range(1, ctx.cfg.limits.fsm_repair_attempts + 1):
        errs = fsm.validate()
        if not errs:
            break
        ctx.log(f"  FSM 구조 결함 {len(errs)}건 → 수리 시도 {attempt}")
        for e in errs:
            ctx.log(f"    - {e}")
        ctx.store.save_fsm(fsm)
        d = _ask(ctx, f"fsm_repair:{attempt}", prompt(
            "fsm_repair",
            errors="\n".join(f"- {e}" for e in errs),
            fsm_yaml=ctx.store.fsm_yaml.read_text(encoding="utf-8"),
        ))
        fsm = _to_fsm(d, ctx.cfg.project)

    # 구조가 맞으면 이제 **의미**를 본다. 원칙 불변식·유스케이스 재생·커버리지를
    # 그래프 질의로 검사한다. 구현 전에 걸리는 것이 요점이다 --
    # v3~v5 의 "배출 실패 후 판매 상태 복귀" 결함이 여기서 잡힌다.
    fsm = _simulate_and_repair(ctx, fsm)

    errs = fsm.validate()
    ctx.store.save_fsm(fsm)
    if errs:
        raise RuntimeError(
            "FSM 구조 결함을 자동으로 못 고쳤습니다. artifacts/fsm.yaml 을 직접 고치고 재개하세요:\n"
            + "\n".join(f"  - {e}" for e in errs)
        )
    ctx.log(
        f"FSM: 상태 {len(fsm.states)} / 이벤트 {len(fsm.events)} / 전이 {len(fsm.transitions)} "
        f"→ {ctx.store.fsm_yaml}"
    )
    return fsm


def _simulate_and_repair(ctx: Ctx, fsm: FSM) -> FSM:
    """모델 검사에 걸린 것을 되먹여 고친다.

    결과는 결함 단정이 아니라 **위반 후보** 다 (술어를 자유 변수로 두므로 실제로는
    불가능한 경로도 나온다). 그래서 수리 프롬프트에 "타당하지 않으면 고치지 말고
    이유를 적으라"고 넣는다. 상한을 넘겨도 예외로 멈추지 않는다 --
    구조 결함과 달리 의미 위반은 사람이 판단할 여지가 있다.
    """
    from factory import simulate

    ucs = ctx.store.load_usecases()
    prev = None
    for attempt in range(1, ctx.cfg.limits.fsm_repair_attempts + 1):
        findings = simulate.run_all(fsm, ucs, _principles(ctx))
        fixable = [f for f in findings if f.fixable_by_fsm]
        if attempt == 1:
            for f in findings[:10]:
                ctx.log(f"    - {f}")
            other = len(findings) - len(fixable)
            if other:
                ctx.log(f"    ({other}건은 FSM 수리로 고칠 수 없어 게이트에서 사람이 봅니다)")
        if not fixable:
            ctx.log(f"  모델 검사: FSM 으로 고칠 지적 없음 (전체 {len(findings)}건)")
            return fsm
        # 진전이 없으면 멈춘다. 같은 지적을 상한까지 다시 던지는 것은 토큰만 태운다 --
        # 모델이 "고칠 것이 아니다"라고 판단했거나, 검사 쪽이 틀린 것이다. 둘 다 사람이 볼 일이다.
        if prev is not None and len(fixable) >= prev:
            ctx.log(f"  모델 검사: {len(fixable)}건에서 진전 없음 -- 수리를 멈추고 게이트에서 사람이 판단합니다")
            return fsm
        prev = len(fixable)
        ctx.log(f"  모델 검사: {len(fixable)}건 수리 시도 {attempt}/{ctx.cfg.limits.fsm_repair_attempts}")
        findings = fixable
        if attempt == ctx.cfg.limits.fsm_repair_attempts:
            ctx.log("  수리 상한 도달 -- 남은 지적은 FSM 게이트에서 사람이 판단합니다")
            return fsm
        ctx.store.save_fsm(fsm)
        d = _ask(ctx, f"fsm_sim_repair:{attempt}", prompt(
            "fsm_sim_repair",
            findings=chr(10).join(f"- {f}" for f in findings),
            principles=(ctx.store.principles_md.read_text(encoding="utf-8")
                        if ctx.store.principles_md.exists() else "(원칙 문서 없음)"),
            fsm_yaml=ctx.store.fsm_yaml.read_text(encoding="utf-8"),
        ))
        fsm = _to_fsm(d, ctx.cfg.project)
    return fsm


def _to_fsm(d: dict, project: str) -> FSM:
    return FSM(
        project=d.get("project", project),
        states=[State(**s) for s in d.get("states", [])],
        events=[Event(**e) for e in d.get("events", [])],
        transitions=[Transition(**t) for t in d.get("transitions", [])],
    )
