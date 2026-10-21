"""Hand-computed behavioral oracles; synthetic text, no inference or DB."""

import copy
import hashlib
import json
import math

import pytest

from ttakkari.kb_eval import core as e


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def cases():
    return [
        {
            "id": "q1",
            "group": "g1",
            "split": "dev",
            "documents": ["d1", "d2"],
            "answerable": True,
            "relevance": {"d1": 3, "d2": 1, "d3": 0},
            "domain": "synthetic",
            "types": ["fact"],
            "language_pair": "ko-en",
        },
        {
            "id": "q2",
            "group": "g2",
            "split": "dev",
            "documents": ["d5", "d6"],
            "answerable": True,
            "relevance": {"d5": 2, "d6": 1},
            "domain": "synthetic",
            "types": ["negation"],
            "language_pair": "en-en",
        },
        {
            "id": "q3",
            "group": "g3",
            "split": "dev",
            "documents": ["d8"],
            "answerable": False,
            "relevance": {"d8": 0},
            "domain": "synthetic",
            "types": ["near_miss_abstain"],
            "language_pair": "ko-en",
        },
    ]


def predictions():
    return {
        "q1": {
            "ranked": ["d2", "d4", "d1"],
            "candidates": ["d1", "d2"],
            "coverage": "weak",
            "answer_correct": True,
        },
        "q2": {
            "ranked": ["d9", "d6", "d10"],
            "candidates": ["d6"],
            "coverage": "none",
            "answer_correct": False,
        },
        "q3": {
            "ranked": ["d8"],
            "candidates": [],
            "coverage": "strong",
            "answer_correct": False,
        },
    }


def test_hand_oracle_three_questions_ten_documents():
    result = e.metrics(cases(), predictions(), k=3)
    assert result["recall"] == pytest.approx(0.75)
    assert result["mrr"] == pytest.approx(0.75)
    # Hand: q1 DCG=1+7/2, IDCG=7+1/log2(3); q2 DCG=1/log2(3).
    assert result["ndcg"] == pytest.approx(
        ((4.5 / (7 + 1 / math.log2(3))) + ((1 / math.log2(3)) / (3 + 1 / math.log2(3))))
        / 2
    )
    assert result["false_strong"] == {"errors": 1, "denominator": 2, "rate": 0.5}
    assert result["false_abstain"] == {"errors": 1, "denominator": 2, "rate": 0.5}
    assert result["abstain_f1"] == 0
    assert result["candidate_recall"] == 0.75
    assert result["total"] == 3
    assert result["per_query"][0]["judged_at_k"] == pytest.approx(2 / 3)
    assert result["slices"]["language_pair"]["ko-en"]["queries"] == 2


def test_invalid_and_missing_are_visible_in_denominators():
    cs, ps = cases(), predictions()
    cs[0]["anchor_status"] = "anchor_ambiguous"
    del ps["q3"]
    result = e.metrics(cs, ps, k=3)
    assert result["total"] == 3
    assert result["invalid"] == 1
    assert result["failed"] == 1
    assert result["valid_answerable"] == 1
    assert result["recall"] == 0.5
    assert result["recall_all_answerable_bounds"] == [0.25, 0.75]
    assert {r["failure"] for r in result["per_query"]} >= {
        "anchor_ambiguous",
        "missing_prediction",
    }


def source(body="first answer last"):
    return {
        "doc_key": "d1",
        "source_revision_id": "r1",
        "raw_sha256": sha(body),
        "body_sha256": sha(body),
        "body": body,
        "source_uri": "fixture://d1",
        "observed_at": "2026-10-21T00:00:00Z",
    }


def quote(src=None, text="answer", **changes):
    src = src or source()
    return {
        **{
            key: src[key]
            for key in (
                "doc_key",
                "source_revision_id",
                "raw_sha256",
                "body_sha256",
                "source_uri",
                "observed_at",
            )
        },
        "quote": text,
        "quote_sha256": sha(text),
        "context_before": "",
        "context_after": "",
        "answer_id": "a1",
        "match_normalization_version": "exact-v1",
        **changes,
    }


def test_anchor_raw_repetition_context_and_revision_negative_controls():
    src = source("answer left answer right")
    assert e.anchor(quote(src), src)["status"] == "anchor_ambiguous"
    assert e.anchor(quote(src, context_after=" right"), src)["span"] == [12, 18]
    assert e.anchor(quote(), source("new answer"))["reason"] == "source_changed"
    assert (
        e.anchor(quote(quote_sha256="0" * 64), source())["reason"]
        == "quote_hash_mismatch"
    )
    src = source("x²")
    assert e.anchor(quote(src, "x2"), src)["reason"] == "normalization_review_required"


