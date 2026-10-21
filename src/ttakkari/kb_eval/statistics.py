"""Question-weighted document-cluster bootstrap and conservative family gate.

Percentile family intervals use Bonferroni alpha/m tails; p values use a
null-boundary recentered bootstrap and Holm. Neither assumes independent queries.
"""

import numpy as np

from .validation import BOOTSTRAP_POLICY, integer, number, probability


def _matrix(values, groups):
    raw = np.asarray(values, dtype=object)
    if raw.ndim not in (1, 2) or not raw.size:
        raise ValueError("unpaired_or_nonfinite_results")
    for value in raw.flat:
        number(value, "statistical_value")
    if type(groups) is not list or any(type(g) is not str or not g for g in groups):
        raise ValueError("invalid_cluster_groups")
    a = np.asarray(values, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    if len(a) != len(groups) or not len(a) or not np.isfinite(a).all():
        raise ValueError("unpaired_or_nonfinite_results")
    names = sorted(set(groups))
    sums = np.array([a[np.array(groups) == g].sum(axis=0) for g in names])
    sizes = np.array([groups.count(g) for g in names])
    return a, sums, sizes


def _bootstrap(sums, sizes, rng, replicates):
    integer(replicates, "replicates", minimum=1)
    if replicates < BOOTSTRAP_POLICY["diagnostic_minimum_replicates"]:
        raise ValueError("bootstrap_replicates_too_small")
    # Bound peak allocation independently of the number of source clusters.
    batches = []
    for start in range(0, replicates, 256):
        draws = rng.integers(0, len(sizes), (min(256, replicates - start), len(sizes)))
        batches.append(sums[draws].sum(axis=1) / sizes[draws].sum(axis=1)[:, None])
    return np.concatenate(batches)


def cluster_interval(values, groups, *, seed, replicates=10000, alpha=0.05):
    integer(seed, "seed")
    number(alpha, "alpha")
    if not 0 < alpha < 0.5:
        raise ValueError("invalid_alpha")
    a, sums, sizes = _matrix(values, groups)
    if (abs(a) > 1).any():
        raise ValueError("metric_delta_out_of_range")
    samples = _bootstrap(sums, sizes, np.random.default_rng(seed), replicates)[:, 0]
    low, high = np.quantile(samples, [alpha, 1 - alpha])
    return {
        "delta": float(a.mean()),
        "low": float(low),
        "high": float(high),
        "clusters": len(sizes),
        "queries": len(a),
        "cluster_sizes": sizes.tolist(),
        "effective_clusters": float(sizes.sum() ** 2 / (sizes @ sizes)),
        "estimand": "question_weighted",
        "ci_degenerate": bool(low == high),
        "sampling_status": "unverified_single_cluster"
        if len(sizes) < 2
        else "unverified_degenerate_interval"
        if low == high
        else "diagnostic_only_low_replicates"
        if replicates < BOOTSTRAP_POLICY["minimum_replicates"]
        else "empirical_cluster_interval",
        "bootstrap_policy": dict(BOOTSTRAP_POLICY),
        "seed": seed,
        "replicates": replicates,
    }


def holm(pvalues):
    for p in pvalues.values():
        probability(p, "pvalue")
    if not pvalues:
        raise ValueError("invalid_pvalues")
    ordered = sorted(pvalues, key=lambda k: (pvalues[k], k))
    adjusted, running = {}, 0.0
    for i, key in enumerate(ordered):
        running = min(1.0, max(running, (len(ordered) - i) * pvalues[key]))
        adjusted[key] = running
    return adjusted


def _family(
    values, groups, *, seed, replicates, margin=0.05, alpha=0.05, simulated=False
):
    if set(values) != {"recall", "ndcg"} or margin != 0.05 or alpha != 0.05:
        raise ValueError("unregistered_primary_family")
    integer(seed, "seed")
    for series in values.values():
        for value in series:
            number(value, "statistical_value")
    keys = sorted(values)
    a, sums, sizes = _matrix(np.array([values[k] for k in keys]).T, groups)
    if not simulated and (abs(a) > 1).any():
        raise ValueError("metric_delta_out_of_range")
    means = a.mean(axis=0)
    bs = _bootstrap(sums, sizes, np.random.default_rng(seed), replicates)
    tails = np.quantile(bs, [alpha / len(keys), 1 - alpha / len(keys)], axis=0)
    # Null draws = (bootstrap - observed_mean) - margin.
    ps = {
        k: float(
            (1 + np.count_nonzero(bs[:, i] - means[i] - margin >= means[i]))
            / (replicates + 1)
        )
        for i, k in enumerate(keys)
    }
    adj = holm(ps)
    details = {
        k: {
            "delta": float(means[i]),
            "low": float(tails[0, i]),
            "high": float(tails[1, i]),
            "p": ps[k],
            "holm_p": adj[k],
        }
        for i, k in enumerate(keys)
    }
    passed = len(sizes) >= 2 and all(
        d["low"] > -margin and d["holm_p"] <= alpha for d in details.values()
    )
    failed = len(sizes) >= 2 and any(d["high"] < -margin for d in details.values())
    return details, passed, failed


def simulate_power(dev_deltas, groups, *, seed, trials=200, replicates=10000):
    """Nested empirical cluster simulation; dev residuals preserve joint correlation.

    Prototype power is conditional on this dev distribution and cluster count.
    A small or constant dev pilot cannot establish population power.
    """
    integer(trials, "power_trials", minimum=1)
    integer(seed, "seed")
    integer(
        replicates,
        "replicates",
        minimum=BOOTSTRAP_POLICY["diagnostic_minimum_replicates"],
    )
    if set(dev_deltas) != {"recall", "ndcg"}:
        raise ValueError("unregistered_primary_family")
    for series in dev_deltas.values():
        for value in series:
            number(value, "statistical_value")
    keys = sorted(dev_deltas)
    a, _, sizes = _matrix(np.array([dev_deltas[k] for k in keys]).T, groups)
    if (abs(a) > 1).any():
        raise ValueError("metric_delta_out_of_range")
    centered = a - a.mean(axis=0)
    rng = np.random.default_rng(seed)
    indices = [np.flatnonzero(np.array(groups) == g) for g in sorted(set(groups))]
    counts = [0, 0]
    for trial in range(trials):
        draws = rng.integers(0, len(indices), len(indices))
        sample = np.concatenate([centered[indices[i]] for i in draws])
        sample_groups = [str(j) for j, i in enumerate(draws) for _ in indices[i]]
        for arm, loss in enumerate((0.0, -0.10)):
            vals = {k: (sample[:, i] + loss).tolist() for i, k in enumerate(keys)}
            _, passed, _ = _family(
                vals,
                sample_groups,
                seed=seed + trial + 1,
                replicates=replicates,
                simulated=True,
            )
            counts[arm] += passed
    probabilities = [v / trials for v in counts]

    # Wilson bounds quantify finite Monte Carlo uncertainty; no 0/200 -> certainty.
    def bounds(x):
        p, z = x / trials, 1.959963984540054
        den = 1 + z * z / trials
        half = z * ((p * (1 - p) / trials + z * z / (4 * trials * trials)) ** 0.5) / den
        mid = (p + z * z / (2 * trials)) / den
        return [max(0.0, mid - half), min(1.0, mid + half)]

    cis = [bounds(c) for c in counts]
    sufficient = (
        trials >= 200
        and replicates >= 10000
        and len(sizes) >= 20
        and float(sizes.sum() ** 2 / (sizes @ sizes)) >= 20
        and (a.std(axis=0, ddof=1) > 0).all()
        and cis[0][0] >= 0.8
        and cis[1][1] <= 0.05
    )
    return {
        "status": "established" if sufficient else "insufficient",
        "seed": seed,
        "trials": trials,
        "replicates": replicates,
        "clusters": len(sizes),
        "neutral_pass_probability": probabilities[0],
        "loss_pass_probability": probabilities[1],
        "neutral_ci": cis[0],
        "loss_ci": cis[1],
        "method": "dev_joint_cluster_residual_nested_bootstrap",
        "planned_cluster_sizes": sorted(sizes.tolist()),
        "residual_support": "recentered simulation may extend beyond observed [-1,1]; no clipping",
        "limitations": "Conditional empirical pilot; minimum 20 effective clusters is a harness guard, not a sufficiency guarantee.",
    }


def quality_family(
    deltas, groups, *, seed, replicates=10000, dev_deltas=None, dev_groups=None
):
    details, passed, failed = _family(deltas, groups, seed=seed, replicates=replicates)
    from collections import Counter

    reasons, power = [], None
    if dev_deltas is not None:
        if dev_groups is None or sorted(Counter(groups).values()) != sorted(
            Counter(dev_groups).values()
        ):
            reasons.append("power_design_mismatch")
        elif set(groups) & set(dev_groups):
            reasons.append("dev_evaluation_group_overlap")
        else:
            power = simulate_power(
                dev_deltas, dev_groups, seed=seed, replicates=replicates
            )
    if replicates < BOOTSTRAP_POLICY["minimum_replicates"]:
        reasons.append("bootstrap_replicates_below_registered_minimum")
    if any(d["low"] == d["high"] for d in details.values()):
        reasons.append("degenerate_bootstrap_interval")
    if power is None or power["status"] != "established":
        reasons.append("power_not_established")
    if len(set(groups)) < 2:
        reasons.append("insufficient_clusters")
    status = (
        "inconclusive"
        if reasons
        else "pass"
        if passed
        else "fail"
        if failed
        else "inconclusive"
    )
    return {
        "status": status,
        "reasons": reasons,
        "metrics": details,
        "power": power,
        "margin": 0.05,
        "family_alpha": 0.05,
        "ci_method": "bonferroni_one_sided_cluster_percentile",
        "scope": "statistical_noninferiority_only",
    }


def binomial_interval(errors, n, *, alpha=0.05):
    """Clopper-Pearson one-sided bounds (each tail alpha), not a cluster CI."""
    import math

    number(alpha, "alpha")
    if (
        type(errors) is not int
        or type(n) is not int
        or not 0 <= errors <= n
        or n < 1
        or not 0 < alpha < 0.5
    ):
        raise ValueError("invalid_binomial_counts")

    def cdf(k, p):
        if p == 0:
            return 1.0
        if p == 1:
            return float(k >= n)
        return min(
            1.0,
            sum(
                math.exp(
                    math.lgamma(n + 1)
                    - math.lgamma(i + 1)
                    - math.lgamma(n - i + 1)
                    + i * math.log(p)
                    + (n - i) * math.log1p(-p)
                )
                for i in range(k + 1)
            ),
        )

    def root(k, target):
        low, high = 0.0, 1.0
        for _ in range(60):
            mid = (low + high) / 2
            if cdf(k, mid) > target:
                low = mid
            else:
                high = mid
        return (low + high) / 2

    return [
        0.0 if errors == 0 else root(errors - 1, 1 - alpha),
        1.0 if errors == n else root(errors, alpha),
    ]
