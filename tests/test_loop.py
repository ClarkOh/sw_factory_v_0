"""공장 루프 자체의 테스트.

여기서는 LLM을 전혀 쓰지 않는다. ScriptedWorker로 워커의 출력을 고정해 놓고
**오케스트레이터의 제어 흐름만** 검증한다.

이 분리가 없으면 "루프가 잘못됐는가, 모델이 이상한 답을 했는가"를 구별할 수 없어
디버깅이 불가능해진다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from factory.artifacts import ArtifactStore
from factory.config import FactoryConfig, Gates, Limits
from factory.context import Ctx
from factory.models import Status, TicketType
from factory.orchestrator import Orchestrator, Phase, RunState
from factory.stages import deliver, planning
from factory.tracker.local import LocalTracker
from factory.worker.base import WorkerResult, WorkerTask
from factory.worker.scripted import ScriptedWorker

# --------------------------------------------------------------------------
# 고정된 가짜 도메인: 카운터
# --------------------------------------------------------------------------

REQUIREMENTS_YAML = """
requirements:
  - id: REQ-01
    text: 사용자는 카운터를 1씩 증가시킬 수 있다
    kind: functional
    source: 본문
  - id: REQ-02
    text: 사용자는 카운터를 0으로 되돌릴 수 있다
    kind: functional
    source: 본문
"""

USECASES_YAML = """
usecases:
  - id: UC-01
    title: 카운터 증가
    actor: 사용자
    precondition: 카운터가 존재한다
    postcondition: 값이 1 늘어난다
    main_flow: ["1. 사용자가 증가를 요청한다", "2. 시스템이 값을 1 늘린다"]
    alt_flows: []
    acceptance: ["증가 후 값이 이전보다 1 크다"]
    requirements: [REQ-01]
  - id: UC-02
    title: 카운터 초기화
    actor: 사용자
    precondition: 카운터 값이 0보다 크다
    postcondition: 값이 0이다
    main_flow: ["1. 사용자가 초기화를 요청한다", "2. 시스템이 값을 0으로 만든다"]
    alt_flows: []
    acceptance: ["초기화 후 값이 0이다"]
    requirements: [REQ-02]
"""

FSM_YAML = """
project: counter
states:
  - {id: ZERO, desc: 값이 0, initial: true, final: false}
  - {id: NONZERO, desc: 값이 양수, initial: false, final: false}
events:
  - {id: INC, desc: 증가 요청}
  - {id: RESET, desc: 초기화 요청}
transitions:
  - {id: T-001, src: ZERO, event: INC, dst: NONZERO, guard: "", action: "n += 1", usecase: UC-01}
  - {id: T-002, src: NONZERO, event: INC, dst: NONZERO, guard: "", action: "n += 1", usecase: UC-01}
  - {id: T-003, src: NONZERO, event: RESET, dst: ZERO, guard: "", action: "n = 0", usecase: UC-02}
"""

SPEC_TEST_CODE = '''\
"""FSM 명세로부터 생성된 테스트. 구현 쪽에서 수정 금지."""
from app.counter import Counter


def test_t_001():
    c = Counter()
    assert c.state == "ZERO"
    c.inc()
    assert c.state == "NONZERO"
    assert c.n == 1


def test_t_002():
    c = Counter()
    c.inc()
    c.inc()
    assert c.state == "NONZERO"
    assert c.n == 2


def test_t_003():
    c = Counter()
    c.inc()
    c.reset()
    assert c.state == "ZERO"
    assert c.n == 0
'''

GOOD_IMPL = '''\
class Counter:
    def __init__(self) -> None:
        self.n = 0

    @property
    def state(self) -> str:
        return "ZERO" if self.n == 0 else "NONZERO"

    def inc(self) -> None:
        self.n += 1

    def reset(self) -> None:
        self.n = 0
'''

BAD_IMPL = '''\
class Counter:
    def __init__(self) -> None:
        self.n = 0

    @property
    def state(self) -> str:
        return "ZERO"          # 항상 틀린 값 -> 모든 전이 테스트 실패

    def inc(self) -> None:
        pass

    def reset(self) -> None:
        pass
'''

DRIVER_CODE = '''\
"""카운터 FSM 진입점. `python -m app` 또는 `python -m app --script <파일>`."""
import sys

from app.counter import Counter


def main(argv):
    c = Counter()
    lines = []
    if "--script" in argv:
        path = argv[argv.index("--script") + 1]
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    for ln in lines:
        before = c.state
        if ln == "INC":
            c.inc()
        elif ln == "RESET":
            c.reset()
        else:
            print("알 수 없는 이벤트:", ln)
            continue
        print(f"  {before} --{ln}--> {c.state} (n={c.n})")
    print(f"최종: state={c.state} n={c.n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

# raw 문자열: 안의 \n 이 이 파일에서 해석되지 않고 생성될 파일에 그대로 들어가야 한다.
E2E_CODE = r'''"""유스케이스로부터 나온 시나리오 테스트. 전이의 조합을 검증한다."""
import subprocess
import sys
from pathlib import Path

from app.counter import Counter

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_uc01_repeated_increments_accumulate():
    """UC-01 주 흐름: 여러 번 증가시키면 누적된다 (전이 하나로는 안 드러나는 것)."""
    c = Counter()
    for _ in range(5):
        c.inc()
    assert c.n == 5
    assert c.state == "NONZERO"


def test_uc02_reset_then_reuse():
    """UC-02 + 복귀: 초기화한 뒤에도 정상적으로 다시 증가한다."""
    c = Counter()
    c.inc(); c.inc(); c.reset()
    assert (c.n, c.state) == (0, "ZERO")
    c.inc()
    assert (c.n, c.state) == (1, "NONZERO")


def test_packaged_app_runs_end_to_end(tmp_path):
    """진입점 검증: 단위 테스트가 모두 통과해도 프로그램이 안 돌 수 있다."""
    script = tmp_path / "s.txt"
    script.write_text("INC\nINC\nRESET\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "app", "--script", str(script)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
'''

DECOMPOSE_YAML = """
subtasks:
  - title: Counter 클래스 골격 작성
    description: app/counter.py 에 상태와 값을 갖는 클래스를 만든다
  - title: 전이 동작 구현
    description: 이벤트 메서드가 값과 상태를 갱신하도록 한다
