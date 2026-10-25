"""Stage-2 lightweight ML axes for collector triage (C4).

One frozen multilingual-e5-large embedding per document (the model Ttakkari already ships for meeting/KB vectors,
run in the existing disposable embedding child process, never in the API process) feeds independent axes:
  - topic_relevance_semantic: cosine(goal as `query:`, title+lead as `passage:`)
  - information_value: ridge head, ordinal 0..4, trained on FineWeb-C kor_Hang HUMAN educational-value labels
  - low_quality_likelihood: logistic head on the FineWeb-C human "problematic content" flag
  - explanation_quality: not measured (no labelled data exists for it)
Axes are never merged into one score. An axis may change a decision only when its artifact validation is
`validated` for the document's language; otherwise it is recorded with `validation: unvalidated` and the reason.
ML can only move a document towards review/precise evaluation or rescue it from a lexical hold; it never holds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np

VERSION = "e5-heads-v1"
DOC_CHARS = 1500
EMBED_DIM = 1024  # multilingual-e5-large; the artifact heads are fitted in this space
EXPECTED_SNAPSHOT = "ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b"
ARTIFACT_PATH = Path(__file__).with_name("triage_ml_artifact.json")
QUERY_PREFIX, PASSAGE_PREFIX = "query: ", "passage: "
# Languages the human/external labels cover; everything else is recorded as unvalidated.
VALIDATED_LANGUAGES = frozenset({"ko"})

# Validation bars, fixed before the first evaluation run (held-out AUC and the lower end of its bootstrap CI).
HEAD_MIN_AUC, HEAD_MIN_AUC_LOWER, HEAD_MIN_POSITIVES = 0.75, 0.60, 15
REL_MIN_AUC, REL_MIN_AUC_LOWER = 0.80, 0.70

AXIS_NAMES = ("topic_relevance_semantic", "information_value", "low_quality_likelihood", "explanation_quality")

EmbedFn = Callable[[list[str], list[str]], Awaitable[tuple[list[list[float]], dict[str, Any]]]]


def doc_passage(title: str, text: str) -> str:
    return f"{title.strip()}\n{text}"[:DOC_CHARS] if title.strip() else text[:DOC_CHARS]


def _floats(values: Any) -> list[float]:
    return [round(float(v), 6) for v in values]


def validation(metrics: dict[str, Any], *, min_auc: float, min_lower: float, min_pos: int | None,
               label_source: str) -> dict[str, Any]:
    """validated only when held-out AUC, its 95% bootstrap lower bound and the positive support all clear the bars."""
    auc, lower = float(metrics["auc"]), float(metrics["auc_ci95"][0])
    n_pos = int(metrics.get("n_pos", 10**9))
    problems = []
    if auc < min_auc:
        problems.append(f"auc {auc:.3f} < {min_auc}")
    if lower < min_lower:
        problems.append(f"auc_ci95_lower {lower:.3f} < {min_lower}")
    if min_pos is not None and n_pos < min_pos:
        problems.append(f"positives {n_pos} < {min_pos}")
    return {
        "status": "unvalidated" if problems else "validated",
        "reason": "; ".join(problems) if problems else "held-out external human labels clear the preset bars",
        "metrics": {k: metrics[k] for k in ("auc", "auc_ci95", "n_test", "n_pos") if k in metrics},
        "bars": {"min_auc": min_auc, "min_auc_ci95_lower": min_lower, "min_positives": min_pos},
        "label_source": label_source,
        "languages": sorted(VALIDATED_LANGUAGES),
    }


def build_artifact(*, embedding_model: str, snapshot: str, mean, std, edu, problematic, relevance_bands: dict,
                   evaluation: dict) -> dict[str, Any]:
    heads, rel = evaluation["heads"], evaluation["relevance"]
    m = heads["models"]["e5-large"]
    human = heads["label_source"]
    artifact: dict[str, Any] = {
        "version": VERSION,
        "embedding": {"model": embedding_model, "snapshot": snapshot, "doc_chars": DOC_CHARS,
                      "query_prefix": QUERY_PREFIX, "passage_prefix": PASSAGE_PREFIX,
                      "mean": _floats(mean), "std": _floats(std)},
        "heads": {
            "information_value": {
                "kind": "ridge", "scale": "ordinal", "range": [0, 4], "weights": _floats(edu[0]),
                "bias": round(float(edu[1]), 6), "lambda": edu[2], "review_floor": round(float(edu[3]), 6),
                "validation": validation(m["edu"], min_auc=HEAD_MIN_AUC, min_lower=HEAD_MIN_AUC_LOWER,
                                         min_pos=HEAD_MIN_POSITIVES, label_source=human),
            },
            "low_quality_likelihood": {
                "kind": "logistic", "scale": "raw_logit", "weights": _floats(problematic[0]),
                "bias": round(float(problematic[1]), 6), "lambda": problematic[2],
                "review_at": round(float(problematic[3]), 6),
                "validation": validation(m["problematic"], min_auc=HEAD_MIN_AUC, min_lower=HEAD_MIN_AUC_LOWER,
                                         min_pos=HEAD_MIN_POSITIVES, label_source=human),
            },
        },
        "relevance": {
            "low": round(float(relevance_bands["low"]), 6), "high": round(float(relevance_bands["high"]), 6),
            "validation": validation(rel["scores"]["e5-large"], min_auc=REL_MIN_AUC, min_lower=REL_MIN_AUC_LOWER,
                                     min_pos=None, label_source=rel["label_source"] + " (proxy: QA passages)"),
        },
        "training": {"label_source": human, "manifest_sha256": heads["manifest_sha256"],
                     "n_train": heads["n_train"], "n_test": heads["n_test"]},
    }
    artifact["digest"] = _digest(artifact)
    return artifact


def _digest(artifact: dict[str, Any]) -> str:
    body = {k: v for k, v in artifact.items() if k != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


_artifact_cache: dict[str, Any] = {}


def load_artifact() -> dict[str, Any] | None:
    """The committed head artifact, or None when missing or when its digest does not match its content."""
    if "value" not in _artifact_cache:
        try:
            data = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
            _artifact_cache["value"] = data if data.get("digest") == _digest(data) else None
        except (OSError, ValueError):
            _artifact_cache["value"] = None
    return _artifact_cache["value"]


def reset_artifact_cache() -> None:
    _artifact_cache.clear()


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def _std_vec(artifact: dict[str, Any], vec: np.ndarray) -> np.ndarray:
    emb = artifact["embedding"]
    return (vec - np.asarray(emb["mean"])) / np.asarray(emb["std"])


def _unit(vec: list[float]) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float64)
    return v / max(float(np.linalg.norm(v)), 1e-12)


def score_document(artifact: dict[str, Any], goal_vec: list[float], doc_vec: list[float], *, language: str,
                   snapshot_ok: bool) -> dict[str, dict[str, Any]]:
    """Axis results for one document. Every axis carries its own validation state and reason."""
    g, d = _unit(goal_vec), _unit(doc_vec)
    z = _std_vec(artifact, d)
    lang_ok = language in VALIDATED_LANGUAGES

    def state(block: dict[str, Any]) -> tuple[str, str]:
        v = block["validation"]
        if v["status"] != "validated":
            return "unvalidated", f"artifact_unvalidated: {v['reason']}"
        if not snapshot_ok:
            return "unvalidated", "embedding_snapshot_mismatch"
        if not lang_ok:
            return "unvalidated", f"language_not_validated:{language}"
        return "validated", v["reason"]

    out: dict[str, dict[str, Any]] = {}
    rel_block = artifact["relevance"]
    status, reason = state(rel_block)
    cos = float(g @ d)
    out["topic_relevance_semantic"] = {
        "status": "measured", "value": round(cos, 4), "scale": "similarity", "validation": status, "reason": reason,
        "bands": {"low": rel_block["low"], "high": rel_block["high"]},
    }
    for axis, key in (("information_value", "information_value"), ("low_quality_likelihood", "low_quality_likelihood")):
        head = artifact["heads"][key]
        raw = float(z @ np.asarray(head["weights"]) + head["bias"])
        status, reason = state(head)
        entry = {"status": "measured", "scale": head["scale"], "validation": status, "reason": reason}
        if axis == "information_value":
            entry.update(value=round(max(0.0, min(4.0, raw)), 3), raw=round(raw, 3), review_floor=head["review_floor"])
        else:
            entry.update(value=round(raw, 3), probability_uncalibrated=round(_sigmoid(raw), 4),
                         review_at=head["review_at"])
        out[axis] = entry
    out["explanation_quality"] = {
        "status": "unmeasured", "value": None, "scale": "none", "validation": "unvalidated",
        "reason": "no labelled data for explanation quality; only rule-based structure signals exist",
    }
    return out


_child_lock = asyncio.Lock()
# The e5 child peaks near 2.1GB RSS, so it shares the Mac-wide slot file lock that already keeps local Qwen to one
# heavy model at a time (runs.qwen_slot). Acquisition order, outermost first: `_child_lock` (this process), then the
# machine slot via a non-blocking try-lock polled until a deadline. The slot is the only machine lock this path ever
# takes and it is never held while waiting for anything else; the Qwen path never takes `_child_lock`. A bounded
# try-lock cannot hold-and-wait, so no cycle with the Qwen teacher is possible.
GATE_WAIT_SECONDS = 20.0
GATE_POLL_SECONDS = 0.25


class EmbedGateBusy(RuntimeError):
    """Another process (a worker's e5 child or the Qwen teacher) owns the machine slot past the wait budget."""


async def _acquire_gate(wait: float, poll: float) -> int:
    # lazy: the runs package pulls models and profiles
    from ttakkari.runs import qwen_slot

    deadline = time.monotonic() + wait
    while True:
        fd = qwen_slot.acquire_process_lock()
        if fd is not None:
            return fd
        if time.monotonic() >= deadline:
            raise EmbedGateBusy("embedder_gate_busy")
        await asyncio.sleep(poll)


async def default_embed(texts: list[str], prefixes: list[str]) -> tuple[list[list[float]], dict[str, Any]]:
    """The existing disposable e5 child (ttakkari.context.embedder_worker, offline, parent-watched). It is named, not
    imported: the collector package keeps no import dependency on the context/knowledge packages. The child is only
    spawned while this process holds the machine slot; a busy slot raises EmbedGateBusy and no child starts."""
    from ttakkari.config import get_settings
    from ttakkari.model_process import run_json_worker

    s = get_settings()
    async with _child_lock:
        from ttakkari.runs import qwen_slot

        fd = await _acquire_gate(GATE_WAIT_SECONDS, GATE_POLL_SECONDS)
        try:
            result = await run_json_worker(
                module="ttakkari.context.embedder_worker",
                request={"texts": texts, "prefixes": prefixes, "query": False, "model": s.context_model,
                         "cache_dir": str(s.models_dir), "threads": s.context_embedding_cpu_threads},
                timeout=s.context_embedding_timeout_seconds, error_prefix="embedding_worker",
            )
        finally:
            qwen_slot.release_process_lock(fd)
    vectors = result.get("vectors", [])
    if len(vectors) != len(texts) or any(
        len(v) != EMBED_DIM or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v) for v in vectors
    ):
        raise ValueError("invalid_embedding_output")
    return vectors, {"snapshot": result.get("snapshot"), **result.get("metrics", {})}


