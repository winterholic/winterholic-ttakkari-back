"""Native GIL-held owner-death check. Only test-created processes are used."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from ttakkari import model_process


async def wait_file(path, timeout=5):
    async with asyncio.timeout(timeout):
        while not path.exists():
            await asyncio.sleep(.01)


@pytest.mark.parametrize("module,operation", [
    ("ttakkari.tts.worker_supertonic", "synthesize"),
    ("ttakkari.meetings.local_stt.worker", "transcribe"),
])
async def test_owner_death_stops_worker_even_during_gil_held_native_call(tmp_path, module, operation):
    ready, finished = tmp_path / "ready", tmp_path / "finished"
    executable = tmp_path / "native-worker"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import ctypes,importlib,json,os,sys\nfrom pathlib import Path\n"
        f"worker=importlib.import_module({module!r})\n"
        "def native_inference(request):\n"
        "    sleep=ctypes.PyDLL(None).sleep\n"
        f"    Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid(),'parent':os.getppid()}}))\n"
        "    sleep(8)  # PyDLL retains the GIL; the Python guard thread cannot run.\n"
        f"    Path({str(finished)!r}).touch()\n"
        "    return {}\n"
        f"worker.{operation}=native_inference\n"
        "raise SystemExit(worker.main())\n"
    )
    executable.chmod(0o755)
    source = (
        "import asyncio\nfrom pathlib import Path\nfrom ttakkari.model_process import run_json_worker\n"
        f"asyncio.run(run_json_worker(module={module!r},python=Path({str(executable)!r}),request={{}},timeout=15))\n"
    )
    env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
    env.update(PYTHONPATH=str(Path(model_process.__file__).resolve().parents[1]), TMPDIR=str(tmp_path))
    sibling = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(20)", env=env)
    parent = await asyncio.create_subprocess_exec(sys.executable, "-c", source, env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    identity = None
    try:
        await wait_file(ready)
        identity = json.loads(ready.read_text())
        assert identity["parent"] == parent.pid
        parent.kill()
        await parent.wait()
        async with asyncio.timeout(3):
            while True:
                try:
                    os.kill(identity["pid"], 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.01)
        assert not finished.exists(), "native work finished after its owner was killed"
        assert sibling.returncode is None, "native fallback affected a separate test child"
    finally:
        if parent.returncode is None:
            parent.kill()
        await parent.wait()
        if sibling.returncode is None:
            sibling.kill()
        await sibling.wait()
        if identity:
            # The deliberately finite native call is our failure-path cleanup.
            async with asyncio.timeout(12):
                while True:
                    try:
                        os.kill(identity["pid"], 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(.02)


async def test_live_owner_preserves_native_output_and_reaps_monitor(tmp_path):
    executable = tmp_path / "live-native-worker"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import ctypes,json,sys\nfrom pathlib import Path\n"
        "from ttakkari.model_process import start_parent_guard\n"
        "guard=start_parent_guard()\n"
        "try:\n"
        "    json.load(sys.stdin)\n"
        "    ctypes.PyDLL(None).sleep(2)\n"
        "finally:\n"
        "    guard.set()\n"
        "    guard.set()  # stop/reap is idempotent\n"
        "Path(sys.argv[-1]).write_text(json.dumps({'native_output':'preserved','monitor_exit':guard.process.returncode}))\n"
    )
    executable.chmod(0o755)
    observations = []

    async def observe(event):
        observations.append(event)

    with model_process.worker_observations(observe):
        result = await model_process.run_json_worker(module="unused", python=executable, request={}, timeout=8)
    assert result["native_output"] == "preserved" and result["monitor_exit"] == 0
    assert [e["state"] for e in observations] == ["running", "exited"]
    assert observations[-1]["returncode"] == 0


def test_monitor_start_failure_is_not_silently_replaced_with_thread(monkeypatch):
    monkeypatch.setenv("TTAKKARI_MODEL_PARENT_PID", str(os.getppid()))
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv("TTAKKARI_MODEL_OWNER_FD", str(read_fd))

    def refused(*args, **kwargs):
        raise OSError("synthetic monitor spawn refusal")

    monkeypatch.setattr(model_process.subprocess, "Popen", refused)
    try:
        with pytest.raises(OSError, match="monitor spawn refusal"):
            model_process.start_parent_guard()
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_monitor_rejects_non_pipe_descriptor_before_start(monkeypatch, tmp_path):
    monkeypatch.setenv("TTAKKARI_MODEL_PARENT_PID", str(os.getppid()))
    with (tmp_path / "not-a-pipe").open("w") as handle:
        monkeypatch.setenv("TTAKKARI_MODEL_OWNER_FD", str(handle.fileno()))
        with pytest.raises(RuntimeError, match="owner_pipe_invalid"):
            model_process.start_parent_guard()
