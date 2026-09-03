"""Tracker 인터페이스.

파이프라인은 Jira를 직접 알지 못한다. 이 인터페이스만 안다.
LocalTracker(파일)로 루프를 검증하고, 같은 인터페이스로 JiraTracker를 끼운다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from factory.models import Ticket


class Tracker(ABC):
    """일감 저장소. Jira / 로컬 YAML / 그 외 무엇이든."""

    @abstractmethod
    def create(self, ticket: Ticket) -> Ticket:
        """티켓 생성. key를 채워서 돌려준다."""

    @abstractmethod
    def update(self, ticket: Ticket) -> Ticket:
        """티켓 갱신 (status, attempts, last_error 등)."""

    @abstractmethod
    def get(self, key: str) -> Ticket | None: ...

    @abstractmethod
    def search(
        self,
        *,
        status: str | None = None,
        type: str | None = None,
        parent: str | None = None,
        label: str | None = None,
    ) -> list[Ticket]:
        """조건에 맞는 티켓 목록. Jira 어댑터에서는 JQL로 번역된다."""

    @abstractmethod
    def comment(self, key: str, body: str) -> None:
        """실행 이력을 티켓에 남긴다. 사람이 나중에 읽을 감사 로그."""

    # --- 편의 메서드 (어댑터가 재정의할 필요 없음) ---

    def transition(self, key: str, status: str, note: str = "") -> Ticket:
        t = self.get(key)
        if t is None:
            raise KeyError(f"티켓 없음: {key}")
        prev, t.status = t.status, status
        self.update(t)
        self.comment(key, f"status: {prev} → {status}" + (f"\n{note}" if note else ""))
        return t

    def find_by_signature(self, signature: str) -> Ticket | None:
        """FSM 전이 지문으로 기존 티켓 찾기 — 재실행 시 중복 생성 방지(멱등성)."""
        for t in self.search():
            if t.signature and t.signature == signature:
                return t
        return None
