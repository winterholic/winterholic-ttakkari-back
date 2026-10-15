"""STT request adapter for a disposable model process."""
from __future__ import annotations

from pathlib import Path

from ttakkari.model_process import run_json_worker


async def run_worker(audio_path: Path, params: dict, settings) -> dict:
    request = {
        **params,
        "audio_path": str(audio_path),
        "model_path": str(settings.local_stt_model_path),
        "max_audio_seconds": settings.local_stt_max_audio_seconds,
        "cpu_threads": settings.local_stt_cpu_threads,
    }
    for field in (
        "diarization_model_path",
        "diarization_device",
        "separation_model_path",
    ):
        value = getattr(settings, "local_stt_" + field, None)
        if value is not None:
            request[field] = str(value)
    return await run_json_worker(
        module="ttakkari.meetings.local_stt.worker", request=request,
        python=settings.local_stt_python, timeout=settings.local_stt_timeout_seconds,
        error_prefix="local_stt_worker",
    )
