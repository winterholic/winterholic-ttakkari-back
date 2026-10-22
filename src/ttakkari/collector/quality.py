"""Collector Platform: L0 rule signals and the L1 scorer port (CP-S2-06).

L0 is cheap and deterministic. It only produces *hold* flags: a flag never deletes a document, it keeps it
recoverable and auditable (architecture 7). Short text, odd length or repetition are not proof of low quality.

L1 (a trained classifier) is an interface plus a policy hook here. No model ships with this package: with no scorer
registered every axis stays `unmeasured`, never 0. A scorer must declare model_id/model_revision/preprocess_version
and a scale from the envelope contract, and the policy decides per job whether its output is shadow-only or gates.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from ttakkari.collector.contracts import (
    ALLOWED_AXIS_SCALES,
    QUALITY_AXES,
    CollectorContractError,
)

L0_VERSION = "l0-1"
_URL = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class L0Thresholds:
    min_chars: int = 200
    max_symbol_ratio: float = 0.40
    min_alpha_ratio: float = 0.45
    max_duplicate_line_ratio: float = 0.50
    max_repeated_trigram_ratio: float = 0.60
    max_url_density: float = 0.20
    short_line_chars: int = 30
    max_short_line_ratio: float = 0.85
    short_line_min_lines: int = 10


@dataclass(frozen=True)
class L0Result:
    signals: dict[str, float | int]
    flags: tuple[str, ...]
    version: str = L0_VERSION


def l0_signals(text: str, thresholds: L0Thresholds | None = None) -> L0Result:
    t = thresholds or L0Thresholds()
    chars = len(text)
    non_space = [c for c in text if not c.isspace()]
    n = len(non_space) or 1
    letters = sum(c.isalpha() for c in non_space)
    digits = sum(c.isdigit() for c in non_space)
    symbols = n - letters - digits
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    line_counts = Counter(lines)
    repeated_lines = sum(c for c in line_counts.values() if c > 1)
    words = text.split()
    word_n = len(words)
    # Gopher-style repetition: share of word 3-grams that occur more than once.
    trigrams = [" ".join(words[i : i + 3]) for i in range(max(word_n - 2, 0))]
    tri_counts = Counter(trigrams)
    repeated_tri = sum(c for c in tri_counts.values() if c > 1)
    short_lines = sum(len(ln) < t.short_line_chars for ln in lines)
    signals: dict[str, float | int] = {
        "char_count": chars,
        "word_count": word_n,
        "line_count": len(lines),
        "mean_line_chars": round(sum(map(len, lines)) / len(lines), 2) if lines else 0.0,
        "alpha_ratio": round(letters / n, 4),
        "digit_ratio": round(digits / n, 4),
        "symbol_ratio": round(symbols / n, 4),
        "duplicate_line_ratio": round(repeated_lines / len(lines), 4) if lines else 0.0,
        "repeated_trigram_ratio": round(repeated_tri / len(trigrams), 4) if trigrams else 0.0,
        "url_density": round(len(_URL.findall(text)) / word_n, 4) if word_n else 0.0,
        "short_line_ratio": round(short_lines / len(lines), 4) if lines else 0.0,
    }
    flags: list[str] = []
    if chars < t.min_chars:
        flags.append("l0_too_short")
    if chars and signals["symbol_ratio"] > t.max_symbol_ratio:
        flags.append("l0_symbol_heavy")
    if chars >= t.min_chars and signals["alpha_ratio"] < t.min_alpha_ratio:
        flags.append("l0_low_alpha_ratio")
    if signals["duplicate_line_ratio"] > t.max_duplicate_line_ratio and len(lines) >= 4:
        flags.append("l0_repeated_lines")
    if signals["repeated_trigram_ratio"] > t.max_repeated_trigram_ratio and word_n >= 30:
        flags.append("l0_repetitive")
    if signals["url_density"] > t.max_url_density and word_n >= 10:
        flags.append("l0_link_heavy")
    if len(lines) >= t.short_line_min_lines and signals["short_line_ratio"] > t.max_short_line_ratio:
        flags.append("l0_mostly_short_lines")
    return L0Result(signals, tuple(flags))


# ---- L1 port -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AxisScore:
    value: float
    scale: str  # raw_logit | ordinal | rule | similarity
    calibrated_probability: float | None = None
    calibration_id: str | None = None
    calibration_gold_hash: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScorerInfo:
    name: str
    model_id: str
    model_revision: str
    preprocess_version: str


class QualityScorer(Protocol):
    """A local, bounded scorer. It must not call an LLM; usage-bounded teachers belong to a later budget gate."""

    info: ScorerInfo

    async def score(self, text: str, language: str) -> dict[str, AxisScore]: ...


_registry: dict[str, QualityScorer] = {}


def register_scorer(scorer: QualityScorer) -> None:
    _registry[scorer.info.name] = scorer


def unregister_scorer(name: str) -> None:
    _registry.pop(name, None)


def get_scorer(name: str) -> QualityScorer | None:
    return _registry.get(name)


@dataclass(frozen=True)
class L1Policy:
    """Per-job L1 stance. `mode`: off (never call), shadow (record, never gate), gate (hold below `hold_below`)."""

    mode: str = "off"
    scorer: str | None = None
    hold_below: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in ("off", "shadow", "gate"):
            raise CollectorContractError(f"l1.mode must be off|shadow|gate, got {self.mode!r}")
        if self.mode != "off" and not self.scorer:
            raise CollectorContractError("l1.scorer is required unless l1.mode is off")
        for axis in self.hold_below:
            if axis not in QUALITY_AXES:
                raise CollectorContractError(f"l1.hold_below axis {axis!r} is not a quality axis")
        if self.hold_below and self.mode != "gate":
            raise CollectorContractError("l1.hold_below only applies when l1.mode is gate")


def unmeasured_axis(preprocess_version: str) -> dict[str, Any]:
    return {
        "status": "unmeasured", "value": None, "scale": "none", "model_id": None, "model_revision": None,
        "preprocess_version": preprocess_version, "calibration_id": None, "calibration_gold_hash": None,
        "calibrated_probability": None, "query_id": None, "evidence": [],
    }


def unmeasured_quality(preprocess_version: str) -> dict[str, dict[str, Any]]:
    return {axis: unmeasured_axis(preprocess_version) for axis in QUALITY_AXES}


@dataclass(frozen=True)
class L1Outcome:
    quality: dict[str, dict[str, Any]]
    hold_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    error: str | None = None


async def run_l1(policy: L1Policy, text: str, language: str, preprocess_version: str) -> L1Outcome:
    """Apply the L1 policy. Never raises on a scorer failure: a failed scorer yields an error note, not a pass."""
    quality = unmeasured_quality(preprocess_version)
    if policy.mode == "off":
        return L1Outcome(quality, notes=("l1_off",))
    scorer = get_scorer(policy.scorer or "")
    if scorer is None:
        return L1Outcome(quality, error="scorer_unavailable", notes=("l1_scorer_unavailable",))
    try:
        scores = await scorer.score(text, language)
    except Exception as e:  # noqa: BLE001  a scorer bug must not turn into an accept
        return L1Outcome(quality, error=f"scorer_error:{type(e).__name__}", notes=("l1_scorer_error",))
    holds: list[str] = []
    for axis, s in scores.items():
        if axis not in QUALITY_AXES:
            return L1Outcome(quality, error=f"scorer_unknown_axis:{axis}", notes=("l1_scorer_error",))
        if s.scale not in ALLOWED_AXIS_SCALES or s.scale == "none":
            return L1Outcome(quality, error=f"scorer_bad_scale:{axis}", notes=("l1_scorer_error",))
        if axis == "relevance":
            continue  # relevance needs a query_id; document-level L1 never fills it
        quality[axis] = {
            "status": "measured", "value": float(s.value), "scale": s.scale, "model_id": scorer.info.model_id,
            "model_revision": scorer.info.model_revision, "preprocess_version": scorer.info.preprocess_version,
            "calibration_id": s.calibration_id, "calibration_gold_hash": s.calibration_gold_hash,
            "calibrated_probability": s.calibrated_probability, "query_id": None, "evidence": list(s.evidence),
        }
        floor = policy.hold_below.get(axis)
        if policy.mode == "gate" and floor is not None and s.value < floor:
            holds.append(f"l1_{axis}_below_threshold")
    notes = ("l1_shadow",) if policy.mode == "shadow" else ("l1_gate",)
    return L1Outcome(quality, tuple(holds), notes)
