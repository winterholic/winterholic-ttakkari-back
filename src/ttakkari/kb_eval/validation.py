"""Numeric trust boundary shared by timing and quality statistics."""

import math
from numbers import Real

# Mirrors kb-eval/statistical-policy.json; a unit test enforces equality.
BOOTSTRAP_POLICY = {
    "version": "kb-e0-statistics-v3",
    "minimum_replicates": 10000,
    "diagnostic_minimum_replicates": 100,
    "basis": "P1 fixes 10000 draws; P3 adopts the same conservative engineering "
    "minimum before pilot validation",
    "sufficiency": "Monte Carlo floor only; not evidence of sampling coverage or power",
    "degeneracy": {
        "timer_resolution_ms": 0.001,
        "latency_min_cv": 0.0001,
        "latency_arm_rule": "an arm is information-free when its e2e "
        "SD < max(timer_resolution_ms, "
        "latency_min_cv * mean e2e)",
        "latency_basis": "timer_resolution_ms is an absolute "
        "measurement floor, not a variability claim: "
        "perf_counter on the reference host is "
        "mach_absolute_time with 4.17e-8 s resolution "
        "(reports/kb-e0/quality-hardening/timer-resolution.txt); "
        "1 us is a conservative cross-host floor 24x "
        "coarser. latency_min_cv 1e-4 is relative: "
        "1/2000 of the 0.2 relative-latency margin, "
        "so a trace whose arm varies less than that "
        "cannot move a p95 ratio decision and is "
        "treated as constant. The absolute-only 0.1 "
        "ms floor of v2 held informative traces with "
        "mean <= 20 ms (third review table 1-4).",
        "latency_ratio_ci_min_width": 0.0005,
        "latency_ratio_basis": "p95 ratio CI width is already "
        "scale-free; 5e-4 = 1/400 of the 0.2 "
        "relative margin (ratio 1.2). It does "
        "not depend on the mean e2e, so it "
        "applies equally to fast and full "
        "modes.",
        "quality_ci_min_width": 0.001,
        "quality_dev_min_std": 0.001,
        "quality_basis": "0.001 = noninferiority margin 0.05 / 50. A "
        "metric-delta CI narrower than 2% of the "
        "margin, or a dev pilot whose per-question "
        "delta SD is below it, is practically "
        "constant (far above float noise, which is "
        "~1e-16) and cannot establish pass or power. "
        "Real per-question delta SD is typically near "
        "0.1, so power=established from a pilot near "
        "this floor is optimistic and must not be "
        "used as approval evidence before a measured "
        "pilot.",
        "scope": "Pre-registered engineering floors before any real "
        "pilot; recheck on measured traces. Exceeding them is "
        "necessary, not sufficient, for an informative "
        "interval.",
    },
}
DEGENERACY = BOOTSTRAP_POLICY["degeneracy"]


def degenerate_width(low, high, minimum):
    """Scale-aware degeneracy: exact equality misses float-noise intervals."""
    return not high - low >= minimum


def integer(value, name, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(
            "invalid_" + name + ": integer >= " + str(minimum) + " required"
        )
    return value


def number(value, name, *, minimum=None, maximum=None):
    try:
        finite = (
            not isinstance(value, bool)
            and isinstance(value, Real)
            and math.isfinite(value)
        )
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        raise ValueError("invalid_" + name + ": finite numeric value required")
    if (minimum is not None and value < minimum) or (
        maximum is not None and value > maximum
    ):
        raise ValueError("invalid_" + name + ": out of range")
    return value


def probability(value, name):
    return number(value, name, minimum=0.0, maximum=1.0)
