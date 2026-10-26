"""Collector Platform: Lightweight multi-axis quality scoring and conservative auto-triage (C4).

Stage 1 (this module's rules) is local CPU, sub-millisecond per document, no model calls. Stage 2
(`triage_ml`, optional `ml` argument) adds independent embedding-based axes; they can only add review/precise-evaluation
flags or rescue a lexical hold, and only when their own validation status is `validated`. Stage 3 is a persisted
precise-evaluation queue state (no engine is attached yet). Stage 4 is ingest-with-flag / recoverable hold.
Evaluates:
  1. Content Quality: length, structure, boilerplate ratio, truncation.
  2. Novelty / Near-duplicate: exact match and MinHash Jaccard similarity.
  3. Source Trust: verified license, allowed hosts, robots/HTTPS signals.
  4. Goal Relevance: token overlap, title match, leading text and term frequency.

Conservative auto-triage:
  - Clearly good -> auto ingest (accept)
  - Clearly bad / irrelevant / spam / duplicate -> auto hold (recoverable)
  - Ambiguous -> ingest with 'review_needed' flag (conservative collection: avoid dropping good docs).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ttakkari.collector.contracts import (
    QUALITY_AXES,
)
from ttakkari.collector.triage_ml import VERSION as ML_VERSION

TRIAGE_VERSION = "multiaxis-triage-v2"
MODEL_ID = "ttakkari/lightweight-multiaxis-v1"
MODEL_REVISION = "2026.10.25"
PREPROCESS_VERSION = "triage-tokens-v1"

# Stopwords for relevance scoring (Korean particles/common words and English common words)
STOPWORDS = {
    # English
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "me",
    "more",
    "most",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "now",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "s",
    "same",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
    # Korean particles / generic markers
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "로",
    "으로",
    "과",
    "와",
    "도",
    "에서",
    "수",
    "등",
    "및",
    "위한",
    "대한",
    "통해",
    "관련",
    "수집",
    "자료",
    "문서",
    "연구",
    "조사",
    "있는",
    "하는",
    "되는",
    "하여",
    "그",
    "이것",
    "저것",
    "어떤",
    "모든",
}

_WORD_PATTERN = re.compile(r"[a-zA-Z0-9_\uac00-\ud7a3]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Tokenize into alphanumeric words, stripping trailing Korean particles."""
    raw_tokens = _WORD_PATTERN.findall(text.casefold())
    tokens: list[str] = []
    # Common Korean postposition suffixes to strip from word endings (josa stripping)
    suffixes = (
        "에서",
        "으로",
        "에는",
        "이다",
        "이며",
        "이고",
        "은",
        "는",
        "이",
        "가",
        "을",
        "를",
        "에",
        "의",
        "로",
        "과",
        "와",
        "도",
    )
    for word in raw_tokens:
        if word in STOPWORDS:
            continue
        cleaned = word
        for suf in suffixes:
            if cleaned.endswith(suf) and len(cleaned) > len(suf) + 1:
                cleaned = cleaned[: -len(suf)]
                break
        if cleaned not in STOPWORDS and len(cleaned) >= 2:
            tokens.append(cleaned)
    return tokens


