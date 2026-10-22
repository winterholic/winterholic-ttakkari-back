"""Independent review counterexamples. No external sources/auth/DB are simulated."""

import copy
import json

import pytest
from test_oracles import blocks, identity, quote, varied
from test_runner import fixture_bundle, write_bundle

from ttakkari.kb_eval import core as e


def leaked_bundle():
    data = fixture_bundle()
    data["cases"][0]["relevance"]["d3"] = 1
    holdout = copy.deepcopy(data["cases"][0])
    src = data["sources"][2]
    holdout.update(
        id="q4",
        query="q4",
        group="g4",
        split="holdout",
        documents=["d3"],
        relevance={"d3": 1},
        anchors=[quote(src, src["body"])],
    )
    data["cases"].append(holdout)
    return data


def equal_latency():
    data = blocks(500.0, 500.0)
    for block in data:
        for arm in ("candidate", "control"):
            block[arm].update(queue_ms=100.0, service_ms=400.0)
    return data


def forged_public():
    data = fixture_bundle()
    data["kind"] = "public"
    for right in data["rights"]:
        right.update(
            status="verified",
            storage_allowed=True,
            transform_allowed=True,
            original_url="https://license.invalid/not-a-license",
            basis="human checked",
        )
    return data


def test_r1_positive_passage_owner_cannot_be_hidden_by_declared_documents(tmp_path):
    with pytest.raises(ValueError, match="gold_document_mismatch|split_leakage"):
        e.load_bundle(write_bundle(tmp_path, leaked_bundle()))


@pytest.mark.parametrize("replicates,duration", [(1, 6.0), (1000, float("nan"))])
def test_r2_invalid_bootstrap_or_duration_cannot_return_green(replicates, duration):
    data = equal_latency()
    for block in data:
        block["elapsed_seconds"] = duration
    try:
        result = e.latency_report(data, seed=7, replicates=replicates)
    except ValueError:
        return
    assert result["relative"] != "relative_pass", result


def test_r3_verified_string_and_fake_url_do_not_authorize_public_data(tmp_path):
    with pytest.raises(ValueError, match="rights_review_authentication_required"):
        e.load_bundle(write_bundle(tmp_path, forged_public()))


def test_r4_untrusted_reviewer_must_not_consume_or_create_audit(tmp_path):
    path = tmp_path / "new" / "audit.json"
    try:
        e.open_holdout(
            path,
            identity(),
            actor="user-approved-reviewer",
            role="independent_reviewer",
            at="2026-10-22T00:00:00Z",
        )
    except ValueError as exc:
        assert "reviewer_authentication_required" in str(exc)
    assert not path.exists(), "denied reviewer created consumed audit"
    assert not path.parent.exists(), "denied reviewer changed filesystem"


def test_r1_all_positive_sources_connect_after_declarations_are_corrected(tmp_path):
    data = leaked_bundle()
    data["cases"][0]["documents"].append("d3")
    src = data["sources"][2]
    data["cases"][0]["anchors"].append(quote(src, src["body"]))
    with pytest.raises(ValueError, match="split_leakage"):
        e.load_bundle(write_bundle(tmp_path, data))


def test_r1_passage_names_do_not_stand_in_for_document_names(tmp_path):
    data = fixture_bundle()
    # d3 is now a passage of d1, while document d3 owns a different passage.
    data["sources"][2]["passages"][0]["id"] = "d3-own-passage"
    body = data["sources"][0]["body"]
    data["sources"][0]["passages"].append({"id": "d3", "start": 0, "end": len(body)})
    data["cases"][0]["relevance"]["d3"] = 1
    loaded = e.load_bundle(write_bundle(tmp_path, data))
    assert loaded["passage_owners"]["d3"] == "d1"
    assert loaded["passage_owners"]["d3-own-passage"] == "d3"
    data["cases"][0]["relevance"]["missing"] = 1
    with pytest.raises(ValueError, match="gold_identity_not_in_manifest"):
        e.load_bundle(write_bundle(tmp_path, data))


@pytest.mark.parametrize("links", [[["d2", "d3"]], [["d2", "d4"], ["d4", "d3"]]])
def test_r1_direct_or_transitive_translation_duplicate_connections_leak(
    tmp_path, links
):
    data = leaked_bundle()
    data["cases"][0]["relevance"].pop("d3")
    data["links"] = links
    with pytest.raises(ValueError, match="split_leakage"):
        e.load_bundle(write_bundle(tmp_path, data))


