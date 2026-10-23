"""네 레지스트리(Tool/Model/Skill/Recipe)의 실제 상태 계산과 레시피 실행 가능성 판정."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from ttakkari.creative.v2.capabilities import (
    CAPABILITIES,
    GLOBAL_PREFERS,
    GLOBAL_REQUIRES,
    INTEGRATION_RANK,
    LEVEL_RANK,
    EntryState,
    Level,
    Recipe,
    RegistryEntry,
    Strict,
    best_level,
)
from ttakkari.creative.v2.registry_data import MODELS, RECIPES, SKILLS, TOOLS

SNAPSHOT_PATH = Path(__file__).parent / "data" / "capability_snapshot.json"


@lru_cache(maxsize=1)
def load_snapshot() -> dict[str, Any] | None:
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except (OSError, ValueError):
        return None


class Provider(Strict):
    entry_id: str
    capability: str
    level: Level
    integration: str
    evidence: list[str]


class GroupResult(Strict):
    options: list[str]
    hard: bool
    level: Level
    best: Provider | None
    alternatives: list[Provider]
    note: str


Verdict = Literal[
    "studio_executable",
    "recipe_demonstrated",
    "capabilities_executed",
    "partially_demonstrated",
    "unverified",
    "blocked",
]


class RecipeEvaluation(Strict):
    recipe_id: str
    name: str
    verdict: Verdict
    summary: str
    groups: list[GroupResult]
    preferred: list[GroupResult]
    weakest: list[list[str]]
    blockers: list[str]
    unverified: list[str]
    cases: list[str]
    case_evidence: list[str]
    constraints: list[str]


class Registry:
    def __init__(
        self,
        snapshot: dict[str, Any] | None = None,
        *,
        use_default_snapshot: bool = True,
    ):
        self.snapshot = (
            snapshot
            if snapshot is not None or not use_default_snapshot
            else load_snapshot()
        )
        self.entries: dict[str, RegistryEntry] = {
            e.id: e for e in [*TOOLS, *MODELS, *SKILLS]
        }
        self.recipes: dict[str, Recipe] = {r.id: r for r in RECIPES}
        self._states: dict[str, EntryState] = {}

    # --- 항목 상태 ---
    def _probe(self, probe_id: str) -> tuple[bool | None, str | None]:
        if self.snapshot is None:
            return None, None
        if probe_id.startswith("skill:"):
            skill = self.snapshot.get("skills", {}).get(probe_id[6:])
            return (skill is not None), None
        probe = self.snapshot.get("probes", {}).get(probe_id)
        if probe is None:
            return None, None
        return bool(probe["installed"]), probe.get("version")

    def _record_level(self, slug: str) -> tuple[Level | None, str]:
        record = (self.snapshot or {}).get("records", {}).get(slug)
        if record is None:
            return None, f"record:{slug}(기록 없음)"
        label = f"record:{slug}({record['status']}, 검수 {record['verdict']}{'·독립' if record['independent'] else ''})"
        if record["independent"] and record["verdict"] == "pass":
            return "verified", label
        if record["status"] == "completed" and record["media_count"] > 0:
            return "executed", label
        return "executed_partial", label

    def state(self, entry_id: str) -> EntryState:
        cached = self._states.get(entry_id)
        if cached:
            return cached
        entry = self.entries[entry_id]
        blockers: list[str] = []
        version: str | None = None
        installed: bool | None
        if entry.known_missing:
            installed = False
            blockers.append("등록·실행된 적이 없다")
        elif self.snapshot is None:
            installed = None
            blockers.append("호스트 스냅샷이 없어 설치 여부를 모른다")
        else:
            results = [self._probe(p) for p in entry.probes]
            installed = all(r[0] for r in results) if results else True
            for probe_id, (ok, ver) in zip(entry.probes, results, strict=True):
                version = version or ver
                if not ok:
                    blockers.append(f"탐침 {probe_id} 미설치")
        has_code = True
        if entry.kind == "skill" and self.snapshot is not None:
            skill = self.snapshot.get("skills", {}).get(entry.id.removeprefix("skill."))
            if skill is not None:
                has_code = bool(skill["has_code"])
                if not has_code:
                    blockers.append("실행 코드 없음(지침 문서만 있음)")
        for dep in entry.depends_on:
            dep_state = self.state(dep)
            if dep_state.installed is False:
                installed = False
                blockers.append(f"의존 {dep} 미설치")
        levels: dict[str, Level] = {}
        evidence: dict[str, list[str]] = {}
        for claim in entry.claims:
            refs: list[str] = []
            if installed is None:
                level: Level = "declared"
            elif installed is False:
                level = "missing"
            elif not has_code:
                level = "declared"
            else:
                level = "installed"
                for slug in claim.records:
                    rec_level, label = self._record_level(slug)
                    refs.append(label)
                    if rec_level and LEVEL_RANK[rec_level] > LEVEL_RANK[level]:
                        level = rec_level
            levels[claim.capability] = (
                best_level([levels[claim.capability], level])
                if claim.capability in levels
                else level
            )
            evidence.setdefault(claim.capability, []).extend(refs)
        state = EntryState(
            id=entry.id,
            kind=entry.kind,
            name=entry.name,
            installed=installed,
            version=version,
            integration=entry.integration,
            levels=levels,
            evidence=evidence,
            blockers=blockers,
            cost_note=entry.cost_note,
            license=entry.license,
            notes=entry.notes,
        )
        self._states[entry_id] = state
        return state

    def states(self, kind: str | None = None) -> list[EntryState]:
        return [
            self.state(i) for i, e in self.entries.items() if kind in (None, e.kind)
        ]

    # --- 능력 공급자 ---
    def providers(self, capability: str) -> list[Provider]:
        out: list[Provider] = []
        for entry_id in self.entries:
            state = self.state(entry_id)
            if capability in state.levels:
                out.append(
                    Provider(
                        entry_id=entry_id,
                        capability=capability,
                        level=state.levels[capability],
                        integration=state.integration,
                        evidence=state.evidence.get(capability, []),
                    )
                )
        return sorted(
            out,
            key=lambda p: (
                -LEVEL_RANK[p.level],
                -INTEGRATION_RANK[p.integration],
                p.entry_id,
            ),
        )

    def capability_level(self, capability: str) -> Level:
        providers = self.providers(capability)
        return providers[0].level if providers else "missing"

    # --- 레시피 ---
    def _group(self, options: list[str], hard: bool) -> GroupResult:
        pool: list[Provider] = []
        for cap in options:
            pool.extend(self.providers(cap))
        pool.sort(
            key=lambda p: (
                -LEVEL_RANK[p.level],
                -INTEGRATION_RANK[p.integration],
                p.entry_id,
                p.capability,
            )
        )
        best = pool[0] if pool else None
        level: Level = best.level if best else "missing"
        note = ""
        if len(options) > 1 and best:
            note = f"대안 중 {best.capability}({best.entry_id})가 가장 높은 수준"
        if not pool:
            note = "이 능력을 제공하는 항목이 레지스트리에 없다"
        return GroupResult(
            options=options,
            hard=hard,
            level=level,
            best=best,
            alternatives=pool[1:4],
            note=note,
        )

    def evaluate_recipe(self, recipe_id: str) -> RecipeEvaluation:
        recipe = self.recipes[recipe_id]
        hard_groups = [
            [c] for c in GLOBAL_REQUIRES if not any(c in g for g in recipe.requires)
        ] + recipe.requires
        groups = [self._group(g, True) for g in hard_groups]
        preferred = [
            self._group(g, False)
            for g in [*recipe.prefers, *[[c] for c in GLOBAL_PREFERS]]
        ]
        lowest = min((LEVEL_RANK[g.level] for g in groups), default=0)
        case_levels = [self._record_level(slug) for slug in recipe.cases]
        case_evidence = [label for _, label in case_levels]
        case_ok = any(lv in ("executed", "verified") for lv, _ in case_levels)
        blockers = [
            f"{'/'.join(g.options)}: " + (g.note or f"최고 수준 {g.level}")
            for g in groups
            if LEVEL_RANK[g.level] <= LEVEL_RANK["declared"]
        ]
        unverified = [
            f"{'/'.join(g.options)}: 설치만 확인({g.best.entry_id if g.best else '-'}), 실행 기록 없음"
            for g in groups
            if g.level == "installed"
        ]
        weakest = [g.options for g in groups if LEVEL_RANK[g.level] == lowest]
        if lowest <= LEVEL_RANK["declared"]:
            verdict: Verdict = "blocked"
            summary = f"필수 능력 {len(blockers)}개가 없거나 선언뿐이라 지금은 실행할 수 없다."
        elif lowest == LEVEL_RANK["installed"]:
            verdict, summary = (
                "unverified",
                f"필수 능력은 설치돼 있으나 {len(unverified)}개는 실행 기록이 없다.",
            )
        elif lowest == LEVEL_RANK["executed_partial"]:
            verdict, summary = (
                "partially_demonstrated",
                "필수 능력 모두 실행 기록이 있으나 일부는 부분 성공이다.",
            )
        else:
            runtime_ok = all(
                g.best and g.best.integration == "creative_runtime" for g in groups
            )
            if not case_ok:
                verdict = "capabilities_executed"
                summary = "필수 능력은 각각 실행 기록이 있으나 이 레시피 조합을 끝까지 만든 기록은 없다."
            elif runtime_ok:
                verdict, summary = (
                    "studio_executable",
                    "필수 능력과 레시피 사례 모두 실행 기록이 있고 제작 런타임에 연결돼 있다.",
                )
            else:
                verdict = "recipe_demonstrated"
                summary = "사례 기록이 있다. 조건부·미검토 판정은 품질 통과가 아니며 완성된 레시피를 입증하지 않는다. 스튜디오 제작 런타임에도 연결되지 않았다(lab 수동 실행)."
        return RecipeEvaluation(
            recipe_id=recipe.id,
            name=recipe.name,
            verdict=verdict,
            summary=summary,
            groups=groups,
            preferred=preferred,
            weakest=weakest,
            blockers=blockers,
            unverified=unverified,
            cases=recipe.cases,
            case_evidence=case_evidence,
            constraints=recipe.constraints,
        )

    def recipe_matrix(self) -> list[RecipeEvaluation]:
        return [self.evaluate_recipe(r) for r in self.recipes]

    def snapshot_meta(self) -> dict[str, Any]:
        if self.snapshot is None:
            return {"available": False}
        return {
            "available": True,
            "generated_at": self.snapshot.get("generated_at"),
            "host": self.snapshot.get("host"),
            "command": self.snapshot.get("command"),
            "probe_count": len(self.snapshot.get("probes", {})),
            "record_count": len(self.snapshot.get("records", {})),
        }

    def summary(self) -> dict[str, Any]:
        matrix = self.recipe_matrix()
        counts: dict[str, int] = {}
        for item in matrix:
            counts[item.verdict] = counts.get(item.verdict, 0) + 1
        return {
            "snapshot": self.snapshot_meta(),
            "capabilities": {
                k: {
                    "name": v[0],
                    "description": v[1],
                    "best_level": self.capability_level(k),
                }
                for k, v in CAPABILITIES.items()
            },
            "tools": [s.model_dump() for s in self.states("tool")],
            "models": [s.model_dump() for s in self.states("model")],
            "skills": [s.model_dump() for s in self.states("skill")],
            "recipe_verdicts": counts,
        }


def default_registry() -> Registry:
    return Registry()
