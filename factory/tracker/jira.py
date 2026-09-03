"""Jira Tracker 어댑터.

LocalTracker로 루프가 검증되면 config의 tracker를 'jira'로 바꾸는 것만으로 전환된다.
파이프라인 코드는 한 줄도 바뀌지 않는다.

인증
  Cloud : JIRA_URL=https://xxx.atlassian.net, JIRA_EMAIL, JIRA_TOKEN  → api/3, Basic
  DC    : JIRA_URL=https://jira.사내, JIRA_TOKEN(PAT)                → api/2, Bearer

미구현: 아래 _map_* 함수만 실제 프로젝트의 필드/워크플로에 맞게 채우면 된다.
Jira는 status 이름과 transition id가 프로젝트마다 다르므로 하드코딩할 수 없다.
"""
from __future__ import annotations

import os

from factory.models import Ticket
from factory.tracker.base import Tracker

# 프로젝트 워크플로에 맞춰 채울 곳 ------------------------------------------
STATUS_MAP = {           # 내부 Status → Jira status 이름
    "todo": "To Do",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "blocked": "Blocked",
    "done": "Done",
}
TYPE_MAP = {             # 내부 TicketType → Jira issuetype 이름
    "epic": "Epic",
    "story": "Story",
    "subtask": "Sub-task",
    "bug": "Bug",
}
# 커스텀 필드에 FSM 추적성을 심고 싶다면 여기에 (예: "customfield_10050": "fsm_ref")
TRACE_FIELDS: dict[str, str] = {}


class JiraTracker(Tracker):
    def __init__(self, project_key: str, url: str | None = None):
        self.project_key = project_key
        self.url = (url or os.environ.get("JIRA_URL", "")).rstrip("/")
        self.email = os.environ.get("JIRA_EMAIL", "")
        self.token = os.environ.get("JIRA_TOKEN", "")
        self.cloud = ".atlassian.net" in self.url
        self.api = f"{self.url}/rest/api/{'3' if self.cloud else '2'}"
        if not self.url or not self.token:
            raise RuntimeError("JIRA_URL / JIRA_TOKEN 환경변수가 필요합니다")
        raise NotImplementedError(
            "JiraTracker는 인터페이스만 정의된 상태입니다. "
            "Jira 접속 정보가 준비되면 requests 기반 구현을 채웁니다."
        )

    def create(self, ticket: Ticket) -> Ticket: ...
    def update(self, ticket: Ticket) -> Ticket: ...
    def get(self, key: str) -> Ticket | None: ...
    def search(self, **kw) -> list[Ticket]: ...
    def comment(self, key: str, body: str) -> None: ...
