"""단계들이 공유하는 실행 컨텍스트."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.artifacts import ArtifactStore
from factory.config import FactoryConfig
from factory.tracker.base import Tracker
from factory.worker.base import Worker

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


# `{이름}` 형태의 자리표시자만 치환한다.
# str.format 을 쓰면 프롬프트에 든 코드 예제가 폭발한다 --
# `{(t["src"], t["event"]) for t in TRANSITIONS}` 를 치환 필드로 해석해서 TypeError를 낸다.
# 프롬프트에는 앞으로도 코드가 계속 들어가므로 로더 쪽을 좁게 만드는 게 맞다.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def prompt(name: str, **kw: Any) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    return _PLACEHOLDER.sub(
        lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0),
        text,
    )


def ask_yaml(
    ctx: "Ctx",
    label: str,
    body: str,
    *,
    cwd: Path | None = None,
    tools: list[str] | None = None,
) -> dict:
    """워커에게 묻고 YAML을 받아온다. 실패해도 응답을 버리지 않는다.

    LLM 호출은 느리고 비싸다. 응답 원문을 항상 디스크에 남기고,
    파싱에 실패하면 오류를 그대로 되돌려주며 한 번 더 묻는다.
    이 처리가 없는 단계가 하나라도 있으면 거기서 몇 분짜리 결과가 통째로 날아간다.
    """
    from factory.artifacts import extract_yaml
    from factory.worker.base import WorkerTask

    prompt_text, last_err = body, ""
    for attempt in (1, 2):
        res = ctx.worker.run(WorkerTask(
            prompt=prompt_text,
            cwd=cwd or ctx.cfg.workspace,
            allowed_tools=tools or ["Read", "Glob", "Grep"],
            timeout=ctx.cfg.limits.worker_timeout_s,
            label=label if attempt == 1 else f"{label}:retry",
        ))
        save_raw(ctx, f"{label}.{attempt}", res.text or res.error)
        if not res.ok:
            from factory.worker.base import WorkerUnavailable
            if res.infra_failure():
                raise WorkerUnavailable(f"{label}: {res.error}")
            raise RuntimeError(f"{label} 워커 실패: {res.error}")
        try:
            return extract_yaml(res.text)
        except ValueError as e:
            last_err = str(e)
            ctx.log(f"  {label}: YAML 파싱 실패 -> 재질의 (원문은 .factory/raw/ 에 보존)")
            prompt_text = (
                f"직전 응답의 YAML을 파싱할 수 없었습니다.\n오류: {last_err[:300]}\n\n"
                "흔한 원인입니다. 확인하세요:\n"
                "- 값에 `: `(콜론+공백)가 들어가면 따옴표로 감싸야 합니다\n"
                "- `!`, `*`, `&`, `%` 로 시작하는 값도 따옴표가 필요합니다\n"
                "- 상태·이벤트 이름 ON/OFF/YES/NO 는 불리언으로 읽힙니다\n\n"
                "같은 요청을 다시 수행하되, 유효한 YAML만 출력하세요.\n\n" + body
            )
    raise RuntimeError(f"{label}: 두 번 시도했으나 YAML을 얻지 못했습니다.\n{last_err}")


def save_raw(ctx: "Ctx", name: str, text: str) -> None:
    """워커 응답 원문을 남긴다. 파싱이 깨져도 사람이 열어 볼 수 있게."""
    d = ctx.cfg.workspace / ".factory" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name.replace(':', '_')}.txt").write_text(text or "", encoding="utf-8")


@dataclass
class Ctx:
    cfg: FactoryConfig
    store: ArtifactStore
    tracker: Tracker
    worker: Worker
    logfile: Path | None = None
    _seen: list[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        if self.logfile:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            with self.logfile.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