def calculate_relevance(goal: str, title: str, text: str) -> tuple[float, list[str]]:
    """Compute local goal relevance score [0.0, 1.0] and evidence notes.

    CPU time: < 1ms for typical documents.
    """
    if not goal.strip():
        return 1.0, ["no_goal_specified"]

    goal_tokens = _tokenize(goal)
    if not goal_tokens:
        return 0.5, ["uninformative_goal"]

    goal_set = set(goal_tokens)
    goal_counts = Counter(goal_tokens)

    # 1. Title match (strong signal)
    title_tokens = _tokenize(title) if title else []
    title_matches = [t for t in title_tokens if t in goal_set]
    title_score = len(title_matches) / len(goal_set) if goal_set else 0.0
    title_score = min(1.0, title_score * 1.5)  # Boost title overlap

    # 2. Leading text / Introduction match (first 1500 chars)
    lead_tokens = _tokenize(text[:1500])
    lead_matches = set(lead_tokens) & goal_set
    lead_score = len(lead_matches) / len(goal_set) if goal_set else 0.0

    # 3. Full body TF / term presence
    body_tokens = _tokenize(text[:30000])
    if not body_tokens:
        return 0.0, ["empty_body_for_relevance"]

    body_counts = Counter(body_tokens)
    matched_goal_terms = sum(1 for term in goal_set if body_counts.get(term, 0) > 0)
    term_coverage = matched_goal_terms / len(goal_set)

    # Weighted term frequency (saturation curve)
    tf_sum = 0.0
    for term, g_count in goal_counts.items():
        count = body_counts.get(term, 0)
        if count > 0:
            tf_sum += (1.0 + math.log(count)) * g_count
    norm_tf = min(1.0, tf_sum / (len(goal_tokens) * 3.0))

    # Composite relevance
    score = (
        0.40 * title_score + 0.25 * lead_score + 0.25 * term_coverage + 0.10 * norm_tf
    )
    score = round(max(0.0, min(1.0, score)), 4)

    evidence = [
        f"term_coverage={term_coverage:.2f}",
        f"title_overlap={title_score:.2f}",
        f"lead_overlap={lead_score:.2f}",
        f"matched_terms={matched_goal_terms}/{len(goal_set)}",
    ]
    if title_matches:
        evidence.append(f"title_matches={','.join(sorted(set(title_matches))[:5])}")

    return score, evidence


def calculate_quality(
    text: str,
    l0_signals: dict[str, Any] | None = None,
    *,
    truncated: bool = False,
) -> tuple[float, list[str]]:
    """Compute content quality score [0.0, 1.0] from structure, boilerplate, and text signals."""
    chars = len(text)
    if chars == 0:
        return 0.0, ["empty_text"]

    evidence: list[str] = []
    score = 1.0

    # Length penalty
    if chars < 50:
        score -= 0.70
        evidence.append("too_short_<50")
    elif chars < 150:
        score -= 0.50
        evidence.append("too_short_<150")
    elif chars < 400:
        score -= 0.20
        evidence.append("short_<400")
    elif chars > 2000:
        score += 0.05

    # Signals from L0 or computed directly
    if l0_signals:
        alpha_ratio = float(l0_signals.get("alpha_ratio", 0.5))
        symbol_ratio = float(l0_signals.get("symbol_ratio", 0.1))
        short_line_ratio = float(l0_signals.get("short_line_ratio", 0.0))
        duplicate_line_ratio = float(l0_signals.get("duplicate_line_ratio", 0.0))
        repeated_trigram_ratio = float(l0_signals.get("repeated_trigram_ratio", 0.0))
    else:
        non_space = [c for c in text if not c.isspace()]
        n = len(non_space) or 1
        alpha_ratio = sum(c.isalpha() for c in non_space) / n
        symbol_ratio = sum(not c.isalnum() for c in non_space) / n
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        short_line_ratio = sum(len(ln) < 30 for ln in lines) / max(1, len(lines))
        duplicate_line_ratio = 0.0
        repeated_trigram_ratio = 0.0

    if symbol_ratio > 0.40:
        score -= 0.45
        evidence.append(f"symbol_heavy={symbol_ratio:.2f}")
    elif symbol_ratio > 0.25:
        score -= 0.20

    if alpha_ratio < 0.35:
        score -= 0.30
        evidence.append(f"low_alpha={alpha_ratio:.2f}")

    if duplicate_line_ratio > 0.40:
        score -= 0.30
        evidence.append(f"high_duplicate_lines={duplicate_line_ratio:.2f}")

    if repeated_trigram_ratio > 0.50:
        score -= 0.30
        evidence.append(f"high_repetition={repeated_trigram_ratio:.2f}")

    if short_line_ratio > 0.80 and chars > 300:
        score -= 0.20
        evidence.append("mostly_short_lines")

    if truncated:
        score -= 0.10
        evidence.append("truncated_extraction")

    # Structural richness bonus: code blocks, markdown headings, lists
    has_headings = bool(re.search(r"^#{1,6}\s+\S+", text, re.MULTILINE))
    has_code = (
        "```" in text or "def " in text or "class " in text or "function " in text
    )
    has_lists = bool(re.search(r"^\s*[-*•\d+.]\s+\S+", text, re.MULTILINE))

    if has_headings:
        score += 0.05
    if has_code:
        score += 0.05
    if has_lists:
        score += 0.05

    score = round(max(0.0, min(1.0, score)), 4)
    return score, evidence


