"""Disposable model worker supervision. No ML imports or inherited credentials."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


async def run_json_worker(
    *,
    module: str,
    request: dict,
    timeout: float,
    python: Path | None = None,
    error_prefix: str = "model_worker",
) -> dict:
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYANNOTE_METRICS_ENABLED": "0",
            "DO_NOT_TRACK": "1",
            # The optional runtime contains dependencies, while workers use this release code.
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        }
    )
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ttakkari-model-") as temp:
        env.update(
            {
                "HOME": temp,
                "HF_HOME": str(Path(temp) / "hf"),
                "MPLCONFIGDIR": str(Path(temp) / "mpl"),
                "XDG_CACHE_HOME": str(Path(temp) / "cache"),
                "TORCH_HOME": str(Path(temp) / "torch"),
            }
        )
        output = Path(temp) / "result.json"
        # stderr is a small fixed error code from worker.main; keep it off API output.
        process = await asyncio.create_subprocess_exec(
            str(python or sys.executable),
            "-m",
            module,
            str(output),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(
                process.communicate(json.dumps(request).encode()),
                timeout=timeout,
            )
            if process.returncode != 0 or not output.is_file():
                raise RuntimeError(f"{error_prefix}_failed")
            if output.stat().st_size > 64 * 1024 * 1024:
                raise RuntimeError(f"{error_prefix}_output_too_large")
            result = json.loads(output.read_text(encoding="utf-8"))
            result.setdefault("metrics", {}).update(
                {
                    "process_exited": True,
                    "worker_pid": process.pid,
                    "wall_seconds": time.perf_counter() - started,
                }
            )
            return result
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


def watch_parent(parent_pid: int, stop: threading.Event) -> None:
    while not stop.wait(1):
        if os.getppid() != parent_pid or parent_pid == 1:
            os._exit(74)
            return