"""


# --------------------------------------------------------------------------
# 픽스처
# --------------------------------------------------------------------------

def make_worker(impl_code: str = GOOD_IMPL, *, tamper: bool = False) -> ScriptedWorker:
    """단계별 응답이 고정된 워커."""
    w = ScriptedWorker()
    w.register("elicit", lambda t: WorkerResult(ok=True, text=REQUIREMENTS_YAML))
    w.register("usecase", lambda t: WorkerResult(ok=True, text=USECASES_YAML))
    w.register("fsm", lambda t: WorkerResult(ok=True, text=FSM_YAML))
    w.register("decompose", lambda t: WorkerResult(ok=True, text=DECOMPOSE_YAML))

    def write_tests(t: WorkerTask) -> WorkerResult:
        p = t.cwd / "tests" / "test_fsm.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SPEC_TEST_CODE, encoding="utf-8")
        return WorkerResult(ok=True, text="테스트 작성 완료")

    def write_impl(t: WorkerTask) -> WorkerResult:
        p = t.cwd / "app" / "counter.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(impl_code, encoding="utf-8")
        # 전이마다 다른 파일도 건드린다 — 실제 워커처럼 티켓별 변경분이 생기도록.
        m = re.search(r"^id: (T-\d+)$", t.prompt, re.MULTILINE)
        if m:
            note = t.cwd / "app" / "transitions" / f"{m.group(1).replace('-', '_').lower()}.py"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(f'"""{m.group(1)} 구현 메모."""\n', encoding="utf-8")
        if tamper:
            # 나쁜 워커: 테스트를 통과시키려고 테스트를 지워버린다.
            (t.cwd / "tests" / "test_fsm.py").write_text(
                "def test_t_001():\n    pass\n", encoding="utf-8")
        return WorkerResult(ok=True, text="구현 완료")

    def write_driver(t: WorkerTask) -> WorkerResult:
        p = t.cwd / "app" / "__main__.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DRIVER_CODE, encoding="utf-8")
        if tamper:
            # 나쁜 워커: 진입점을 만들면서 도메인 코드까지 건드린다.
            (t.cwd / "app" / "counter.py").write_text("# 망가뜨림\n", encoding="utf-8")
        return WorkerResult(ok=True, text="진입점 작성 완료")

    def write_e2e(t: WorkerTask) -> WorkerResult:
        p = t.cwd / "tests" / "e2e" / "test_scenarios.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(E2E_CODE, encoding="utf-8")
        if tamper:
            # 나쁜 워커: E2E를 통과시키려고 단위 테스트를 느슨하게 만든다.
            (t.cwd / "tests" / "test_fsm.py").write_text(
                "def test_t_001():\n    pass\n", encoding="utf-8")
        return WorkerResult(ok=True, text="E2E 시나리오 작성 완료")

    w.register("spec_test", write_tests)
    w.register("implement", write_impl)
    w.register("debug", write_impl)      # 디버그도 같은 코드를 쓴다 -> 나쁜 구현이면 계속 실패
    w.register("driver", write_driver)
    w.register("e2e_spec", write_e2e)
    # 기본 검토 결과는 '지적 없음'. 검토를 시험하는 테스트만 make_critic_worker 로 덮어쓴다.
    w.register("critic", lambda t: WorkerResult(ok=True, text=CRITIC_NONE))
    return w


def make_ctx(tmp_path: Path, worker: ScriptedWorker, **gate_kw) -> Ctx:
    ws = tmp_path / "ws"
    repo = tmp_path / "repo"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "requirements.md").write_text("카운터를 증가시키고 초기화할 수 있어야 한다.", encoding="utf-8")

    gates = Gates(fsm="auto", ticketize="auto", commit="auto", push="manual")
    for k, v in gate_kw.items():
        setattr(gates, k, v)

    cfg = FactoryConfig(
        project="counter", workspace=ws, repo=repo,
        tracker="local", worker="scripted",
        gates=gates,
        limits=Limits(implement_attempts=3, e2e_attempts=1, worker_timeout_s=60),
    )
    return Ctx(cfg=cfg, store=ArtifactStore(ws), tracker=LocalTracker(ws), worker=worker)


# --------------------------------------------------------------------------
# 시나리오
# --------------------------------------------------------------------------

def test_happy_path_runs_to_completion(tmp_path):
    """정상 경로: 요구사항부터 커밋까지 한 번에 끝난다."""
    ctx = make_ctx(tmp_path, make_worker())
    orch = Orchestrator(ctx)
    assert orch.run() is Phase.COMPLETE

    stories = ctx.tracker.search(type=TicketType.STORY.value)
    assert len(stories) == 3, "전이 3개 -> 스토리 3개"
    assert all(s.status == Status.DONE.value for s in stories)

    epics = ctx.tracker.search(type=TicketType.EPIC.value)
    assert len(epics) == 2
    assert all(e.status == Status.DONE.value for e in epics), "하위가 다 끝나면 에픽도 닫힌다"

    assert not ctx.tracker.search(type=TicketType.BUG.value), "정상 경로에는 버그가 없어야 한다"
    assert len(orch.state.green_nodes) == 3

    log = deliver.git(ctx, "log", "--oneline").stdout
    assert log.count("\n") >= 3, f"티켓마다 커밋이 남아야 한다:\n{log}"
    # 부분문자열이 아니라 git이 실제로 트레일러로 인식하는지 확인한다.
    # (본문 어딘가에 그 글자가 있는 것과, 마지막 문단의 트레일러인 것은 다르다.)
    trailers = deliver.git(
        ctx, "log", "--format=%(trailers:key=FSM-Transition,valueonly=true)").stdout
    assert {"T-001", "T-002", "T-003"} <= set(trailers.split()), (
        f"커밋마다 FSM 전이 트레일러가 있어야 한다:\n{trailers!r}")


def test_failure_escalates_to_backlog(tmp_path):
    """구현이 계속 실패하면 무한 재시도하지 않고 백로그로 넘어간다."""
    ctx = make_ctx(tmp_path, make_worker(BAD_IMPL))
    orch = Orchestrator(ctx)
    orch.run()

    stories = ctx.tracker.search(type=TicketType.STORY.value)
    blocked = [s for s in stories if s.status == Status.BLOCKED.value]
    assert blocked, "실패한 스토리는 BLOCKED 여야 한다"
    assert all(s.attempts == 3 for s in blocked), "상한만큼만 시도한다"

    bugs = ctx.tracker.search(type=TicketType.BUG.value)
    assert len(bugs) >= 1
    assert all("needs-human" in b.labels for b in bugs), "사람 검토 큐로 라벨링된다"
    assert bugs[0].fsm_ref, "버그도 FSM 전이로 역추적된다"


def test_test_tampering_is_reverted(tmp_path):
    """구현 워커가 테스트를 고치면 되돌려진다 (DoD 자기인증 방지)."""
    ctx = make_ctx(tmp_path, make_worker(GOOD_IMPL, tamper=True))
    Orchestrator(ctx).run()

    body = (ctx.cfg.repo / "tests" / "test_fsm.py").read_text(encoding="utf-8")
    assert "def test_t_002" in body, "지워진 테스트가 복구되어야 한다"
    assert body == SPEC_TEST_CODE, "테스트 파일이 원본 그대로여야 한다"

    audit = (ctx.cfg.workspace / "backlog" / "comments.log").read_text(encoding="utf-8")
    assert "위반" in audit, "위반 사실이 감사 로그에 남아야 한다"


def test_gate_pauses_and_resumes(tmp_path):
    """게이트가 manual이면 멈추고, 승인 후 그 지점에서 재개된다."""
    ctx = make_ctx(tmp_path, make_worker(), fsm="manual")

    stopped = Orchestrator(ctx).run()
    assert stopped is Phase.MODEL
    assert ctx.store.fsm_yaml.exists(), "멈추기 전에 FSM은 만들어 놓는다 (사람이 봐야 하므로)"
    assert not ctx.tracker.search(), "승인 전에는 티켓을 만들지 않는다"

    orch2 = Orchestrator(ctx)          # 새 프로세스처럼 상태를 다시 읽는다
    assert orch2.state.phase == Phase.MODEL.value
    orch2.approve("fsm")

    orch3 = Orchestrator(ctx)
    assert orch3.run() is Phase.COMPLETE
    assert len(ctx.tracker.search(type=TicketType.STORY.value)) == 3


def test_ticketize_is_idempotent(tmp_path):
    """같은 FSM으로 두 번 돌려도 티켓이 늘지 않는다."""
    ctx = make_ctx(tmp_path, make_worker())
    orch = Orchestrator(ctx)
    orch.run()
    first = len(ctx.tracker.search())

    planning.ticketize(ctx)
    assert len(ctx.tracker.search()) == first, "재실행이 티켓을 복제하면 안 된다"


def test_spec_change_reopens_story(tmp_path):
    """FSM 전이가 바뀌면 완료된 스토리도 재작업 대상으로 되돌아온다."""
    ctx = make_ctx(tmp_path, make_worker())
    Orchestrator(ctx).run()
    story = next(s for s in ctx.tracker.search(type=TicketType.STORY.value) if s.fsm_ref == "T-001")
    assert story.status == Status.DONE.value

    fsm = ctx.store.load_fsm()
    tr = next(t for t in fsm.transitions if t.id == "T-001")
    tr.action = "n += 2"                       # 명세 변경
    ctx.store.save_fsm(fsm)

    planning.ticketize(ctx)
    again = ctx.tracker.get(story.key)
    assert again.status == Status.TODO.value
    assert "re-spec" in again.labels
    assert again.attempts == 0


def test_fsm_validation_catches_structural_defects():
    """잘못된 FSM은 티켓이 되기 전에 걸린다."""
    from factory.models import FSM, Event, State, Transition

    bad = FSM(
        project="x",
        states=[State("A", initial=True), State("B"), State("ORPHAN")],
        events=[Event("E")],
        transitions=[
            Transition("T-1", "A", "E", "B"),
            Transition("T-2", "A", "E", "A"),      # guard 없는 중복 -> 비결정적
            Transition("T-3", "A", "E", "GHOST"),  # 미정의 상태
        ],
    )
    errs = " | ".join(bad.validate())
    assert "비결정적" in errs
    assert "GHOST" in errs
    assert "ORPHAN" in errs      # 도달 불가
    assert "B" in errs           # 나가는 전이 없음 (deadlock)


# --------------------------------------------------------------------------
# 회귀 테스트 — 실제 운영 중 발견된 버그
# --------------------------------------------------------------------------

def test_gate_resume_does_not_regenerate_artifact(tmp_path):
    """게이트 재개 시 FSM을 다시 만들면 안 된다.

    실제로 터졌던 버그: MODEL 단계가 성공한 뒤 게이트에서 멈췄는데 단계가 확정되지 않아,
    재개할 때 FSM을 처음부터 다시 생성했다. LLM은 같은 입력에 다른 답을 내므로
    사람이 승인한 FSM과 실제로 쓰이는 FSM이 달라진다.
    """
    worker = make_worker()
    ctx = make_ctx(tmp_path, worker, fsm="manual")

    assert Orchestrator(ctx).run() is Phase.MODEL
    fsm_calls_before = sum(1 for c in worker.calls if c.label == "fsm")
    assert fsm_calls_before == 1
    fsm_text = ctx.store.fsm_yaml.read_text(encoding="utf-8")

    Orchestrator(ctx).approve("fsm")
    assert Orchestrator(ctx).run() is Phase.COMPLETE

    fsm_calls_after = sum(1 for c in worker.calls if c.label == "fsm")
    assert fsm_calls_after == 1, "재개하면서 FSM을 다시 생성했다"
    assert ctx.store.fsm_yaml.read_text(encoding="utf-8") == fsm_text, "승인한 FSM이 바뀌었다"


def test_run_cap_resumes_instead_of_skipping(tmp_path):
    """실행 상한에 걸리면 다음 실행에서 이어서 해야 한다. E2E로 건너뛰면 안 된다.

    실제로 터졌던 버그: 상한 도달 시 WORK 단계가 완료로 처리되어 남은 스토리를 영영 버렸다.
    """
    ctx = make_ctx(tmp_path, make_worker())
    ctx.cfg.limits.max_tickets_per_run = 1

    first = Orchestrator(ctx).run()
    assert first is Phase.WORK, "상한에 걸렸으면 WORK 단계에 머물러야 한다"
    done = ctx.tracker.search(type=TicketType.STORY.value, status=Status.DONE.value)
    assert len(done) == 1

    Orchestrator(ctx).run()      # 2번째
    Orchestrator(ctx).run()      # 3번째
    final = Orchestrator(ctx).run()

    assert final is Phase.COMPLETE
    assert len(ctx.tracker.search(type=TicketType.STORY.value, status=Status.DONE.value)) == 3


def test_yaml_repair_handles_unquoted_special_scalars():
    """`guard: !recognized` 같은 응답을 버리지 않고 살려낸다."""
    from factory.artifacts import extract_yaml

    text = (
        "```yaml\n"
        "transitions:\n"
        "  - id: T-003\n"
        "    guard: !recognized\n"
        "    action: *nothing\n"
        "  - {id: T-004, guard: !valid, dst: IDLE}\n"
        "```"
    )
    trs = extract_yaml(text)["transitions"]
    assert trs[0]["guard"] == "!recognized"
    assert trs[0]["action"] == "*nothing"
    assert trs[1]["guard"] == "!valid"


def test_yaml_repair_leaves_valid_yaml_alone():
    """멀쩡한 YAML은 건드리지 않는다."""
    from factory.yamlfix import repair

    src = 'a: 1\nb: "!quoted"\nc: [1, 2]\nd: |\n  literal\n'
    assert repair(src) == src


# --------------------------------------------------------------------------
# DRIVER 단계 — 라이브러리를 실행 가능한 프로그램으로
# --------------------------------------------------------------------------

def test_driver_makes_the_library_runnable(tmp_path):
    """전이를 다 구현해도 진입점이 없으면 '실행'할 수 없다. DRIVER 단계가 그 간극을 메운다."""
    ctx = make_ctx(tmp_path, make_worker())
    assert Orchestrator(ctx).run() is Phase.COMPLETE

    main_py = ctx.cfg.repo / "app" / "__main__.py"
    assert main_py.exists(), "진입점이 생성되어야 한다"

    from factory.stages import entry
    ok, out = entry.smoke_run(ctx, ["INC", "INC", "RESET"])
    assert ok, f"생성된 프로그램이 실행되지 않는다:\n{out}"
    assert "최종: state=ZERO n=0" in out, out


def test_driver_does_not_touch_domain_code(tmp_path):
    """진입점 생성이 도메인 코드를 오염시키면 되돌린다.

    껍데기를 만들다가 로직을 고치면, 통과했던 DoD 테스트의 근거가 조용히 무너진다.
    """
    ctx = make_ctx(tmp_path, make_worker(GOOD_IMPL, tamper=True))
    Orchestrator(ctx).run()

    impl = (ctx.cfg.repo / "app" / "counter.py").read_text(encoding="utf-8")
    assert impl == GOOD_IMPL, "도메인 코드가 복구되어야 한다"
    assert (ctx.cfg.repo / "app" / "__main__.py").exists(), "진입점 자체는 남아야 한다"


def test_driver_generation_is_idempotent(tmp_path):
    """이미 진입점이 있으면 다시 만들지 않는다 (사람이 손댄 것을 덮어쓰지 않게)."""
    worker = make_worker()
    ctx = make_ctx(tmp_path, worker)
    Orchestrator(ctx).run()

    main_py = ctx.cfg.repo / "app" / "__main__.py"
    main_py.write_text(DRIVER_CODE + "\n# 사람이 손댄 줄\n", encoding="utf-8")

    from factory.stages import entry
    entry.generate_driver(ctx)
    assert "사람이 손댄 줄" in main_py.read_text(encoding="utf-8")
    assert sum(1 for c in worker.calls if c.label == "driver") == 1


def _load_spec(path: Path):
    """생성된 app/_fsm_spec.py 를 모듈로 읽어들인다."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_fsm_spec_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_spec_table_distinguishes_three_reasons_for_nothing_happening(tmp_path):
    """'아무 일도 안 일어남'의 세 가지 이유를 구별한다.

    뭉뚱그리면 진단이 쓸모없어진다:
      명세에도 없다 (사용자 잘못) / 아직 안 지었다 (공장 진행 중) / guard가 거짓이다 (정상 동작)
    """
    from factory.stages import entry

    ctx = make_ctx(tmp_path, make_worker())
    ctx.cfg.limits.max_tickets_per_run = 1
    Orchestrator(ctx).run()                       # T-001 만 done, 나머지 todo

    spec = _load_spec(entry.write_fsm_spec(ctx))

    # 1) 명세에도 없는 조합 -- ZERO 상태에서 RESET 은 FSM에 없다
    assert "명세에도 없는" in spec.diagnose("ZERO", "RESET", moved=False)

    # 2) 명세엔 있으나 아직 안 지었다 -- 티켓 번호와 상태를 짚어야 한다
    msg = spec.diagnose("NONZERO", "RESET", moved=False)
    assert "아직 구현되지 않았습니다" in msg
    assert "T-003" in msg and "todo" in msg

    # 3) 구현은 됐는데 guard 가 거짓
    msg = spec.diagnose("ZERO", "INC", moved=False)
    assert "구현되어 있습니다" in msg and "T-001" in msg

    # 상태가 실제로 움직였으면 아무 말도 하지 않는다
    assert spec.diagnose("ZERO", "INC", moved=True) == ""


