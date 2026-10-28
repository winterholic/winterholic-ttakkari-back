"""Research invariants: unknown applicability is never compatibility."""

import uuid

import pydantic
import pytest

from ttakkari.api.research import SelectionIn
from ttakkari.research.rules import applicability, comparable, rank_methods
from ttakkari.runs import profiles


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
            "id": "conflict", "from_id": "claim", "to_id": "familiar", "predicate": "challenges_under_conditions", "conditions": {"dataset": "A"},
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


@pytest.mark.parametrize('predicate,expected', [('challenges_under_conditions', 'candidate'), ('conflicts_with', 'needs_review'), ('contradicts', 'needs_review')])
def test_directed_challenge_affects_only_target_but_symmetric_conflicts_affect_both(predicate, expected):
    method = {'id': 'method', 'title': 'method', 'conditions': {'backend': 'metal'}, 'cost_seconds': 1,
              'research_context': {'relations': [{'id': 'edge', 'predicate': predicate,
                  'from_id': 'method', 'to_id': 'hypothesis', 'conditions': {'dataset': 'B'},
                  'other_node': {'id': 'hypothesis', 'conditions': {'dataset': 'B'}}}], 'truncated': False}}
    result = rank_methods([method], {'backend': 'metal', 'dataset': 'B'}, 10)[0]
    assert result['eligibility'] == expected
    edge = result['reason']['research_context']['relations'][0]
    assert edge['affects_selection'] is (predicate != 'challenges_under_conditions')


@pytest.mark.parametrize('edge_conditions,node_conditions,environment,expected', [
    ({'dataset': 'B'}, {'dataset': 'A'}, {'backend': 'metal', 'dataset': 'B'}, 'candidate'),
    ({}, {'dataset': 'A'}, {'backend': 'metal', 'dataset': 'A'}, 'needs_review'),
    ({'dataset': 'A'}, {}, {'backend': 'metal', 'dataset': 'A'}, 'needs_review'),
    ({'dataset': 'A'}, {'hardware': 'm5'}, {'backend': 'metal', 'dataset': 'A'}, 'needs_review'),
    ({'dataset': 'A'}, {'hardware': 'm5'}, {'backend': 'metal', 'dataset': 'A', 'hardware': 'm4'}, 'candidate'),
])
def test_relation_and_other_node_conditions_are_conjunctive_without_overwriting(edge_conditions, node_conditions, environment, expected):
    method = {'id': 'method', 'title': 'method', 'conditions': {'backend': 'metal'}, 'cost_seconds': 1,
              'research_context': {'relations': [{'id': 'edge', 'predicate': 'challenges_under_conditions',
                  'from_id': 'hypothesis', 'to_id': 'method', 'conditions': edge_conditions,
                  'other_node': {'id': 'hypothesis', 'conditions': node_conditions}}], 'truncated': False}}
    result = rank_methods([method], environment, 10)[0]
    assert result['eligibility'] == expected
    edge = result['reason']['research_context']['relations'][0]
    assert edge['conditions'] == edge_conditions and edge['other_node']['conditions'] == node_conditions
    assert 'other_node_applicability' in edge


def test_catalog_selection_request_model_accepts_catalog_source_and_rejects_invalid():
    """Validates SelectionIn schema contract from dac9737b (candidate_source='catalog')."""
    default_req = SelectionIn()
    assert default_req.candidate_source == "research"

    catalog_req = SelectionIn(candidate_source="catalog", query="kv_cache", budget_seconds=120)
    assert catalog_req.candidate_source == "catalog"
    assert catalog_req.query == "kv_cache"
    assert catalog_req.budget_seconds == 120

    with pytest.raises(pydantic.ValidationError):
        SelectionIn(candidate_source="unsupported_source")


def test_catalog_selection_novel_candidate_without_cost_requires_review():
    """Catalog candidates without an underlying method Node (cost_seconds=None) require review (dac9737b)."""
    method = {
        "id": "catalog-m1",
        "title": "Unmeasured Catalog Method",
        "candidate_source": "catalog",
        "conditions": {"backend": ["metal"]},
        "cost_seconds": None,
        "evidence_count": 0,
        "execution_status": "hypothesis_stage",
        "is_executable": False,
    }
    ranked = rank_methods([method], {"backend": "metal"}, 60)
    assert len(ranked) == 1
    assert ranked[0]["id"] == "catalog-m1"
    assert ranked[0]["eligibility"] == "needs_review"
    assert ranked[0]["reason"]["expected_effect"] == "unknown_until_measured"
    assert ranked[0]["reason"]["information_value"] == "unassessed"


