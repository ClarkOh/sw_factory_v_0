"""워커 호출 비용 기록과 집계.

**왜 필요한가.** 4일치 실행을 마치고 "토큰을 얼마나 썼나"를 물었을 때,
공장은 답할 수 없었다. `WorkerResult.cost_usd` 를 받아만 놓고 버렸기 때문이다.
결국 `~/.claude/projects/**/*.jsonl` 세션 기록을 역으로 파싱해 복원했는데,
그 기록은 언젠가 사라진다.

**무엇에 쓰는가.** 절대 토큰 수는 프로젝트 크기에 비례하므로 예산으로 쓸 수 없다.
전이 10개짜리의 1억과 500개짜리의 1억은 다른 의미다. 두 가지로만 쓴다:

1. **프로젝트 안** -- 같은 단계의 중앙값 대비 배수로 폭주를 잡는다.
   큰 프로젝트를 막지 않으면서 이상만 걸린다.
2. **같은 프로젝트 반복** -- 도메인이 고정된 통제된 실험. 공장 변경의 효과를 재는 유일한 척도.

기록은 append-only JSONL 이다. 실행이 중간에 죽어도 그때까지가 남는다.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def record(workspace: Path, label: str, result, phase: str = "") -> None:
    """워커 호출 하나를 적립한다. 실패해도 파이프라인을 멈추지 않는다."""
    try:
        path = Path(workspace) / ".factory" / "cost.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        usage = ((result.raw or {}).get("usage") or {}) if result.raw else {}
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": phase,
            "stage": _stage_of(label),
            "label": label,
            "ok": bool(result.ok),
            "duration_s": round(result.duration_s, 1),
            "cost_usd": round(result.cost_usd, 6),
            "tokens": {k: int(usage.get(k, 0) or 0) for k in TOKEN_KEYS},
        }
        row["total_tokens"] = sum(row["tokens"].values())
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        # 회계가 파이프라인을 죽이면 안 된다. 기록 실패는 조용히 넘긴다.
        pass


def _stage_of(label: str) -> str:
    """`implement:SWF-12:2` -> `implement`. 티켓·시도 번호를 떼어 단계만 남긴다."""
    return (label or "?").split(":", 1)[0]


# --------------------------------------------------------------------------
# 폭주 감지
# --------------------------------------------------------------------------

@dataclass
class Outlier:
    stage: str
    label: str
    tokens: int
    median: int

    @property
    def ratio(self) -> float:
        return self.tokens / self.median if self.median else 0.0


def check_outlier(workspace: Path, label: str, threshold: float = 5.0,
                  min_samples: int = 8) -> Outlier | None:
    """방금 호출이 같은 단계 중앙값의 threshold 배를 넘었는가.

    표본이 적으면 중앙값이 못 미더우므로 판정하지 않는다.
    절대값이 아니라 배수로 보기 때문에 프로젝트 크기와 무관하게 동작한다.
    """
    rows = load(workspace)
    if not rows:
        return None
    stage = _stage_of(label)
    same = [r["total_tokens"] for r in rows if r["stage"] == stage and r["total_tokens"]]
    if len(same) < min_samples:
        return None
    latest = next((r for r in reversed(rows) if r["label"] == label), None)
    if not latest or not latest["total_tokens"]:
        return None
    # 자기 자신을 뺀 중앙값과 비교한다
    others = same[:-1] if len(same) > 1 else same
    med = statistics.median(others)
    if med and latest["total_tokens"] > med * threshold:
        return Outlier(stage, label, latest["total_tokens"], int(med))
    return None


# --------------------------------------------------------------------------
# 집계
# --------------------------------------------------------------------------

def load(workspace: Path) -> list[dict]:
    path = Path(workspace) / ".factory" / "cost.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(workspace: Path, transitions: int = 0) -> dict:
    """단계별 집계와 단위당 지표. 기준선 비교는 이 값들로 한다."""
    rows = load(workspace)
    by_stage: dict[str, list[int]] = defaultdict(list)
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    cost = 0.0
    seconds = 0.0
    for r in rows:
        by_stage[r["stage"]].append(r["total_tokens"])
        for k in TOKEN_KEYS:
            totals[k] += r["tokens"].get(k, 0)
        cost += r.get("cost_usd", 0.0)
        seconds += r.get("duration_s", 0.0)

    stages = {}
    for name, vals in sorted(by_stage.items(), key=lambda kv: -sum(kv[1])):
        clean = [v for v in vals if v]
        stages[name] = {
            "calls": len(vals),
            "tokens": sum(vals),
            "median": int(statistics.median(clean)) if clean else 0,
            "max": max(vals) if vals else 0,
        }

    grand = sum(totals.values())
    out = {
        "calls": len(rows),
        "tokens": {"total": grand, **totals},
        "cost_usd": round(cost, 4),
        "wall_hours": round(seconds / 3600, 2),
        "by_stage": stages,
        "derived": {},
    }
    if transitions:
        out["derived"]["tokens_per_transition"] = round(grand / transitions)
    impl = stages.get("implement", {}).get("median") or 0
    fix = max(stages.get(k, {}).get("median") or 0
              for k in ("remediate_test", "remediate_code")) if stages else 0
    if impl and fix:
        out["derived"]["cost_ratio_fix_vs_implement"] = round(fix / impl, 1)
    return out
