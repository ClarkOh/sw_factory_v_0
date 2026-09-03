"""Worker 인터페이스 — '실제로 코드를 쓰는 주체'의 추상화."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkerTask:
    prompt: str
    cwd: Path
    allowed_tools: list[str] = field(default_factory=list)
    timeout: int = 900                    # 초. --max-turns가 없으므로 이게 유일한 상한.
    label: str = ""                       # 로그용


class WorkerUnavailable(RuntimeError):
    """워커가 실행조차 못 했다 -- 코드 결함이 아니라 인프라 문제.

    사용량 한도, 실행 파일 부재, 네트워크 단절. 이걸 '시도 실패'로 세면
    재시도 예산을 아무 일도 안 하고 태우고, 멀쩡한 티켓을 BLOCKED 로 만든다.
    호출부는 이 예외를 잡아 티켓이 아니라 **실행 전체**를 멈춰야 한다 --
    워커가 죽어 있으면 다음 티켓도 똑같이 실패한다.
    """


@dataclass
class WorkerResult:
    ok: bool
    text: str = ""
    error: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    raw: dict | None = None

    def is_outage(self) -> bool:
        """다음 호출도 똑같이 실패할 상황인가.

        타임아웃은 제외한다 -- 그건 "이 작업이 크다"는 신호이지 인프라 장애가 아니다.
        한 티켓의 타임아웃 때문에 여섯 시간짜리 실행을 멈추면 안 된다.
        재시도 루프(구현·수정)는 이걸 보고, 단발 생성 단계는 infra_failure() 를 본다.
        """
        e = (self.error or "").strip()
        return (not self.ok) and (
            e in ("exit 1:", "exit 1")
            or "실행 파일을 찾을 수 없음" in e
            or "작업 디렉터리가 없음" in e
        )

    def infra_failure(self) -> bool:
        """워커가 일을 하긴 했는가. 단발 생성 단계용 -- 타임아웃도 포함한다."""
        if self.ok:
            return False
        e = (self.error or "").strip()
        return (
            e in ("exit 1:", "exit 1")
            or e.startswith("timeout after")
            or "실행 파일을 찾을 수 없음" in e
            or "작업 디렉터리가 없음" in e
        )


class Worker(ABC):
    @abstractmethod
    def run(self, task: WorkerTask) -> WorkerResult: ...
