"""의도 → 필요 능력 → 실행 가능 Tool/Model/Skill → 레시피 조합 결정.

모든 단계는 결정적이다. 같은 의도와 같은 레지스트리 스냅샷이면 같은 결정과 같은 근거를 낸다.
키워드는 여기서 쓰지 않는다. 입력은 검증된 IntentSpec뿐이다.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import Field

from ttakkari.creative.v2.capabilities import (
    GLOBAL_PREFERS,
    GLOBAL_REQUIRES,
    LEVEL_RANK,
    Level,
    Strict,
)
from ttakkari.creative.v2.catalog_data import ENTRIES
from ttakkari.creative.v2.duration import PLATFORM_CAPS
from ttakkari.creative.v2.intent import IntentSpec, Selection, validate_intent
from ttakkari.creative.v2.registry import Provider, Registry

MATCHING_VERSION = "2026-10-22.1"
ENTRY_ID = re.compile(r"^C\d{2}\.")
VERDICT_RANK = {
    "studio_executable": 5,
    "recipe_demonstrated": 4,
    "capabilities_executed": 3,
    "partially_demonstrated": 2,
    "unverified": 1,
    "blocked": 0,
}
WEIGHT = {"must": 3.0, "should": 2.0, "prefer": 1.0}


class NeedSource(Strict):
    entry_id: str
    relation: Literal["requires", "prefers"]


class CapabilityNeed(Strict):
    options: list[str]
    priority: Literal["must", "should", "prefer"]
    sources: list[NeedSource]
    level: Level
    best: Provider | None
    alternatives: list[Provider]
    status: Literal["executed", "unverified", "missing"]


class ConflictFinding(Strict):
    a: str
    b: str
    kind: Literal["catalog", "constraint"]
    relation_strength: Literal["hard", "soft"]
    resolution: Literal["dropped", "hybrid", "needs_user", "blocked_both_hard"]
    dropped: str | None = None
    rationale: str
    alternatives: list[str] = Field(default_factory=list)


class RecipeCandidate(Strict):
    recipe_id: str
    name: str
    score: float
    fit: float
    coverage: float
    executability: float
    verdict: str
    disqualified: str | None
    reasons: list[str]


class Gap(Strict):
    options: list[str]
    priority: str
    status: str
    why: str
    alternatives: list[str]


class TraceStep(Strict):
    step: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class MatchResult(Strict):
    matching_version: str = MATCHING_VERSION
    status: Literal["ready", "ready_unverified", "blocked", "needs_user_decision"]
    execution_scope: Literal["studio_runtime", "lab_manual", "none"]
    selections: list[Selection]
    dropped: list[dict[str, str]]
    conflicts: list[ConflictFinding]
    needs: list[CapabilityNeed]
    candidates: list[RecipeCandidate]
    primary_recipe: str | None
    auxiliary_recipes: list[str]
    gaps: list[Gap]
    style_resolution_needed: list[str]
    trace: list[TraceStep]
    fingerprint: str


def _selection_weight(sel: Selection) -> float:
    if sel.strength == "hard":
        return 3.0
    return 2.5 if sel.role == "primary" else 1.0


def _priority(sel: Selection, relation: str) -> Literal["must", "should", "prefer"]:
    if relation == "prefers":
        return "prefer"
    return "must" if sel.strength == "hard" else "should"


def _fingerprint(registry: Registry, intent: IntentSpec) -> str:
    states = [(s.id, s.installed, sorted(s.levels.items())) for s in registry.states()]
    payload = json.dumps(
        {
            "v": MATCHING_VERSION,
            "intent": intent.model_dump(mode="json"),
            "states": states,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _need_status(level: Level) -> Literal["executed", "unverified", "missing"]:
    if LEVEL_RANK[level] >= LEVEL_RANK["executed_partial"]:
        return "executed"
    return "unverified" if level == "installed" else "missing"


def _find_conflicts(
    intent: IntentSpec, selections: list[Selection]
) -> tuple[list[ConflictFinding], list[Selection], list[dict[str, str]]]:
    by_id = {s.entry_id: s for s in selections}
    findings: list[ConflictFinding] = []
    dropped: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for sel in selections:
        for rel in ENTRIES[sel.entry_id].relations:
            if rel.type != "conflicts_with" or rel.target not in by_id:
                continue
            pair = tuple(sorted((sel.entry_id, rel.target)))
            if pair in seen:
                continue
            seen.add(pair)  # type: ignore[arg-type]
            a, b = by_id[pair[0]], by_id[pair[1]]
            alts = sorted(
                {
                    r.target
                    for e in (a.entry_id, b.entry_id)
                    for r in ENTRIES[e].relations
                    if r.type == "alternative_to"
                }
            )
            if rel.strength == "soft":
                findings.append(
                    ConflictFinding(
                        a=a.entry_id,
                        b=b.entry_id,
                        kind="catalog",
                        relation_strength="soft",
                        resolution="hybrid",
                        rationale="두 항목은 혼합할 수 있지만 규칙이 부딪친다. Style DNA에서 충돌 해결을 명시해야 한다.",
                        alternatives=alts,
                    )
                )
                continue
            if a.strength == "hard" and b.strength == "hard":
                findings.append(
                    ConflictFinding(
                        a=a.entry_id,
                        b=b.entry_id,
                        kind="catalog",
                        relation_strength="hard",
                        resolution="blocked_both_hard",
                        rationale="둘 다 필수로 지정됐지만 동시에 성립하지 않는다. 사용자가 하나를 고르거나 시퀀스를 나눠야 한다.",
                        alternatives=alts,
                    )
                )
                continue
            loser = None
            if a.strength == "hard" or b.strength == "hard":
                loser = b if a.strength == "hard" else a
                reason = f"{(a if loser is b else b).entry_id}가 필수라서 {loser.entry_id}를 제외했다."
            elif (a.role == "primary") != (b.role == "primary"):
                loser = b if a.role == "primary" else a
                reason = f"{(a if loser is b else b).entry_id}가 주 선택이라 {loser.entry_id}를 제외했다."
            if loser is None:
                findings.append(
                    ConflictFinding(
                        a=a.entry_id,
                        b=b.entry_id,
                        kind="catalog",
                        relation_strength="hard",
                        resolution="needs_user",
                        rationale="둘 다 선호이고 주 선택도 아니라 자동으로 고를 근거가 없다.",
                        alternatives=alts,
                    )
                )
            else:
                dropped[loser.entry_id] = reason
                findings.append(
                    ConflictFinding(
                        a=a.entry_id,
                        b=b.entry_id,
                        kind="catalog",
                        relation_strength="hard",
                        resolution="dropped",
                        dropped=loser.entry_id,
                        rationale=reason,
                        alternatives=alts,
                    )
                )
    # 플랫폼 상한 제약: Shorts는 근거 있는 상한이 있다.
    if "C01.shorts" in by_id and "C01.shorts" not in dropped:
        cap = PLATFORM_CAPS["youtube_shorts"]
        req = intent.duration
        if (
            req.seconds is not None
            and req.mode in ("exact", "preferred")
            and req.seconds > cap["max_seconds"]
        ):
            exact = req.mode == "exact"
            findings.append(
                ConflictFinding(
                    a="C01.shorts",
                    b="duration",
                    kind="constraint",
                    relation_strength="hard" if exact else "soft",
                    resolution="needs_user" if exact else "hybrid",
                    rationale=f"쇼츠 상한 {cap['max_seconds']:g}초(근거 {cap['source']}, 확인 {cap['as_of']})보다 긴 {req.seconds:g}초가 요청됐다.",
                    alternatives=["C01.tech_explainer", "C01.documentary", "분할 게시"],
                )
            )
    kept = [s for s in selections if s.entry_id not in dropped]
    return (
        findings,
        kept,
        [{"entry_id": k, "reason": v} for k, v in sorted(dropped.items())],
    )


def _derive_needs(
    registry: Registry, selections: list[Selection]
) -> tuple[list[CapabilityNeed], list[str]]:
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    notes: list[str] = []
    selected_ids = {s.entry_id for s in selections}
    for sel in selections:
        for rel in ENTRIES[sel.entry_id].relations:
            if rel.type not in ("requires", "prefers"):
                continue
            if ENTRY_ID.match(rel.target):
                if rel.target not in selected_ids and rel.type == "requires":
                    notes.append(
                        f"{sel.entry_id}는 {rel.target}를 요구하지만 선택되지 않았다."
                    )
                continue
            key = tuple(sorted(rel.target.split("|")))
            slot = groups.setdefault(key, {"priority": "prefer", "sources": []})
            priority = _priority(sel, rel.type)
            if WEIGHT[priority] > WEIGHT[slot["priority"]]:
                slot["priority"] = priority
            slot["sources"].append(NeedSource(entry_id=sel.entry_id, relation=rel.type))  # type: ignore[arg-type]
    for cap in GLOBAL_REQUIRES:
        slot = groups.setdefault((cap,), {"priority": "prefer", "sources": []})
        slot["priority"] = "must"
    for cap in GLOBAL_PREFERS:
        groups.setdefault((cap,), {"priority": "prefer", "sources": []})
    needs: list[CapabilityNeed] = []
    for key in sorted(groups, key=lambda k: (-WEIGHT[groups[k]["priority"]], k)):
        slot = groups[key]
        group = registry._group(list(key), True)
        needs.append(
            CapabilityNeed(
                options=list(key),
                priority=slot["priority"],
                sources=slot["sources"],
                level=group.level,
                best=group.best,
                alternatives=group.alternatives,
                status=_need_status(group.level),
            )
        )
    return needs, notes


def _recipe_universe(recipe) -> set[str]:
    return {c for g in [*recipe.requires, *recipe.prefers] for c in g}


def _score_recipes(
    registry: Registry, selections: list[Selection], needs: list[CapabilityNeed]
) -> list[RecipeCandidate]:
    total_weight = sum(_selection_weight(s) for s in selections) or 1.0
    need_weight = sum(WEIGHT[n.priority] for n in needs) or 1.0
    by_id = {s.entry_id: s for s in selections}
    out: list[RecipeCandidate] = []
    for recipe in registry.recipes.values():
        evaluation = registry.evaluate_recipe(recipe.id)
        reasons: list[str] = []
        matched = [e for e in recipe.fits if e in by_id]
        fit = sum(_selection_weight(by_id[e]) for e in matched) / total_weight
        reasons.append("맞는 선택: " + (", ".join(matched) if matched else "없음"))
        universe = _recipe_universe(recipe)
        covered = [n for n in needs if set(n.options) & universe]
        coverage = sum(WEIGHT[n.priority] for n in covered) / need_weight
        reasons.append(f"필요 능력 {len(covered)}/{len(needs)}개를 이 레시피가 다룬다")
        executability = VERDICT_RANK[evaluation.verdict] / 5
        reasons.append(f"실행 판정 {evaluation.verdict}")
        disq = None
        for e in recipe.conflicts:
            if e in by_id and (
                by_id[e].strength == "hard" or by_id[e].role == "primary"
            ):
                disq = f"{e}가 필수 또는 주 선택이라 이 레시피와 충돌한다"
            elif e in by_id:
                reasons.append(f"{e}는 이 레시피와 충돌(감점)")
                fit = max(0.0, fit - 0.25 * _selection_weight(by_id[e]) / total_weight)
        score = round(0.40 * fit + 0.30 * coverage + 0.30 * executability, 4)
        out.append(
            RecipeCandidate(
                recipe_id=recipe.id,
                name=recipe.name,
                score=0.0 if disq else score,
                fit=round(fit, 4),
                coverage=round(coverage, 4),
                executability=round(executability, 4),
                verdict=evaluation.verdict,
                disqualified=disq,
                reasons=reasons,
            )
        )
    return sorted(
        out, key=lambda c: (c.disqualified is not None, -c.score, c.recipe_id)
    )


def _substitutes(
    registry: Registry, entry_id: str, blocked_options: set[str]
) -> list[str]:
    """막힌 능력을 요구하는 항목을 대신할 수 있는 대안 항목 중, 요구 능력이 충족되는 것."""
    found = []
    for rel in ENTRIES[entry_id].relations:
        if rel.type != "alternative_to" or rel.target not in ENTRIES:
            continue
        reqs = [
            r.target
            for r in ENTRIES[rel.target].relations
            if r.type == "requires" and not ENTRY_ID.match(r.target)
        ]
        req_caps = {c for t in reqs for c in t.split("|")}
        if (
            all(
                registry._group(t.split("|"), True).level not in ("missing", "declared")
                for t in reqs
            )
            and not req_caps & blocked_options
        ):
            found.append(rel.target)
    return found


def match_intent(
    brief: str, intent: IntentSpec, registry: Registry | None = None
) -> MatchResult:
    registry = registry or Registry()
    validation = validate_intent(brief, intent)
    if not validation.valid:
        raise ValueError(
            "의도 검증 실패: "
            + "; ".join(f"{e.path} {e.message}" for e in validation.errors)
        )
    trace: list[TraceStep] = []
    trace.append(
        TraceStep(
            step="intent",
            summary=f"선택 {len(intent.selections)}개, 제약 {len(intent.constraints)}개, 추출 방식 {intent.extraction.method}",
            data={"warnings": [w.code for w in validation.warnings]},
        )
    )

    conflicts, selections, dropped = _find_conflicts(intent, list(intent.selections))
    trace.append(
        TraceStep(
            step="conflicts",
            summary=f"충돌 {len(conflicts)}건, 제외 {len(dropped)}건",
            data={"resolutions": [c.resolution for c in conflicts]},
        )
    )

    needs, notes = _derive_needs(registry, selections)
    trace.append(
        TraceStep(
            step="needs",
            summary=f"필요 능력 묶음 {len(needs)}개(must {sum(n.priority == 'must' for n in needs)}, should {sum(n.priority == 'should' for n in needs)}, prefer {sum(n.priority == 'prefer' for n in needs)})",
            data={"notes": notes},
        )
    )

    candidates = _score_recipes(registry, selections, needs)
    viable = [c for c in candidates if c.disqualified is None and c.fit > 0]
    primary = viable[0] if viable else None
    auxiliary: list[str] = []
    if primary:
        primary_recipe = registry.recipes[primary.recipe_id]
        uncovered = [
            n
            for n in needs
            if n.priority == "must"
            and not (set(n.options) & _recipe_universe(primary_recipe))
        ]
        for _ in range(2):
            if not uncovered:
                break
            pool = [
                (
                    sum(
                        1
                        for n in uncovered
                        if set(n.options)
                        & _recipe_universe(registry.recipes[c.recipe_id])
                    ),
                    c,
                )
                for c in candidates
                # 막힌 레시피를 보조로 끌어오면 새 필수 능력 누락만 늘어난다.
                if c.disqualified is None
                and c.verdict != "blocked"
                and c.recipe_id not in (primary.recipe_id, *auxiliary)
            ]
            pool = [(gain, c) for gain, c in pool if gain > 0]
            if not pool:
                break
            _, best = min(pool, key=lambda x: (-x[0], -x[1].score, x[1].recipe_id))
            auxiliary.append(best.recipe_id)
            uncovered = [
                n
                for n in uncovered
                if not (
                    set(n.options) & _recipe_universe(registry.recipes[best.recipe_id])
                )
            ]
        trace.append(
            TraceStep(
                step="recipes",
                summary=f"주 레시피 {primary.recipe_id} {primary.name}(점수 {primary.score}), 보조 {', '.join(auxiliary) or '없음'}",
                data={"top": [(c.recipe_id, c.score) for c in candidates[:5]]},
            )
        )
    else:
        trace.append(
            TraceStep(
                step="recipes",
                summary="맞는 레시피가 없다. 선택 항목을 다루는 레시피가 없거나 모두 충돌한다.",
                data={"top": [(c.recipe_id, c.score) for c in candidates[:5]]},
            )
        )

    # 선택된 레시피의 필수 능력도 최종 필요 능력에 합친다.
    chosen = [
        registry.recipes[r]
        for r in ([primary.recipe_id] if primary else []) + auxiliary
    ]
    known = {tuple(n.options) for n in needs}
    final = list(needs)
    for recipe in chosen:
        for group in recipe.requires:
            key = tuple(sorted(group))
            if key in known:
                continue
            known.add(key)
            res = registry._group(list(key), True)
            final.append(
                CapabilityNeed(
                    options=list(key),
                    priority="must",
                    sources=[],
                    level=res.level,
                    best=res.best,
                    alternatives=res.alternatives,
                    status=_need_status(res.level),
                )
            )

    gaps: list[Gap] = []
    for n in final:
        if n.priority == "prefer" and n.status != "missing":
            continue
        if n.status == "executed":
            continue
        subs: list[str] = []
        for src in n.sources:
            subs += _substitutes(registry, src.entry_id, set(n.options))
        lower = [
            f"{p.entry_id}({p.level})"
            for p in n.alternatives
            if LEVEL_RANK[p.level] > LEVEL_RANK["missing"]
        ]
        why = (
            "이 능력을 제공하는 항목이 없다"
            if n.status == "missing"
            else f"설치만 확인됐고 실행 기록이 없다({n.best.entry_id if n.best else '-'})"
        )
        gaps.append(
            Gap(
                options=n.options,
                priority=n.priority,
                status=n.status,
                why=why,
                alternatives=sorted(set(subs)) + lower,
            )
        )

    must_missing = [g for g in gaps if g.priority == "must" and g.status == "missing"]
    must_unverified = [
        g for g in gaps if g.priority == "must" and g.status == "unverified"
    ]
    blocking_conflicts = [
        c for c in conflicts if c.resolution in ("needs_user", "blocked_both_hard")
    ]
    wired = bool(final) and all(
        n.best and n.best.integration == "creative_runtime"
        for n in final
        if n.priority == "must"
    )
    if blocking_conflicts or primary is None:
        status = "needs_user_decision"
        scope = "none"
    elif must_missing:
        status, scope = "blocked", "none"
    elif must_unverified:
        status, scope = "ready_unverified", "lab_manual"
    else:
        status, scope = "ready", "studio_runtime" if wired else "lab_manual"
    trace.append(
        TraceStep(
            step="decision",
            summary=f"결정 {status}, 실행 범위 {scope}",
            data={
                "missing_must": [g.options for g in must_missing],
                "unverified_must": [g.options for g in must_unverified],
                "blocking_conflicts": [(c.a, c.b) for c in blocking_conflicts],
            },
        )
    )

    style_needed = [
        f"{c.a} × {c.b}"
        for c in conflicts
        if c.resolution == "hybrid" and c.kind == "catalog"
    ]
    return MatchResult(
        status=status,
        execution_scope=scope,
        selections=selections,
        dropped=dropped,
        conflicts=conflicts,
        needs=final,
        candidates=candidates,
        primary_recipe=primary.recipe_id if primary else None,
        auxiliary_recipes=auxiliary,
        gaps=gaps,
        style_resolution_needed=style_needed,
        trace=trace,
        fingerprint=_fingerprint(registry, intent),
    )
