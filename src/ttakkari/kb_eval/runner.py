"""Offline fixture runner and crash-resumable cache. No retrieval algorithm."""

import json
import platform
from pathlib import Path

import numpy
import psutil
import pydantic

from .integrity import anchor, digest
from .metrics import metrics
from .rights import validate_rights
from .state import artifact_lock, atomic_json, cache_key, canonical, verify_replay

EXACT_REPLAY_EXCLUDES = ["resources", "timers", "wall_clock", "checkpoint"]


def resources(out, *, gpu_collector=None):
    def measure(fn, unit):
        try:
            value = fn()
            if value is None:
                raise NotImplementedError("collector returned no measurement")
            return {"status": "measured", "value": value, "unit": unit, "reason": None}
        except (OSError, NotImplementedError, psutil.Error) as exc:
            return {
                "status": "unverified",
                "value": None,
                "unit": unit,
                "reason": type(exc).__name__ + ": " + str(exc),
            }

    proc = psutil.Process()
    result = {
        "rss_bytes": measure(lambda: proc.memory_info().rss, "bytes"),
        "cpu_seconds": measure(
            lambda: proc.cpu_times().user + proc.cpu_times().system, "seconds"
        ),
        "artifact_disk_bytes": measure(
            lambda: sum(p.stat().st_size for p in Path(out).rglob("*") if p.is_file()),
            "bytes",
        ),
        "scope": "harness_process_and_run_artifacts_only",
    }
    for name in ("gpu_bytes", "index_disk_bytes", "swap_in_bytes_per_second"):
        result[name] = {
            "status": "unverified",
            "value": None,
            "unit": "bytes" if name != "swap_in_bytes_per_second" else "bytes/second",
            "reason": "unsupported: no collector configured for this surface",
        }
    if gpu_collector is not None:
        result["gpu_bytes"] = measure(gpu_collector, "bytes")
    return result


def _runtime():
    here = Path(__file__).parent
    return {
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "pydantic": pydantic.__version__,
        "harness_source_sha256": digest(
            b"".join(
                p.name.encode() + b"\0" + p.read_bytes()
                for p in sorted(here.glob("*.py"))
            )
        ),
    }


