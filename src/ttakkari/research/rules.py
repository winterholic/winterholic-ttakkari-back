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


def comparable(a: dict, b: dict) -> dict:
    required = (
        "model",
        "model_revision",
        "dataset",
        "dataset_revision",
        "metric",
        "protocol",
    )
    missing = [k for k in required if a.get(k) is None or b.get(k) is None]
    differences = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    return {
        "comparable": not missing and not differences,
        "missing": missing,
        "differences": sorted(differences),
    }


def rank_methods(
    methods: list[dict], environment: dict, budget_seconds: int
) -> list[dict]:
    out = []
    for method in methods:
        fit = applicability(method.get("conditions", {}), environment)
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
                if fit["status"] == "applicable" and valid_cost
                else "needs_review"
            )
        )
        # Missing evidence is an uncertainty to investigate, not a novelty reward.
        out.append(
            {
                **method,
                "applicability": fit,
                "eligibility": eligibility,
                "reason": {
                    "conditions": fit,
                    "cost_seconds": cost,
                    "within_budget": valid_cost and cost <= budget_seconds,
                    "evidence_links": method.get("evidence_count", 0),
                    "expected_effect": "unknown_until_measured",
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