def test_spec_table_is_generated_not_transcribed(tmp_path):
    """전이 표는 fsm.yaml 에서 기계적으로 나온다 -- 모델이 베껴 쓰지 않는다."""
    from factory.stages import entry

    ctx = make_ctx(tmp_path, make_worker())
    Orchestrator(ctx).run()
    spec = _load_spec(entry.write_fsm_spec(ctx))

    fsm = ctx.store.load_fsm()
    assert len(spec.TRANSITIONS) == len(fsm.transitions) == 3
    assert {t["id"] for t in spec.TRANSITIONS} == {t.id for t in fsm.transitions}
    assert spec.INITIAL == "ZERO"
    assert set(spec.EVENTS) == {"INC", "RESET"}
    # 모든 전이가 티켓으로 역추적된다
    assert all(t["ticket"] for t in spec.TRANSITIONS)


# --------------------------------------------------------------------------
# E2E_SPEC 단계 — 유스케이스로부터 시나리오 테스트
# --------------------------------------------------------------------------

def test_e2e_scenarios_generated_and_run(tmp_path):
    """유스케이스에서 E2E 시나리오가 나오고, 파이프라인이 실제로 돌린다."""
    ctx = make_ctx(tmp_path, make_worker())
    assert Orchestrator(ctx).run() is Phase.COMPLETE

    e2e = ctx.cfg.repo / "tests" / "e2e" / "test_scenarios.py"
    assert e2e.exists()

    from factory.stages import build, deliver
    out = deliver.run_e2e(ctx)
    assert out is not None and out.passed, out.output if out else "E2E가 건너뛰어졌다"
    # 단위 테스트와 E2E가 둘 다 살아 있어야 한다
    assert build.run_tests(ctx).passed


def test_e2e_catches_what_unit_tests_cannot(tmp_path):
    """진입점이 깨지면 단위 테스트는 전부 통과해도 E2E가 잡는다.

    이 성질이 없으면 E2E 계층은 단위 테스트의 중복일 뿐이다.
    """
    from factory.stages import build, deliver

    ctx = make_ctx(tmp_path, make_worker())
    Orchestrator(ctx).run()

    (ctx.cfg.repo / "app" / "__main__.py").write_text(
        'raise SystemExit("진입점 고장")\n', encoding="utf-8")

    assert build.run_tests(ctx, nodes=["tests/test_fsm.py"]).passed, "단위 테스트는 눈치채지 못한다"
    assert not deliver.run_e2e(ctx).passed, "E2E는 잡아야 한다"


def test_e2e_generation_cannot_loosen_unit_tests(tmp_path):
    """E2E를 통과시키려고 단위 테스트를 느슨하게 만드는 것을 막는다."""
    ctx = make_ctx(tmp_path, make_worker(GOOD_IMPL, tamper=True))
    Orchestrator(ctx).run()

    body = (ctx.cfg.repo / "tests" / "test_fsm.py").read_text(encoding="utf-8")
    assert body == SPEC_TEST_CODE, "단위 테스트가 원본 그대로여야 한다"


# --------------------------------------------------------------------------
# YAML 불리언 강제 변환 — 실제로 FSM 상태 이름을 통째로 날린 버그
# --------------------------------------------------------------------------

def test_boolish_identifiers_survive_yaml_parsing():
    """`id: OFF` 는 유효한 YAML이라 파싱 오류가 안 나고 조용히 False 가 된다.

    실제로 겪은 사고: 자판기 FSM의 초기 상태 이름이 OFF 였는데 id=False 로 파싱되어
    상태 이름이 사라졌다. 오류가 안 나므로 '실패 시 복구' 방식으로는 잡을 수 없다.
    """
    from factory.artifacts import extract_yaml

    d = extract_yaml(
        "states:\n"
        "  - id: OFF\n"
        "    initial: true\n"
        "    final: false\n"
        "  - {id: ON, initial: false, final: false}\n"
        "events:\n"
        "  - id: YES\n"
        "transitions:\n"
        "  - {id: T-1, src: OFF, event: YES, dst: ON}\n"
    )
    assert [s["id"] for s in d["states"]] == ["OFF", "ON"]
    assert d["events"][0]["id"] == "YES"
    assert (d["transitions"][0]["src"], d["transitions"][0]["dst"]) == ("OFF", "ON")
    # 진짜 불리언 필드는 건드리면 안 된다
    assert d["states"][0]["initial"] is True
    assert d["states"][0]["final"] is False


def test_ordinary_yaml_is_untouched_by_proactive_repair():
    """복구를 항상 돌리므로, 멀쩡한 값이 바뀌지 않는지 확인한다."""
    from factory.artifacts import extract_yaml

    d = extract_yaml("a: 1\nb: true\nc: [1, 2]\nenabled: yes\nguard: \"x > 0\"\n")
    assert d == {"a": 1, "b": True, "c": [1, 2], "enabled": True, "guard": "x > 0"}


def test_validator_rejects_non_string_identifiers():
    """파싱이 깨져도 티켓이 되기 전에 막는다 (방어선 2)."""
    from factory.models import FSM, Event, State, Transition

    bad = FSM("x", [State(False, initial=True), State("A")], [Event("E")],
              [Transition("T-1", False, "E", "A")])
    errs = " | ".join(bad.validate())
    assert "문자열이 아님" in errs


