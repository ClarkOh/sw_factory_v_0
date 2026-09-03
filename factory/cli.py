"""공장 CLI.

  python -m factory.cli run      [-c factory.yaml]   # 현재 단계부터 끝까지 (게이트에서 멈춤)
  python -m factory.cli status                       # 어디까지 왔는지
  python -m factory.cli board                        # 티켓 보드
  python -m factory.cli approve <gate>               # 게이트 승인
  python -m factory.cli push                         # 수동 푸시
  python -m factory.cli reset [--hard]               # 진행 상태 초기화
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from factory.artifacts import ArtifactStore
from factory.config import FactoryConfig
from factory.context import Ctx
from factory.models import Status, TicketType
from factory.orchestrator import Orchestrator, Phase, RunState


def build_ctx(cfg_path: Path) -> Ctx:
    cfg = FactoryConfig.load(cfg_path)
    cfg.workspace.mkdir(parents=True, exist_ok=True)

    if cfg.tracker == "jira":
        from factory.tracker.jira import JiraTracker
        tracker = JiraTracker(cfg.jira_project_key)
    else:
        from factory.tracker.local import LocalTracker
        tracker = LocalTracker(cfg.workspace)

    if cfg.worker == "scripted":
        from factory.worker.scripted import ScriptedWorker
        worker = ScriptedWorker()
    else:
        from factory.worker.headless import HeadlessClaudeWorker
        worker = HeadlessClaudeWorker(model=cfg.model or None)

    # 모든 워커 호출이 지나는 단일 관문. 여기서 비용을 적립하므로 어떤 단계도 우회할 수 없다.
    from factory.worker.metered import MeteredWorker
    worker = MeteredWorker(worker, cfg.workspace)

    ctx = Ctx(
        cfg=cfg,
        store=ArtifactStore(cfg.workspace),
        tracker=tracker,
        worker=worker,
        logfile=cfg.workspace / ".factory" / "run.log",
    )
    worker.log = ctx.log          # 폭주 경고를 실행 로그로 내보낸다
    return ctx


def cmd_run(ctx: Ctx, _args) -> int:
    orch = Orchestrator(ctx)
    phase = orch.run()
    ctx.log(f"정지 지점: {phase.value} | 통계: {orch.state.stats}")
    return 0 if phase is Phase.COMPLETE else 2


def cmd_status(ctx: Ctx, _args) -> int:
    st = RunState.load(ctx.cfg.workspace / ".factory" / "state.json")
    print(f"프로젝트 : {ctx.cfg.project}")
    print(f"단계     : {st.phase}   (갱신 {st.updated or '-'})")
    print(f"트래커   : {ctx.cfg.tracker}   워커: {ctx.cfg.worker}")
    print(f"게이트   : {ctx.cfg.gates}")
    print(f"승인됨   : {st.approvals or '없음'}")
    print(f"통계     : {st.stats or '없음'}")
    print(f"초록 기준선: {len(st.green_nodes)}개 테스트")
    return 0


def cmd_board(ctx: Ctx, _args) -> int:
    tickets = ctx.tracker.search()
    if not tickets:
        print("티켓 없음")
        return 0
    order = {s.value: i for i, s in enumerate(
        [Status.TODO, Status.IN_PROGRESS, Status.IN_REVIEW, Status.BLOCKED, Status.DONE])}
    for status in sorted({t.status for t in tickets}, key=lambda s: order.get(s, 99)):
        group = [t for t in tickets if t.status == status]
        print(f"\n[{status}] {len(group)}건")
        for t in group:
            mark = {"epic": "#", "story": "-", "subtask": "  .", "bug": "!"}.get(t.type, "?")
            extra = f"  attempts={t.attempts}" if t.attempts else ""
            print(f"  {mark} {t.key:<8} {t.title[:70]}{extra}")
    blocked = [t for t in tickets if t.status == Status.BLOCKED.value]
    needs = [t for t in tickets if "needs-human" in t.labels and t.status != Status.DONE.value]
    if blocked or needs:
        print(f"\n사람이 볼 것: BLOCKED {len(blocked)}건 / needs-human {len(needs)}건")
    return 0


def cmd_approve(ctx: Ctx, args) -> int:
    Orchestrator(ctx).approve(args.gate)
    print("이제 `python -m factory.cli run` 으로 재개하세요.")
    return 0


def cmd_driver(ctx: Ctx, args) -> int:
    """진입점(app/__main__.py) 생성. 이미 있으면 --force 로 재생성."""
    from factory.stages import entry
    path = entry.generate_driver(ctx, force=args.force)
    print(f"\n실행: cd {ctx.cfg.repo} && python -m app")
    return 0 if path.exists() else 1


def cmd_demo(ctx: Ctx, args) -> int:
    """생성된 프로그램을 이벤트 스크립트로 한 번 돌려본다."""
    from factory.stages import entry
    if not (ctx.cfg.repo / "app" / "__main__.py").exists():
        print("진입점이 없습니다. 먼저 `driver` 를 실행하세요.", file=sys.stderr)
        return 1
    entry.write_fsm_spec(ctx)      # 티켓 상태 반영해 진단 표를 최신화
    events = []
    if args.script:
        events = [ln for ln in Path(args.script).read_text(encoding="utf-8").splitlines() if ln.strip()]
    ok, out = entry.smoke_run(ctx, events)
    print(out)
    return 0 if ok else 1


def cmd_cost(ctx: Ctx, _args) -> int:
    """이 프로젝트가 지금까지 쓴 비용. 기준선 비교는 단위당 값으로 한다."""
    from factory import cost

    n = len(ctx.store.load_fsm().transitions) if ctx.store.fsm_yaml.exists() else 0
    s = cost.summarize(ctx.cfg.workspace, transitions=n)
    if not s["calls"]:
        print("기록 없음 (.factory/cost.jsonl). 아직 워커를 부르지 않았거나 이전 실행입니다.")
        return 0

    t = s["tokens"]
    print(f"호출 {s['calls']:,}회 | 벽시계 {s['wall_hours']}시간"
          + (f" | ${s['cost_usd']:,.2f}" if s["cost_usd"] else " | 비용 미보고"))
    print(f"\n  입력(신규)  {t['input_tokens']:>14,}")
    print(f"  캐시 생성   {t['cache_creation_input_tokens']:>14,}")
    print(f"  캐시 읽기   {t['cache_read_input_tokens']:>14,}")
    print(f"  출력        {t['output_tokens']:>14,}")
    print(f"  {'-'*26}\n  합계        {t['total']:>14,}")

    print(f"\n{'단계':<18}{'호출':>6}{'토큰':>15}{'중앙값':>12}{'최대/중앙':>10}")
    print("-" * 61)
    for name, v in s["by_stage"].items():
        r = v["max"] / v["median"] if v["median"] else 0
        print(f"{name:<18}{v['calls']:>6,}{v['tokens']:>15,}{v['median']:>12,}{r:>9.1f}x")

    d = s["derived"]
    if d:
        print("\n단위당 (기준선 비교는 이 값으로)")
        if "tokens_per_transition" in d:
            print(f"  전이당 토큰                 {d['tokens_per_transition']:>12,}  (전이 {n}개)")
        if "cost_ratio_fix_vs_implement" in d:
            print(f"  지적 해소 : 기능 구현 비용비 {d['cost_ratio_fix_vs_implement']:>11}배")
    return 0


def cmd_release(ctx: Ctx, _args) -> int:
    """출시 판정. 순수 코드이므로 언제든 다시 물어볼 수 있다."""
    from factory import release
    v = release.judge(ctx)
    print(release.render(v, ctx.cfg.project))
    return 0 if v.released else 2


def cmd_push(ctx: Ctx, _args) -> int:
    from factory.stages import deliver
    return 0 if deliver.push(ctx) else 1


def cmd_reset(ctx: Ctx, args) -> int:
    p = ctx.cfg.workspace / ".factory" / "state.json"
    if p.exists():
        p.unlink()
        print(f"진행 상태 삭제: {p}")
    if args.hard:
        import shutil
        for d in (ctx.cfg.workspace / "artifacts", ctx.cfg.workspace / "backlog"):
            if d.exists():
                shutil.rmtree(d)
                print(f"삭제: {d}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factory", description="SW Factory 파이프라인")
    ap.add_argument("-c", "--config", default="factory.yaml", help="설정 파일 경로")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="현재 단계부터 실행").set_defaults(fn=cmd_run)
    sub.add_parser("status", help="진행 상태").set_defaults(fn=cmd_status)
    sub.add_parser("board", help="티켓 보드").set_defaults(fn=cmd_board)
    sub.add_parser("push", help="수동 푸시").set_defaults(fn=cmd_push)
    sub.add_parser("cost", help="토큰·비용 집계").set_defaults(fn=cmd_cost)
    sub.add_parser("release", help="출시 판정").set_defaults(fn=cmd_release)

    ap_dr = sub.add_parser("driver", help="진입점(app/__main__.py) 생성")
    ap_dr.add_argument("--force", action="store_true", help="이미 있어도 다시 생성")
    ap_dr.set_defaults(fn=cmd_driver)

    ap_dm = sub.add_parser("demo", help="생성된 프로그램 실행")
    ap_dm.add_argument("--script", help="이벤트 스크립트 파일 (없으면 빈 입력으로 기동만 확인)")
    ap_dm.set_defaults(fn=cmd_demo)

    ap_ap = sub.add_parser("approve", help="게이트 승인")
    ap_ap.add_argument("gate", choices=["fsm", "ticketize", "commit", "push"])
    ap_ap.set_defaults(fn=cmd_approve)

    ap_rs = sub.add_parser("reset", help="진행 상태 초기화")
    ap_rs.add_argument("--hard", action="store_true", help="산출물과 백로그까지 삭제")
    ap_rs.set_defaults(fn=cmd_reset)

    args = ap.parse_args(argv)
    cfg_path = Path(args.config).resolve()
    if not cfg_path.exists():
        print(f"설정 파일이 없습니다: {cfg_path}", file=sys.stderr)
        return 1
    return args.fn(build_ctx(cfg_path), args)


if __name__ == "__main__":
    raise SystemExit(main())
