"""단계 간 산출물의 직렬화.

각 단계는 파일을 읽고 파일을 쓴다. 메모리로 넘기지 않는다.
이유: 어느 단계에서든 멈췄다가 재개할 수 있고, 사람이 중간 산출물을 직접 고칠 수 있다.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from factory.models import (FSM, Event, Requirement, Rule, RuleTable, State,
                            Transition, UseCase)


class ArtifactStore:
    def __init__(self, workspace: Path):
        self.root = Path(workspace)
        self.dir = self.root / "artifacts"
        self.dir.mkdir(parents=True, exist_ok=True)

    # --- 경로 ---
    @property
    def requirements_md(self) -> Path: return self.root / "requirements.md"
    @property
    def principles_md(self) -> Path: return self.root / "principles.md"
    @property
    def requirements_yaml(self) -> Path: return self.dir / "requirements.yaml"
    @property
    def usecases_yaml(self) -> Path: return self.dir / "usecases.yaml"
    @property
    def fsm_yaml(self) -> Path: return self.dir / "fsm.yaml"

    # --- 요구사항 ---
    def save_requirements(self, reqs: list[Requirement]) -> None:
        _dump(self.requirements_yaml, {"requirements": [vars(r) for r in reqs]})

    def load_requirements(self) -> list[Requirement]:
        d = _load(self.requirements_yaml)
        return [Requirement(**r) for r in d.get("requirements", [])]

    # --- 유스케이스 ---
    def save_usecases(self, ucs: list[UseCase]) -> None:
        _dump(self.usecases_yaml, {"usecases": [vars(u) for u in ucs]})

    def load_usecases(self) -> list[UseCase]:
        d = _load(self.usecases_yaml)
        return [UseCase(**u) for u in d.get("usecases", [])]

    # --- FSM ---
    def save_fsm(self, fsm: FSM) -> None:
        _dump(self.fsm_yaml, {
            "project": fsm.project,
            "states": [vars(s) for s in fsm.states],
            "events": [vars(e) for e in fsm.events],
            "transitions": [vars(t) for t in fsm.transitions],
        })

    def load_fsm(self) -> FSM:
        d = _load(self.fsm_yaml)
        return FSM(
            project=d.get("project", ""),
            states=[State(**s) for s in d.get("states", [])],
            events=[Event(**e) for e in d.get("events", [])],
            transitions=[Transition(**t) for t in d.get("transitions", [])],
        )

    # --- 결정표 ---
    @property
    def rules_yaml(self) -> Path: return self.dir / "rules.yaml"

    def save_rules(self, table: RuleTable) -> None:
        _dump(self.rules_yaml, {
            "project": table.project,
            "rules": [{"id": r.id, "when": r.when, "then": r.then,
                       "usecase": r.usecase, "notes": r.notes} for r in table.rules],
        })

    def load_rules(self) -> RuleTable:
        d = _load(self.rules_yaml)
        return RuleTable(project=d.get("project", ""),
                         rules=[Rule(**r) for r in d.get("rules", [])])

    def model_yaml_text(self) -> str:
        """중간 형식의 원문. 워커 프롬프트에 그대로 들어간다."""
        p = self.rules_yaml if self.rules_yaml.exists() else self.fsm_yaml
        return p.read_text(encoding="utf-8")

    def load_model(self):
        """이 프로젝트의 중간 형식. FSM 이든 결정표든 원자(atoms)를 내놓는다.

        어느 형식인지는 파일의 존재로 안다 -- MODEL 단계가 하나만 만들기 때문이다.
        둘 다 있으면 설정이 꼬인 것이므로 조용히 고르지 않고 바로 알린다.
        """
        has_fsm, has_rules = self.fsm_yaml.exists(), self.rules_yaml.exists()
        if has_fsm and has_rules:
            raise RuntimeError("fsm.yaml 과 rules.yaml 이 둘 다 있다 — "
                               "MODEL 단계가 하나만 만들어야 한다")
        return self.load_rules() if has_rules else self.load_fsm()


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------
# 워커 출력에서 YAML 뽑아내기.
# LLM은 코드펜스나 설명을 덧붙이는 경향이 있으므로 관대하게 파싱한다.
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)


def extract_yaml(text: str) -> dict:
    """워커 응답에서 YAML 문서를 추출한다.

    코드펜스 안팎을 모두 후보로 보고, 파싱에 실패하면 흔한 파손을 한 번 복구해 재시도한다.
    실패하면 ValueError -- 호출부가 워커에게 되물을 수 있도록 원문을 메시지에 담는다.
    """
    from factory.yamlfix import repair

    raw = text or ""
    candidates: list[str] = list(_FENCE.findall(raw)) + [raw]

    best: dict | None = None
    for c in candidates:
        # 복구본을 먼저 시도한다. `id: OFF` 는 **유효한** YAML이라 파싱 오류가 나지 않고,
        # 조용히 id=False 가 되어 상태 이름이 사라진다. 오류 시에만 복구하면 이걸 놓친다.
        # repair()는 보수적이라(특수문자 시작 값과 식별자 필드의 불리언 토큰만 따옴표) 항상 돌려도 안전하다.
        for attempt in (repair(c), c):
            try:
                parsed = yaml.safe_load(attempt)
            except yaml.YAMLError:
                continue
            if isinstance(parsed, dict) and parsed:
                # 후보가 여럿이면 키가 가장 많은 것 (설명문이 dict로 파싱되는 경우 방지)
                if best is None or len(parsed) > len(best):
                    best = parsed
                break
    if best is None:
        raise ValueError("응답에서 YAML을 찾지 못했습니다:\n" + raw[:800])
    return best
