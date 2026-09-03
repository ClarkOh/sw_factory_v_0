"""공장 설정. factory.yaml 로 저장된다."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml


@dataclass
class Gates:
    """사람 승인이 필요한 지점.

    'auto'  = 자동 통과
    'manual'= 여기서 멈추고 사람이 `factory approve <gate>` 하기를 기다림

    push가 기본 manual인 이유: 되돌리기 어렵고 바깥으로 나가는 유일한 액션.
    """
    fsm: str = "manual"        # FSM은 뒤의 모든 것의 근거 → 사람이 한 번 본다
    ticketize: str = "auto"
    commit: str = "auto"
    push: str = "manual"


@dataclass
class Limits:
    implement_attempts: int = 3    # 구현+디버그 재시도 상한. 초과 시 BLOCKED + 버그티켓.
    e2e_attempts: int = 2
    fsm_repair_attempts: int = 3   # FSM 구조 오류 자동 수리 시도
    critic_cycles: int = 3         # 검토->수정->재검토 최대 사이클. 연속 2회 무소득이면 조기 종료.
    worker_timeout_s: int = 900
    gen_timeout_s: int = 2400      # 테스트·E2E·진입점 생성 전용. 한 번에 수십 개를 쓰므로
                                   # 티켓 하나짜리 작업(900s)보다 훨씬 오래 걸린다.
    max_tickets_per_run: int = 0   # 0 = 무제한. 첫 검증 때는 1~2로 두는 걸 권장.


@dataclass
class FactoryConfig:
    project: str
    workspace: Path                 # 산출물/백로그가 쌓이는 곳
    repo: Path                      # 실제 코드가 있는 git 저장소
    tracker: str = "local"          # local | jira
    jira_project_key: str = ""
    worker: str = "headless"        # headless | scripted
    model: str = ""                 # "" = claude 기본값
    gates: Gates = field(default_factory=Gates)
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def load(cls, path: Path) -> "FactoryConfig":
        d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        base = Path(path).parent
        return cls(
            project=d["project"],
            workspace=(base / d.get("workspace", ".")).resolve(),
            repo=(base / d.get("repo", "repo")).resolve(),
            tracker=d.get("tracker", "local"),
            jira_project_key=d.get("jira_project_key", ""),
            worker=d.get("worker", "headless"),
            model=d.get("model", ""),
            gates=Gates(**(d.get("gates") or {})),
            limits=Limits(**(d.get("limits") or {})),
        )

    def save(self, path: Path) -> None:
        d = asdict(self)
        d["workspace"] = str(self.workspace)
        d["repo"] = str(self.repo)
        Path(path).write_text(
            yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
