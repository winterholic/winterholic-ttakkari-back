"""Third review hardening: scale-aware latency floors and guard regression tests."""

import numpy as np
import pytest

from ttakkari.kb_eval import core as e
from ttakkari.kb_eval.validation import DEGENERACY


def normal_blocks(mean, cv, *, ratio=0.9, seed=3, n=100):
    rng = np.random.default_rng(seed)
    data = []
    for i in range(n):
        arms = {}
        for arm, scale in (("candidate", ratio), ("control", 1.0)):
            e2e = float(mean * scale * (1 + cv * rng.standard_normal()))
            arms[arm] = {
                "e2e_ms": e2e,
                "queue_ms": 0.0,
                "service_ms": e2e,
                "stages_ms": {},
                "load": 10.0,
            }
        data.append(
            {
                "block": i,
                "order": "AB" if i % 2 == 0 else "BA",
                "query_id": f"q{i}",
                "profile": "full:c1:warm:N40",
                "elapsed_seconds": 7.0,
                **arms,
            }
        )
    return data


@pytest.mark.parametrize("mean", [0.5, 1.0, 2.0, 5.0, 20.0, 200.0, 800.0, 5000.0])
@pytest.mark.parametrize("cv", [0.005, 0.01, 0.02, 0.05, 0.10])
def test_latency_floor_is_scale_free_for_informative_traces(mean, cv):
    r = e.latency_report(normal_blocks(mean, cv), seed=7)
    assert r["reasons"] == [], (mean, cv, r["reasons"])
    assert r["relative"] == "relative_pass"


@pytest.mark.parametrize("mean", [0.5, 5.0, 800.0, 5000.0])
def test_latency_float_noise_stays_unverified_at_every_scale(mean):
    data = normal_blocks(mean, 0.0)
    for b in data:
        b["candidate"]["e2e_ms"] += b["block"] * 1e-9
        b["candidate"]["service_ms"] = b["candidate"]["e2e_ms"]
    r = e.latency_report(data, seed=7)
    assert r["relative"] == "relative_unverified"
    assert "degenerate_arm_variance" in r["reasons"]


def test_relative_floor_catches_large_mean_near_constant_trace():
    # std 0.25 ms clears the absolute timer floor; CV 5e-5 is below the relative one.
    cv = DEGENERACY["latency_min_cv"] / 2
    r = e.latency_report(normal_blocks(5000.0, cv), seed=7)
    assert "degenerate_arm_variance" in r["reasons"]
    assert r["relative"] == "relative_unverified"


def test_absolute_floor_catches_sub_resolution_variation_at_small_mean():
    # CV 2e-4 clears the relative floor; std 1e-4 ms is below timer resolution.
    r = e.latency_report(normal_blocks(0.5, 2e-4), seed=7)
    assert "degenerate_arm_variance" in r["reasons"]


def test_small_mean_regression_is_still_detected():
    r = e.latency_report(normal_blocks(2.0, 0.05, ratio=1.5), seed=7)
    assert r["reasons"] == []
    assert r["relative"] == "relative_fail"


# Latency guards the third review found unprotected (L1, L2, L12, L13).


def test_order_imbalance_tolerance_is_one_block():
    data = normal_blocks(500.0, 0.05)
    data[1]["order"] = "AB"  # AB 51 / BA 49
    assert e.latency_report(data, seed=7)["reasons"] == ["order_imbalance"]
    odd = normal_blocks(500.0, 0.05, n=101)  # AB 51 / BA 50 is balanced
    assert e.latency_report(odd, seed=7)["reasons"] == []


def test_ninety_nine_blocks_are_insufficient_even_past_ten_minutes():
    data = normal_blocks(500.0, 0.05, n=99)
    assert sum(b["elapsed_seconds"] for b in data) >= 600
    assert e.latency_report(data, seed=7)["reasons"] == ["insufficient_observation"]


def test_queue_plus_service_beyond_e2e_is_rejected():
    data = normal_blocks(500.0, 0.05)
    data[3]["control"]["queue_ms"] = 1.0
    with pytest.raises(ValueError, match="timer_critical_path_inconsistent"):
        e.latency_report(data, seed=7)


def test_duplicate_block_id_is_rejected():
    data = normal_blocks(500.0, 0.05)
    data[5]["block"] = data[4]["block"]
    with pytest.raises(ValueError, match="duplicate_latency_block"):
        e.latency_report(data, seed=7)


