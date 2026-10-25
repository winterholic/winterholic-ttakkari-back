"""Deterministic eligibility, comparison and explainable selection rules.

Scores order investigation, never certify truth or predict a speedup.
"""

from __future__ import annotations

import math


def applicability(conditions: dict, environment: dict) -> dict:
    missing, mismatches = [], []
    for key, expected in conditions.items():
        actual = environment.get(key)
        if actual is None:
            missing.append(key)
        elif isinstance(expected, dict):
            if (
                not expected
                or set(expected) - {"min", "max"}
                or not isinstance(actual, (int, float))
                or any(not isinstance(v, (int, float)) for v in expected.values())
            ):
                missing.append(key)
            elif ("min" in expected and actual < expected["min"]) or (
                "max" in expected and actual > expected["max"]
            ):
                mismatches.append(key)
        elif actual not in (expected if isinstance(expected, list) else [expected]):
            mismatches.append(key)
    return {
        "status": "incompatible"
        if mismatches
        else "unknown"
        if missing or not conditions
        else "applicable",
        "missing": missing,
        "mismatches": mismatches,
        "unspecified_conditions": not bool(conditions),
    }


def comparable(a: dict, b: dict, vary: list[str] | None = None) -> dict:
    required = (
        "model",
        "model_revision",
        "dataset",
        "dataset_revision",
        "metric",
        "protocol",
        "hardware",
        "software_revision",
    )
    missing = [k for k in required if a.get(k) is None or b.get(k) is None]
    differences = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    controlled = set(vary or [])
    invalid_vary = sorted(
        controlled & {"dataset", "dataset_revision", "metric", "protocol"}
    )
    uncontrolled = sorted(set(differences) - controlled)
    return {
        "comparable": not missing and not uncontrolled and not invalid_vary,
        "missing": missing,
        "differences": sorted(differences),
        "controlled_variables": sorted(controlled),
        "uncontrolled_differences": uncontrolled,
        "invalid_vary": invalid_vary,
    }


def _selection_relation(relation: dict, method_id: str, environment: dict) -> dict:
    """A directed challenge targets to_id; edge and endpoint conditions both restrict it."""
    relation_conditions = relation.get("conditions", {})
    node_conditions = relation.get("other_node", {}).get("conditions", {})
    relation_fit = applicability(relation_conditions, environment)
    node_fit = applicability(node_conditions, environment)
    # An empty condition adds no constraint. If both are empty, applicability is unknown.
    fits = [fit for conditions, fit in [(relation_conditions, relation_fit), (node_conditions, node_fit)] if conditions]
    fits = fits or [applicability({}, environment)]
    status = (
        "incompatible" if any(f["status"] == "incompatible" for f in fits)
        else "unknown" if any(f["status"] == "unknown" for f in fits)
        else "applicable"
    )
    applicable = {
        "status": status,
        "missing": sorted({key for fit in fits for key in fit["missing"]}),
        "mismatches": sorted({key for fit in fits for key in fit["mismatches"]}),
        "unspecified_conditions": not bool(relation_conditions or node_conditions),
    }
    predicate = relation.get("predicate")
    relevant_direction = predicate in {"contradicts", "conflicts_with"} or (
        predicate == "challenges_under_conditions" and relation.get("to_id") == method_id
    )
    return {
        **relation,
        "relation_applicability": relation_fit,
        "other_node_applicability": node_fit,
        "applicability": applicable,
        "affects_selection": relevant_direction and status != "incompatible",
    }


def rank_methods(
    methods: list[dict], environment: dict, budget_seconds: int
) -> list[dict]:
    out = []
    for method in methods:
        fit = applicability(method.get("conditions", {}), environment)
        stored_context = method.get("research_context", {})
        relations = [
            _selection_relation(relation, method.get("id"), environment)
            for relation in stored_context.get("relations", [])
        ]
        context = {"relations": relations, "truncated": bool(stored_context.get("truncated"))}
        # A challenge is an unresolved report, not proof that a method cannot work.
        unresolved = context["truncated"] or any(
            r["affects_selection"]
            for r in relations
        )
        cost = method.get("cost_seconds")
        valid_cost = (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost > 0
        )
        eligibility = (
            "excluded"
            if fit["status"] == "incompatible" or (valid_cost and cost > budget_seconds)
            else (
                "candidate"
                if fit["status"] == "applicable" and valid_cost and not unresolved
                else "needs_review"
            )
        )
        # Missing evidence is an uncertainty to investigate, not a novelty reward.
        out.append(
            {
                **{key: value for key, value in method.items() if key != "research_context"},
                "applicability": fit,
                "eligibility": eligibility,
                "reason": {
                    "conditions": fit,
                    "cost_seconds": cost,
                    "within_budget": valid_cost and cost <= budget_seconds,
                    "evidence_links": method.get("evidence_count", 0),
                    "expected_effect": "unknown_until_measured",
                    "information_value": "unassessed",
                    "evidence_quality": "unassessed",
                    "research_context": context,
                },
                "priority": (
                    0
                    if eligibility == "candidate"
                    else 1
                    if eligibility == "needs_review"
                    else 2
                ),
            }
        )
    return sorted(
        out,
        key=lambda m: (
            m["priority"],
            -(min(m.get("evidence_count", 0), 3)),
            m["title"],
        ),
    )
