"""Audio-layer repair of a finished result: re-mix the audio track only, stream-copy the picture.

Executable for `audio_level` and `audio_seam` defects. No producer, model or GPU is involved, so the
production flock held by the caller is the only lease needed. The video stream is copied bit for bit;
that is checked by hashing the copied packets before and after, and a mismatch aborts the repair.
Scenes without an audio defect keep their level (measured before/after, not assumed).
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ttakkari.creative.render import ffmpeg
from ttakkari.creative.v2 import audio_measure, engines, quality

MAX_SEAM_GAIN_DB = 15.0
MAX_LEVEL_GAIN_DB = 30.0
SEAM_PAD_SECONDS = 1.5
SEAM_TOLERANCE_DB = 1.0
RAMP_SECONDS = 0.1
PEAK_LIMIT = 0.85  # linear, about -1.4 dBFS, leaves room for AAC overshoot
AUDIO_CODES = {"audio_level", "audio_seam"}


def _hop_gain(db: np.ndarray, length: int, sample_rate: int) -> np.ndarray:
    """Interpolate a per-hop dB curve (20 ms hops) to per-sample linear gain."""
    centers = (np.arange(len(db)) + 0.5) * 0.02 * sample_rate
    return 10 ** (np.interp(np.arange(length), centers, db) / 20)


def compensate_seams(samples: np.ndarray, dips: list[dict[str, float]], sample_rate: int = audio_measure.SAMPLE_RATE) -> np.ndarray:
    """Lift each level dip back toward the track's own median with a smooth gain curve, capped in dB."""
    if not dips:
        return samples
    env = audio_measure.envelope_db(samples, sample_rate)
    curve = np.zeros(len(env))
    for dip in dips:
        lo = max(0, int((dip["start"] - SEAM_PAD_SECONDS) / 0.02))
        hi = min(len(env), int((dip["end"] + SEAM_PAD_SECONDS) / 0.02))
        deficit = dip["reference_db"] - env[lo:hi]
        curve[lo:hi] = np.maximum(curve[lo:hi], np.clip(deficit - SEAM_TOLERANCE_DB, 0.0, MAX_SEAM_GAIN_DB))
    gain = _hop_gain(curve, len(samples), sample_rate).astype(np.float32)
    return samples * gain[:, None]