def test_anchor_remaps_answer_once_across_chunk_boundaries():
    src = source("abcANSWERxyz")
    a = quote(src, "ANSWER")
    result = e.anchor(
        a,
        src,
        passages=[
            {"id": "p1", "start": 0, "end": 6},
            {"id": "p2", "start": 6, "end": 12},
        ],
    )
    assert result["answer_id"] == "a1"
    assert result["passage_sets"] == [["p1", "p2"]]


def test_translation_duplicate_multidoc_transitive_leakage():
    cs = cases()
    cs[1].update(documents=["d7", "d6"], split="holdout")
    with pytest.raises(ValueError, match="split_leakage"):
        e.components(cs, [["d2", "d4"], ["d4", "d7"]])
    cs[1]["split"] = "dev"
    result = e.components(cs, [["d2", "d4"], ["d4", "d7"]])
    assert result["q1"] == result["q2"]
    assert result["q1"] != result["q3"]


def test_unequal_cluster_preserves_question_estimand():
    result = e.cluster_interval(
        [1.0] * 9 + [-1.0], ["big"] * 9 + ["small"], seed=4, replicates=10000
    )
    assert result["delta"] == 0.8
    assert result["low"] == -1
    assert result["high"] == 1
    assert result["clusters"] == 2
    assert result["effective_clusters"] == pytest.approx(100 / 82)


def test_correlated_cluster_interval_wider_than_question_bootstrap():
    values = [0.2] * 50 + [-0.2] * 50
    clustered = e.cluster_interval(values, ["a"] * 50 + ["b"] * 50, seed=3)
    independent = e.cluster_interval(values, list(map(str, range(100))), seed=3)
    assert clustered["high"] - clustered["low"] > 3 * (
        independent["high"] - independent["low"]
    )


def test_holm_stepdown_adjustment_and_stopping():
    assert e.holm({"a": 0.01, "b": 0.04, "c": 0.03}) == pytest.approx(
        {"a": 0.03, "b": 0.06, "c": 0.06}
    )
    assert e.holm({"a": 0.03, "b": 0.04}) == pytest.approx({"a": 0.06, "b": 0.06})


def test_missing_power_and_tiny_clusters_never_pass():
    r = e.quality_family(
        {"recall": [0.02] * 3, "ndcg": [0.03] * 3}, ["a", "b", "c"], seed=2
    )
    assert r["status"] == "inconclusive"
    assert "power_not_established" in r["reasons"]
    power = e.simulate_power(
        {"recall": [0.8, -0.8], "ndcg": [0.8, -0.8]},
        ["a", "b"],
        seed=3,
        trials=60,
        replicates=200,
    )
    assert power["status"] == "insufficient"
    assert power["neutral_pass_probability"] < 0.8
    # Tiny cluster resampling can be badly anti-conservative; report and reject it.
    assert power["loss_pass_probability"] > 0.05


def blocks(candidate=900.0, control=1000.0, biased=False):
    return [
        {
            "block": i,
            "order": "AB" if i % 2 == 0 else "BA",
            "query_id": f"q{i}",
            "candidate": {
                "e2e_ms": candidate,
                "queue_ms": 200.0,
                "service_ms": candidate - 200,
                "stages_ms": {"inference": 30.0},
                "load": 20.0 if biased else 10.0,
            },
            "control": {
                "e2e_ms": control,
                "queue_ms": 200.0,
                "service_ms": control - 200,
                "stages_ms": {"inference": 30.0},
                "load": 1.0 if biased else 10.0,
            },
            "profile": "full:c1:warm:N40",
            "elapsed_seconds": 6.0,
        }
        for i in range(100)
    ]


def test_control_also_exceeds_slo_relative_is_separate():
    result = e.latency_report(blocks(), seed=7)
    assert result["candidate_absolute"] == "absolute_fail"
    assert result["control_absolute"] == "absolute_fail"
    assert result["relative"] == "relative_pass"
    assert result["ratio_ci"] == pytest.approx([0.9, 0.9])


def test_ab_load_bias_and_candidate_only_regression():
    assert (
        e.latency_report(blocks(biased=True), seed=7)["relative"]
        == "relative_unverified"
    )
    r = e.latency_report(blocks(650.0, 500.0), seed=7)
    assert r["relative"] == "relative_fail"
    assert r["candidate_absolute"] == "absolute_pass"
    assert e.latency_report(blocks()[:2], seed=7)["relative"] == "relative_unverified"


