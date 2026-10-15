"""Bounded overlap-only separation orchestration; no unverified replacements."""

from __future__ import annotations

import logging

from .core import compare_overlap_candidate

log = logging.getLogger(__name__)


def reprocess_overlaps(
    regions: list[dict],
    *,
    separate,
    recognize,
    original,
    references: list[dict] | None = None,
    max_region_seconds: float = 15,
    max_total_seconds: float = 120,
) -> list[dict]:
    used, out = 0.0, []
    for region in regions:
        a, b = region["start"], region["end"]
        row = dict(region)
        if len(region["speaker_ids"]) != 2:
            row["status"] = "unsupported_speaker_count"
        elif b - a > max_region_seconds or used + b - a > max_total_seconds:
            row["status"] = "duration_budget_exceeded"
        else:
            used += b - a  # failed work also consumes budget
            try:
                sources = separate(a, b)
                if len(sources) != 2:
                    raise ValueError("Separator source count mismatch")
                tracks = [recognize(source, a) for source in sources]
                baseline = original(a, b)
                ref = next(
                    (
                        r["texts"]
                        for r in (references or [])
                        if r["start"] == a
                        and r["end"] == b
                        and r.get("human_verified") is True
                    ),
                    None,
                )
                decision = compare_overlap_candidate(
                    baseline, [t["text"] for t in tracks], references=ref
                )
                row.update(
                    status="succeeded",
                    original_text=baseline,
                    tracks=tracks,
                    decision=decision,
                    selected_tracks=tracks if decision["adopt"] else None,
                    source_identity_status="unmapped_region_local",
                )
            except Exception as exc:  # noqa: BLE001 -- preserve optional failure as explicit evidence
                log.warning("Overlap separation failed: %s", type(exc).__name__)
                row.update(status="failed", failure_type=type(exc).__name__)
        out.append(row)
    return out


def enhance_result(
    result: dict, *, audio, offset: float, request: dict, recognize
) -> dict:
    """Optional failures are explicit metadata; the original STT still survives."""
    import time
    from pathlib import Path

    from .core import annotate_diarization
    from .diarization import diarize
    from .separation import load_separator

    out = {
        **result,
        "diarization_status": "not_run",
        "overlap_detection_status": "not_run",
        "separation_status": "not_run",
        "identity_status": "not_run",
        "speaker_identities": {},
    }
    model = request.get("diarization_model_path")
    if not model:
        return out
    if not (Path(model) / "config.yaml").is_file():
        out["diarization_status"] = "unavailable"
        out["diarization_reason"] = "local_model_missing"
        return out
    try:
        evidence = diarize(
            audio,
            model_path=Path(model),
            offset=offset,
            device=request.get("diarization_device", "cpu"),
            cpu_threads=int(request.get("cpu_threads", 4)),
        )
        out.update(
            annotate_diarization(
                result["segments"], evidence["turns"], duration=result["duration"]
            )
        )
        out.update(
            diarization=evidence,
            diarization_status="succeeded",
            overlap_detection_status="succeeded",
        )
    except Exception as exc:  # noqa: BLE001 -- optional stage boundary, persisted failure status
        log.warning("Diarization failed: %s", type(exc).__name__)
        out.update(
            diarization_status="failed", diarization_failure_type=type(exc).__name__
        )
        return out
    separation_path = request.get("separation_model_path")
    if not separation_path:
        out["separation_status"] = "not_configured"
        return out
    if not out["overlap_regions"]:
        out["separation_status"] = "no_overlap"
        return out
    try:
        started = time.perf_counter()
        separator = load_separator(
            Path(separation_path),
            device=request.get("separation_device", "cpu"),
            cpu_threads=int(request.get("cpu_threads", 4)),
        )
        out["separation_load_seconds"] = time.perf_counter() - started

        def clip(a, b):
            return audio[round((a - offset) * 16000) : round((b - offset) * 16000)]

        out["overlap_candidates"] = reprocess_overlaps(
            out["overlap_regions"],
            separate=lambda a, b: separator(clip(a, b)),
            recognize=recognize,
            original=lambda a, b: recognize(clip(a, b), a)["text"],
            references=request.get("overlap_references"),
        )
        statuses = {r["status"] for r in out["overlap_candidates"]}
        out["separation_status"] = (
            "partial_failure"
            if "failed" in statuses
            else "succeeded"
            if "succeeded" in statuses
            else "skipped"
        )
        out["separation_seconds"] = time.perf_counter() - started
        out["separation_model"] = "speechbrain/sepformer-whamr"
        out["separation_model_version"] = Path(separation_path).name
    except Exception as exc:  # noqa: BLE001 -- optional stage boundary, persisted failure status
        log.warning("Separation initialization failed: %s", type(exc).__name__)
        out.update(
            separation_status="failed", separation_failure_type=type(exc).__name__
        )
    return out
