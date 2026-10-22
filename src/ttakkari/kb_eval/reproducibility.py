"""Live tolerance arithmetic on fixtures; never an inference execution receipt."""

import math


def live_tolerance(baseline, candidate, *, k, epsilon):
    if k < 1 or epsilon != 0.001 or not baseline or not candidate:
        raise ValueError("unregistered_tolerance")
    b, c = dict(baseline), dict(candidate)
    if len(b) != len(baseline) or len(c) != len(candidate) or b.keys() != c.keys():
        raise ValueError("unpaired_tolerance_results")
    if not all(math.isfinite(v) for v in [*b.values(), *c.values()]):
        raise ValueError("nonfinite_tolerance_score")
    bt, ct = [key for key, _ in baseline[:k]], [key for key, _ in candidate[:k]]
    boundary = baseline[min(k, len(baseline)) - 1][1]
    near = {key for key, score in baseline if abs(score - boundary) <= 2 * epsilon}
    core = set(bt) - near
    reasons = []
    if any(abs(b[key] - c[key]) > epsilon for key in b):
        reasons.append("score_epsilon_exceeded")
    if not core <= set(ct):
        reasons.append("stable_core_changed")
    if not (set(bt) ^ set(ct)) <= near:
        reasons.append("outside_boundary_near_tie")
    bp, cp = (
        {key: i for i, (key, _) in enumerate(baseline)},
        {key: i for i, (key, _) in enumerate(candidate)},
    )
    for a, a_score in b.items():
        for z, z_score in b.items():
            if a_score - z_score > 2 * epsilon and (bp[a] > bp[z] or cp[a] > cp[z]):
                reasons.append("strict_order_changed")
    return {
        "status": "tolerance_fail" if reasons else "fixture_tolerance_match",
        "reasons": sorted(set(reasons)),
        "topk_overlap": len(set(bt) & set(ct)) / min(k, len(baseline)),
        "near_tie": sorted(near),
        "stable_core": sorted(core),
        "actual_inference": "unexecuted",
        "policy_status": "initial-unvalidated",
        "aggregate_overlap_gate": "unverified_requires_query_mean_ge_0.99",
    }
