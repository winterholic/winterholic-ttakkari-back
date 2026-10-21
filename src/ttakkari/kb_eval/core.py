"""Stable pure harness entry points; no model routing or retrieval engine."""

from .comparison import compare_runs
from .integrity import anchor, components
from .latency import balanced_orders, latency_report, timed_call
from .loader import load_bundle
from .metrics import answer_recall, classify_failure, metrics
from .reproducibility import live_tolerance
from .runner import resources, run
from .state import cache_key, open_holdout, review_update, verify_replay
from .statistics import (
    binomial_interval,
    cluster_interval,
    holm,
    quality_family,
    simulate_power,
)

__all__ = [
    "anchor",
    "answer_recall",
    "balanced_orders",
    "binomial_interval",
    "cache_key",
    "classify_failure",
    "cluster_interval",
    "compare_runs",
    "components",
    "holm",
    "latency_report",
    "live_tolerance",
    "load_bundle",
    "metrics",
    "open_holdout",
    "quality_family",
    "resources",
    "review_update",
    "run",
    "simulate_power",
    "timed_call",
    "verify_replay",
]
