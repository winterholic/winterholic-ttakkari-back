"""Second independent review (N1 scale-aware degeneracy, N2 reason isolation)."""

import pytest
from test_oracles import blocks, varied

from ttakkari.kb_eval import core as e

H = [f"h{i}" for i in range(40)]
D = [f"d{i}" for i in range(40)]


def alternating(amplitude):
    return [amplitude if i % 2 else -amplitude for i in range(40)]


def test_n1_latency_float_noise_trace_is_not_relative_pass():
    data = blocks()
    for b in data:
        b["candidate"]["e2e_ms"] += b["block"] * 1e-9
    r = e.latency_report(data, seed=7)
    assert r["ratio_ci"][0] < r["ratio_ci"][1]
    assert r["ci_degenerate"] is True
    assert r["relative"] == "relative_unverified"
    assert "degenerate_paired_ratio" in r["reasons"]
    assert r["candidate_absolute"] == "absolute_fail"


def test_n1_quality_float_noise_deltas_cannot_pass_or_establish_power():
    noise = [i * 1e-9 for i in range(40)]
    r = e.quality_family(
        {"recall": noise, "ndcg": noise},
        H,
        seed=12,
        dev_deltas={"recall": noise, "ndcg": noise},
        dev_groups=D,
    )
    assert r["status"] == "inconclusive"
    assert r["power"]["status"] == "insufficient"
    assert r["reasons"] == ["degenerate_bootstrap_interval", "power_not_established"]
    ci = e.cluster_interval(noise, H, seed=7)
    assert ci["ci_degenerate"] is True
    assert ci["sampling_status"] == "unverified_degenerate_interval"


def test_n1_latency_constant_arm_below_resolution_is_unverified():
    data = varied(blocks(), spread=0.5)
    for b in data:
        b["control"]["e2e_ms"] = 1000.0 + b["block"] * 1e-6
        b["control"]["service_ms"] = b["control"]["e2e_ms"] - b["control"]["queue_ms"]
    r = e.latency_report(data, seed=7)
    assert r["reasons"] == ["degenerate_arm_variance"]
    assert r["relative"] == "relative_unverified"


def test_n1_normal_controls_keep_their_verdicts():
    good = e.latency_report(varied(blocks(500.0, 500.0)), seed=7)
    assert good["relative"] == "relative_pass" and good["reasons"] == []
    bad = e.latency_report(varied(blocks(650.0, 500.0)), seed=7)
    assert bad["relative"] == "relative_fail" and bad["reasons"] == []
    pilot = {"recall": alternating(0.01), "ndcg": alternating(0.01)}
    kw = {"seed": 12, "dev_deltas": pilot, "dev_groups": D}
    neutral = e.quality_family(pilot, H, **kw)
    loss = {k: [x - 0.10 for x in v] for k, v in pilot.items()}
    assert neutral["status"] == "pass" and neutral["reasons"] == []
    assert e.quality_family(loss, H, **kw)["status"] == "fail"


def base():
    return varied(blocks(500.0, 500.0))


def test_n2_every_latency_reason_is_the_sole_cause_on_varied_input():
    assert e.latency_report([], seed=7)["reasons"] == ["no_observations"]
    cases = {}
    data = base()
    for b in data:
        b["order"] = "AB"
    cases["order_imbalance"] = data
    data = base()
    data[0]["profile"] = "full:c4:warm:N40"
    cases["profile_mismatch"] = data
    data = base()
    for b in data:
        b["elapsed_seconds"] = 5.0
    cases["insufficient_observation"] = data
    data = base()
    for b in data:
        b["candidate"]["load"], b["control"]["load"] = 20.0, 10.0
    cases["load_imbalance"] = data
    data = base()
    for b in data:
        load = 20.0 if b["order"] == "AB" else 1.0
        b["candidate"]["load"] = b["control"]["load"] = load
    cases["order_load_imbalance"] = data
    for reason, data in cases.items():
        r = e.latency_report(data, seed=7)
        assert r["reasons"] == [reason], (reason, r["reasons"])
        assert r["relative"] == "relative_unverified"
    low = e.latency_report(base(), seed=7, replicates=1)
    assert low["reasons"] == ["bootstrap_replicates_below_registered_minimum"]


def test_n2_quality_low_replicates_reason_is_reported_exactly():
    pilot = {"recall": alternating(0.01), "ndcg": alternating(0.01)}
    r = e.quality_family(
        pilot, H, seed=12, replicates=100, dev_deltas=pilot, dev_groups=D
    )
    assert r["reasons"] == [
        "bootstrap_replicates_below_registered_minimum",
        "power_not_established",
    ]


@pytest.mark.parametrize(
    "candidate,k,reasons",
    [
        ([("a", 1.0), ("b", 0.5), ("c", 0.31)], 2, ["score_epsilon_exceeded"]),
        (
            [("b", 0.5), ("c", 0.3), ("a", 1.0)],
            2,
            [
                "outside_boundary_near_tie",
                "stable_core_changed",
                "strict_order_changed",
            ],
        ),
        (
            [("a", 1.0), ("c", 0.3), ("b", 0.5)],
            2,
            ["outside_boundary_near_tie", "strict_order_changed"],
        ),
        ([("a", 1.0), ("c", 0.3), ("b", 0.5)], 3, ["strict_order_changed"]),
    ],
)
def test_live_tolerance_reasons_are_exact(candidate, k, reasons):
    baseline = [("a", 1.0), ("b", 0.5), ("c", 0.3)]
    r = e.live_tolerance(baseline, candidate, k=k, epsilon=0.001)
    assert r["reasons"] == reasons
    assert r["status"] == "tolerance_fail"