def test_nonmonotonic_timer_and_unknown_mode_are_rejected():
    ticks = iter([0.0, 0.02, 0.01, 0.03])
    with pytest.raises(ValueError, match="nonmonotonic_timer"):
        e.timed_call(dict, clock=lambda: next(ticks))
    with pytest.raises(ValueError, match="invalid_mode"):
        e.latency_report(normal_blocks(500.0, 0.05), seed=7, mode="slow")


# Quality gate (Q3 inconclusive zone, Q7 neutral power floor, single cluster).

H = [f"h{i}" for i in range(40)]
D = [f"d{i}" for i in range(40)]


def alternating(center, amplitude, n=40):
    return [center + (amplitude if i % 2 else -amplitude) for i in range(n)]


def test_wide_interval_straddling_margin_is_inconclusive_not_fail():
    pilot = {"recall": alternating(0.0, 0.01), "ndcg": alternating(0.0, 0.01)}
    wide = {"recall": alternating(-0.03, 0.15), "ndcg": alternating(-0.03, 0.15)}
    r = e.quality_family(wide, H, seed=12, dev_deltas=pilot, dev_groups=D)
    assert r["reasons"] == []
    lows = [m["low"] for m in r["metrics"].values()]
    highs = [m["high"] for m in r["metrics"].values()]
    assert max(lows) < -0.05 < min(highs)
    assert r["status"] == "inconclusive"


def test_noisy_pilot_without_neutral_pass_probability_is_insufficient():
    p = e.simulate_power(
        {"recall": alternating(0.0, 0.2), "ndcg": alternating(0.0, 0.2)}, D, seed=12
    )
    assert p["neutral_ci"][0] < 0.8
    assert p["loss_ci"][1] <= 0.05
    assert p["status"] == "insufficient"


def test_unequal_clusters_below_twenty_effective_cannot_establish_power():
    groups = ["big"] * 41 + [f"s{i}" for i in range(39)]
    p = e.simulate_power(
        {"recall": alternating(0.0, 0.01, 80), "ndcg": alternating(0.0, 0.01, 80)},
        groups,
        seed=12,
    )
    assert p["clusters"] == 40
    assert p["status"] == "insufficient"


def test_single_cluster_reports_insufficient_clusters_exactly():
    v = alternating(0.0, 0.02)
    r = e.quality_family({"recall": v, "ndcg": v}, ["one"] * 40, seed=12)
    assert r["status"] == "inconclusive"
    assert r["reasons"] == [
        "degenerate_bootstrap_interval",
        "power_not_established",
        "insufficient_clusters",
    ]


# Defect 1 and 3 detail guards (D1b, D1c, D1e, D3b-e and neighbours).


def bundle():
    from test_runner import fixture_bundle

    return fixture_bundle()


def load(tmp_path, data):
    from test_runner import write_bundle

    return e.load_bundle(write_bundle(tmp_path, data))


def test_positive_gold_outside_declared_documents_is_rejected_without_holdout(
    tmp_path,
):
    data = bundle()
    data["cases"][0]["relevance"]["d3"] = 1
    with pytest.raises(ValueError, match="gold_document_mismatch:q1:d3"):
        load(tmp_path, data)


def test_shared_group_label_joins_split_components(tmp_path):
    data = bundle()
    data["cases"][1]["split"] = "holdout"
    data["cases"][1]["group"] = data["cases"][0]["group"]
    with pytest.raises(ValueError, match="split_leakage"):
        load(tmp_path, data)


def test_anchor_on_undeclared_document_is_rejected(tmp_path):
    from test_oracles import quote

    data = bundle()
    data["cases"][0]["anchors"].append(quote(data["sources"][4], "answer 5"))
    with pytest.raises(ValueError, match="anchor_document_mismatch"):
        load(tmp_path, data)


def test_answerable_case_without_anchor_is_rejected(tmp_path):
    data = bundle()
    data["cases"][0]["anchors"] = []
    with pytest.raises(ValueError, match="missing_gold_anchor"):
        load(tmp_path, data)


def test_source_body_hash_mismatch_is_rejected(tmp_path):
    data = bundle()
    data["sources"][0]["body"] += " tampered"
    with pytest.raises(ValueError, match="source_hash_mismatch:d1"):
        load(tmp_path, data)