@dataclass(frozen=True)
class TriageThresholds:
    # Auto-accept floors (clearly good)
    min_accept_relevance: float = 0.25
    min_accept_quality: float = 0.45
    min_accept_novelty: float = 0.35

    # Auto-hold ceilings (clearly bad)
    max_hold_relevance: float = 0.05
    max_hold_quality: float = 0.20

    # Near-duplicate threshold
    max_near_jaccard_accept: float = 0.85


@dataclass(frozen=True)
class TriageOutcome:
    action: str  # "accept" | "hold" | "error"
    triage_status: str  # "auto_accepted" | "review_needed" | "auto_held"
    scores: dict[str, float]
    reasons: list[str]
    thresholds: dict[str, float]
    quality_envelope: dict[str, dict[str, Any]]
    signals: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def evaluate_triage(
    *,
    goal: str,
    title: str,
    text: str,
    l0_signals: dict[str, Any] | None = None,
    exact_duplicate: bool = False,
    near_duplicates: list[dict[str, Any]] | None = None,
    source_policy_allowed: bool = True,
    truncated: bool = False,
    thresholds: TriageThresholds | None = None,
    shadow_mode: bool = False,
    ml: dict[str, Any] | None = None,
) -> TriageOutcome:
    """Conservative multi-axis evaluation and triage.

    Conservative principle:
      False Negative (holding a genuinely good document) is minimized.
      Ambiguous documents are accepted with 'review_needed' rather than held.
    """
    t = thresholds or TriageThresholds()
    chars = len(text)

    # 1. Base error check
    if not text.strip():
        reasons = ["empty_text"]
        return _make_outcome(
            action="error",
            status="auto_held",
            scores={
                "relevance": 0.0,
                "quality": 0.0,
                "novelty": 1.0,
                "trust": 1.0,
                "composite": 0.0,
            },
            reasons=reasons,
            t=t,
            goal=goal,
            title=title,
            text=text,
            shadow_mode=shadow_mode,
        )

    # 2. Compute individual axes
    rel_score, rel_evidence = calculate_relevance(goal, title, text)
    qual_score, qual_evidence = calculate_quality(text, l0_signals, truncated=truncated)

    # Novelty from exact & near dedupe
    if exact_duplicate:
        nov_score = 0.0
        nov_evidence = ["exact_duplicate"]
    elif near_duplicates:
        max_jaccard = max((d.get("jaccard", 0.0) for d in near_duplicates), default=0.0)
        nov_score = round(max(0.0, 1.0 - max_jaccard), 4)
        nov_evidence = [f"near_duplicate_max_jaccard={max_jaccard:.2f}"]
    else:
        nov_score = 1.0
        nov_evidence = ["novel_content"]

    # Source Trust
    trust_score = 1.0 if source_policy_allowed else 0.5
    # Composite score
    composite = round(
        0.40 * rel_score + 0.35 * qual_score + 0.15 * nov_score + 0.10 * trust_score,
        4,
    )
    scores = {
        "relevance": rel_score,
        "quality": qual_score,
        "novelty": nov_score,
        "trust": trust_score,
        "composite": composite,
    }

    # 3. Conservative Decision Logic
    reasons: list[str] = []
    action = "accept"
    status = "auto_accepted"

    # Hard hold conditions (Clearly Bad)
    if exact_duplicate:
        action = "hold"
        status = "auto_held"
        reasons.append("exact_duplicate")
    elif qual_score < t.max_hold_quality and chars < 300:
        action = "hold"
        status = "auto_held"
        reasons.append("triage:low_quality")
    elif (
        len(set(_tokenize(goal))) >= 2
        and any("\uac00" <= char <= "\ud7a3" for char in goal)
        == any("\uac00" <= char <= "\ud7a3" for char in text)
        and rel_score <= t.max_hold_relevance
        and "term_coverage=0.00" in rel_evidence
    ):
        # Zero lexical overlap is only a hard hold when the goal and text use the
        # same script; cross-language retrieval is uncertain, not clearly irrelevant.
        action = "hold"
        status = "auto_held"
        reasons.append("triage:irrelevant_to_goal")
    # Ambiguous / Review Needed conditions (Conservative: keep document, flag review)
    elif (
        rel_score < t.min_accept_relevance
        or qual_score < t.min_accept_quality
        or nov_score < t.min_accept_novelty
        or truncated
    ):
        action = "accept"
        status = "review_needed"
        reasons.append("review_needed")
        if rel_score < t.min_accept_relevance:
            reasons.append("review:low_relevance")
        if qual_score < t.min_accept_quality:
            reasons.append("review:low_quality")
        if nov_score < t.min_accept_novelty:
            reasons.append("review:near_duplicate")
        if truncated:
            reasons.append("review:truncated")
    else:
        # Clearly Good
        action = "accept"
        status = "auto_accepted"
        reasons.append("triage:accepted")
        if rel_score >= 0.50:
            reasons.append("triage:high_relevance")

    action, status, reasons, precise, effects = _apply_stage2(
        ml, action=action, status=status, reasons=reasons
    )
    return _make_outcome(
        action=action,
        status=status,
        scores=scores,
        reasons=reasons,
        t=t,
        goal=goal,
        title=title,
        text=text,
        rel_evidence=rel_evidence,
        qual_evidence=qual_evidence,
        nov_evidence=nov_evidence,
        shadow_mode=shadow_mode,
        ml=ml,
        precise=precise,
        ml_effects=effects,
    )


