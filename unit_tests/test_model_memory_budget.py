"""RSS guard tests use only owned, model-free child processes."""
from __future__ import annotations

import json

import psutil
import pytest

from ttakkari import model_process


def worker(tmp_path, *, allocate_mb=0):
    executable = tmp_path / f"worker-{allocate_mb}"
    marker = tmp_path / f"input-{allocate_mb}"
    executable.write_text(
        "#!/usr/bin/env python3\nimport sys,json,ctypes\nfrom pathlib import Path\n"
        "sys.stdin.read()\n"
        f"Path({str(marker)!r}).write_text('received')\n"
        f"payload=bytearray({allocate_mb}*1024*1024)\n"
        + ("ctypes.PyDLL(None).sleep(5)\n" if allocate_mb else "")
        + "Path(sys.argv[-1]).write_text(json.dumps({'actual_output':True}))\n"
    )
    executable.chmod(0o755)
    return executable, marker


async def test_real_rss_growth_stops_owned_worker_and_reports_measurement(tmp_path):
    executable, marker = worker(tmp_path, allocate_mb=96)
    observations = []

    async def observe(event):
        observations.append(event)

    with (model_process.worker_memory_limit(64) as guard, model_process.worker_observations(observe),
          pytest.raises(model_process.WorkerMemoryLimitError, match="memory_budget_exceeded")):
        await model_process.run_json_worker(module="unused", request={}, timeout=5, python=executable)
    assert marker.exists()
    assert guard.snapshot()["sampled_peak_rss_bytes"] > 64 * 1048576
    assert guard.snapshot()["error_code"] == "memory_budget_exceeded"
    assert observations[-1]["state"] == "exited"
    assert observations[-1]["returncode"] != 0


@pytest.mark.parametrize("refusal", [False, True])
async def test_unobservable_or_over_limit_child_receives_no_request(tmp_path, monkeypatch, refusal):
    executable, marker = worker(tmp_path)
    if refusal:
        def denied(pid):
            raise psutil.AccessDenied(pid)
        monkeypatch.setattr(model_process, "_read_worker_rss", denied, raising=False)
    else:
        # Initial exec RSS can be below 1 MiB; inject an already over-limit observation.
        monkeypatch.setattr(model_process, "_read_worker_rss", lambda pid: 2 * 1048576)
    with (model_process.worker_memory_limit(128 if refusal else 1) as guard,
          pytest.raises(model_process.WorkerMemoryLimitError)):
        await model_process.run_json_worker(module="unused", request={}, timeout=5, python=executable)
    assert not marker.exists()
    assert guard.snapshot()["error_code"] == ("memory_probe_unavailable" if refusal else "memory_budget_exceeded")


async def test_below_threshold_output_and_sampled_scope_are_preserved(tmp_path):
    executable, _ = worker(tmp_path)
    with model_process.worker_memory_limit(128) as guard:
        result = await model_process.run_json_worker(module="unused", request={}, timeout=5, python=executable)
    assert result["actual_output"] is True
    assert result["metrics"]["process_exited"] is True
    snapshot = guard.snapshot()
    assert snapshot["scope"] == "each_owned_model_child_rss"
    assert snapshot["samples"] >= 1
    assert 0 < snapshot["sampled_peak_rss_bytes"] < 128 * 1048576
    assert snapshot["error_code"] is None
    assert snapshot["hard_limit"] is False
    print(json.dumps(snapshot, sort_keys=True))


async def test_latched_failure_does_not_spawn_another_child(tmp_path, monkeypatch):
    executable, _ = worker(tmp_path)
    with model_process.worker_memory_limit(128) as guard:
        guard.error_code = "memory_budget_exceeded"
        spawned = []
        original = model_process.asyncio.create_subprocess_exec
        async def spawn(*args, **kwargs):
            spawned.append(True)
            return await original(*args, **kwargs)
        monkeypatch.setattr(model_process.asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(model_process.WorkerMemoryLimitError):
            await model_process.run_json_worker(module="unused", request={}, timeout=5, python=executable)
    assert spawned == []


async def test_worker_exit_between_poll_and_rss_read_keeps_valid_output(monkeypatch):
    import asyncio
    release = asyncio.Event()
    class ExitingChild:
        pid = 123
        returncode = None
        async def communicate(self, payload):
            await release.wait()
            self.returncode = 0
    reads = []
    def read(pid):
        reads.append(pid)
        if len(reads) == 1:
            return 2 * 1048576
        asyncio.get_running_loop().call_soon(release.set)
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(model_process, "_read_worker_rss", read)
    guard = model_process.WorkerMemoryGuard(128)
    await asyncio.wait_for(model_process._communicate_with_memory(ExitingChild(), b'{}', guard), 1)
    assert guard.error_code is None
    assert guard.samples == 1


def test_parent_guard_import_stays_free_of_optional_runtime_dependencies():
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {**os.environ, "PYTHONPATH": str(Path(model_process.__file__).parents[1])}
    result = subprocess.run([sys.executable, "-S", "-c", "from ttakkari.model_process import start_parent_guard"],
                            env=env, capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
