"""파일 기반 Tracker. Jira 없이 루프 전체를 검증하기 위한 구현.

<workspace>/backlog/tickets.yaml   — 티켓 전체
<workspace>/backlog/comments.log   — 감사 로그 (append-only)
"""
from __future__ import annotations

import itertools
from pathlib import Path

import yaml

from factory.models import Ticket
from factory.tracker.base import Tracker


class LocalTracker(Tracker):
    def __init__(self, workspace: Path, prefix: str = "SWF"):
        self.root = Path(workspace) / "backlog"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "tickets.yaml"
        self.log = self.root / "comments.log"
        self.prefix = prefix
        self._tickets: dict[str, Ticket] = {}
        self._load()

    # --- 영속화 ---

    def _load(self) -> None:
        if not self.db.exists():
            return
        raw = yaml.safe_load(self.db.read_text(encoding="utf-8")) or []
        self._tickets = {d["key"]: Ticket.from_dict(d) for d in raw}

    def _flush(self) -> None:
        data = [t.to_dict() for t in self._tickets.values()]
        self.db.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _next_key(self) -> str:
        used = set()
        for k in self._tickets:
            _, _, num = k.rpartition("-")
            if num.isdigit():
                used.add(int(num))
        for n in itertools.count(1):
            if n not in used:
                return f"{self.prefix}-{n}"
        raise AssertionError("unreachable")

    # --- Tracker 구현 ---

    def create(self, ticket: Ticket) -> Ticket:
        if not ticket.key:
            ticket.key = self._next_key()
        self._tickets[ticket.key] = ticket
        self._flush()
        self.comment(ticket.key, f"created: [{ticket.type}] {ticket.title}")
        return ticket

    def update(self, ticket: Ticket) -> Ticket:
        self._tickets[ticket.key] = ticket
        self._flush()
        return ticket

    def get(self, key: str) -> Ticket | None:
        return self._tickets.get(key)

    def search(
        self,
        *,
        status: str | None = None,
        type: str | None = None,
        parent: str | None = None,
        label: str | None = None,
    ) -> list[Ticket]:
        out = list(self._tickets.values())
        if status is not None:
            out = [t for t in out if t.status == status]
        if type is not None:
            out = [t for t in out if t.type == type]
        if parent is not None:
            out = [t for t in out if t.parent == parent]
        if label is not None:
            out = [t for t in out if label in t.labels]
        return sorted(out, key=lambda t: int(t.key.rpartition("-")[2] or 0))

    def comment(self, key: str, body: str) -> None:
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(f"[{key}] {body}\n")
