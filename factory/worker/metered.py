"""비용을 기록하는 워커 껍데기.

어떤 워커든 감싸서 호출마다 토큰·비용·시간을 적립한다.
단계마다 따로 붙이지 않고 **여기 한 곳에서** 처리하는 이유는,
`ask_yaml` 때와 같은 교훈 때문이다 -- 단계별로 구현하면 반드시 빠뜨리는 곳이 생긴다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from factory import cost
from factory.worker.base import Worker, WorkerResult, WorkerTask


class MeteredWorker(Worker):
    def __init__(self, inner: Worker, workspace: Path,
                 outlier_threshold: float = 5.0):
        self.inner = inner
        self.workspace = Path(workspace)
        self.outlier_threshold = outlier_threshold
        self.phase: str = ""                     # 오케스트레이터가 단계마다 갱신
        self.log: Callable[[str], None] = lambda _m: None

    def run(self, task: WorkerTask) -> WorkerResult:
        res = self.inner.run(task)
        cost.record(self.workspace, task.label, res, phase=self.phase)

        # 폭주 감지: 절대값이 아니라 같은 단계 중앙값 대비 배수로 본다.
        # 프로젝트가 크다고 걸리면 안 되고, 한 티켓이 유독 헤매는 것만 걸려야 한다.
        out = cost.check_outlier(self.workspace, task.label, self.outlier_threshold)
        if out:
            self.log(
                f"  [비용] {out.label} 이 {out.stage} 중앙값의 {out.ratio:.1f}배를 "
                f"썼습니다 ({out.tokens:,} vs {out.median:,}) -- 헤매고 있을 수 있습니다")
        return res

    # 내부 워커의 속성에 그대로 접근할 수 있게 (bin, model 등 진단용)
    def __getattr__(self, name: str):
        return getattr(self.inner, name)