# --------------------------------------------------------------------------
# 프롬프트 크기와 중단 복구 — 전이 42개짜리 실행에서 실제로 터진 것들
# --------------------------------------------------------------------------

def test_dod_test_extraction_is_bounded(tmp_path):
    """티켓 프롬프트에 테스트 파일 '전체'가 아니라 해당 함수만 들어가야 한다.

    실제로 터진 버그: 전이 42개 시점에 test_fsm.py 가 36KB 가 되었고,
    티켓마다 그걸 통째로 프롬프트에 넣어 Windows 명령줄 상한(32767자)을 넘겨 죽었다.
    """
    from factory.stages.planning import _read_dod_tests

    ctx = make_ctx(tmp_path, make_worker())
    Orchestrator(ctx).run()

    story = next(s for s in ctx.tracker.search(type=TicketType.STORY.value)
                 if s.fsm_ref == "T-002")
    snippet = _read_dod_tests(ctx, story)
    full = (ctx.cfg.repo / "tests" / "test_fsm.py").read_text(encoding="utf-8")

    assert "def test_t_002" in snippet, "자기 테스트는 들어가야 한다"
    assert "def test_t_003" not in snippet, "남의 테스트까지 넣으면 안 된다"
    assert "from app.counter import Counter" in snippet, "import 는 있어야 의존 대상을 안다"
    assert len(snippet) < len(full), "파일 전체보다 작아야 한다"


def test_interrupted_ticket_is_requeued(tmp_path):
    """프로세스가 티켓 처리 중 죽으면 그 티켓이 유실되지 않아야 한다.

    _next_story 는 todo 만 집으므로, in_progress 로 남은 티켓은 영영 처리되지 않는다.
    """
    ctx = make_ctx(tmp_path, make_worker())
    ctx.cfg.limits.max_tickets_per_run = 1
    Orchestrator(ctx).run()                      # 1건 done, 나머지 todo

    victim = ctx.tracker.search(type=TicketType.STORY.value, status=Status.TODO.value)[0]
    ctx.tracker.transition(victim.key, Status.IN_PROGRESS.value, "크래시 흉내")

    ctx.cfg.limits.max_tickets_per_run = 0
    Orchestrator(ctx).run()

    assert ctx.tracker.get(victim.key).status == Status.DONE.value, "중단된 티켓이 유실됐다"
    assert all(s.status == Status.DONE.value
               for s in ctx.tracker.search(type=TicketType.STORY.value))


def test_prompt_loader_survives_code_in_templates():
    """프롬프트에 든 코드 예제가 자리표시자로 오해되면 안 된다.

    str.format 을 쓰던 시절 `{(t["src"], t["event"]) for t in TRANSITIONS}` 가
    치환 필드로 해석되어 파이프라인 전체가 TypeError 로 죽었다.
    """
    from factory.context import prompt

    # 실제 템플릿들이 코드를 담은 채로 모두 로드되는가
    for name, kw in [
        ("fsm", dict(project="vm", usecases_yaml="<UC>")),
        ("e2e_spec", dict(e2e_path="t.py", usecases_yaml="<UC>", fsm_yaml="<FSM>")),
        ("driver", dict(fsm_yaml="<FSM>", interface="<I>")),
        ("decompose", dict(tid="T-1", src="A", dst="B", event="E",
                           guard="g", action="a", usecase="u", test_code="c")),
    ]:
        text = prompt(name, **kw)
        assert text and "{" in text or True     # 로드 자체가 예외 없이 끝나야 한다

    e2e = prompt("e2e_spec", e2e_path="t.py", usecases_yaml="<UC>", fsm_yaml="<FSM>")
    assert 'for t in TRANSITIONS}' in e2e, "코드 예제가 소실됐다"
    assert "random.Random(42)" in e2e

    fsm = prompt("fsm", project="vm", usecases_yaml="<UC>")
    assert "payload: {amount: int}" in fsm, "YAML 예제의 중괄호가 깨졌다"
    assert "project: vm" in fsm, "자리표시자가 치환되지 않았다"

    # 값을 안 준 자리표시자는 그대로 남아야 한다 (조용히 사라지면 프롬프트가 망가진다)
    assert "{max_attempts}" in prompt("debug", attempt=1)


# --------------------------------------------------------------------------
# REVIEW 단계 — 검토 -> 수정 -> 재검토, 백로그가 빌 때까지
# --------------------------------------------------------------------------

CRITIC_TWO = """
checked:
  - 요구사항-테스트 추적을 훑음
findings:
  - id: F-01
    severity: high
    category: vacuous-test
    title: 초기화 테스트가 상태만 보고 값은 확인하지 않는다
    evidence: tests/test_fsm.py::test_t_003 이 state 만 단언한다
    impact: reset 이 값을 안 지워도 통과한다
    action: n == 0 단언 추가
    refs: [T-003]
  - id: F-02
    severity: high
    category: missing-rule
    title: 카운터 상한이 문서에 없다
    evidence: requirements.md 에 최대값 규정이 없다
    impact: 무한히 증가한다
    action: 상한 요구사항을 추가할 것
    refs: [REQ-01]
"""

CRITIC_NONE = """
checked:
  - 다시 훑었으나 새로 나온 것 없음
findings: []
"""


def make_critic_worker(scripts):
    """검토 응답을 회차별로 미리 정해 둔 워커."""
    w = make_worker()
    calls = {"n": 0}

    def critic_handler(t):
        i = min(calls["n"], len(scripts) - 1)
        calls["n"] += 1
        return WorkerResult(ok=True, text=scripts[i])

    def remediate_handler(t):
        return WorkerResult(ok=True, text="수정 완료")

    w.register("critic", critic_handler)
    w.register("remediate_test", remediate_handler)
    w.register("remediate_code", remediate_handler)
    return w


def test_review_files_findings_as_tickets(tmp_path):
    """지적은 로그가 아니라 백로그 티켓이 되어야 한다."""
    from factory.stages import remediate

    ctx = make_ctx(tmp_path, make_critic_worker([CRITIC_TWO, CRITIC_NONE, CRITIC_NONE]))
    assert Orchestrator(ctx).run() is Phase.COMPLETE

    critic_tickets = ctx.tracker.search(label="critic")
    assert len(critic_tickets) == 2
    assert all(t.type == TicketType.BUG.value for t in critic_tickets)
    assert all("needs-human" in t.labels for t in critic_tickets)
    # 분류가 라벨에 실려야 라우팅이 가능하다
    assert {"vacuous-test", "missing-rule"} <= {l for t in critic_tickets for l in t.labels}


def test_review_routes_by_who_can_fix_it(tmp_path):
    """기계가 닫을 수 있는 것과 사람이 정해야 하는 것을 갈라야 한다."""
    from factory.stages import remediate

    ctx = make_ctx(tmp_path, make_critic_worker([CRITIC_TWO, CRITIC_NONE, CRITIC_NONE]))
    Orchestrator(ctx).run()

    by_title = {t.title: t for t in ctx.tracker.search(label="critic")}
    vac = next(t for k, t in by_title.items() if "vacuous-test" in t.labels)
    mis = next(t for k, t in by_title.items() if "missing-rule" in t.labels)

    assert remediate.route(vac) == "test"
    assert remediate.route(mis) == "human"
    assert vac.status == Status.DONE.value, "기계가 닫을 수 있는 지적은 닫혀야 한다"
    assert mis.status == Status.BLOCKED.value, "요구사항 결정은 기계가 하면 안 된다"


def test_review_terminates_when_critic_keeps_complaining(tmp_path):
    """검토가 매번 새 지적을 내도 루프는 끝나야 한다.

    종료 조건이 없으면 이 단계는 영원히 돈다.
    """
    endless = [CRITIC_TWO.replace("F-01", f"F-{i:02d}").replace(
        "초기화 테스트가", f"{i}회차 검토 - 초기화 테스트가") for i in range(1, 9)]
    ctx = make_ctx(tmp_path, make_critic_worker(endless))
    ctx.cfg.limits.critic_cycles = 3

    assert Orchestrator(ctx).run() is Phase.COMPLETE, "상한에 걸려도 파이프라인은 끝나야 한다"
    assert not [t for t in ctx.tracker.search(label="critic")
                if t.status not in (Status.DONE.value, Status.BLOCKED.value)], \
        "열린 채로 남은 지적이 있으면 백로그가 비지 않는다"


def test_review_is_clean_when_nothing_is_wrong(tmp_path):
    """지적이 없으면 티켓도 없어야 한다. 억지로 채우지 않는다."""
    ctx = make_ctx(tmp_path, make_critic_worker([CRITIC_NONE]))
    assert Orchestrator(ctx).run() is Phase.COMPLETE
    assert not ctx.tracker.search(label="critic")


def test_critic_drops_findings_without_evidence(tmp_path):
    """근거 없는 지적은 티켓이 되지 않는다. 인상비평은 아무도 안 읽는다."""
    vague = """
findings:
  - id: F-01
    severity: high
    category: vacuous-test
    title: 전반적으로 테스트가 약해 보인다
  - id: F-02
    severity: low
    category: vacuous-test
    title: 초기화 테스트가 값을 확인하지 않는다
    evidence: tests/test_fsm.py::test_t_003
"""
    ctx = make_ctx(tmp_path, make_critic_worker([vague, CRITIC_NONE, CRITIC_NONE]))
    Orchestrator(ctx).run()
    titles = [t.title for t in ctx.tracker.search(label="critic")]
    assert len(titles) == 1, f"근거 없는 지적이 티켓이 됐다: {titles}"
    assert "초기화" in titles[0]


