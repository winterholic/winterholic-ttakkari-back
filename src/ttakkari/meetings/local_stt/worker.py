"""One request per process. No app Settings, database, tokens, or network downloads.

Usage: python -m ttakkari.meetings.local_stt.worker OUTPUT_JSON < REQUEST_JSON
The supervisor must wait for process exit on success, timeout, cancellation, and error.
"""

from __future__ import annotations

import dataclasses
import gc
import hashlib
import importlib.metadata
import json
import os
import resource
import sys
import threading
import time
from pathlib import Path

from ttakkari.meetings.local_stt.core import normalize_segments, term_prompt


def decode_options(profile: str, terms: list[str]) -> dict:
    if profile not in ("baseline", "improved"):
        raise ValueError("Invalid decoding profile")
    return {
        "language": "ko",
        "task": "transcribe",
        "beam_size": 5,
        "temperature": 0,
        "word_timestamps": True,
        "vad_filter": profile == "improved",
        "condition_on_previous_text": profile == "baseline",
        "initial_prompt": term_prompt(terms) or None if profile == "improved" else None,
        "vad_parameters": {"min_silence_duration_ms": 500, "speech_pad_ms": 300}
        if profile == "improved"
        else None,
    }


def transcribe(request: dict) -> dict:
    started = time.perf_counter()
    # The parent supplies an isolated cache and prohibits credential discovery.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import av
    import ctranslate2
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    imports_done = time.perf_counter()
    path = Path(request["audio_path"])
    model_path = Path(request["model_path"])
    if not path.is_file() or not (model_path / "model.bin").is_file():
        raise ValueError("Audio or local model missing")
    limit = float(request.get("max_audio_seconds", 7200))
    with av.open(str(path)) as container:
        if container.duration and container.duration / av.time_base > limit + 1:
            raise ValueError("Audio exceeds duration limit")
    audio = decode_audio(str(path), sampling_rate=16000)
    duration = len(audio) / 16000
    if duration <= 0 or duration > limit:
        raise ValueError("Invalid audio duration")
    start = float(request.get("start", 0))
    end = float(request.get("end", duration))
    if not 0 <= start < end <= duration + 0.02:
        raise ValueError("Invalid reprocessing interval")
    audio = audio[round(start * 16000) : round(min(end, duration) * 16000)]
    decoded = time.perf_counter()
    options = decode_options(
        request.get("profile", "improved"), request.get("terms", [])
    )
    model = WhisperModel(
        str(model_path),
        device="cpu",
        compute_type="int8",
        cpu_threads=int(request.get("cpu_threads", 4)),
        num_workers=1,
        local_files_only=True,
    )
    loaded = time.perf_counter()
    iterator, info = model.transcribe(audio, **options)
    raw = [dataclasses.asdict(segment) for segment in iterator]
    inferred = time.perf_counter()
    segments = normalize_segments(raw, duration=len(audio) / 16000, offset=start)
    del iterator, model, audio
    gc.collect()
    released = time.perf_counter()
    with path.open("rb") as source:
        audio_sha = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "schema_version": 1,
        "engine": "faster-whisper",
        "engine_version": importlib.metadata.version("faster-whisper"),
        "runtime_version": ctranslate2.__version__,
        "model": "large-v3",
        "model_version": model_path.name,
        "compute_type": "int8",
        "device": "cpu",
        "options": options,
        "audio_sha256": audio_sha,
        "duration": duration,
        "start": start,
        "end": min(end, duration),
        "text": "".join(s["text"] for s in raw).strip(),
        "segments": segments,
        "raw_segments": raw,
        "language": info.language,
        "duration_after_vad": info.duration_after_vad,
        "diarization_status": "not_run",
        "overlap_detection_status": "not_run",
        "metrics": {
            "import_seconds": imports_done - started,
            "audio_decode_seconds": decoded - imports_done,
            "load_seconds": loaded - decoded,
            "inference_seconds": inferred - loaded,
            "release_request_seconds": released - inferred,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "rtf_inference": (inferred - loaded) / (end - start),
        },
    }


def watch_parent(parent_pid: int, stop: threading.Event) -> None:
    """A backend crash must not leave an orphan inference process holding the model."""
    while not stop.wait(1):
        if os.getppid() != parent_pid or parent_pid == 1:
            os._exit(74)
            return  # reachable only in tests replacing the process exit primitive


def main() -> int:
    stop = threading.Event()
    guard = threading.Thread(
        target=watch_parent, args=(os.getppid(), stop), daemon=True
    )
    guard.start()
    try:
        result = transcribe(json.load(sys.stdin))
        Path(sys.argv[1]).write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 -- process boundary: nonzero exit is consumed by supervisor
        # No unbounded traceback containing local paths or third-party model output.
        print(f"local_stt_worker_failed:{type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
