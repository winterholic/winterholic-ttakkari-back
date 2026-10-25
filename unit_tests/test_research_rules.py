"""Research invariants: unknown applicability is never compatibility."""

import pytest

from ttakkari.research.rules import applicability, comparable, rank_methods


def test_unknown_environment_and_mismatch_are_distinct():
    assert (
        applicability({"backend": ["cuda"]}, {"backend": "metal"})["status"]
        == "incompatible"
    )
    assert applicability({"backend": ["metal"]}, {})["status"] == "unknown"
    assert (
        applicability({"backend": ["metal"]}, {"backend": "metal"})["status"]
        == "applicable"
    )
    assert applicability({}, {"backend": "metal"})["status"] == "unknown"


def test_missing_or_different_comparison_conditions_cannot_be_fair():
    assert not comparable({}, {})["comparable"]
    a = {
        "model": "qwen",
        "model_revision": "a",
        "dataset": "eval",
        "dataset_revision": "v1",
        "metric": "recall",
        "protocol": "p1",
        "hardware": "Mac Studio",
        "software_revision": "abc",
    }
    assert comparable(a, a)["comparable"]
    assert not comparable(a, {**a, "dataset_revision": "v2"})["comparable"]


def test_controlled_treatment_is_not_an_uncontrolled_difference():
    a = {
        "model": "qwen",
        "model_revision": "a",
        "dataset": "eval",
        "dataset_revision": "v1",
        "metric": "throughput",
        "protocol": "p1",
        "hardware": "m5",
        "software_revision": "s1",
        "cache": "f16",
    }
    b = {**a, "cache": "q8"}
    assert not comparable(a, b)["comparable"]
    assert comparable(a, b, vary=["cache"])["comparable"]


def test_novel_method_and_unknown_cost_are_visible_without_automatic_preference():
    methods = [
        {
            "id": "new",
            "title": "unexpected method",
            "conditions": {"backend": ["metal"]},
            "cost_seconds": None,
            "evidence_count": 0,
        },
        {
            "id": "cuda",
            "title": "cuda method",
            "conditions": {"backend": ["cuda"]},
            "cost_seconds": 1,
            "evidence_count": 5,
        },
    ]
    out = rank_methods(methods, {"backend": "metal"}, 60)
    assert {x["id"] for x in out} == {"new", "cuda"}
    assert next(x for x in out if x["id"] == "new")["eligibility"] == "needs_review"
    assert next(x for x in out if x["id"] == "cuda")["eligibility"] == "excluded"


@pytest.mark.parametrize(
    "conditions", [{"ram_gb": {"min": 128}}, {"ram_gb": {"max": 32}}]
)
def test_numeric_resource_constraints(conditions):
    assert applicability(conditions, {"ram_gb": 64})["status"] == "incompatible"


@pytest.mark.parametrize(
    ("dataset", "expected"), [("A", "needs_review"), ("B", "candidate"), (None, "needs_review")]
)
def test_conflicting_report_requires_review_only_in_relevant_or_unknown_conditions(dataset, expected):
    methods = [{
        "id": "familiar", "title": "A familiar method", "conditions": {"backend": "metal"},
        "cost_seconds": 1, "evidence_count": 100,
        "research_context": {"truncated": False, "relations": [{
            "id": "conflict", "predicate": "challenges_under_conditions", "conditions": {"dataset": "A"},
            "other_node": {"id": "claim", "kind": "claim", "epistemic_status": "source_reported"},
        }]},
    }, {"id": "novel", "title": "Z new hypothesis", "conditions": {"backend": "metal"},
        "cost_seconds": 1, "evidence_count": 0}]
    environment = {"backend": "metal", **({"dataset": dataset} if dataset else {})}
    ranked = rank_methods(methods, environment, 60)
    familiar = next(m for m in ranked if m["id"] == "familiar")
    assert familiar["eligibility"] == expected
    assert familiar["reason"]["research_context"]["relations"][0]["applicability"]["status"] == (
        "unknown" if dataset is None else "applicable" if dataset == "A" else "incompatible"
    )
    assert familiar["reason"]["expected_effect"] == "unknown_until_measured"
    assert familiar["reason"]["information_value"] == "unassessed"
    if expected == "needs_review":
        assert ranked[0]["id"] == "novel"  # Links never override a relevant unresolved conflict.


def test_past_failure_and_combination_hypothesis_remain_visible_without_excluding_method():
    relations = [{"id": "past-failure", "predicate": "failed_under_conditions", "conditions": {"backend": "cuda"}},
                 {"id": "combination", "predicate": "combination_hypothesis", "conditions": {"backend": "metal"}}]
    result = rank_methods([{"id": "m", "title": "method", "conditions": {"backend": "metal"},
                            "cost_seconds": 2, "research_context": {"relations": relations, "truncated": False}}],
                          {"backend": "metal"}, 10)[0]
    assert result["eligibility"] == "candidate"
    context = result["reason"]["research_context"]
    assert [r["id"] for r in context["relations"]] == ["past-failure", "combination"]
    assert context["relations"][0]["applicability"]["status"] == "incompatible"
    assert result["reason"]["information_value"] == "unassessed"


def test_truncated_context_requires_review_and_preserves_hard_exclusion():
    methods = [{"id": "m", "title": "method", "conditions": {"backend": "metal"},
                "cost_seconds": 2, "research_context": {"relations": [], "truncated": True}}]
    assert rank_methods(methods, {"backend": "metal"}, 10)[0]["eligibility"] == "needs_review"
    assert rank_methods(methods, {"backend": "cuda"}, 10)[0]["eligibility"] == "excluded"
