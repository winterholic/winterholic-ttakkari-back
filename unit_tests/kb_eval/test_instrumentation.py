import pytest

from ttakkari.kb_eval import core as e


def test_balanced_random_blocks_and_timers_do_not_sum_parallel_stages():
    orders = e.balanced_orders(6, seed=12)
    assert orders.count("AB") == orders.count("BA") == 3
    assert e.balanced_orders(6, seed=12) == orders
    ticks = iter([0.0, 0.020, 0.100, 0.110])
    output, timing = e.timed_call(
        lambda: {"answer": "fixture"}, admit=lambda: None, clock=lambda: next(ticks)
    )
    assert output == {"answer": "fixture"}
    assert timing["e2e_ms"] == pytest.approx(110.0)
    assert timing["queue_ms"] == pytest.approx(20.0)
    assert timing["service_ms"] == pytest.approx(80.0)
    assert timing["serialize_transport_ms"] == pytest.approx(10.0)
    assert timing["stages_ms"]["model_queue"] is None
    assert timing["stages_missing"]["model_queue"] == "adapter_did_not_report"


def test_resource_unsupported_is_unverified_and_zero_is_measured(tmp_path):
    result = e.resources(tmp_path, gpu_collector=lambda: 0)
    assert result["gpu_bytes"] == {
        "status": "measured",
        "value": 0,
        "unit": "bytes",
        "reason": None,
    }

    def unsupported():
        raise NotImplementedError("Metal not provided")

    result = e.resources(tmp_path, gpu_collector=unsupported)
    assert result["gpu_bytes"]["status"] == "unverified"
    assert result["gpu_bytes"]["value"] is None
    assert "Metal not provided" in result["gpu_bytes"]["reason"]


def test_ab_order_load_bias_not_hidden_by_equal_arm_average():
    from test_oracles import blocks

    bs = blocks(650.0, 500.0)
    for b in bs:
        for arm in ("candidate", "control"):
            b[arm]["load"] = 20.0 if b["order"] == "AB" else 1.0
    r = e.latency_report(bs, seed=12)
    assert r["relative"] == "relative_unverified"
    assert "order_load_imbalance" in r["reasons"]


def test_external_stage_timer_failure_keeps_e2e_and_error():
    ticks = iter([0.0, 0.010, 0.030, 0.040])

    def broken():
        raise ValueError("fixture adapter down")

    output, timing = e.timed_call(broken, clock=lambda: next(ticks))
    assert output["failure"] == "adapter_error"
    assert timing["e2e_ms"] == pytest.approx(40.0)
    assert output["reason"] == "fixture adapter down"


def test_unsupported_null_gpu_collector_never_becomes_measured(tmp_path):
    result = e.resources(tmp_path, gpu_collector=lambda: None)
    assert result["gpu_bytes"]["status"] == "unverified"
    assert result["gpu_bytes"]["value"] is None