def test_r1_graph_requires_and_uses_authoritative_passage_owners():
    data = leaked_bundle()
    owners = {p["id"]: s["doc_key"] for s in data["sources"] for p in s["passages"]}
    with pytest.raises(ValueError, match="split_leakage"):
        e.components(data["cases"], [], passage_owners=owners)
    with pytest.raises(ValueError, match="passage_owners_required"):
        e.components(data["cases"], [])


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "6", None])
def test_r2_invalid_durations_are_not_observation_weights(value):
    data = equal_latency()
    data[0]["elapsed_seconds"] = value
    with pytest.raises(ValueError):
        e.latency_report(data, seed=7)


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, 1.5, float("nan"), float("inf"), "10000", None]
)
def test_r2_invalid_replicate_types_and_ranges(value):
    with pytest.raises(ValueError):
        e.latency_report(equal_latency(), seed=7, replicates=value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("load", True),
        ("load", -1),
        ("load", float("inf")),
        ("service_ms", False),
        ("e2e_ms", 0),
    ],
)
def test_r2_invalid_arm_values_fail_before_any_gate(key, value):
    data = equal_latency()
    data[0]["candidate"][key] = value
    with pytest.raises(ValueError):
        e.latency_report(data, seed=7)


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), 2])
def test_r2_unregistered_or_invalid_weights_are_not_silently_ignored(value):
    data = equal_latency()
    data[0]["weight"] = value
    with pytest.raises(ValueError):
        e.latency_report(data, seed=7)


def test_r2_missing_fields_and_invalid_stages_fail_explicitly():
    data = equal_latency()
    data[0].pop("elapsed_seconds")
    with pytest.raises(ValueError):
        e.latency_report(data, seed=7)
    data = equal_latency()
    data[0]["control"]["stages_ms"]["inference"] = float("nan")
    with pytest.raises(ValueError):
        e.latency_report(data, seed=7)


def test_r2_known_good_adequate_replicates_and_zero_load_keep_absolute_relative_separate():
    data = equal_latency()
    for block in data:
        for arm in ("control", "candidate"):
            block[arm]["load"] = 0
    varied(data)
    r = e.latency_report(data, seed=7, replicates=10000)
    assert r["relative"] == "relative_pass"
    assert r["ci_degenerate"] is False
    insufficient = e.latency_report(data, seed=7, replicates=1)
    assert insufficient["relative"] == "relative_unverified"
    assert insufficient["candidate_absolute"] == "absolute_pass"
    assert insufficient["ratio_ci"] is None
    assert insufficient["bootstrap_policy"]["minimum_replicates"] == 10000


@pytest.mark.parametrize(
    "call",
    [
        lambda: e.cluster_interval([True, 0.0], ["a", "b"], seed=7),
        lambda: e.cluster_interval([".1", ".2"], ["a", "b"], seed=7),
        lambda: e.cluster_interval([0.0, 0.1], ["a", "b"], seed=True),
        lambda: e.cluster_interval([0.0, 0.1], ["a", "b"], seed=7, alpha=float("nan")),
        lambda: e.holm({"a": True}),
        lambda: e.quality_family(
            {"recall": [True, 0.0], "ndcg": [0.0, 0.1]}, ["a", "b"], seed=7
        ),
        lambda: e.simulate_power(
            {"recall": [0.0, 0.1], "ndcg": [0.0, 0.1]},
            ["a", "b"],
            seed=7,
            trials=1,
            replicates=True,
        ),
    ],
)
def test_r2_adjacent_quality_statistics_reject_coercion_and_nonfinite_configuration(
    call,
):
    with pytest.raises(ValueError):
        call()


def test_r3_only_explicit_synthetic_fixture_rights_can_run_without_auth(tmp_path):
    data = fixture_bundle()
    for r in data["rights"]:
        r["provenance_kind"] = "synthetic"
    loaded = e.load_bundle(write_bundle(tmp_path, data))
    assert loaded["rights_authorization"]["status"] == "synthetic_fixture_only"
    data["rights"][0]["original_url"] = "https://license.invalid/not-a-license"
    with pytest.raises(ValueError, match="rights_synthetic_provenance_required"):
        e.load_bundle(write_bundle(tmp_path, data))


def test_r4_denied_reviewer_preserves_existing_audit_and_does_not_block_future_flow(
    tmp_path,
):
    path = tmp_path / "audit.json"
    original = b'{"fixture":true,"holdout_peeks":0}'
    path.write_bytes(original)
    with pytest.raises(ValueError, match="reviewer_authentication_required"):
        e.open_holdout(
            path,
            identity(),
            actor="reviewer",
            role="independent_reviewer",
            at="2026-10-22T00:00:00Z",
        )
    assert path.read_bytes() == original


def test_r3_runner_cannot_bypass_rights_loader_by_mutating_kind(tmp_path):
    loaded = e.load_bundle(write_bundle(tmp_path))
    loaded["kind"] = "public"
    loaded["rights_authorization"] = {"status": "authenticated"}
    with pytest.raises(ValueError, match="rights_review_authentication_required"):
        e.run(loaded, tmp_path / "public-run")
    assert not (tmp_path / "public-run").exists()


