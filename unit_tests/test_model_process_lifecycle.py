"""Model-free lifecycle checks; signal only children created by this test."""
from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

from ttakkari import model_process


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancel_during_spawn_waits_for_handle_then_reaps(monkeypatch, cancel_count):
    original = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(model_process.run_json_worker(module="unused", request={}, timeout=30))
    try:
        await asyncio.wait_for(created.wait(), 5)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None, "spawned child escaped cancellation cleanup"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_repeated_cancel_cannot_interrupt_reaping(monkeypatch):
    original = asyncio.create_subprocess_exec
    created, reaping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        original_wait = process.wait

        async def delayed_wait():
            reaping.set()
            await release.wait()
            return await original_wait()

        process.wait = delayed_wait
        created.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(model_process.run_json_worker(module="unused", request={}, timeout=30))
    try:
        await asyncio.wait_for(created.wait(), 5)
        task.cancel()
        await asyncio.wait_for(reaping.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "cancellation returned before child wait completed"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.mark.parametrize("module,operation", [
    ("ttakkari.tts.worker_supertonic", "synthesize"),
    ("ttakkari.meetings.local_stt.worker", "transcribe"),
])
@pytest.mark.parametrize("identity", ["different", "1", "invalid"])
def test_worker_rejects_wrong_parent_before_inference(monkeypatch, tmp_path, module, operation, identity):
    worker = importlib.import_module(module)
    monkeypatch.setenv("TTAKKARI_MODEL_PARENT_PID", str(os.getpid()) if identity == "different" else identity)
    calls = []
    monkeypatch.setattr(worker, operation, lambda request: calls.append(request) or {})
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "argv", ["worker", str(tmp_path / "output.json")])

    class ParentLost(BaseException):
        pass

    def exit_worker(code):
        assert code == 74
        raise ParentLost

    monkeypatch.setattr(os, "_exit", exit_worker)
    with pytest.raises(ParentLost):
        worker.main()
    assert calls == []
    assert not (tmp_path / "output.json").exists()


async def test_supervisor_binds_expected_parent_without_forwarding_override(monkeypatch, tmp_path):
    executable = tmp_path / "worker"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os,json,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[-1]).write_text(json.dumps({'parent':os.environ.get('TTAKKARI_MODEL_PARENT_PID')}))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("TTAKKARI_MODEL_PARENT_PID", "1")
    result = await model_process.run_json_worker(module="unused", request={}, timeout=5, python=executable)
    assert result["parent"] == str(os.getpid())
    assert result["metrics"]["process_exited"] is True


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("module,operation", [
    ("ttakkari.tts.worker_supertonic", "synthesize"),
    ("ttakkari.meetings.local_stt.worker", "transcribe"),
])
def test_live_parent_allows_normal_worker_result(monkeypatch, tmp_path, declared, module, operation):
    # STT main changes its disposable process environment; isolate the in-process stub too.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    if declared:
        monkeypatch.setenv("TTAKKARI_MODEL_PARENT_PID", str(os.getppid()))
    else:
        monkeypatch.delenv("TTAKKARI_MODEL_PARENT_PID", raising=False)
    worker = importlib.import_module(module)
    monkeypatch.setattr(worker, operation, lambda request: {"unchanged": request})
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"sample":1}'))
    monkeypatch.setattr(sys, "argv", ["worker", str(tmp_path / "output.json")])
    assert worker.main() == 0
    assert json.loads((tmp_path / "output.json").read_text()) == {"unchanged": {"sample": 1}}


async def test_spawn_failure_after_cancel_preserves_cancellation(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def failed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        raise OSError("synthetic spawn refusal")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_spawn)
    task = asyncio.create_task(model_process.run_json_worker(module="unused", request={}, timeout=5))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


async def _wait_file(path):
    async with asyncio.timeout(6):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("module,operation", [
    ("ttakkari.tts.worker_supertonic", "synthesize"),
    ("ttakkari.meetings.local_stt.worker", "transcribe"),
])
@pytest.mark.parametrize("phase", ["before_guard", "during_inference"])
async def test_actual_parent_death_exits_only_owned_model_free_child(tmp_path, module, operation, phase):
    ready, go, inferred, exited = [tmp_path / name for name in ("ready", "go", "inferred", "exited")]
    executable = tmp_path / "model-free-worker"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import importlib,json,os,sys,threading,time\nfrom pathlib import Path\n"
        f"ready,go,inferred,exited = [Path(p) for p in {list(map(str, (ready, go, inferred, exited)))!r}]\n"
        "real_exit=os._exit\n"
        "def record_exit(code):\n"
        "    exited.write_text(str(code))\n"
        "    real_exit(code)\n"
        "os._exit=record_exit\n"
        "threading.Timer(10, lambda: real_exit(75)).start()\n"
        f"worker=importlib.import_module({module!r})\n"
        "def fake_inference(request):\n"
        "    inferred.write_text('model-free')\n"
        "    ready.write_text(json.dumps({'pid':os.getpid(),'expected':os.environ['TTAKKARI_MODEL_PARENT_PID'],'parent':os.getppid()}))\n"
        "    threading.Event().wait(9)\n"
        "    return {}\n"
        f"setattr(worker,{operation!r},fake_inference)\n"
        f"if {phase!r} == 'before_guard':\n"
        "    ready.write_text(json.dumps({'pid':os.getpid(),'expected':os.environ['TTAKKARI_MODEL_PARENT_PID'],'parent':os.getppid()}))\n"
        "    deadline=time.monotonic()+8\n"
        "    while not go.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        "raise SystemExit(worker.main())\n"
    )
    executable.chmod(0o755)
    code = (
        "import asyncio\nfrom pathlib import Path\nfrom ttakkari.model_process import run_json_worker\n"
        f"asyncio.run(run_json_worker(module={module!r},request={{}},timeout=20,python=Path({str(executable)!r})))\n"
    )
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
    env["PYTHONPATH"] = str(Path(model_process.__file__).resolve().parents[1])
    # Keep every temporary home inside pytest's own directory, including abrupt parent exit.
    env["TMPDIR"] = str(tmp_path)
    sibling = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(15)", env=env)
    parent = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        await _wait_file(ready)
        identity = json.loads(ready.read_text())
        assert int(identity["expected"]) == identity["parent"] == parent.pid
        parent.kill()
        assert await parent.wait() != 0
        go.touch()
        await _wait_file(exited)
        assert exited.read_text() == "74"
        if phase == "before_guard":
            assert not inferred.exists()
        else:
            assert inferred.read_text() == "model-free"
        # Observe only the exact PID created above, never enumerate host processes.
        async with asyncio.timeout(6):
            while True:
                try:
                    os.kill(identity["pid"], 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
        assert sibling.returncode is None
    finally:
        for process in (parent, sibling):
            if process.returncode is None:
                process.kill()
            await process.wait()