def identity():
    return {
        "corpus": "a" * 64,
        "gold": "b" * 64,
        "policy": "c" * 64,
        "build": "b1",
        "model": "model-sha",
        "analyzer": "exact-v1",
        "config": "d" * 64,
        "seed": 23,
        "code": "e" * 40,
        "role": "query",
        "window": "w1",
        "quantization": "fixture",
    }


@pytest.mark.parametrize(
    "field,new",
    [("model", "other"), ("role", "passage"), ("window", "w2"), ("seed", 24)],
)
def test_cache_key_separates_model_role_window_seed_and_one_byte(field, new):
    original = e.cache_key(identity(), b"abc")
    assert e.cache_key(identity(), b"abd") != original
    changed = {**identity(), field: new}
    assert e.cache_key(changed, b"abc") != original


def test_cache_replay_compares_score_bytes_and_integrity(tmp_path):
    key = e.cache_key(identity(), b"abc")
    payload = {
        "ranked": ["p1"],
        "score_bytes": ["3ff0000000000000"],
        "metrics": {"recall": 1.0},
    }
    record = {
        "key": key,
        "payload": payload,
        "payload_sha256": sha(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        ),
    }
    assert (
        e.verify_replay(record, key, copy.deepcopy(payload))["status"] == "exact_match"
    )
    altered = {**payload, "score_bytes": ["3ff0000000000001"]}
    with pytest.raises(ValueError, match="replay_mismatch"):
        e.verify_replay(record, key, altered)
    with pytest.raises(ValueError, match="cache_key_mismatch"):
        e.verify_replay(record, "f" * 64, payload)
    record["payload"]["ranked"] = []
    with pytest.raises(ValueError, match="cache_corrupt"):
        e.verify_replay(record, key, payload)


def test_holdout_one_use_and_developer_open_denied(tmp_path):
    audit = tmp_path / "holdout.json"
    with pytest.raises(ValueError, match="independent_reviewer_required"):
        e.open_holdout(
            audit,
            identity(),
            actor="developer",
            role="developer",
            at="2026-10-21T00:00:00Z",
        )
    result = e.open_holdout(
        audit,
        identity(),
        actor="reviewer-test",
        role="independent_reviewer",
        at="2026-10-21T00:00:00Z",
    )
    assert result["holdout_peeks"] == 1
    with pytest.raises(ValueError, match="holdout_consumed"):
        e.open_holdout(
            audit,
            {**identity(), "policy": "changed"},
            actor="reviewer-test",
            role="independent_reviewer",
            at="2026-10-21T00:01:00Z",
        )


def test_mobile_review_contract_resume_no_machine_human_promotion(tmp_path):
    path = tmp_path / "review.json"
    result = e.review_update(
        path,
        {
            "id": "q1",
            "label_version": 0,
            "decision": "approve",
            "actor_kind": "agent",
            "actor": "test-agent",
            "at": "2026-10-21T00:00:00Z",
        },
    )
    assert result["labels"]["q1"]["verification"] == "agent_verified"
    assert e.review_update(path, None)["cursor"] == "q1"
    with pytest.raises(ValueError, match="label_version_conflict"):
        e.review_update(
            path,
            {
                "id": "q1",
                "label_version": 0,
                "decision": "discard",
                "actor_kind": "agent",
                "actor": "test-agent",
                "at": "2026-10-21T00:01:00Z",
            },
        )
    with pytest.raises(ValueError, match="human_attestation_required"):
        e.review_update(
            path,
            {
                "id": "q2",
                "label_version": 0,
                "decision": "approve",
                "actor_kind": "human",
                "actor": "assistant",
                "at": "2026-10-21T00:01:00Z",
            },
        )


def test_zero_of_forty_cannot_certify_five_percent_error():
    result = e.binomial_interval(0, 40, alpha=0.05)
    assert result[1] == pytest.approx(1 - 0.05 ** (1 / 40))
    assert result[1] > 0.05
    assert e.binomial_interval(40, 40, alpha=0.05)[0] == pytest.approx(0.05 ** (1 / 40))
    with pytest.raises(ValueError, match="invalid_binomial_counts"):
        e.binomial_interval(41, 40)


def test_answer_level_counts_boundary_cover_once():
    result = e.answer_recall(
        {"a1": [["p1", "p2"]], "a2": [["p3"], ["p4"]]}, ["p1", "p2", "p4"], k=3
    )
    assert result == {
        "metric_unit": "answer",
        "hits": 2,
        "denominator": 2,
        "recall": 1.0,
    }
    assert e.answer_recall({"a1": [["p1", "p2"]]}, ["p1"], k=3)["recall"] == 0.0


