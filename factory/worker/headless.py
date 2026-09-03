"""Claude Code headless 워커.

`claude -p <prompt> --output-format json` 를 서브프로세스로 띄운다.
CLI 2.1.251에는 --max-turns가 없으므로 폭주 방지는 timeout(벽시계)이 담당한다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from factory.worker.base import Worker, WorkerResult, WorkerTask

# 워커가 기본으로 쓸 도구. 이 목록에 없는 도구는 승인 프롬프트에 걸려 멈춘다.
DEFAULT_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(python *)", "Bash(python -m pytest *)", "Bash(pytest *)",
    "Bash(git status*)", "Bash(git diff*)", "Bash(git add*)",
    "Bash(ls*)", "Bash(cat*)", "Bash(mkdir*)",
]


class HeadlessClaudeWorker(Worker):
    def __init__(
        self,
        model: str | None = None,
        permission_mode: str = "acceptEdits",
        extra_args: list[str] | None = None,
        bin: str = "claude",
    ):
        self.bin = shutil.which(bin) or bin
        self.model = model
        self.permission_mode = permission_mode
        self.extra_args = extra_args or []

    def run(self, task: WorkerTask) -> WorkerResult:
        # 프롬프트는 argv가 아니라 stdin으로 넘긴다.
        # Windows CreateProcess의 명령줄 상한은 32767자다. 전이가 40개를 넘어가면
        # 프롬프트가 그 선을 넘고, 파이프라인이 FileNotFoundError로 죽는다 (실제로 겪었다).
        cmd = [
            self.bin, "-p",
            "--output-format", "json",
            "--permission-mode", self.permission_mode,
        ]
        tools = task.allowed_tools or DEFAULT_TOOLS
        if tools:
            cmd += ["--allowedTools", *tools]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=task.prompt,
                cwd=str(task.cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=task.timeout,
            )
        except subprocess.TimeoutExpired:
            return WorkerResult(
                ok=False,
                error=f"timeout after {task.timeout}s",
                duration_s=time.monotonic() - t0,
            )
        except FileNotFoundError as exc:
            # 원인을 구별해서 알린다. 실행 파일이 없는 것과 작업 디렉터리가 없는 것은
            # 둘 다 FileNotFoundError지만 고치는 방법이 완전히 다르다.
            if not Path(self.bin).exists() and shutil.which(self.bin) is None:
                reason = f"claude 실행 파일을 찾을 수 없음: {self.bin}"
            elif not task.cwd.exists():
                reason = f"작업 디렉터리가 없음: {task.cwd}"
            else:
                reason = f"프로세스 실행 실패: {exc}"
            return WorkerResult(ok=False, error=reason, duration_s=time.monotonic() - t0)
        except OSError as exc:
            return WorkerResult(ok=False, error=f"프로세스 실행 실패: {exc}",
                                duration_s=time.monotonic() - t0)

        dur = time.monotonic() - t0
        if proc.returncode != 0:
            return WorkerResult(
                ok=False,
                error=f"exit {proc.returncode}: {(proc.stderr or '')[-2000:]}",
                duration_s=dur,
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # --output-format json이 깨진 경우: 원문을 그대로 결과로 취급한다.
            return WorkerResult(ok=True, text=proc.stdout.strip(), duration_s=dur)

        return WorkerResult(
            ok=not payload.get("is_error", False),
            text=payload.get("result", ""),
            error="" if not payload.get("is_error") else str(payload.get("result", "")),
            session_id=payload.get("session_id", ""),
            cost_usd=float(payload.get("total_cost_usd", 0.0) or 0.0),
            duration_s=dur,
            raw=payload,
        )