@pytest.mark.parametrize(
    "change,error",
    [
        ({"provenance_kind": "external"}, "rights_synthetic_provenance_required:code"),
        (
            {"original_url": "https://example.org/license"},
            "rights_synthetic_provenance_required:code",
        ),
        (
            {
                "review_evidence": {
                    "evidence_id": "ev1",
                    "version": "1",
                    "content_sha256": "a" * 64,
                    "original_url": "fixture://authored-e0",
                    "reviewer_context": {
                        "claimed_issuer": "x",
                        "claimed_subject": "y",
                        "checked_at": "2026-10-21T00:00:00Z",
                    },
                }
            },
            "rights_synthetic_provenance_required:code",
        ),
        ({"status": "unverified"}, "rights_unverified:code"),
        ({"storage_allowed": False}, "rights_unverified:code"),
    ],
)
def test_each_fixture_rights_detail_is_enforced(tmp_path, change, error):
    data = bundle()
    data["rights"][0].update(change)
    with pytest.raises(ValueError, match=error):
        load(tmp_path, data)


def test_links_require_transform_rights(tmp_path):
    data = bundle()
    data["links"] = [["d9", "d10"]]
    data["rights"][0]["transform_allowed"] = False
    with pytest.raises(ValueError, match="rights_transform_unverified:code"):
        load(tmp_path, data)
    data["rights"][0]["transform_allowed"] = True
    assert load(tmp_path, data)["rights_authorization"]["authority"] == "none"


def test_external_source_observation_or_duplicate_right_is_rejected(tmp_path):
    data = bundle()
    data["sources"][0]["source_uri"] = "https://example.org/d1"
    with pytest.raises(ValueError, match="external source in fixture"):
        load(tmp_path, data)
    data = bundle()
    data["observations"]["kind"] = "database_export"
    data["observations"]["database_revision"] = "db1"
    with pytest.raises(ValueError, match="external source in fixture"):
        load(tmp_path, data)
    data = bundle()
    data["rights"].append(dict(data["rights"][0]))
    with pytest.raises(ValueError, match="duplicate_rights_component"):
        load(tmp_path, data)


@pytest.mark.parametrize(
    "mode,mean,expected",
    [
        ("full", 750.0, "absolute_pass"),
        ("full", 850.0, "absolute_fail"),
        ("fast", 190.0, "absolute_pass"),
        ("fast", 210.0, "absolute_fail"),
    ],
)
def test_absolute_slo_values_are_800_full_and_200_fast(mode, mean, expected):
    r = e.latency_report(normal_blocks(mean, 0.001, ratio=1.0), seed=7, mode=mode)
    assert r["control_absolute"] == expected


def test_noninferiority_p_value_is_recentred_on_the_margin():
    import math

    # Bootstrap mean deviation is 0.2*(2k/40-1), k ~ Bin(40, 1/2), so
    # P*(deviation >= mean + margin = 0.08) = P(k >= 28) exactly.
    v = alternating(0.03, 0.2)
    r = e.quality_family({"recall": v, "ndcg": v}, H, seed=12)
    exact = sum(math.comb(40, k) for k in range(28, 41)) / 2**40
    assert abs(r["metrics"]["recall"]["p"] - exact) < 0.003


def test_relevance_grade_above_three_and_incomplete_cache_identity_fail():
    from test_oracles import cases, identity, predictions

    cs = cases()
    first = next(iter(cs[0]["relevance"]))
    cs[0]["relevance"][first] = 4
    with pytest.raises(ValueError, match="invalid_relevance_grade"):
        e.metrics(cs, predictions(), k=3)
    partial = identity()
    partial.pop("quantization")
    with pytest.raises(ValueError, match="incomplete_cache_identity"):
        e.cache_key(partial, b"x")


# Fourth review: statistical pass conditions (Q1, Q2, Q5) and n-monotonicity.

PILOT = {"recall": alternating(0.0, 0.01), "ndcg": alternating(0.0, 0.01)}


def jitter(values):
    return [x + (0.001 if i % 2 else -0.001) for i, x in enumerate(values)]


def test_pass_requires_every_lower_bound_above_minus_margin():
    # Two sparse -0.5 outliers stretch the left tail only: Holm p stays tiny
    # while the ndcg lower bound falls below -margin.
    ndcg = jitter([0.002] * 38 + [-0.5, -0.5])
    r = e.quality_family(
        {"recall": alternating(0.0, 0.004), "ndcg": ndcg},
        H,
        seed=12,
        dev_deltas=PILOT,
        dev_groups=D,
    )
    assert r["reasons"] == []
    assert r["metrics"]["ndcg"]["low"] < -0.05
    assert max(m["holm_p"] for m in r["metrics"].values()) <= 0.05
    assert r["status"] == "inconclusive"


def test_pass_requires_holm_adjusted_p_within_alpha():
    # Sparse +0.5 outliers stretch the right tail: lower bounds clear -margin
    # but the recentred null p, after Holm, exceeds alpha.
    v = jitter([-0.04] * 38 + [0.5, 0.5])
    r = e.quality_family(
        {"recall": v, "ndcg": v}, H, seed=12, dev_deltas=PILOT, dev_groups=D
    )
    assert r["reasons"] == []
    assert min(m["low"] for m in r["metrics"].values()) > -0.05
    assert min(m["holm_p"] for m in r["metrics"].values()) > 0.05
    assert r["status"] == "inconclusive"


