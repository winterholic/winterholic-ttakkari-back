"""Numeric trust boundary shared by timing and quality statistics."""

import math
from numbers import Real

BOOTSTRAP_POLICY = {
    "version": "kb-e0-statistics-v1",
    "minimum_replicates": 10000,
    "diagnostic_minimum_replicates": 100,
    "basis": "P1 fixes 10000 draws; P3 adopts the same conservative engineering minimum before pilot validation",
    "sufficiency": "Monte Carlo floor only; not evidence of sampling coverage or power",
}


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