def test_yaml_repair_handles_colons_in_free_text():
    """제목이나 근거에 `: ` 가 들어가도 파싱이 깨지지 않아야 한다.

    모델은 "1번째 지적: 테스트가 약하다" 같은 제목을 흔히 쓴다.
    따옴표가 없으면 YAML이 매핑으로 착각해 응답 전체가 버려진다.
    """
    from factory.artifacts import extract_yaml

    d = extract_yaml(
        "findings:\n"
        "  - id: F-01\n"
        "    title: 1번째 지적: 초기화 테스트가 값을 확인하지 않는다\n"
        "    evidence: tests/test_fsm.py::test_t_003 이 state 만 본다\n"
        "    impact: reset 이 값을 안 지워도 통과: 문제다\n"
    )
    f = d["findings"][0]
    assert f["title"] == "1번째 지적: 초기화 테스트가 값을 확인하지 않는다"
    assert f["impact"] == "reset 이 값을 안 지워도 통과: 문제다"
    assert f["id"] == "F-01"


def test_yaml_repair_handles_colons_in_sequence_items():
    """`- 서술문: 설명` 형태의 리스트 항목도 살려야 한다.

    검토자의 checked 목록이 이 형태다. 매핑 항목(`- id: T-001`)은 건드리면 안 된다.
    """
    from factory.artifacts import extract_yaml
    from factory.yamlfix import repair

    d = extract_yaml(
        "checked:\n"
        "  - REQ-01~38 을 추적했다: 유스케이스와 전이 모두\n"
        "  - tests/e2e/test_scenarios.py::test_uc02 가 socket 을 막는다\n"
        "findings:\n"
        "  - id: F-01\n"
        "    title: 제목에도 콜론: 들어간다\n"
        "    evidence: tests/test_fsm.py::test_t_003\n"
        "    refs: [REQ-12, T-014]\n"
    )
    assert d["checked"][0] == "REQ-01~38 을 추적했다: 유스케이스와 전이 모두"
    assert "::test_uc02" in d["checked"][1]
    assert d["findings"][0]["title"] == "제목에도 콜론: 들어간다"
    assert d["findings"][0]["refs"] == ["REQ-12", "T-014"], "매핑·리스트 구조가 보존돼야 한다"

    # 매핑 항목은 그대로
    src = "- id: T-001\n- {id: T-2, src: A}\n"
    assert repair(src) == src


def test_every_yaml_stage_saves_raw_response(tmp_path):
    """파싱이 깨져도 원문은 남아야 한다. 몇 분짜리 호출을 통째로 버리면 안 된다."""
    from factory.context import ask_yaml

    ctx = make_ctx(tmp_path, make_worker())
    ctx.worker.register("probe", lambda t: WorkerResult(ok=True, text="a: 1\n"))
    ask_yaml(ctx, "probe", "무엇이든")

    raw = ctx.cfg.workspace / ".factory" / "raw" / "probe.1.txt"
    assert raw.exists() and raw.read_text(encoding="utf-8").strip() == "a: 1"


def test_unparseable_response_is_retried_once(tmp_path):
    """YAML을 못 얻으면 오류를 되돌려주며 한 번 더 묻는다."""
    from factory.context import ask_yaml

    ctx = make_ctx(tmp_path, make_worker())
    calls = {"n": 0}

    def flaky(t):
        calls["n"] += 1
        return WorkerResult(ok=True, text="설명만 있고 YAML이 없음" if calls["n"] == 1 else "ok: 1\n")

    ctx.worker.register("flaky", flaky)
    assert ask_yaml(ctx, "flaky", "요청") == {"ok": 1}
    assert calls["n"] == 2, "한 번 더 물어야 한다"
    assert (ctx.cfg.workspace / ".factory" / "raw" / "flaky.1.txt").exists(), "실패한 원문도 남아야 한다"


def test_review_survives_a_dying_worker(tmp_path):
    """검토 도중 워커가 죽어도 이미 완성된 빌드를 무너뜨리면 안 된다.

    실제로 겪은 일: 3회차 검토에서 워커가 exit 1 로 죽자 파이프라인 전체가
    예외로 끝났다. 그때까지 고친 5건도 함께 실패로 보고됐다.
    """
    scripts = [CRITIC_TWO]
    w = make_critic_worker(scripts)

    calls = {"n": 0}

    def dying_critic(t):
        calls["n"] += 1
        if calls["n"] == 1:
            return WorkerResult(ok=True, text=CRITIC_TWO)
        return WorkerResult(ok=False, error="exit 1: ")      # 한도 초과 흉내

    w.register("critic", dying_critic)
    ctx = make_ctx(tmp_path, w)

    # 워커가 죽으면(사용량 한도) 실행은 REVIEW 에서 멈춘다 -- COMPLETE 로 위장하지도,
    # 예외로 터지지도 않는다. 죽은 워커는 이 실행 안에서 돌아오지 않으므로 재개가 맞다.
    assert Orchestrator(ctx).run() is Phase.REVIEW, "워커가 죽으면 REVIEW 에서 멈춰 재개를 기다려야 한다"
    # 1회차에 나온 지적과 그 수정은 살아 있어야 한다
    assert ctx.tracker.search(label="critic"), "죽기 전에 나온 지적이 사라졌다"
    fixed = [t for t in ctx.tracker.search(label="critic") if t.status == Status.DONE.value]
    assert fixed, "죽기 전에 고친 것이 유실됐다"

    # 워커가 돌아오면 그 자리에서 이어진다
    w.register("critic", lambda t: WorkerResult(ok=True, text=CRITIC_NONE))
    assert Orchestrator(ctx).run() is Phase.COMPLETE, "재개하면 끝까지 가야 한다"


def test_yaml_repair_covers_description_field():
    """`description` 안의 `: ` 로 파이프라인이 죽은 적이 있다 (v3, 티켓 10/60에서)."""
    from factory.artifacts import extract_yaml

    d = extract_yaml(
        "subtasks:\n"
        "  - title: 전이를 추가한다\n"
        "    description: 자기 전이를 추가한다 (유스케이스 1b: 누적 금액 0이면 아무 일도 없음).\n"
    )
    assert d["subtasks"][0]["description"].endswith("아무 일도 없음).")