@pytest.mark.parametrize("trials,status", [(100, "insufficient"), (200, "established")])
def test_power_requires_registered_two_hundred_trials(trials, status):
    p = e.simulate_power(PILOT, D, seed=12, trials=trials)
    assert p["loss_ci"][1] <= 0.05 and p["neutral_ci"][0] >= 0.8
    assert p["status"] == status


@pytest.mark.parametrize("cv", [0.0005, 0.003])
@pytest.mark.parametrize("n", [100, 1000, 5000])
def test_more_blocks_never_turn_an_informative_pass_into_unverified(cv, n):
    r = e.latency_report(normal_blocks(500.0, cv, n=n), seed=7)
    assert r["reasons"] == [], (cv, n, r["reasons"])
    assert r["relative"] == "relative_pass"


def proportional(noise):
    data = normal_blocks(500.0, 0.05, ratio=1.0)
    for b in data:
        c = b["control"]["e2e_ms"] * 0.9 * (1 + noise * b["block"])
        b["candidate"].update(e2e_ms=c, service_ms=c)
    return data


@pytest.mark.parametrize("noise", [0.0, 1e-6])
def test_constant_paired_ratio_is_unverified_despite_varying_arms(noise):
    r = e.latency_report(proportional(noise), seed=7)
    assert r["reasons"] == ["degenerate_paired_ratio"]
    assert r["relative"] == "relative_unverified"
    assert r["ci_degenerate"] is True


@pytest.mark.parametrize("sd,degenerate", [(0.5e-4, True), (2e-4, False)])
def test_paired_log_ratio_floor_boundary(sd, degenerate):
    rng = np.random.default_rng(5)
    data = normal_blocks(500.0, 0.05, ratio=1.0)
    for b in data:
        c = b["control"]["e2e_ms"] * 0.9 * float(np.exp(sd * rng.standard_normal()))
        b["candidate"].update(e2e_ms=c, service_ms=c)
    r = e.latency_report(data, seed=7)
    assert ("degenerate_paired_ratio" in r["reasons"]) is degenerate
    assert r["relative"] == ("relative_unverified" if degenerate else "relative_pass")


def test_exactly_zero_width_ratio_ci_is_unverified():
    # Tied top blocks pin both p95s; paired ratios still vary below the tail.
    data = normal_blocks(500.0, 0.05, ratio=1.0)
    for b in data:
        top = b["block"] >= 80
        ctrl = 1000.0 if top else 400.0 + b["block"]
        cand = 900.0 if top else 300.0 + 2 * b["block"]
        b["control"].update(e2e_ms=ctrl, service_ms=ctrl)
        b["candidate"].update(e2e_ms=cand, service_ms=cand)
    r = e.latency_report(data, seed=7)
    assert r["ratio_ci"] == [0.9, 0.9]
    assert r["reasons"] == ["degenerate_ratio_ci"]


@pytest.mark.parametrize("n", [40, 400, 4000])
def test_more_clusters_never_make_an_informative_quality_interval_degenerate(n):
    v = list(0.01 * np.random.default_rng(1).standard_normal(n))
    groups = [f"h{i}" for i in range(n)]
    r = e.quality_family({"recall": v, "ndcg": v}, groups, seed=12)
    assert r["reasons"] == ["power_not_established"]
    ci = e.cluster_interval(v, groups, seed=7)
    assert ci["sampling_status"] == "empirical_cluster_interval"
    assert ci["ci_degenerate"] is False


@pytest.mark.parametrize("amplitude,degenerate", [(0.0009, True), (0.002, False)])
def test_quality_delta_sd_floor_boundary(amplitude, degenerate):
    v = alternating(0.0, amplitude)
    floor = DEGENERACY["quality_min_delta_sd"]
    assert bool(np.std(v, ddof=1) < floor) is degenerate
    r = e.quality_family({"recall": v, "ndcg": v}, H, seed=12)
    assert ("degenerate_bootstrap_interval" in r["reasons"]) is degenerate


def test_zero_width_guard_is_strict_and_nan_safe():
    from ttakkari.kb_eval.validation import zero_width

    assert zero_width(1.0, 1.0) is True
    assert zero_width(1.0, 0.5) is True
    assert zero_width(float("nan"), 1.0) is True
    assert zero_width(1.0, 1.0 + 1e-12) is False