def _run(bundle, out, *, limit=None):
    out = Path(out)
    if limit is not None and limit < 1:
        raise ValueError("limit_must_be_positive")
    # Holdout never enters developer runner. A future authenticated reviewer route
    # must consume the one-use audit before making its data visible.
    if any(c["split"] == "holdout" for c in bundle["cases"]):
        raise ValueError("holdout_access_denied")
    manifest = {
        "identity": bundle["identity"],
        "input_sha256": bundle["input_sha256"],
        "runtime": _runtime(),
        "declared_manifest": bundle["manifest"],
        "policy": bundle["policy"],
        "exposure": bundle["exposure"],
        "exact_replay_excludes": EXACT_REPLAY_EXCLUDES,
        "adapter": "fixed_prediction_fixture",
    }
    out.mkdir(parents=True, exist_ok=True)
    mf = out / "manifest.json"
    if mf.exists() and canonical(json.loads(mf.read_bytes())) != canonical(manifest):
        raise ValueError("resume_manifest_mismatch")
    atomic_json(mf, manifest)
    cache = out / "cache"
    cache.mkdir(exist_ok=True)
    completed, used, rows, predictions = [], 0, [], {}
    sources = {s["doc_key"]: s for s in bundle["sources"]}
    for c in bundle["cases"]:
        record_path = cache / (c["id"] + ".json")
        key_identity = {**bundle["identity"], "runtime": manifest["runtime"]}
        key = cache_key(key_identity, canonical(c))
        payload = bundle["predictions"].get(c["id"])
        if payload is None:
            payload = {
                "ranked": [],
                "candidates": [],
                "coverage": "unknown",
                "answer_correct": None,
                "score_bytes": [],
                "failure": "missing_prediction",
            }
        if record_path.exists():
            record = json.loads(record_path.read_bytes())
            verify_replay(record, key, payload)
        else:
            if limit is not None and used >= limit:
                break
            record = {
                "key": key,
                "payload": payload,
                "payload_sha256": digest(canonical(payload)),
            }
            atomic_json(record_path, record)
            used += 1
        record = json.loads(record_path.read_bytes())
        verify_replay(record, key, payload)
        predictions[c["id"]] = record["payload"]
        checked = {**c, "group": bundle["components"][c["id"]], "anchor_checks": []}
        for a in c["anchors"]:
            checked["anchor_checks"].append(
                anchor(
                    a, sources[a["doc_key"]], passages=sources[a["doc_key"]]["passages"]
                )
            )
        bad = [a for a in checked["anchor_checks"] if a["status"] != "valid"]
        if bad:
            checked["anchor_status"] = bad[0]["status"]
        elif any(not a["passage_sets"] for a in checked["anchor_checks"]):
            payload = {**payload, "failure": "not_in_corpus"}
            predictions[c["id"]] = payload
        rows.append(checked)
        completed.append(c["id"])
        # A crash before this write rechecks the durable cache on resume.
        atomic_json(
            out / "checkpoint.json",
            {"manifest_sha256": digest(canonical(manifest)), "completed": completed},
        )
    interrupted = len(completed) < len(bundle["cases"])
    blockers = [
        "independent_harness_review_pending",
        "actual_database_manifest_unverified",
        "mobile_browser_unverified",
        "live_inference_unexecuted",
        "human_holdout_E1b_pending",
        "power_not_established",
        "no_product_search_adapter",
    ]
    # NOTE: non-fixture kinds are held by validate_rights before this point.
    # Reintroduce a tested public_regression_only restriction with the
    # authenticated rights review flow; an unreachable branch hides its absence.
    result = {
        "schema_version": "kb-e0-report-v1",
        "state": "interrupted" if interrupted else "complete_fixture",
        "completed": len(completed),
        "accounting": {
            "total": len(bundle["cases"]),
            "completed": len(completed),
            "pending": len(bundle["cases"]) - len(completed),
        },
        "adapter": "fixed_prediction_fixture",
        "product_search": "unverified",
        "rights_authorization": bundle["rights_authorization"],
        "final_quality": "not_authorized",
        "blockers": blockers,
        "metrics": metrics(rows, predictions) if rows else None,
        "anchors": [{"id": r["id"], "checks": r["anchor_checks"]} for r in rows],
        "execution": {
            "inference": "unexecuted",
            "actual_database": "unverified",
            "browser": "unverified",
            "observations": bundle["observations"]["kind"],
        },
        "quality_gate": {
            "status": "inconclusive",
            "reasons": ["power_not_established", "fixture_only"],
        },
        "latency": {
            "status": "unverified",
            "reason": "fixture replay is not a search latency measurement",
        },
        "resources": resources(out),
    }
    # Recompute metrics from frozen outputs rather than trusting cached metrics.
    if result["metrics"] and result["metrics"]["invalid"]:
        result["quality_gate"] = {
            "status": "input_invalid",
            "reasons": ["invalid_required_gold"],
        }
    deterministic = {k: result[k] for k in ("metrics", "anchors", "accounting")}
    replay_path = out / "deterministic.json"
    if replay_path.exists() and not interrupted:
        old = json.loads(replay_path.read_bytes())
        verify_replay(old, digest(canonical(manifest)), deterministic)
    result["replay"] = {
        "status": "pending" if interrupted else "exact_match",
        "metrics": "pending"
        if interrupted
        else "exact_match"
        if replay_path.exists()
        else "baseline_recorded",
        "compared": "candidate ids/order/score bytes, metrics and anchors",
        "live_inference": "unexecuted",
    }
    if not interrupted:
        atomic_json(
            replay_path,
            {
                "key": digest(canonical(manifest)),
                "payload": deterministic,
                "payload_sha256": digest(canonical(deterministic)),
            },
        )
    atomic_json(out / "report.json", result)
    return result


def run(bundle, out, *, limit=None):
    bundle = {**bundle, "rights_authorization": validate_rights(bundle)}
    with artifact_lock(Path(out) / ".run.lock", blocking=False):
        return _run(bundle, out, limit=limit)
