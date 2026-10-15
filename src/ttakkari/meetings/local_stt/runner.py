"""Async supervisor. Model lifetime ends at child exit, even on timeout/cancellation."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path


async def run_worker(audio_path: Path, params: dict, settings) -> dict:
    cache = settings.home / "models" / "local-stt"
    cache.mkdir(parents=True, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update(
        {
            "HF_HOME": str(cache),
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    request = {
        **params,
        "audio_path": str(audio_path),
        "model_path": str(settings.local_stt_model_path),
        "max_audio_seconds": settings.local_stt_max_audio_seconds,
        "cpu_threads": settings.local_stt_cpu_threads,
    }
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ttakkari-stt-") as temp:
        output = Path(temp) / "result.json"
        # stderr is a small fixed error code from worker.main; keep it off API output.
        process = await asyncio.create_subprocess_exec(
            str(settings.local_stt_python or sys.executable),
            "-m",
            "ttakkari.meetings.local_stt.worker",
            str(output),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(
                process.communicate(json.dumps(request).encode()),
                timeout=settings.local_stt_timeout_seconds,
            )
            if process.returncode != 0 or not output.is_file():
                raise RuntimeError("local_stt_worker_failed")
            if output.stat().st_size > 64 * 1024 * 1024:
                raise RuntimeError("local_stt_output_too_large")
            result = json.loads(output.read_text(encoding="utf-8"))
            result.setdefault("metrics", {}).update(
                {"process_exited": True, "wall_seconds": time.perf_counter() - started}
            )
            return result
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