def test_catalog_selection_direct_failures_distinguish_applicable_and_conflicting_conditions():
    """Direct catalog failures (selection_failures with catalog_ids) filter by applicability (dac9737b)."""
    base = {
        "id": "catalog-m2",
        "title": "Catalog Method With Failure History",
        "candidate_source": "catalog",
        "conditions": {"backend": "metal"},
        "cost_seconds": 10,
        "evidence_count": 2,
    }

    # Case A: Applicable failure under matching conditions -> forces needs_review
    matching_failure = {
        "experiment_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "declared_environment": {"dataset": "A"},
        "observed_environment": {"dataset": "A", "research_execution_owner": "worker-1"},
    }
    method_a = {
        **base,
        "research_context": {"truncated": False, "failures": [matching_failure]},
    }
    ranked_a = rank_methods([method_a], {"backend": "metal", "dataset": "A"}, 60)
    assert ranked_a[0]["eligibility"] == "needs_review"
    fail_a = ranked_a[0]["reason"]["research_context"]["failures"][0]
    assert fail_a["applicability"]["status"] == "applicable"
    assert fail_a["affects_selection"] is True
    assert fail_a["execution_metadata_fields"] == ["research_execution_owner"]

    # Case B: Incompatible failure (different backend) -> affects_selection is False, remains candidate
    incompatible_failure = {
        "experiment_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "declared_environment": {"backend": "cuda"},
        "observed_environment": {"backend": "cuda"},
    }
    method_b = {
        **base,
        "research_context": {"truncated": False, "failures": [incompatible_failure]},
    }
    ranked_b = rank_methods([method_b], {"backend": "metal"}, 60)
    assert ranked_b[0]["eligibility"] == "candidate"
    fail_b = ranked_b[0]["reason"]["research_context"]["failures"][0]
    assert fail_b["applicability"]["status"] == "incompatible"
    assert fail_b["affects_selection"] is False

    # Case C: Conflicting failure (declared vs observed mismatch) -> unknown status, affects_selection True
    conflicting_failure = {
        "experiment_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "declared_environment": {"backend": "metal"},
        "observed_environment": {"backend": "cuda"},
    }
    method_c = {
        **base,
        "research_context": {"truncated": False, "failures": [conflicting_failure]},
    }
    ranked_c = rank_methods([method_c], {"backend": "metal"}, 60)
    assert ranked_c[0]["eligibility"] == "needs_review"
    fail_c = ranked_c[0]["reason"]["research_context"]["failures"][0]
    assert fail_c["applicability"]["status"] == "unknown"
    assert fail_c["environment_conflicts"] == ["backend"]
    assert fail_c["affects_selection"] is True


def test_schedule_quota_wait_profile_contract_and_model_capacity_key():
    """Quota wait manager._wait_for_reset contract requires Profile with .engine and .model (beea9a89)."""
    # Real Profile dataclass has model: str | None = None
    prof = profiles.Profile(id=uuid.uuid4(), name="claude-main", engine="claude")
    assert prof.model is None

    # model_capacity_key succeeds with None model (maps to 'default')
    key_default = profiles.model_capacity_key(prof.engine, prof.model)
    assert isinstance(key_default, uuid.UUID)

    # Key with explicit model is distinct
    key_model = profiles.model_capacity_key("claude", "claude-sonnet-5-5-high")
    assert isinstance(key_model, uuid.UUID)
    assert key_default != key_model

    # Passing an object lacking .model (such as the legacy FakeProfile) raises AttributeError
    class LegacyFakeProfile:
        def __init__(self, name: str, engine: str):
            self.name = name
            self.engine = engine

    fake = LegacyFakeProfile("claude-main", "claude")
    with pytest.raises(AttributeError):
        profiles.model_capacity_key(fake.engine, fake.model)