def _to_review(reasons: list[str], *extra: str) -> list[str]:
    kept = [r for r in reasons if r not in ("triage:accepted", "triage:high_relevance", "review_needed")]
    return ["review_needed", *kept, *[e for e in extra if e not in kept]]


def _apply_stage2(
    ml: dict[str, Any] | None, *, action: str, status: str, reasons: list[str]
) -> tuple[str, str, list[str], list[str], list[str]]:
    """Fold validated stage-2 axes into the rule decision. Never produces a hold; never trusts an unvalidated axis."""
    reasons = list(reasons)
    precise: list[str] = []
    effects: list[str] = []
    axes = (ml or {}).get("axes") or {}
    sem = axes.get("topic_relevance_semantic")
    prob = axes.get("low_quality_likelihood")
    sem_ok = bool(sem) and sem["validation"] == "validated"
    prob_ok = bool(prob) and prob["validation"] == "validated"
    if sem_ok:
        value, low, high = sem["value"], sem["bands"]["low"], sem["bands"]["high"]
        if action == "hold" and "triage:irrelevant_to_goal" in reasons:
            if value > low:
                action, status = "accept", "review_needed"
                reasons = ["review_needed", "review:semantic_overrides_lexical_hold"]
                precise.append("lexical_semantic_disagreement")
                effects.append("rescued_from_lexical_hold")
        elif action == "accept":
            if "review:low_relevance" in reasons:
                if value >= high:
                    reasons.remove("review:low_relevance")
                    effects.append("lexical_low_relevance_cleared_by_semantic")
                    if any(r.startswith("review:") for r in reasons):
                        reasons = _to_review(reasons, "relevance_confirmed_by_semantic")
                    else:
                        status, reasons = "auto_accepted", ["triage:accepted", "relevance_confirmed_by_semantic"]
                elif value >= low:
                    precise.append("semantic_boundary_band")
            elif value < low:
                status, reasons = "review_needed", _to_review(reasons, "review:semantic_low_relevance")
                precise.append("lexical_semantic_disagreement")
                effects.append("semantic_flags_low_relevance")
    if prob_ok and action == "accept" and prob["value"] >= prob["review_at"]:
        status, reasons = "review_needed", _to_review(reasons, "review:ml_low_quality_likelihood")
        precise.append("ml_flags_low_quality_but_rules_pass")
        effects.append("ml_low_quality_review")
    if not (sem_ok or prob_ok) and action == "accept" and status == "review_needed" and any(
        r in reasons for r in ("review:low_relevance", "review:low_quality")
    ):
        precise.append("stage2_unavailable_or_unvalidated")
    if action != "accept":
        precise = []
    return action, status, reasons, sorted(set(precise)), effects


