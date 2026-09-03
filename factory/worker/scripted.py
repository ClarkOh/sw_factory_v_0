"""토큰을 쓰지 않는 결정론적 워커.

목적: 오케스트레이터 루프(재시도·백로그 승격·게이트·재개)를 LLM 없이 검증한다.
루프 버그와 모델 출력 문제를 섞어서 디버깅하면 아무것도 못 고친다.

핸들러는 (label 접두사 → 콜러블) 로 등록한다.
"""
from __future__ import annotations

from typing import Callable

from factory.worker.base import Worker, WorkerResult, WorkerTask

Handler = Callable[[WorkerTask], WorkerResult]


class ScriptedWorker(Worker):
    def __init__(self, handlers: dict[str, Handler] | None = None):
        self.handlers: dict[str, Handler] = handlers or {}
        self.calls: list[WorkerTask] = []

    def register(self, label_prefix: str, fn: Handler) -> None:
        self.handlers[label_prefix] = fn

    def run(self, task: WorkerTask) -> WorkerResult:
        self.calls.append(task)
        for prefix, fn in self.handlers.items():
            if task.label.startswith(prefix):
                return fn(task)
        return WorkerResult(ok=True, text=f"[scripted] no handler for label={task.label!r}")