def test_live_tolerance_fixture_near_tie_and_stable_core():
    baseline = [("a", 1.0), ("b", 0.5), ("c", 0.499)]
    near = [("a", 1.0), ("c", 0.4995), ("b", 0.4994)]
    result = e.live_tolerance(baseline, near, k=2, epsilon=0.001)
    assert result["status"] == "fixture_tolerance_match"
    assert result["topk_overlap"] == 0.5
    bad = [("c", 1.1), ("b", 0.5), ("a", 0.49)]
    assert (
        e.live_tolerance(baseline, bad, k=2, epsilon=0.001)["status"]
        == "tolerance_fail"
    )
    assert result["actual_inference"] == "unexecuted"


def test_metrics_abstain_intervals_and_language_pair_accounting():
    r = e.metrics(cases(), predictions(), k=3)
    assert r["false_strong"]["denominator"] == 2
    assert r["abstain_intervals"]["false_strong"]["binomial_one_sided"][1] > 0.5
    assert r["slices"]["language_pair"]["en-en"]["false_abstain"]["denominator"] == 1
    assert r["abstain_intervals"]["gate"] == "inconclusive"


def test_stage_failure_classification_never_invents_missing_telemetry():
    c = cases()[0]
    assert e.classify_failure(c, {"ranked": [], "candidates": []}) == "trace_unverified"
    assert (
        e.classify_failure(
            c,
            {
                "ranked": [],
                "candidates": [],
                "trace": {"indexed": ["d1", "d2"], "routes": {"lexical": []}},
            },
        )
        == "no_candidate"
    )
    assert (
        e.classify_failure(
            c,
            {
                "ranked": [],
                "candidates": [],
                "trace": {"indexed": [], "routes": {"lexical": []}},
            },
        )
        == "not_in_corpus"
    )
    assert (
        e.classify_failure(
            c,
            {
                "ranked": [],
                "candidates": [],
                "trace": {"indexed": ["d1"], "routes": {"dense": ["d1"]}, "fused": []},
            },
        )
        == "cut_by_fusion"
    )
    assert (
        e.classify_failure(
            c, {"ranked": ["d4"], "candidates": ["d1"], "trace": {"fused": ["d1"]}}
        )
        == "ranked_out"
    )


def test_dev_power_design_and_holdout_groups_must_match_without_overlap():
    values = {"recall": [0.01, -0.01, 0.02], "ndcg": [0.01, -0.01, 0.02]}
    r = e.quality_family(
        values,
        ["h1", "h2", "h3"],
        seed=12,
        dev_deltas={"recall": [0.01, -0.01], "ndcg": [0.01, -0.01]},
        dev_groups=["d1", "d2"],
    )
    assert r["status"] == "inconclusive"
    assert "power_design_mismatch" in r["reasons"]
    contaminated = e.quality_family(
        values,
        ["h1", "h2", "h3"],
        seed=12,
        dev_deltas=values,
        dev_groups=["h1", "h2", "h3"],
    )
    assert contaminated["status"] == "inconclusive"
    assert "dev_evaluation_group_overlap" in contaminated["reasons"]


def test_power_residual_bootstrap_handles_asymmetric_bounded_pilot():
    r = e.simulate_power(
        {"recall": [1.0, 1.0, -1.0], "ndcg": [1.0, 1.0, -1.0]},
        ["a", "b", "c"],
        seed=12,
        trials=10,
        replicates=100,
    )
    assert r["status"] == "insufficient"
    assert r["trials"] == 10
    with pytest.raises(ValueError, match="invalid_power_trials"):
        e.simulate_power(
            {"recall": [0.0, 1.0], "ndcg": [0.0, 1.0]}, ["a", "b"], seed=1, trials=0
        )


def test_primary_family_positive_and_regression_controls_are_statistical_only():
    pilot = {"recall": [0.01, -0.01] * 20, "ndcg": [0.01, -0.01] * 20}
    kwargs = {
        "seed": 12,
        "dev_deltas": pilot,
        "dev_groups": [f"d{i}" for i in range(40)],
    }
    groups = [f"h{i}" for i in range(40)]
    neutral = e.quality_family(
        {"recall": [0.0] * 40, "ndcg": [0.0] * 40}, groups, **kwargs
    )
    regression = e.quality_family(
        {"recall": [-0.10] * 40, "ndcg": [-0.10] * 40}, groups, **kwargs
    )
    assert neutral["status"] == "pass"
    assert regression["status"] == "fail"
    assert neutral["scope"] == regression["scope"] == "statistical_noninferiority_only"
    assert neutral["power"]["neutral_ci"][0] > 0.8
    assert neutral["power"]["loss_ci"][1] < 0.05