def test_no_stage_bypasses_ask_yaml():
    """YAML을 받는 모든 단계는 ask_yaml 을 써야 한다.

    원문 보존·재질의가 없는 단계가 하나라도 남으면 거기서 몇 분짜리 응답이 통째로 날아가고
    파이프라인이 죽는다. decompose 가 정확히 그렇게 빠져 있었다.
    """
    import re
    from pathlib import Path

    stages = Path(__file__).resolve().parents[1] / "factory" / "stages"
    offenders = [
        p.name for p in stages.glob("*.py")
        if re.search(r"\bextract_yaml\(", p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"ask_yaml 을 우회하는 단계: {offenders}"


# --------------------------------------------------------------------------
# 인프라 실패 vs 코드 실패 — 사용량 한도로 멀쩡한 티켓이 BLOCKED 된 사고
# --------------------------------------------------------------------------

def test_worker_outage_stops_the_run_instead_of_blocking_tickets(tmp_path):
    """워커가 실행조차 안 되면(exit 1, stderr 비어 있음) 그건 티켓의 잘못이 아니다.

    실제로 겪은 일: 사용량 한도에 걸린 워커가 6초 안에 3번 'exit 1: ' 을 냈고,
    루프는 그걸 구현 실패 3회로 세어 티켓을 BLOCKED 하고 가짜 BUG 티켓을 만들었다.
    그 다음 티켓에서 파이프라인이 통째로 죽었다.
    """
    w = make_worker()
    calls = {"n": 0}

    def outage(t):
        calls["n"] += 1
        return WorkerResult(ok=False, error="exit 1: ")       # 한도 초과의 실제 모양

    w.register("implement", outage)
    w.register("debug", outage)
    ctx = make_ctx(tmp_path, w)

    stopped = Orchestrator(ctx).run()
    assert stopped is Phase.WORK, "실행이 WORK 에서 멈춰야 한다 (COMPLETE 도 예외도 아님)"

    stories = ctx.tracker.search(type=TicketType.STORY.value)
    assert not [s for s in stories if s.status == Status.BLOCKED.value], "멀쩡한 티켓이 BLOCKED 됐다"
    assert not ctx.tracker.search(type=TicketType.BUG.value), "가짜 BUG 티켓이 생겼다"
    assert calls["n"] == 1, f"워커가 죽었는데 {calls['n']}번이나 다시 불렀다 -- 재시도 예산을 태운다"
    assert all(s.status == Status.TODO.value for s in stories), "재개할 수 있게 todo 로 남아야 한다"
    assert all(s.attempts == 0 for s in stories), "실행조차 안 된 시도가 횟수로 세어졌다"


def test_real_code_failure_still_retries_and_blocks(tmp_path):
    """반대로 워커가 일을 했는데 실패한 것은 여전히 재시도하고 BLOCKED 해야 한다.

    인프라 판정이 너무 넓으면 진짜 결함이 '워커 문제'로 위장해 영원히 todo 에 남는다.
    """
    ctx = make_ctx(tmp_path, make_worker(BAD_IMPL))
    Orchestrator(ctx).run()
    blocked = [s for s in ctx.tracker.search(type=TicketType.STORY.value)
               if s.status == Status.BLOCKED.value]
    assert blocked and all(s.attempts == 3 for s in blocked)


def test_spec_tests_must_match_current_fsm_one_to_one(tmp_path):
    """생성된 테스트 집합은 현재 FSM 전이와 정확히 1:1이어야 한다.

    v4 에서 실제로 겪은 일: 저장소에 v3 테스트(60개)가 남아 있었고 워커가 그걸 베껴서
    56 전이짜리 FSM 에 60개 테스트가 생겼다. 존재하지 않는 전이의 테스트가 DoD 가 되어
    티켓 21건이 SPEC_CONFLICT 로 BLOCKED 됐다.
    """
    from factory.stages import build

    w = make_worker()
    def stale_tests(t):
        p = t.cwd / "tests" / "test_fsm.py"; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SPEC_TEST_CODE + "\n\ndef test_t_999():\n    pass\n", encoding="utf-8")   # 유령 전이
        return WorkerResult(ok=True, text="ok")
    w.register("spec_test", stale_tests)
    ctx = make_ctx(tmp_path, w)
    ctx.cfg.gates.fsm = "auto"

    with pytest.raises(RuntimeError, match="현재 FSM과 맞지 않습니다"):
        Orchestrator(ctx).run()


def test_remediation_cannot_close_a_finding_with_xfail(tmp_path):
    """xfail/skip 을 붙여 초록을 만든 것은 해소가 아니다.

    실제로 겪은 일: "fsync 가 구현도 검증도 되지 않았다"는 지적이, fsync 를 구현하는 대신
    구현되지 않았음을 xfail 테스트로 문서화하는 것으로 닫혔다. 티켓은 done 이 됐고
    결함은 그대로 남았다. 다음 검토 사이클이 같은 결함을 다시 잡았다.
    """
    w = make_critic_worker([CRITIC_TWO, CRITIC_NONE, CRITIC_NONE])

    def waive_instead_of_fixing(t):
        p = t.cwd / "tests" / "test_fsm.py"
        src = p.read_text(encoding="utf-8")
        # skip 은 언제나 초록이다 -- "전체 통과했으니 해소"라는 판정을 정확히 노린다.
        p.write_text(
            "import pytest\n" + src.replace(
                "def test_t_003():",
                '@pytest.mark.skip(reason="아직 구현 안 됨")\ndef test_t_003():'),
            encoding="utf-8")
        return WorkerResult(ok=True, text="문서화 완료")

    w.register("remediate_test", waive_instead_of_fixing)
    ctx = make_ctx(tmp_path, w)
    Orchestrator(ctx).run()

    vac = next(t for t in ctx.tracker.search(label="critic") if "vacuous-test" in t.labels)
    assert vac.status != Status.DONE.value, "면제로 닫힌 지적이 해소로 기록됐다"
    audit = (ctx.cfg.workspace / "backlog" / "comments.log").read_text(encoding="utf-8")
    assert "xfail" in audit, "면제 사실이 감사 로그에 남아야 한다"


# --------------------------------------------------------------------------
# 비용 기록 — 공장 개선의 효과를 재는 유일한 수단
# --------------------------------------------------------------------------

def _metered(ctx):
    """테스트용 Ctx 의 워커를 계량 껍데기로 감싼다 (실제로는 build_ctx 가 한다)."""
    from factory.worker.metered import MeteredWorker
    inner = ctx.worker
    m = MeteredWorker(inner, ctx.cfg.workspace)
    m.log = ctx.log
    ctx.worker = m
    return inner


def test_every_worker_call_is_recorded(tmp_path):
    """단계마다 따로 붙이면 반드시 빠뜨리는 곳이 생긴다. 단일 관문에서 전부 잡혀야 한다."""
    from factory import cost

    ctx = make_ctx(tmp_path, make_worker())
    inner = _metered(ctx)
    Orchestrator(ctx).run()

    rows = cost.load(ctx.cfg.workspace)
    assert len(rows) == len(inner.calls), "기록 수와 실제 호출 수가 다르다"
    assert {r["stage"] for r in rows} >= {"elicit", "usecase", "fsm", "implement", "critic"}
    # 티켓·시도 번호는 떼고 단계만 남아야 집계가 된다
    assert all(":" not in r["stage"] for r in rows)
    assert all("phase" in r and "ok" in r and "total_tokens" in r for r in rows)


def test_cost_summary_reports_per_unit_numbers(tmp_path):
    """절대 토큰은 프로젝트 크기에 비례한다. 비교 가능한 건 단위당 값뿐이다."""
    from factory import cost

    ctx = make_ctx(tmp_path, make_worker())
    _metered(ctx)
    Orchestrator(ctx).run()

    s = cost.summarize(ctx.cfg.workspace, transitions=3)
    assert s["calls"] > 0
    assert "tokens_per_transition" in s["derived"]
    assert s["by_stage"]["implement"]["calls"] >= 3
    for v in s["by_stage"].values():
        assert v["max"] >= v["median"]


def test_outlier_alarm_uses_ratio_not_absolute(tmp_path):
    """폭주 감지는 배수로 한다 -- 절대 상한이면 큰 프로젝트가 통째로 걸린다."""
    from factory import cost

    ws = tmp_path / "ws"
    (ws / ".factory").mkdir(parents=True)
    line = lambda label, n: json.dumps(
        {"ts": "", "phase": "WORK", "stage": "implement", "label": label, "ok": True,
         "duration_s": 1.0, "cost_usd": 0.0,
         "tokens": {"input_tokens": n, "output_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
         "total_tokens": n}, ensure_ascii=False)

    p = ws / ".factory" / "cost.jsonl"
    # 평범한 호출 10개 -- 프로젝트가 크든 작든 이 중앙값은 비슷하다
    p.write_text("\n".join(line(f"implement:T{i}", 100_000) for i in range(10)) + "\n",
                 encoding="utf-8")
    assert cost.check_outlier(ws, "implement:T0") is None, "정상 호출이 걸렸다"

    with p.open("a", encoding="utf-8") as fh:
        fh.write(line("implement:RUNAWAY", 900_000) + "\n")
    out = cost.check_outlier(ws, "implement:RUNAWAY")
    assert out and out.ratio >= 5.0, "중앙값의 9배가 안 걸렸다"

    # 표본이 적으면 중앙값을 못 믿으므로 판정하지 않는다
    ws2 = tmp_path / "ws2"; (ws2 / ".factory").mkdir(parents=True)
    (ws2 / ".factory" / "cost.jsonl").write_text(
        line("implement:A", 100) + "\n" + line("implement:B", 99_000_000) + "\n",
        encoding="utf-8")
    assert cost.check_outlier(ws2, "implement:B") is None, "표본 2개로 판정했다"


# --------------------------------------------------------------------------
# SIMULATE — 구현 전에 명세를 실행해 원칙 위반을 잡는다
# --------------------------------------------------------------------------

def _fsm(transitions, states=None, events=None):
    from factory.models import FSM, Event, State, Transition
    ss = states or [("A", True), ("B", False)]
    return FSM("t",
               [State(i, initial=init) for i, init in ss],
               [Event(e) for e in (events or sorted({t[2] for t in transitions}))],
               [Transition(*t[:4], action=t[4] if len(t) > 4 else "",
                           guard=t[5] if len(t) > 5 else "")
                for t in transitions])


def test_simulate_catches_money_with_no_way_back():
    """P1. 잔액을 보유하는데 돌려줄 길이 없는 상태를 찾는다."""
    from factory import simulate

    bad = _fsm([("T-1", "A", "COIN", "B", "credit += amount"),
                ("T-2", "B", "COIN", "B", "credit += amount")])
    fs = [f for f in simulate.run_all(bad, []) if f.principle == "P1"]
    assert fs and "B" in fs[0].refs, "돈이 갇힌 상태를 못 찾았다"

    ok = _fsm([("T-1", "A", "COIN", "B", "credit += amount"),
               ("T-2", "B", "LEVER", "A", "refund(credit)")])
    assert not [f for f in simulate.run_all(ok, []) if f.principle == "P1"]


def test_simulate_catches_failure_returning_to_selling():
    """P4. v3~v5 가 실제로 갖고 있던 결함 — 배출 실패 후 판매 상태 복귀.

    사용자가 v6 에서야 지적한 것을, 구현 42~78건을 만들기 전에 잡아야 한다.
    """
    from factory import simulate

    bad = _fsm([("T-1", "A", "INSERT_COIN", "A", "credit += amount"),
                ("T-2", "A", "PRESS", "B", "start_dispense()"),
                ("T-3", "B", "DISPENSE_TIMEOUT", "A", "refund(credit)")])
    fs = [f for f in simulate.run_all(bad, []) if f.principle == "P4"]
    assert fs and "T-3" in fs[0].refs

    # 고장 상태로 가면 지적이 없어야 한다
    good = _fsm([("T-1", "A", "INSERT_COIN", "A", "credit += amount"),
                 ("T-2", "A", "PRESS", "B", "start_dispense()"),
                 ("T-3", "B", "DISPENSE_TIMEOUT", "F", "refund(credit)"),
                 ("T-4", "F", "ADMIN_KEY", "A", "clear_fault()")],
                states=[("A", True), ("B", False), ("F", False)])
    assert not [f for f in simulate.run_all(good, []) if f.principle == "P4"]


def test_simulate_does_not_flag_idle_timeout_as_hardware_failure():
    """무조작 타임아웃은 정상 흐름이다. 이걸 고장으로 잡으면 노이즈가 되어 아무도 안 읽는다."""
    from factory import simulate

    f = _fsm([("T-1", "A", "INSERT_COIN", "A", "credit += amount"),
              ("T-2", "A", "IDLE_TIMEOUT_30S", "A", "refund(credit)")])
    assert not [x for x in simulate.run_all(f, []) if x.principle == "P4"]


def test_simulate_replays_usecase_event_sequences():
    """유스케이스가 선언한 이벤트 열을 FSM 이 실제로 밟는가."""
    from factory import simulate
    from factory.models import UseCase

    f = _fsm([("T-1", "A", "COIN", "B"), ("T-2", "B", "PRESS", "A")])
    ok = UseCase(id="UC-01", title="구매", actor="고객",
                 main_flow=[{"step": "1. 코인", "event": "COIN"},
                            {"step": "2. 버튼", "event": "PRESS"}])
    assert not simulate.replay_usecases(f, [ok])

    bad = UseCase(id="UC-02", title="반환", actor="고객",
                  main_flow=[{"step": "1. 코인", "event": "COIN"},
                             {"step": "2. 레버", "event": "LEVER"}])
    fs = simulate.replay_usecases(f, [bad])
    assert fs and "LEVER" in fs[0].refs, "FSM 이 구현하지 않은 유스케이스를 못 잡았다"


def test_simulate_findings_are_candidates_not_verdicts(tmp_path):
    """모델 검사가 파이프라인을 멈춰선 안 된다 -- 위반 후보이지 결함 단정이 아니다."""
    ctx = make_ctx(tmp_path, make_worker())
    # 카운터 FSM 은 잔액 개념이 없어 P1 이 안 걸리고, 유스케이스에 event 표시가 없어
    # 재생 불가 지적이 난다. 그래도 파이프라인은 끝까지 가야 한다.
    assert Orchestrator(ctx).run() is Phase.COMPLETE


# --------------------------------------------------------------------------
# RELEASE — 루프를 끝내는 자리
# --------------------------------------------------------------------------

def test_release_gate_blocks_on_open_high_findings(tmp_path):
    """high 지적이 열려 있으면 출시를 막는다. 이게 무한루프를 끊는 조건이다."""
    from factory import release

    ctx = make_ctx(tmp_path, make_critic_worker([CRITIC_TWO, CRITIC_NONE, CRITIC_NONE]))
    Orchestrator(ctx).run()

    v = release.judge(ctx)
    named = {c.name: c for c in v.checks}
    assert not named["열린 high 지적 없음"].passed, "미해소 high 가 있는데 통과했다"
    assert not v.released
    assert (ctx.cfg.workspace / "RELEASE.md").exists(), "판정서가 남아야 한다"


def test_release_gate_passes_a_clean_build(tmp_path):
    """지적이 없으면 통과해야 한다. 통과할 수 없는 게이트는 게이트가 아니다."""
    from factory import release

    ctx = make_ctx(tmp_path, make_worker())        # critic 기본값 = 지적 없음
    Orchestrator(ctx).run()

    v = release.judge(ctx)
    blockers = [c.name for c in v.blockers]
    assert v.released, f"깨끗한 빌드가 막혔다: {blockers}"


def test_release_treats_medium_low_as_carried_issues(tmp_path):
    """medium·low 는 출시를 막지 않고 알려진 이슈로 이월된다.

    버그가 0이라서 나가는 게 아니라, 분류되지 않은 항목이 0이라서 나간다.
    """
    from factory import release

    only_low = CRITIC_TWO.replace("severity: high", "severity: low").replace(
        "category: missing-rule", "category: vacuous-test")
    ctx = make_ctx(tmp_path, make_critic_worker([only_low, CRITIC_NONE, CRITIC_NONE]))
    Orchestrator(ctx).run()

    v = release.judge(ctx)
    assert not [c for c in v.blockers if c.name == "열린 high 지적 없음"]
    doc = release.render(v, "t")
    if v.known_issues:
        assert "알려진 이슈" in doc and "다음 버전 요구사항 후보" in doc


def test_release_gate_blocks_unapproved_waivers(tmp_path):
    """면제는 승인된 것만. 승인 파일에 적으면 통과한다."""
    from factory import release

    ctx = make_ctx(tmp_path, make_worker())
    Orchestrator(ctx).run()

    t = ctx.cfg.repo / "tests" / "test_fsm.py"
    t.write_text("import pytest\n" + t.read_text(encoding="utf-8").replace(
        "def test_t_003():", '@pytest.mark.skip(reason="미구현")\ndef test_t_003():'),
        encoding="utf-8")
    assert not release.judge(ctx).released, "미승인 면제가 통과했다"

    waived = [c for c in release.judge(ctx).checks if "xfail" in c.name][0]
    loc = waived.detail.split("미승인: ")[1].split(",")[0].strip()
    ap = ctx.cfg.workspace / ".factory" / "approved_waivers.txt"
    ap.write_text(f"# 사람이 승인한 면제\n{loc}\n", encoding="utf-8")
    assert [c for c in release.judge(ctx).checks if "xfail" in c.name][0].passed


def test_release_dedupes_findings_reported_twice(tmp_path):
    """같은 결함을 다른 문장으로 다시 신고한 것을 합친다.

    7건으로 보이는 게 실은 3건이면 판단이 달라진다.
    """
    from factory import release
    from factory.models import Ticket

    ctx = make_ctx(tmp_path, make_worker())
    body = "근거: T-012 와 REQ-07 을 보라"
    a = ctx.tracker.create(Ticket(type="bug", title="[검토/high] 첫 신고",
                                  description=body, labels=["critic", "missing-rule"]))
    b = ctx.tracker.create(Ticket(type="bug", title="[검토/high] 같은 것을 다시 신고",
                                  description=body + " (표현만 다름)",
                                  labels=["critic", "missing-rule"]))
    merged = release._dedupe([ctx.tracker.get(a.key), ctx.tracker.get(b.key)])
    assert len(merged) == 1, "같은 참조를 가진 중복이 안 합쳐졌다"


def test_simulate_replays_from_precondition_not_initial_state():
    """유스케이스는 사전조건이 만족된 상태에서 시작한다. 초기 상태를 강요하면 안 된다.

    v7 에서 실제로 터진 것: 모든 유스케이스를 POWERED_OFF 에서 재생해
    18개가 전부 거짓 양성으로 나왔다. "코인 투입"은 전원이 켜진 뒤의 이야기다.
    사전조건은 산문이라 기계가 못 읽으므로, 도달 가능한 모든 상태를 출발점으로 둔다.
    """
    from factory import simulate
    from factory.models import UseCase

    # OFF(초기) --POWER_ON--> ON --COIN--> ON
    f = _fsm([("T-1", "OFF", "POWER_ON", "ON"), ("T-2", "ON", "COIN", "ON")],
             states=[("OFF", True), ("ON", False)])
    uc = UseCase(id="UC-01", title="코인 투입", actor="고객",
                 precondition="자판기가 판매 상태이다",
                 main_flow=[{"step": "1. 코인을 넣는다", "event": "COIN"}])
    assert not simulate.replay_usecases(f, [uc]), "전원 켜기를 생략한 유스케이스가 거짓 양성으로 잡혔다"

    # 그래도 어디서도 처리 못 하는 이벤트는 여전히 잡아야 한다
    bad = UseCase(id="UC-02", title="레버", actor="고객",
                  main_flow=[{"step": "1. 레버", "event": "LEVER"}])
    assert simulate.replay_usecases(f, [bad]), "진짜 미구현 이벤트를 놓쳤다"


def test_remediation_cleanup_does_not_delete_generated_files(tmp_path):
    """수정 실패 시 정리가 커밋되지 않은 생성물을 지우면 안 된다.

    실제로 겪은 일: E2E_SPEC 이 만든 시나리오 50개가 추적되지 않은 상태였는데,
    첫 수정 실패의 `git clean` 이 통째로 지웠다. 그 뒤 검토는 "e2e 테스트가 0건"이라고 보고했다.
    """
    from factory.stages import remediate

    ctx = make_ctx(tmp_path, make_worker())
    Orchestrator(ctx).run()

    stray = ctx.cfg.repo / "tests" / "e2e" / "generated_but_uncommitted.py"
    stray.write_text("# E2E_SPEC 이 방금 만든 것\n", encoding="utf-8")
    remediate._discard_worktree(ctx)
    assert stray.exists(), "커밋되지 않은 생성물이 정리에 지워졌다"


def test_generated_artifacts_are_committed(tmp_path):
    """DRIVER·E2E_SPEC 산출물은 생성 직후 커밋되어야 한다 (지워지지 않게)."""
    from factory.stages import deliver

    ctx = make_ctx(tmp_path, make_worker())
    Orchestrator(ctx).run()

    tracked = deliver.git(ctx, "ls-files").stdout
    assert "app/__main__.py" in tracked, "진입점이 커밋되지 않았다"
    assert "tests/e2e/test_scenarios.py" in tracked, "E2E 시나리오가 커밋되지 않았다"


def test_remediation_stops_on_worker_outage(tmp_path):
    """수정 중 워커가 죽으면 재시도 예산을 태우지 말고 멈춰야 한다.

    실제로 겪은 일: 한도 초과 뒤 티켓 5개가 각각 3회씩 헛되이 재시도하고 잘못 BLOCKED 됐다.
    """
    from factory.stages import remediate
    from factory.worker.base import WorkerUnavailable

    w = make_critic_worker([CRITIC_TWO, CRITIC_NONE, CRITIC_NONE])
    calls = {"n": 0}

    def outage(t):
        calls["n"] += 1
        return WorkerResult(ok=False, error="exit 1: ")

    w.register("remediate_test", outage)
    ctx = make_ctx(tmp_path, w)
    Orchestrator(ctx).run()

    assert calls["n"] == 1, f"워커가 죽었는데 {calls['n']}번 불렀다"


def test_timeout_is_a_ticket_failure_not_an_outage(tmp_path):
    """타임아웃은 '이 작업이 크다'는 신호다. 한 티켓 때문에 실행 전체를 멈추면 안 된다.

    한도 초과(exit 1, 빈 stderr)는 다음 호출도 똑같이 죽으므로 멈추는 게 맞지만,
    타임아웃은 다음 티켓이 멀쩡할 수 있다.
    """
    from factory.worker.base import WorkerResult

    assert WorkerResult(ok=False, error="exit 1: ").is_outage()
    assert not WorkerResult(ok=False, error="timeout after 900s").is_outage()
    assert WorkerResult(ok=False, error="timeout after 900s").infra_failure(), \
        "단발 생성 단계는 타임아웃에도 우아하게 멈춰야 한다"

    # 타임아웃이 나도 다음 티켓으로 넘어가고, 상한을 다 쓰면 BLOCKED 된다
    w = make_worker()
    hits = {"n": 0}

    def slow(t):
        hits["n"] += 1
        return WorkerResult(ok=False, error="timeout after 900s")

    w.register("implement", slow)
    w.register("debug", slow)
    ctx = make_ctx(tmp_path, w)
    ctx.cfg.limits.max_tickets_per_run = 2
    Orchestrator(ctx).run()

    assert hits["n"] >= 3, "타임아웃 한 번에 실행이 멈췄다"
    blocked = [s for s in ctx.tracker.search(type=TicketType.STORY.value)
               if s.status == Status.BLOCKED.value]
    assert blocked, "상한을 다 쓴 티켓이 BLOCKED 되지 않았다"


def test_simulate_catches_money_settled_before_goods():
    """P2. 한 전이 안에서 돈의 수지가 맞아야 한다.

    v6·v7 이 공유하던 실제 결함: 배출 *개시* 에서 잔액을 비우고 거스름돈을 내주는데
    매출은 배출 *완료* 에서 잡는다. 두 전이를 합치면 맞지만 그 사이 상태에서는
    장부가 깨져 있고, 거기서 정전이나 배출 실패가 나면 음료값이 증발한다.

    검토(Critic)가 두 버전에 걸쳐 증상을 여섯 번 신고했으나 원인을 묶지 못했다.
    이건 코드로 잡힌다 -- 결정론적이고 싸다.
    """
    from factory import simulate

    bad = _fsm([("T-1", "A", "COIN", "B", "credit += amount"),
                ("T-2", "B", "PRESS", "C",
                 "start_dispense(drink); pay(credit - price); credit = 0"),
                ("T-3", "C", "DONE", "A",
                 "stock[drink] -= 1; sales += price(drink)")],
               states=[("A", True), ("B", False), ("C", False)])
    fs = [f for f in simulate.run_all(bad, []) if f.check == "장부 수지"]
    ids = {r for f in fs for r in f.refs}
    assert "T-2" in ids, "잔액을 비우면서 행선지가 없는 전이를 못 잡았다"
    assert "T-3" in ids, "대응하는 잔액 차감 없이 매출만 잡는 전이를 못 잡았다"

    # 정산을 완료 시점으로 모으면 조용해져야 한다
    good = _fsm([("T-1", "A", "COIN", "B", "credit += amount"),
                 ("T-2", "B", "PRESS", "C", "start_dispense(drink)"),
                 ("T-3", "C", "DONE", "A",
                  "stock[drink] -= 1; sales += price(drink); credit -= price(drink); "
                  "pay(credit); credit = 0")],
                states=[("A", True), ("B", False), ("C", False)])
    assert not [f for f in simulate.run_all(good, []) if f.check == "장부 수지"], \
        "올바른 정산 순서가 거짓 양성으로 걸렸다"


def test_ledger_check_ignores_names_that_merely_mention_dispense():
    """`log_late_dispense` 처럼 이름만 비슷한 것을 잡으면 지적이 전부 소음이 된다."""
    from factory import simulate

    f = _fsm([("T-1", "A", "E", "A", "log_late_dispense(serial, drink); persist()"),
              ("T-2", "A", "F", "A", "show_pending_dispense(); persist()")])
    assert not [x for x in simulate.run_all(f, []) if x.check == "장부 수지"]


def test_ledger_check_stays_quiet_where_there_is_no_money_to_lose():
    """세 가지 자리에서 조용해야 한다. 셋 다 v6·v7 에서 실제로 오탐이 났던 곳이다.

    1. 전원을 켜며 `credit = 0` -- 꺼진 기계에는 잃을 잔액이 없다. 초기화다.
    2. guard 가 `credit == 0` 을 요구하는 전이 -- 설계자가 이미 막아 둔 자리다.
    3. 보관액 장부 이름이 `deposit` 이 아닌 경우 -- v6 은 `unreturnedBalance` 였다.
       이름을 박아 둔 검사는 다음 버전에서 조용히 깨진다.
    """
    from factory import simulate

    f = _fsm([("T-1", "OFF", "POWER_ON", "IDLE", "restore_all(); credit = 0; persist()"),
              ("T-2", "IDLE", "COIN", "PAID", "credit += amount; persist()"),
              ("T-3", "PAID", "ADMIN_KEY", "ADMIN", "show_admin_menu(); persist()",
               "credit == 0"),
              ("T-4", "PAID", "POWER_LOST", "OFF",
               "unreturnedBalance += credit; credit = 0; persist()")],
             states=[("OFF", True), ("IDLE", False), ("PAID", False), ("ADMIN", False)])
    found = [f_ for f_ in simulate.run_all(f, []) if f_.check == "장부 수지"]
    assert not found, [str(x) for x in found]


def test_abandoning_credit_is_reported_once_per_event():
    """네 상태에서 같은 실수를 하면 결함은 하나다. 네 건으로 세면 심각도가 부풀고,
    고치는 사람은 같은 판단을 네 번 다시 한다. v7 의 T-007~T-010 이 그랬다."""
    from factory import simulate

    rows = [("T-1", "IDLE", "COIN", "PAID", "credit += amount; persist()")]
    rows += [(f"T-{i+2}", s, "POWER_OFF", "OFF", "")
             for i, s in enumerate(["PAID", "IDLE"])]
    f = _fsm(rows, states=[("IDLE", True), ("PAID", False), ("OFF", False)])
    found = [x for x in simulate.run_all(f, []) if "잔액을 든 채" in x.detail]
    assert len(found) == 1, [str(x) for x in found]
    assert "T-2" in found[0].refs


def test_ledger_check_does_not_depend_on_notation():
    """같은 요구사항에서 세대마다 표기가 달랐다.

        v6  credit = 0        unreturnedBalance += credit
        v7  credit = 0        deposit += credit
        v8  credit := 0       unreturned += ledger.credit

    표기를 하나로 가정한 검사는 다음 세대에서 **조용히 통과한다.** 실제로 v8 에서
    17곳의 `:=` 를 전부 놓쳤고, 지적 0건이 '깨끗하다'가 아니라 '검사가 안 돌았다'였다.
    지적을 못 내는 검사는 없는 검사보다 나쁘다 -- 있다고 믿게 만들기 때문이다.
    """
    from factory import simulate

    for zero, move in [("credit = 0", "deposit += credit"),
                       ("credit := 0", "unreturned += ledger.credit"),
                       ("ledger.credit := 0", "unreturned += credit")]:
        f = _fsm([("T-1", "IDLE", "COIN", "PAID", "credit += amount"),
                  ("T-2", "PAID", "POWER_LOST", "OFF", f"{move}; {zero}; persist()")],
                 states=[("IDLE", True), ("PAID", False), ("OFF", False)])
        found = [x for x in simulate.run_all(f, []) if x.check == "장부 수지"]
        assert not found, f"{zero} / {move} 를 못 읽었다: {[str(x) for x in found]}"

    # 옮겨 담지 않고 비우기만 하면, 표기와 무관하게 걸려야 한다
    for zero in ("credit = 0", "credit := 0"):
        f = _fsm([("T-1", "IDLE", "COIN", "PAID", "credit += amount"),
                  ("T-2", "PAID", "POWER_LOST", "OFF", f"{zero}; persist()")],
                 states=[("IDLE", True), ("PAID", False), ("OFF", False)])
        assert [x for x in simulate.run_all(f, []) if x.check == "장부 수지"], \
            f"{zero} 로 돈을 버리는 것을 놓쳤다"


def test_guard_arithmetic_pinning_credit_to_zero_is_read():
    """guard 는 대부분 평가할 수 없는 도메인 술어지만,
    **잔액 자신에 대한 산술 제약은 평가할 수 있다.**

    v8 은 배출 완료를 두 갈래로 나눴다 -- 거스름돈을 빼고도 남는 게 있으면
    잔액 보유 상태로, 0 이면 무잔액 상태로. 이 산술을 못 읽으면 옳게 만든 설계를
    지적하게 되고, 그러면 수리 루프가 맞는 것을 고치려 든다.
    """
    from factory import simulate

    rows = [("T-1", "IDLE", "COIN", "PAID", "credit += amount"),
            ("T-2", "PAID", "SELECT", "DISPENSING", "start(drink); persist()"),
            ("T-3", "DISPENSING", "DONE", "PAID",
             "commit{ stock(d) -= 1; sales += price(d); payout(change); credit -= change + price(d) }",
             "credit - change - price(d) > 0"),
            ("T-4", "DISPENSING", "DONE", "IDLE",
             "commit{ stock(d) -= 1; sales += price(d); payout(change); credit -= change + price(d) }",
             "credit - change - price(d) == 0"),
            ("T-5", "IDLE", "ADMIN_KEY", "ADMIN", "")]
    f = _fsm(rows, states=[("IDLE", True), ("PAID", False),
                           ("DISPENSING", False), ("ADMIN", False)])
    found = [x for x in simulate.run_all(f, []) if x.check == "장부 수지"]
    assert not found, [str(x) for x in found]

    # 그 산술 구분이 없으면 -- 남는 돈이 IDLE 로 흘러가고, 거기서 관리 모드로 샌다
    rows[3] = ("T-4", "DISPENSING", "DONE", "IDLE", rows[3][4], "")
    del rows[2]
    f = _fsm(rows, states=[("IDLE", True), ("PAID", False),
                           ("DISPENSING", False), ("ADMIN", False)])
    assert [x for x in simulate.run_all(f, []) if "잔액을 든 채" in x.detail], \
        "구분 없이 잔액을 흘려보내는 것을 놓쳤다"
