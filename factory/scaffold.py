"""대상 저장소 초기 구조.

이게 없으면 pytest가 rootdir를 sys.path에 넣지 않아 생성된 테스트가 `import app.*` 에서 깨진다.
워커에게 "알아서 잘 하라"고 맡기면 매번 다른 구조가 나오므로 공장이 고정한다.
"""
from __future__ import annotations

from pathlib import Path

CONVENTIONS = """\
# 프로젝트 규약

- 구현 코드는 `app/` 아래에 둔다. 테스트는 `tests/` 아래.
- 도메인 로직은 I/O와 분리한다. FSM 전이 로직은 순수 함수/메서드로 표현한다.
- 공개 인터페이스를 바꾸면 `INTERFACE.md` 를 같이 갱신한다.
- 외부 라이브러리를 새로 추가하지 않는다. 표준 라이브러리로 해결한다.
- 테스트 파일은 명세다. 구현하는 쪽에서 수정하지 않는다.
"""

INTERFACE = """\
# 공개 인터페이스

테스트는 이 문서에 적힌 것만 사용한다. 여기에 없는 것에 의존하는 테스트는 잘못된 테스트다.

<!-- 구현이 진행되면서 갱신된다. 최초에는 FSM의 상태/이벤트 이름을 그대로 쓴다고 가정한다. -->
"""

GITIGNORE = """\
__pycache__/
*.pyc
.pytest_cache/
.factory/

# 실행이 남기는 런타임 산출물 -- 소스가 아니다.
# 상태를 파일에 저장하는 제품이면 그 파일이 저장소를 오염시킨다.
*_state.json
*.state.json
demo_script.txt
"""

CONFTEST = """\
# pytest가 저장소 루트를 sys.path에 넣도록 하는 앵커 파일.
"""


def scaffold(repo: Path) -> list[str]:
    """없는 것만 만든다. 이미 있는 파일은 건드리지 않는다."""
    created: list[str] = []
    files = {
        "conftest.py": CONFTEST,
        "CONVENTIONS.md": CONVENTIONS,
        "INTERFACE.md": INTERFACE,
        ".gitignore": GITIGNORE,
        "app/__init__.py": "",
        "tests/__init__.py": "",
    }
    for rel, body in files.items():
        p = repo / rel
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        created.append(rel)
    (repo / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
    return created