def _unavailable(status: str, reason: str) -> dict[str, Any]:
    return {"version": VERSION, "status": status, "reason": reason, "axes": {}}


def enabled() -> tuple[bool, str]:
    from ttakkari.config import get_settings

    s = get_settings()
    if not s.collector_triage_ml_enabled:
        return False, "collector_triage_ml_enabled=false"
    if s.context_embedder != "e5":
        return False, f"context_embedder={s.context_embedder}"
    return True, ""


async def evaluate_documents(goal: str, docs: list[dict[str, str]], *, embed: EmbedFn | None = None
                             ) -> list[dict[str, Any]]:
    """Stage-2 signals for a page of documents with one embedding child process. Never raises: on any failure each
    document gets an `unavailable` record and the rule stage alone decides (conservatively)."""
    if not goal.strip():
        return [_unavailable("not_applicable", "no_goal") for _ in docs]
    if embed is None:
        ok, why = enabled()
        if not ok:
            return [_unavailable("disabled", why) for _ in docs]
    artifact = load_artifact()
    if artifact is None:
        return [_unavailable("unavailable", "artifact_missing_or_digest_mismatch") for _ in docs]
    texts = [goal.strip()[:500], *(doc_passage(d["title"], d["text"]) for d in docs)]
    prefixes = [QUERY_PREFIX, *[PASSAGE_PREFIX] * len(docs)]
    started = time.perf_counter()
    try:
        vectors, meta = await (embed or default_embed)(texts, prefixes)
    except EmbedGateBusy:
        # Deferred, not judged: the rule stage decides conservatively and the page can be re-evaluated later.
        return [_unavailable("unavailable", "embedder_gate_busy") for _ in docs]
    except Exception as exc:  # noqa: BLE001 - a model failure is an unavailable axis, never an accept or a crash
        return [_unavailable("unavailable", f"embedder_error:{type(exc).__name__}") for _ in docs]
    wall = time.perf_counter() - started
    snapshot = meta.get("snapshot")
    snapshot_ok = snapshot == artifact["embedding"]["snapshot"]
    results = []
    for doc, vec in zip(docs, vectors[1:], strict=True):
        results.append({
            "version": VERSION, "status": "evaluated", "reason": "",
            "artifact_digest": artifact["digest"],
            "embedding": {"model": artifact["embedding"]["model"], "snapshot": snapshot, "snapshot_ok": snapshot_ok},
            "axes": score_document(artifact, vectors[0], vec, language=doc.get("language", "und"),
                                   snapshot_ok=snapshot_ok),
            "cost": {"batch_docs": len(docs), "batch_wall_ms": round(wall * 1000, 1),
                     "per_doc_ms": round(wall * 1000 / len(docs), 1), "child_metrics": {
                         k: round(float(v), 3) for k, v in meta.items()
                         if k in ("load_seconds", "inference_seconds", "peak_rss_bytes") and isinstance(v, (int, float))}},
        })
    return results
