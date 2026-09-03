"""검토 단계 — 초록색을 의심한다.

파이프라인 전체가 하나의 요구사항 문서에서 나오므로, 문서의 공백은 아무 데서도 걸리지 않는다.
유스케이스도 FSM도 테스트도 같은 공백을 물려받고, 그래서 전부 통과한다.
이 단계는 그 구조적 맹점을 노린다.

두 가지 규칙이 이 단계를 지탱한다:

1. **검토자는 고치지 않는다.** 읽기 전용 도구만 준다.
   스스로 고칠 수 있으면 "고쳤으니 됐다"로 끝나고, 사람이 볼 기회가 사라진다.
2. **지적은 말이 아니라 티켓이 된다.** 로그로 흘려보내면 아무도 안 읽는다.
   백로그에 들어가야 다음 사이클의 일감이 된다.
"""
from __future__ import annotations

from dataclasses import dataclass

from factory.context import Ctx, ask_yaml, prompt
from factory.models import Ticket, TicketType

READ_ONLY = ["Read", "Glob", "Grep"]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
VALID_CATEGORIES = {
    "missing-rule", "untraced-req", "vacuous-test", "incomplete-guard", "guessed-spec",
}


@dataclass
class Finding:
    id: str = ""
    severity: str = "medium"
    category: str = "missing-rule"
    title: str = ""
    evidence: str = ""
    impact: str = ""
    action: str = ""
    refs: list[str] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(
            id=str(d.get("id", "")),
            severity=str(d.get("severity", "medium")).lower(),
            category=str(d.get("category", "missing-rule")).lower(),
            title=str(d.get("title", "")).strip(),
            evidence=str(d.get("evidence", "")).strip(),
            impact=str(d.get("impact", "")).strip(),
            action=str(d.get("action", "")).strip(),
            refs=[str(r) for r in (d.get("refs") or [])],
        )

    def is_usable(self) -> bool:
        """근거 없는 지적은 버린다. 검토가 인상비평이 되면 아무도 안 읽는다."""
        return bool(self.title and self.evidence)

    def key(self) -> str:
        """같은 지적을 매 실행마다 새 티켓으로 만들지 않기 위한 지문."""
        import hashlib
        raw = f"{self.category}|{self.title}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


def review(ctx: Ctx) -> list[Finding]:
    """검토를 돌리고 지적을 티켓으로 남긴다."""
    store = ctx.store
    data = ask_yaml(
        ctx, "critic",
        prompt(
            "critic",
            requirements_md=_read(store.requirements_md),
            requirements_yaml=_read(store.requirements_yaml),
            usecases_yaml=_read(store.usecases_yaml),
            fsm_yaml=_read(store.fsm_yaml),
            repo=str(ctx.cfg.repo),
        ),
        cwd=ctx.cfg.repo,
        tools=READ_ONLY,          # 고칠 수 없다 -- 의도된 제약
    )
    for line in (data.get("checked") or [])[:12]:
        ctx.log(f"  확인함: {line}")

    findings = [Finding.from_dict(d) for d in (data.get("findings") or [])]
    dropped = [f for f in findings if not f.is_usable()]
    findings = [f for f in findings if f.is_usable()]
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))

    if dropped:
        ctx.log(f"  근거 없는 지적 {len(dropped)}건 버림")
    if not findings:
        ctx.log("검토: 지적 사항 없음")
        return []

    counts = {s: sum(1 for f in findings if f.severity == s) for s in ("high", "medium", "low")}
    ctx.log(f"검토: {len(findings)}건 (high {counts['high']} / medium {counts['medium']} / low {counts['low']})")

    for f in findings:
        _file(ctx, f)
    return findings


def _file(ctx: Ctx, f: Finding) -> Ticket | None:
    """지적을 백로그 티켓으로. 이미 있으면 다시 만들지 않는다."""
    sig = f.key()
    if ctx.tracker.find_by_signature(sig):
        ctx.log(f"  [{f.severity}] {f.title[:64]} (기존 티켓 유지)")
        return None

    body = "\n".join([
        f"**분류** {f.category}",
        f"**심각도** {f.severity}",
        "",
        "**근거**", f.evidence,
        "",
        "**영향**", f.impact or "(미기재)",
        "",
        "**제안**", f.action or "(미기재)",
        "",
        f"**참조** {', '.join(f.refs or []) or '(없음)'}",
        "",
        "_자동 검토 단계가 생성했습니다. 사람이 판단해 주세요._",
    ])
    t = ctx.tracker.create(Ticket(
        type=TicketType.BUG.value,
        title=f"[검토/{f.severity}] {f.title[:70]}",
        description=body,
        signature=sig,
        labels=["auto", "critic", "needs-human", f.category],
    ))
    ctx.log(f"  [{f.severity}] {f.title[:64]} -> {t.key}")
    return t


def _read(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else "(없음)"
