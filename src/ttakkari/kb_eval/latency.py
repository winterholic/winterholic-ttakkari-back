"""Block paired latency reporting. Absolute e2e SLO never waived for load."""

import numpy as np


def latency_report(blocks, *, seed, replicates=10000, mode="full"):
    if mode not in {"full", "fast"}:
        raise ValueError("invalid_mode")
    if not blocks:
        return {
            "relative": "relative_unverified",
            "candidate_absolute": "unverified",
            "control_absolute": "unverified",
            "reasons": ["no_observations"],
        }
    arms = {}
    for arm in ("candidate", "control"):
        for b in blocks:
            obs = b[arm]
            if any(
                not np.isfinite(obs[k]) or obs[k] < 0
                for k in ("e2e_ms", "queue_ms", "service_ms", "load")
            ):
                raise ValueError("invalid_timer")
            if obs["queue_ms"] + obs["service_ms"] > obs["e2e_ms"] + 1e-6:
                raise ValueError("timer_critical_path_inconsistent")
        arms[arm] = {
            k: {
                "p50": float(np.quantile([b[arm][k] for b in blocks], 0.5)),
                "p95": float(np.quantile([b[arm][k] for b in blocks], 0.95)),
            }
            for k in ("e2e_ms", "queue_ms", "service_ms", "load")
        }
    reasons = []
    if len({b["block"] for b in blocks}) != len(blocks):
        raise ValueError("duplicate_latency_block")
    orders = [b["order"] for b in blocks]
    if (
        any(o not in {"AB", "BA"} for o in orders)
        or abs(orders.count("AB") - orders.count("BA")) > 1
    ):
        reasons.append("order_imbalance")
    if len({b["profile"] for b in blocks}) != 1:
        reasons.append("profile_mismatch")
    if len(blocks) < 100 or sum(b["elapsed_seconds"] for b in blocks) < 600:
        reasons.append("insufficient_observation")
    loads = np.array(
        [[b[arm]["load"] for arm in ("candidate", "control")] for b in blocks]
    )
    if np.mean(np.abs(loads[:, 0] - loads[:, 1])) > 0.2 * max(1.0, loads.mean()):
        reasons.append("load_imbalance")
    # Order-dependent drift is visible even if each pair has the same load.
    if "AB" in orders and "BA" in orders:
        avgs = [loads[np.array(orders) == order].mean() for order in ("AB", "BA")]
        if abs(avgs[0] - avgs[1]) > 0.2 * max(1.0, loads.mean()):
            reasons.append("order_load_imbalance")
    times = np.array(
        [[b[arm]["e2e_ms"] for arm in ("candidate", "control")] for b in blocks]
    )
    if (times[:, 1] <= 0).any():
        raise ValueError("zero_control_latency")
    rng = np.random.default_rng(seed)
    ratios = []
    for start in range(0, replicates, 256):
        idx = rng.integers(0, len(blocks), (min(256, replicates - start), len(blocks)))
        p95 = np.quantile(times[idx], 0.95, axis=1)
        ratios.extend((p95[:, 0] / p95[:, 1]).tolist())
    low, high = map(float, np.quantile(ratios, [0.025, 0.975]))
    relative = (
        "relative_unverified"
        if reasons
        else "relative_pass"
        if high <= 1.2
        else "relative_fail"
        if low > 1.2
        else "relative_inconclusive"
    )
    slo = 800 if mode == "full" else 200
    return {
        "relative": relative,
        "ratio_ci": [low, high],
        "ratio": float(np.quantile(times[:, 0], 0.95) / np.quantile(times[:, 1], 0.95)),
        **{
            arm + "_absolute": "absolute_pass"
            if arms[arm]["e2e_ms"]["p95"] <= slo
            else "absolute_fail"
            for arm in arms
        },
        "absolute_slo_ms": slo,
        "reasons": reasons,
        "observations": len(blocks),
        "arms": arms,
        "stage_observations": [
            {"block": b["block"], **{arm: b[arm]["stages_ms"] for arm in arms}}
            for b in blocks
        ],
        "profile": blocks[0]["profile"],
        "seed": seed,
        "replicates": replicates,
    }


def balanced_orders(count, *, seed):
    if count < 2 or count % 2:
        raise ValueError("balanced_even_block_count_required")
    orders = ["AB", "BA"] * (count // 2)
    np.random.default_rng(seed).shuffle(orders)
    return orders


def timed_call(call, *, admit=lambda: None, serialize=lambda x: x, clock=None):
    """Adapter timer boundary; adapter-owned parallel stages stay separate/null."""
    import time

    clock = clock or time.perf_counter
    begin = clock()
    try:
        admit()
        entered = clock()
        try:
            output = call()
        except (ValueError, OSError, TimeoutError) as exc:
            output = {"failure": "adapter_error", "reason": str(exc)}
        serviced = clock()
        output = serialize(output)
    except (ValueError, OSError, TimeoutError) as exc:
        entered = serviced = clock()
        output = {"failure": "adapter_error", "reason": str(exc)}
    end = clock()
    if not begin <= entered <= serviced <= end:
        raise ValueError("nonmonotonic_timer")
    missing = {
        key: "adapter_did_not_report"
        for key in (
            "model_queue",
            "tokenize",
            "prefill",
            "inference",
            "db_pool_wait",
            "db_execute",
        )
    }
    return output, {
        "e2e_ms": (end - begin) * 1000,
        "queue_ms": (entered - begin) * 1000,
        "service_ms": (serviced - entered) * 1000,
        "serialize_transport_ms": (end - serviced) * 1000,
        "stages_ms": dict.fromkeys(missing),
        "stages_missing": missing,
    }