def test_independent_hand_oracle_is_preserved():
    from test_oracles import cases, predictions

    cs, ps = cases(), predictions()
    cs[0]["relevance"] = {"d1": 3, "d2": 1}
    cs[1]["relevance"] = {"d5": 2}
    ps["q1"]["ranked"] = ["d4", "d2", "d1"]
    ps["q2"]["ranked"] = ["d9", "d5"]
    r = e.metrics(cs, ps, k=10)
    assert r["recall"] == 1.0
    assert r["ndcg"] == pytest.approx(0.5861350236074894)
    assert r["mrr"] == 0.5
    assert r["false_strong"] == {"errors": 1, "denominator": 2, "rate": 0.5}
    assert r["false_abstain"] == {"errors": 1, "denominator": 2, "rate": 0.5}


def test_forged_review_evidence_and_claimed_identity_do_not_create_rights_authority(
    tmp_path,
):
    data = forged_public()
    for right in data["rights"]:
        right["provenance_kind"] = "external"
        right["review_evidence"] = {
            "evidence_id": "forged-fixture",
            "version": "v1",
            "content_sha256": "a" * 64,
            "original_url": "https://license.invalid/not-a-license",
            "reviewer_context": {
                "claimed_issuer": "forged-fixture",
                "claimed_subject": "caller-chosen",
                "checked_at": "2026-10-22T00:00:00Z",
            },
        }
    with pytest.raises(ValueError, match="rights_review_authentication_required"):
        e.load_bundle(write_bundle(tmp_path, data))


def test_registered_bootstrap_policy_is_fixed_and_not_a_sufficiency_claim():
    from pathlib import Path

    from ttakkari.kb_eval.validation import BOOTSTRAP_POLICY

    path = Path(__file__).resolve().parents[2] / "kb-eval/statistical-policy.json"
    assert json.loads(path.read_text()) == BOOTSTRAP_POLICY
    assert BOOTSTRAP_POLICY["minimum_replicates"] == 10000
    assert "not evidence" in BOOTSTRAP_POLICY["sufficiency"]


def test_r2_quality_degenerate_intervals_are_explicitly_diagnostic():
    r = e.cluster_interval([0.0, 0.0], ["one", "one"], seed=7)
    assert r["ci_degenerate"] is True
    assert r["sampling_status"] == "unverified_single_cluster"
    with pytest.raises(ValueError, match="invalid_statistical_value"):
        e.cluster_interval([10**1000, 0.0], ["a", "b"], seed=7)


def test_r2_unsupported_stage_retains_reason_and_never_becomes_zero():
    data = equal_latency()
    data[0]["candidate"]["stages_ms"]["inference"] = None
    with pytest.raises(ValueError, match="stage_missing_reason_required"):
        e.latency_report(data, seed=7)
    data[0]["candidate"]["stages_missing"] = {"inference": "not_supported_in_fixture"}
    r = e.latency_report(data, seed=7)
    assert r["stage_observations"][0]["candidate"]["inference"] is None
    assert r["stage_missing"][0]["candidate"]["inference"] == "not_supported_in_fixture"


def test_r2_degenerate_ratio_ci_is_unverified_even_with_registered_replicates():
    r = e.latency_report(equal_latency(), seed=7, replicates=10000)
    assert r["ratio_ci"] == [1.0, 1.0]
    assert r["ci_degenerate"] is True
    assert r["relative"] == "relative_unverified"
    assert "degenerate_ratio_ci" in r["reasons"]
    assert r["candidate_absolute"] == "absolute_pass"


def test_r2_known_good_varied_trace_passes_and_negative_control_fails():
    good = e.latency_report(varied(equal_latency()), seed=7)
    assert good["ci_degenerate"] is False
    assert good["ratio_ci"][0] < good["ratio_ci"][1] <= 1.2
    assert good["relative"] == "relative_pass"
    bad = e.latency_report(varied(blocks(650.0, 500.0)), seed=7)
    assert bad["ci_degenerate"] is False
    assert bad["relative"] == "relative_fail"


def test_r2_quality_family_degenerate_bootstrap_is_inconclusive():
    groups = ["a", "a", "b", "b", "c", "c"]
    flat = {"recall": [0.0] * 6, "ndcg": [0.0] * 6}
    r = e.quality_family(
        flat, groups, seed=7, dev_deltas=flat, dev_groups=list("xxyyzz")
    )
    assert r["status"] == "inconclusive"
    assert "degenerate_bootstrap_interval" in r["reasons"]
    ci = e.cluster_interval([0.1, 0.1, 0.1], ["a", "b", "c"], seed=7)
    assert ci["ci_degenerate"] is True
    assert ci["sampling_status"] == "unverified_degenerate_interval"


def test_r4_review_event_actor_is_recorded_as_unauthenticated_claim(tmp_path):
    event = {
        "id": "q1",
        "label_version": 0,
        "decision": "approve",
        "actor_kind": "agent",
        "actor": "user-approved-reviewer",
        "at": "2026-10-22T00:00:00+00:00",
        "correction": None,
    }
    state = e.review_update(tmp_path / "state.json", event)
    label = state["labels"]["q1"]
    assert label["actor_authentication"] == "unauthenticated_claim"
    assert label["verification"] == "agent_verified"
    assert state["history"][-1]["actor_authentication"] == "unauthenticated_claim"