def apply_level(samples: np.ndarray, windows: list[dict[str, float]], target_lufs: float, sample_rate: int = audio_measure.SAMPLE_RATE) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Static gain per merged window to reach target loudness; neighbouring windows are untouched."""
    merged: list[list[float]] = []
    for window in sorted(windows, key=lambda w: w["start"]):
        if merged and window["start"] <= merged[-1][1] + 1e-3:
            merged[-1][1] = max(merged[-1][1], window["end"])
        else:
            merged.append([window["start"], window["end"]])
    out = samples.copy()
    applied = []
    ramp = int(RAMP_SECONDS * sample_rate)
    for start, end in merged:
        a, b = int(start * sample_rate), min(len(samples), int(end * sample_rate))
        measured = audio_measure.loudness(samples[a:b], sample_rate)
        if measured is None or not np.isfinite(measured["lufs"]):
            applied.append({"start": start, "end": end, "gain_db": 0.0, "skipped": "not measurable"})
            continue
        gain_db = float(np.clip(target_lufs - measured["lufs"], -12.0, MAX_LEVEL_GAIN_DB))
        curve = np.full(b - a, 10 ** (gain_db / 20), dtype=np.float32)
        edge = min(ramp, (b - a) // 2)
        if edge:
            shape = np.linspace(0.0, 1.0, edge, dtype=np.float32)
            curve[:edge] = 1 + (curve[:edge] - 1) * shape
            curve[-edge:] = 1 + (curve[-edge:] - 1) * shape[::-1]
        out[a:b] *= curve[:, None]
        applied.append({"start": start, "end": end, "gain_db": round(gain_db, 2), "before_lufs": measured["lufs"]})
    return out, applied


def video_md5(path: Path) -> str:
    """Hash of the video packets exactly as stored (stream copy), independent of the audio track."""
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if result.returncode or not result.stdout.startswith("MD5="):
        raise RuntimeError(f"video stream hash failed: {result.stderr[-300:]}")
    return result.stdout.strip().split("=", 1)[1]


def remix(source: Path, output: Path, *, level_windows: list[dict[str, float]], dips: list[dict[str, float]],
          scenes: list[dict[str, Any]], target_lufs: float = audio_measure.TARGET_LUFS) -> dict[str, Any]:
    """Write `output` = source picture (copied) + repaired audio. Returns before/after measurements."""
    sample_rate = audio_measure.SAMPLE_RATE
    original = audio_measure.decode_pcm(source, sample_rate=sample_rate)
    work = compensate_seams(original, dips, sample_rate)
    work, gains = apply_level(work, level_windows, target_lufs, sample_rate)
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
         "-f", "f32le", "-ar", str(sample_rate), "-ac", str(audio_measure.CHANNELS), "-i", "pipe:0",
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-af", f"alimiter=limit={PEAK_LIMIT}:attack=5:release=50:level=disabled",
         "-c:a", "aac", "-b:a", "192k", "-ar", str(sample_rate), "-movflags", "+faststart", str(output)],
        input=np.ascontiguousarray(work, dtype="<f4").tobytes(), capture_output=True, timeout=600, check=False,
    )
    if result.returncode:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"audio remux failed ({result.returncode}): {result.stderr[-400:].decode(errors='replace')}")
    before_video, after_video = video_md5(source), video_md5(output)
    if before_video != after_video:
        output.unlink(missing_ok=True)
        raise RuntimeError("video stream changed during audio repair; refusing to keep the output")
    repaired = audio_measure.decode_pcm(output, sample_rate=sample_rate)
    return {
        "target_lufs": target_lufs,
        "gains": gains,
        "video_stream_md5": {"before": before_video, "after": after_video, "preserved": True},
        "scenes": {
            sid: {"before": audio_measure.scene_levels(original, [s], sample_rate)[sid],
                  "after": audio_measure.scene_levels(repaired, [s], sample_rate)[sid]}
            for s in scenes for sid in [s["scene_id"]]
        },
        "dips_before": audio_measure.find_dips(original, sample_rate),
        "dips_after": audio_measure.find_dips(repaired, sample_rate),
    }


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def apply(plans, store, plan: dict[str, Any], record: dict[str, Any], data: dict[str, Any], proposal: dict[str, Any],
          final: dict[str, Any], source: Path) -> dict[str, Any]:
    """Run inside the caller's production transaction. Replaces record['final'] with the repaired file.

    The previous final stays on disk and in `final_history`; reviews of it no longer attest the new output.
    """
    from ttakkari.creative.v2 import dispatch
    from ttakkari.creative.v2.production_review import attested_receipts

    actions = proposal["actions"]
    if any(a["code"] not in AUDIO_CODES or not (a.get("window") or {}).get("end") for a in actions):
        raise dispatch.ProductionError("audio repair needs a measured window for every action")
    level_windows = [a["window"] for a in actions if a["code"] == "audio_level"]
    dips = [a["window"] for a in actions if a["code"] == "audio_seam"]
    scenes = [{"scene_id": s["scene_id"], "start_time": s["start_time"], "end_time": s["end_time"]} for s in plan["scenes"]]
    revision_dir = store.folder(plan["id"]) / "media" / f"r{plan['revision']}"
    revision_dir.mkdir(parents=True, exist_ok=True)
    partial = revision_dir / f"{uuid.uuid4()}.partial.mp4"
    report = remix(source, partial, level_windows=level_windows, dips=dips, scenes=scenes)
    sha = _sha256(partial)
    path = revision_dir / f"final-{sha[:16]}.mp4"
    partial.replace(path)
    tolerance = dispatch.QA_BASE_TOLERANCE + dispatch.QA_PER_SEGMENT_TOLERANCE * len(record["segments"])
    qa = engines.qa_media(path, aspect=plan["aspect"], planned_seconds=final["planned_seconds"], tolerance=tolerance)
    now = datetime.now(UTC).isoformat()
    record.setdefault("final_history", []).append(
        {**{k: final.get(k) for k in ("id", "file", "sha256", "size", "status", "assembled_at")}, "superseded_by": sha, "reason": "audio_remix"}
    )
    repair_id = str(uuid.uuid4())
    record["final"] = {
        **{k: v for k, v in final.items() if k not in {"id", "file", "sha256", "size", "status", "qa", "assembled_at", "duration"}},
        "status": "qa_passed" if qa["passed"] else "qa_failed",
        "id": str(uuid.UUID(sha[:32])),
        "file": str(path.relative_to(store.folder(plan["id"]))),
        "sha256": sha,
        "size": path.stat().st_size,
        "qa": qa,
        "duration": {**final.get("duration", {}), "measured_seconds": qa["probe"]["duration"]},
        "derived_from": {"sha256": final["sha256"], "repair_id": repair_id, "kind": "audio_remix"},
        "assembled_at": now,
    }
    record["review_execution"] = {
        "status": "needs_review", "verdict": "unknown",
        "reason": f"결과 파일이 오디오 수리로 바뀌었다({final['sha256'][:12]} → {sha[:12]}). 새 파일을 다시 검수해야 한다",
    }
    record["quality_report"] = quality.evaluate_production(
        plan, {**record, "assets": data["assets"], "quality_evidence": attested_receipts(record)}, media_path=path
    )
    record["repair_plan"] = record["quality_report"]["repair_plan"]
    application = {
        "id": repair_id, "kind": "audio_remix", "status": "applied_rendered",
        "source_revision": plan["revision"], "revision": plan["revision"],
        "proposal_sha256": proposal["proposal_sha256"],
        "source_output_sha256": final["sha256"], "output_sha256": sha,
        "changes": [{"scene_id": a["scene_id"], "layer": a["layer"], "code": a["code"], "strategy": a["strategy"], "window": a["window"]} for a in actions],
        "preserve": deepcopy(proposal["preserve"]), "report": report, "at": now,
        "quality_state": "unknown_until_new_review",
    }
    data.setdefault("repairs", []).append(application)
    dispatch._log(record, "audio_repair_applied", application_id=repair_id, output_sha256=sha)
    return application