_RULE_VALIDATION = {
    "validation": "unvalidated",
    "reason": "hand-written heuristic; no external human-labelled evaluation exists for this axis",
}


def _axes_block(
    scores: dict[str, float], rel_evidence: list[str] | None, qual_evidence: list[str] | None,
    nov_evidence: list[str] | None, ml: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Per-axis score with its model, version and validation state. Axes are listed side by side, never merged."""
    rule = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION, "kind": "rule", **_RULE_VALIDATION}
    axes: dict[str, dict[str, Any]] = {
        "relevance_lexical": {**rule, "value": scores["relevance"], "scale": "similarity", "evidence": rel_evidence or []},
        "content_rules": {**rule, "value": scores["quality"], "scale": "rule", "evidence": qual_evidence or []},
        "novelty": {**rule, "value": scores["novelty"], "scale": "rule", "evidence": nov_evidence or []},
    }
    for name, axis in ((ml or {}).get("axes") or {}).items():
        digest = str((ml or {}).get("artifact_digest", ""))[:12]
        snapshot = str(((ml or {}).get("embedding") or {}).get("snapshot") or "")[:12]
        axes[name] = {**axis, "kind": "ml", "model_id": f"ttakkari/{ML_VERSION}+intfloat/multilingual-e5-large",
                      "model_revision": f"{digest}/{snapshot}"}
    return axes


def _stage2_block(ml: dict[str, Any] | None, effects: list[str]) -> dict[str, Any]:
    ml = ml or {"status": "not_run", "reason": "no stage-2 result for this document"}
    return {
        "status": ml.get("status", "not_run"), "reason": ml.get("reason", ""), "effects": effects,
        "embedding": ml.get("embedding"), "cost": ml.get("cost"), "artifact_digest": ml.get("artifact_digest"),
    }


def _validation_block(axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    states = {name: axis.get("validation", "unvalidated") for name, axis in axes.items()}
    validated = sorted(name for name, v in states.items() if v == "validated")
    return {
        "overall": "partial" if validated else "unvalidated", "validated_axes": validated,
        "axes": states, "note": "unvalidated axes are recorded but never decide; deletion is never automatic",
    }


def _make_outcome(
    *,
    action: str,
    status: str,
    scores: dict[str, float],
    reasons: list[str],
    t: TriageThresholds,
    goal: str,
    title: str,
    text: str,
    rel_evidence: list[str] | None = None,
    qual_evidence: list[str] | None = None,
    nov_evidence: list[str] | None = None,
    shadow_mode: bool = False,
    ml: dict[str, Any] | None = None,
    precise: list[str] | None = None,
    ml_effects: list[str] | None = None,
) -> TriageOutcome:
    threshold_dict = {
        "min_accept_relevance": t.min_accept_relevance,
        "min_accept_quality": t.min_accept_quality,
        "min_accept_novelty": t.min_accept_novelty,
        "max_hold_relevance": t.max_hold_relevance,
        "max_hold_quality": t.max_hold_quality,
    }
    signals = {
        "version": TRIAGE_VERSION,
        "action": action,
        "triage_status": status,
        "scores": scores,
        "reasons": reasons,
        "thresholds": threshold_dict,
        "mode": "shadow" if shadow_mode else "active",
        "verified": False,  # Transparently expose unverified status
        "axes": _axes_block(scores, rel_evidence, qual_evidence, nov_evidence, ml),
        "stage2": _stage2_block(ml, ml_effects or []),
        "precise_eval": {
            "needed": bool(precise), "state": "pending" if precise else "not_needed", "reasons": precise or [],
        },
    }
    signals["validation"] = _validation_block(signals["axes"])

    # Build contracts-compliant quality envelope
    query_id = (
        hashlib.sha256(goal.strip().encode("utf-8")).hexdigest()[:16]
        if goal.strip()
        else None
    )
    quality_env: dict[str, dict[str, Any]] = {}
    for axis in QUALITY_AXES:
        if axis == "relevance":
            if query_id:
                quality_env[axis] = {
                    "status": "measured",
                    "value": float(scores["relevance"]),
                    "scale": "similarity",
                    "model_id": MODEL_ID,
                    "model_revision": MODEL_REVISION,
                    "preprocess_version": PREPROCESS_VERSION,
                    "calibration_id": None,
                    "calibration_gold_hash": None,
                    "calibrated_probability": None,
                    "query_id": query_id,
                    "evidence": rel_evidence or [],
                }
            else:
                quality_env[axis] = {
                    "status": "unmeasured",
                    "value": None,
                    "scale": "none",
                    "model_id": None,
                    "model_revision": None,
                    "preprocess_version": PREPROCESS_VERSION,
                    "calibration_id": None,
                    "calibration_gold_hash": None,
                    "calibrated_probability": None,
                    "query_id": None,
                    "evidence": [],
                }
        elif axis == "utility":
            quality_env[axis] = {
                "status": "measured",
                "value": float(scores["quality"]),
                "scale": "rule",
                "model_id": MODEL_ID,
                "model_revision": MODEL_REVISION,
                "preprocess_version": PREPROCESS_VERSION,
                "calibration_id": None,
                "calibration_gold_hash": None,
                "calibrated_probability": None,
                "query_id": None,
                "evidence": qual_evidence or [],
            }
        elif axis == "novelty":
            quality_env[axis] = {
                "status": "measured",
                "value": float(scores["novelty"]),
                "scale": "rule",
                "model_id": MODEL_ID,
                "model_revision": MODEL_REVISION,
                "preprocess_version": PREPROCESS_VERSION,
                "calibration_id": None,
                "calibration_gold_hash": None,
                "calibrated_probability": None,
                "query_id": None,
                "evidence": nov_evidence or [],
            }
        elif axis == "factuality":
            quality_env[axis] = {
                "status": "unmeasured",
                "value": None,
                "scale": "none",
                "model_id": None,
                "model_revision": None,
                "preprocess_version": PREPROCESS_VERSION,
                "calibration_id": None,
                "calibration_gold_hash": None,
                "calibrated_probability": None,
                "query_id": None,
                "evidence": [],
            }
        elif axis == "spam":
            spam_score = round(max(0.0, 1.0 - scores["quality"]), 4)
            quality_env[axis] = {
                "status": "measured",
                "value": float(spam_score),
                "scale": "rule",
                "model_id": MODEL_ID,
                "model_revision": MODEL_REVISION,
                "preprocess_version": PREPROCESS_VERSION,
                "calibration_id": None,
                "calibration_gold_hash": None,
                "calibrated_probability": None,
                "query_id": None,
                "evidence": qual_evidence or [],
            }
        else:
            # education
            quality_env[axis] = {
                "status": "unmeasured",
                "value": None,
                "scale": "none",
                "model_id": None,
                "model_revision": None,
                "preprocess_version": PREPROCESS_VERSION,
                "calibration_id": None,
                "calibration_gold_hash": None,
                "calibrated_probability": None,
                "query_id": None,
                "evidence": [],
            }

    return TriageOutcome(
        action=action,
        triage_status=status,
        scores=scores,
        reasons=reasons,
        thresholds=threshold_dict,
        quality_envelope=quality_env,
        signals=signals,
        notes=["conservative_triage", f"triage_{status}"],
    )
