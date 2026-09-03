"""공장 자신의 상태기계.

만드는 대상이 FSM이고, 만드는 과정도 FSM이다. 과정을 FSM으로 둔 이유는 하나뿐:
**어디서 멈춰도 그 자리에서 재개할 수 있어야 하기 때문이다.**
LLM 워커는 느리고 비싸고 가끔 죽는다. 처음부터 다시 도는 파이프라인은 쓸 수 없다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from factory.context import Ctx
from factory.models import Status, Ticket, TicketType
from factory.scaffold import scaffold
from factory import release
from factory.stages import analysis, build, critic, deliver, entry, planning, remediate
from factory.worker.base import WorkerUnavailable


class Phase(str, Enum):
    INIT = "INIT"
    ELICIT = "ELICIT"            # 요구사항 -> 원자 요구사항
    USECASE = "USECASE"          # -> 유스케이스
    MODEL = "MODEL"              # -> FSM  (게이트: fsm)
    TICKETIZE = "TICKETIZE"      # -> 티켓 (게이트: ticketize)
    SPEC_TESTS = "SPEC_TESTS"    # FSM -> 테스트 (구현 이전)
    WORK = "WORK"                # 티켓 소진 루프
    DRIVER = "DRIVER"            # 진입점 생성 -- 라이브러리를 실행 가능한 프로그램으로
    E2E_SPEC = "E2E_SPEC"        # 유스케이스 -> 시나리오 테스트
    E2E = "E2E"
    REVIEW = "REVIEW"            # 검토 -> 지적 -> 수정 -> 재검토, 백로그가 빌 때까지
    RELEASE = "RELEASE"          # 출시 판정 -- 루프를 끝내는 자리 (순수 코드)
    COMPLETE = "COMPLETE"


ORDER = [Phase.INIT, Phase.ELICIT, Phase.USECASE, Phase.MODEL, Phase.TICKETIZE,
         Phase.SPEC_TESTS, Phase.WORK, Phase.DRIVER, Phase.E2E_SPEC, Phase.E2E,
         Phase.REVIEW, Phase.RELEASE, Phase.COMPLETE]

GATE_OF = {Phase.MODEL: "fsm", Phase.TICKETIZE: "ticketize"}


def _looks_like_infra(note: str) -> bool:
    from factory.worker.base import WorkerResult
    return WorkerResult(ok=False, error=note).is_outage()


class GateBlocked(Exception):
    """사람 승인 대기. 실패가 아니라 정상적인 일시정지."""

    def __init__(self, gate: str, phase: Phase):
        self.gate, self.phase = gate, phase
        super().__init__(f"게이트 '{gate}' 승인 대기 중 (phase={phase.value})")


@dataclass
class RunState:
    phase: str = Phase.INIT.value
    done_phases: list[str] = field(default_factory=list)   # 산출물이 확정된 단계 (재실행 금지)
    approvals: dict[str, bool] = field(default_factory=dict)
    green_nodes: list[str] = field(default_factory=list)   # 지금까지 통과시킨 테스트 = 회귀 기준선
    review_cycles: int = 0        # 검토를 몇 번 돌았는가. 재개해도 이어져야 예산이 소진된다.
    stats: dict[str, int] = field(default_factory=dict)
    updated: str = ""

    @classmethod
    def load(cls, path: Path) -> "RunState":
        if path.exists():
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        return cls()

    def save(self, path: Path) -> None:
        self.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n


class Orchestrator:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.state_path = ctx.cfg.workspace / ".factory" / "state.json"
        self.state = RunState.load(self.state_path)

    # --- 게이트 ---

    def _check_gate(self, phase: Phase) -> None:
        gate = GATE_OF.get(phase)
        if not gate:
            return
        mode = getattr(self.ctx.cfg.gates, gate, "auto")
        if mode == "auto" or self.state.approvals.get(gate):
            return
        raise GateBlocked(gate, phase)

    def approve(self, gate: str) -> None:
        self.state.approvals[gate] = True
        self.state.save(self.state_path)
        self.ctx.log(f"게이트 '{gate}' 승인됨")

    # --- 메인 루프 ---

    def run(self) -> Phase:
        """현재 단계부터 끝까지. 게이트나 실행 상한을 만나면 그 자리에서 멈춘다.

        단계의 '작업'과 '게이트'를 분리한 이유:
        게이트에서 멈춘 뒤 재개할 때 작업을 다시 돌리면, 사람이 승인한 산출물과
        실제로 쓰이는 산출물이 달라진다. LLM은 같은 입력에도 다른 답을 내기 때문이다.
        """
        while True:
            phase = Phase(self.state.phase)
            if phase is Phase.COMPLETE:
                self.ctx.log("파이프라인 완료")
                return phase

            self.ctx.log(f"-- {phase.value} --")

            if phase.value in self.state.done_phases:
                self.ctx.log("  (이미 확정된 단계 -- 재생성하지 않고 건너뜀)")
                finished = True
            else:
                try:
                    finished = self._execute(phase)
                except WorkerUnavailable as w:
                    # 워커가 실행조차 안 됐다 -- 사용량 한도, 네트워크. 코드 결함이 아니다.
                    # 티켓의 잘못으로 세지 않고 실행 전체를 멈춘다. 재개하면 여기서 이어진다.
                    self.ctx.log(
                        f"[STOP] 워커를 쓸 수 없습니다: {str(w)[:120]}\n"
                        "   사용량 한도나 네트워크 문제일 가능성이 높습니다. 상태는 저장됐고, 다시 run 하면 이어집니다."
                    )
                    self.state.save(self.state_path)
                    return phase
                if finished:
                    self.state.done_phases.append(phase.value)
                self.state.save(self.state_path)

            if not finished:
                # 실행 상한 등으로 이 단계가 아직 안 끝났다. 다음 실행에서 이어서 한다.
                self.state.save(self.state_path)
                return phase

            try:
                self._check_gate(phase)
            except GateBlocked as g:
                self.ctx.log(
                    f"[PAUSE] {g}\n"
                    f"   산출물을 검토한 뒤 `python -m factory.cli approve {g.gate}` 하고 재개하세요."
                )
                self.state.save(self.state_path)
                return phase

            self.state.phase = ORDER[ORDER.index(phase) + 1].value
            self.state.save(self.state_path)

    def _execute(self, phase: Phase) -> bool:
        """단계 작업 수행. 반환값 False = 아직 안 끝났으니 다음 실행에서 이어서."""
        if phase is Phase.INIT:
            self.ctx.cfg.repo.mkdir(parents=True, exist_ok=True)
            if not (self.ctx.cfg.repo / ".git").exists():
                deliver.git(self.ctx, "init", "-q")
                self.ctx.log(f"git 저장소 초기화: {self.ctx.cfg.repo}")
            created = scaffold(self.ctx.cfg.repo)
            if created:
                self.ctx.log(f"스캐폴딩: {', '.join(created)}")
        elif phase is Phase.ELICIT:
            analysis.elicit(self.ctx)
        elif phase is Phase.USECASE:
            analysis.derive_usecases(self.ctx)
        elif phase is Phase.MODEL:
            analysis.model_fsm(self.ctx)
        elif phase is Phase.TICKETIZE:
            planning.ticketize(self.ctx)
        elif phase is Phase.SPEC_TESTS:
            build.spec_tests(self.ctx)
        elif phase is Phase.WORK:
            return self._work_loop()     # 상한에 걸리면 False
        elif phase is Phase.DRIVER:
            entry.generate_driver(self.ctx)
            self._commit_generated("진입점")
        elif phase is Phase.E2E_SPEC:
            build.spec_e2e(self.ctx)
            self._commit_generated("E2E 시나리오")
        elif phase is Phase.E2E:
            self._e2e_loop()
        elif phase is Phase.REVIEW:
            return self._review_loop()
        elif phase is Phase.RELEASE:
            self._release()
        return True

    def _commit_generated(self, what: str) -> None:
        """생성물을 바로 커밋한다.

        커밋은 지금까지 WORK 의 티켓 단위로만 일어났다. 그래서 DRIVER·E2E_SPEC 산출물이
        추적되지 않은 채 남았고, 나중에 수정 단계의 정리 작업에 지워졌다.
        """
        if not deliver.has_changes(self.ctx):
            return
        deliver.ensure_identity(self.ctx)
        deliver.git(self.ctx, "add", "-A")
        res = deliver.git(self.ctx, "commit", "-m", f"{what} 생성", "-m", "Generated by sw_factory.")
        if res.returncode == 0:
            sha = deliver.git(self.ctx, "rev-parse", "--short", "HEAD").stdout.strip()
            self.ctx.log(f"  커밋 {sha}: {what}")

    # --- 출시 판정 ---

    def _release(self) -> None:
        """판정하고 판정서를 남긴다. 차단이어도 예외로 멈추지 않는다 --
        무엇이 막고 있는지 보여주는 것이 이 단계의 일이다."""
        v = release.judge(self.ctx)
        for c in v.checks:
            self.ctx.log(f"  {c}")
        path = self.ctx.cfg.workspace / "RELEASE.md"
        path.write_text(release.render(v, self.ctx.cfg.project), encoding="utf-8")

        if v.released:
            self.ctx.log(f"출시 가능. 알려진 이슈 {len(v.known_issues)}건은 다음 버전 후보입니다.")
            self.state.bump("released")
        else:
            names = ", ".join(c.name for c in v.blockers)
            self.ctx.log(f"출시 차단 — {names}")
            self.state.bump("release_blocked")
        self.ctx.log(f"판정서: {path}")

    # --- 티켓 소진 루프 ---

    def _work_loop(self) -> bool:
        """반환: 백로그를 다 비웠는가. False면 다음 실행에서 이어서 한다."""
        cfg = self.ctx.cfg
        self._requeue_interrupted()
        processed = 0
        while True:
            story = self._next_story()
            if story is None:
                remaining = self.ctx.tracker.search(
                    type=TicketType.STORY.value, status=Status.BLOCKED.value)
                self.ctx.log(
                    f"처리할 스토리 없음 -> 작업 루프 종료"
                    + (f" (BLOCKED {len(remaining)}건은 사람 검토 대기)" if remaining else "")
                )
                return True
            if cfg.limits.max_tickets_per_run and processed >= cfg.limits.max_tickets_per_run:
                todo = len(self.ctx.tracker.search(
                    type=TicketType.STORY.value, status=Status.TODO.value))
                self.ctx.log(
                    f"이번 실행 상한({cfg.limits.max_tickets_per_run}건) 도달 -> 중단. "
                    f"남은 TODO {todo}건은 다시 run 하면 이어서 처리합니다."
                )
                return False
            self._work_one(story)
            processed += 1
            self.state.save(self.state_path)

    # --- 검토 -> 수정 -> 재검토 루프 ---

    def _review_loop(self) -> bool:
        """백로그가 빌 때까지 검토와 수정을 번갈아 돈다.

        종료 조건이 셋이다. 하나라도 없으면 발산한다:
          1) 검토가 연속 2회 새 지적을 못 내면 끝 (loop-until-dry)
          2) 사이클 상한 (critic_cycles)
          3) 기계가 못 닫는 지적은 BLOCKED 로 세운다 -- 요구사항을 정하는 건 사람 몫이라
             영원히 열려 있으면 루프가 끝나지 않는다

        반환값은 항상 True다. 검토는 '못 끝냈으니 다음에'가 아니라
        '여기까지가 기계의 몫'으로 끝나는 단계다.
        """
        cfg = self.ctx.cfg
        dry = 0
        # 사이클 예산은 상태에 저장한다. 안 그러면 한도로 끊길 때마다 1회차부터 다시 시작해
        # 상한에 영영 도달하지 못하고, 릴리스 판정까지 가질 못한다 (v7 에서 실제로 겪었다).
        while self.state.review_cycles < cfg.limits.critic_cycles:
            self.state.review_cycles += 1
            cycle = self.state.review_cycles
            self.state.save(self.state_path)
            self.ctx.log(f"  [검토 {cycle}/{cfg.limits.critic_cycles}]")
            before = {t.key for t in remediate.open_findings(self.ctx)}
            try:
                critic.review(self.ctx)
            except WorkerUnavailable:
                raise                       # 인프라 문제는 검토 중단이 아니라 실행 중단이다
            except (RuntimeError, ValueError) as exc:
                # 검토는 개선 단계다. 워커가 죽었다고 이미 완성된 빌드를 무너뜨리면 안 된다.
                # 사용량 한도·일시적 API 오류가 파이프라인 전체를 실패로 만들면
                # 그때까지 고친 것도 함께 날아간다.
                self.ctx.log(f"  검토 중단: {str(exc)[:200]}")
                self.ctx.log("  여기까지의 지적은 백로그에 남아 있습니다. 다시 run 하면 이어서 검토합니다.")
                break
            fresh = [t for t in remediate.open_findings(self.ctx) if t.key not in before]

            todo = remediate.actionable(self.ctx)
            if not fresh and not todo:
                dry += 1
                self.ctx.log(f"  새 지적 없음 ({dry}/2)")
                if dry >= 2:
                    break
                continue
            dry = 0

            for t in todo:
                self.ctx.log(f"  > {t.key} {t.title[:64]}")
                if remediate.fix(self.ctx, t):
                    self.ctx.tracker.transition(t.key, Status.DONE.value, "검토 지적 해소, 전체 테스트 통과")
                    self.state.bump("critic_fixed")
                    if cfg.gates.commit == "auto":
                        deliver.commit(self.ctx, t)
                else:
                    self.ctx.tracker.transition(
                        t.key, Status.BLOCKED.value,
                        f"{cfg.limits.implement_attempts}회 시도 후에도 해소 실패")
                    self.state.bump("critic_blocked")
                self.state.save(self.state_path)
        if self.state.review_cycles >= cfg.limits.critic_cycles:
            self.ctx.log(f"  사이클 상한({cfg.limits.critic_cycles}) 도달")

        parked = remediate.park_human_findings(self.ctx)
        if parked:
            self.ctx.log(f"  사람 판단 필요 {len(parked)}건: {', '.join(t.key for t in parked)}")
            self.state.bump("needs_human", len(parked))

        left = remediate.open_findings(self.ctx)
        self.ctx.log(f"검토 종료 -- 열린 지적 {len(left)}건")
        return True

    def _requeue_interrupted(self) -> None:
        """지난 실행이 처리 중이던 티켓을 다시 대기열에 넣는다.

        프로세스가 티켓 처리 도중 죽으면 상태가 in_progress 로 남는데,
        _next_story 는 todo 만 집으므로 그 티켓은 영영 처리되지 않는다.
        '어디서 멈춰도 재개된다'는 약속이 조용히 깨지는 지점이었다.
        """
        stuck = self.ctx.tracker.search(
            type=TicketType.STORY.value, status=Status.IN_PROGRESS.value)
        for t in stuck:
            self.ctx.tracker.transition(
                t.key, Status.TODO.value, "지난 실행이 중단됨 -> 대기열로 되돌림")
        if stuck:
            self.ctx.log(
                f"중단된 티켓 {len(stuck)}건을 대기열로 되돌림: "
                f"{', '.join(t.key for t in stuck)}")

    def _next_story(self) -> Ticket | None:
        todo = self.ctx.tracker.search(type=TicketType.STORY.value, status=Status.TODO.value)
        return todo[0] if todo else None

    def _work_one(self, story: Ticket) -> None:
        ctx, cfg = self.ctx, self.ctx.cfg
        ctx.log(f"> {story.key} {story.title}")
        ctx.tracker.transition(story.key, Status.IN_PROGRESS.value)
        story = ctx.tracker.get(story.key)

        subs = planning.decompose(ctx, story)
        history: list[str] = []
        outcome = None
        succeeded = False

        for attempt in range(1, cfg.limits.implement_attempts + 1):
            story.attempts = attempt
            ctx.tracker.update(story)

            if attempt == 1:
                ok, note = build.implement(ctx, story, subs)
            else:
                ok, note = build.debug(ctx, story, outcome, attempt, history)
            history.append(f"[시도 {attempt}] {note[:800]}")

            if not ok and build.SPEC_CONFLICT in note:
                # 워커가 "테스트가 틀렸다"고 주장한다. 기계가 판정할 수 없는 영역 -> 사람에게.
                deliver.raise_bug(ctx, story, "명세 충돌 주장", note)
                self.state.bump("spec_conflict")
                return
            if not ok and _looks_like_infra(note):
                # 워커가 실행조차 안 됐다. 이 티켓의 잘못이 아니고, 다음 티켓도 똑같이 죽는다.
                # 시도 횟수를 되돌리고 실행 전체를 멈춘다 -- 재개하면 여기서 이어진다.
                story.attempts = attempt - 1
                ctx.tracker.update(story)
                ctx.tracker.transition(story.key, Status.TODO.value, f"워커 인프라 실패: {note[:80]}")
                raise WorkerUnavailable(note)
            if not ok:
                ctx.log(f"  시도 {attempt} 워커 오류: {note[:200]}")
                outcome = build.TestOutcome(passed=False, output=note)
                continue

            outcome = build.run_tests(ctx, nodes=story.dod_tests)
            if outcome.passed:
                ctx.log(f"  [OK] DoD 통과 (시도 {attempt})")
                succeeded = True
                break
            ctx.log(f"  [FAIL] DoD 미달 (시도 {attempt}/{cfg.limits.implement_attempts})")

        if not succeeded:
            deliver.raise_bug(
                ctx, story, f"{cfg.limits.implement_attempts}회 시도 후에도 DoD 미달",
                outcome.output if outcome else "",
            )
            self.state.bump("blocked")
            return

        # 회귀 검사: 지금까지 초록이던 테스트가 깨지지 않았는가.
        # 전체 스위트를 돌리면 아직 미구현인 전이들 때문에 항상 빨간색이라 신호가 되지 않는다.
        if self.state.green_nodes:
            reg = build.run_tests(ctx, nodes=self.state.green_nodes)
            if not reg.passed:
                ctx.log(f"  [FAIL] 회귀 발생: {reg.failed_nodes}")
                deliver.raise_bug(ctx, story, "회귀 유발", reg.output)
                self.state.bump("regression")
                return

        if cfg.gates.commit == "auto":
            deliver.commit(ctx, story)
        else:
            ctx.log("  커밋 게이트가 manual -> 커밋 보류")

        for node in story.dod_tests:
            if node not in self.state.green_nodes:
                self.state.green_nodes.append(node)
        for s in subs:
            ctx.tracker.transition(s.key, Status.DONE.value)
        ctx.tracker.transition(story.key, Status.DONE.value, "DoD 통과")
        self.state.bump("done")
        self._close_epic_if_ready(story)

    def _close_epic_if_ready(self, story: Ticket) -> None:
        if not story.parent:
            return
        kids = [t for t in self.ctx.tracker.search(parent=story.parent)
                if t.type == TicketType.STORY.value]
        if kids and all(t.status == Status.DONE.value for t in kids):
            self.ctx.tracker.transition(story.parent, Status.DONE.value, "하위 스토리 전부 완료")
            self.ctx.log(f"  [EPIC] {story.parent} 완료")

    # --- E2E ---

    def _e2e_loop(self) -> None:
        ctx, cfg = self.ctx, self.ctx.cfg
        outcome = None
        for attempt in range(1, cfg.limits.e2e_attempts + 1):
            outcome = deliver.run_e2e(ctx)
            if outcome is None:
                ctx.log("E2E 스위트 없음 -> 생략")
                return
            if outcome.passed:
                ctx.log("[OK] E2E 통과")
                if cfg.gates.push == "auto":
                    deliver.push(ctx)
                else:
                    ctx.log("push 게이트가 manual -> `python -m factory.cli push` 로 직접 내보내세요")
                return
            ctx.log(f"[FAIL] E2E 실패 (시도 {attempt}/{cfg.limits.e2e_attempts})")
            if attempt < cfg.limits.e2e_attempts:
                deliver.debug_e2e(ctx, outcome)

        # E2E 실패는 특정 티켓 탓으로 돌릴 수 없다 -> 독립 버그로 등록.
        bug = ctx.tracker.create(Ticket(
            type=TicketType.BUG.value,
            title="[자동] E2E 실패 - 통합 지점 확인 필요",
            description=f"```\n{outcome.output[-3000:]}\n```",
            labels=["auto", "needs-human", "e2e"],
        ))
        ctx.log(f"  [BLOCKED] E2E 미해결 -> 백로그 {bug.key}")
        self.state.bump("e2e_failed")
