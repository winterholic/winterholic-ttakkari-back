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
