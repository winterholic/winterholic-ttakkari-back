"""Research DB child receipts with model-free, test-owned Python children."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
from test_research_runner_lifecycle import ProbeAdapter, seed_runs

from ttakkari import model_process
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.research import runner
from ttakkari.research.adapters.base import AdapterRunResult
from ttakkari.research.models import Run
from ttakkari.research.ownership import ExecutionOwner

WORKERS_KEY = "research_model_workers"
TTS = "ttakkari.tts.worker_supertonic"
STT = "ttakkari.meetings.local_stt.worker"


async def wait_file(path):
    async with asyncio.timeout(8):
        while not path.exists():
            await asyncio.sleep(.01)


def executable(tmp_path, *, native=False):
    path = tmp_path / "model-free-child"
    path.write_text(
        f"#!{sys.executable}\n"
        "import ctypes,json,sys,threading,time\nfrom pathlib import Path\n"
        "from ttakkari.model_process import start_parent_guard\n"
        "stop=start_parent_guard()\nr=json.load(sys.stdin)\n"
        "Path(r['ready']).write_text('inference began after checkpoint')\n"
        + ("ctypes.PyDLL(None).sleep(9)\n" if native else
           "deadline=time.monotonic()+15\n"
           "while not Path(r['release']).exists() and time.monotonic()<deadline: time.sleep(.01)\n")
        +
        "Path(sys.argv[-1]).write_text(json.dumps({'metrics':{},'text':'model-free'}))\n"
        "stop.set()\n"
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("module", [TTS, STT])
@pytest.mark.parametrize("cancel", [False, True])
async def test_child_receipt_visible_before_inference_and_exit_saved(client, auth, tmp_path, module, cancel):
    _, ids = await seed_runs()
    async with sessionmaker()() as db:
        (await db.get(Run, ids[0])).adapter_id = "tts_synthesis"
        await db.commit()
    adapter = ProbeAdapter()
    ready, release = tmp_path / "ready", tmp_path / "release"

    async def execute(**kwargs):
        result = await model_process.run_json_worker(module=module, python=executable(tmp_path),
            request={"ready": str(ready), "release": str(release)}, timeout=15)
        return AdapterRunResult(status="succeeded", raw_output=result, environment_snapshot={"stub": True})

    adapter.execute_run = execute
    task = asyncio.create_task(runner.execute_run_instance(ids[0], adapter))
    try:
        await wait_file(ready)
        data = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()
        checkpoint = data["environment_snapshot"][WORKERS_KEY]
        assert checkpoint["owner_token"] == data["environment_snapshot"]["research_execution_owner"]["token"]
        assert checkpoint["state"] == "owned"
        [child] = checkpoint["workers"].values()
        assert child["module"] == module and child["state"] == "running"
        assert child["parent_pid"] == os.getpid()
        assert child["pid"] > 0
        if cancel:
            task.cancel()
        else:
            release.touch()
        await task
        final = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()
        receipt = final["environment_snapshot"][WORKERS_KEY]
        assert receipt["state"] == "released"
        [exited] = receipt["workers"].values()
        assert exited["pid"] == child["pid"] and exited["state"] == "exited"
        assert type(exited["returncode"]) is int
        assert final["status"] == ("cancelled" if cancel else "succeeded")
    finally:
        release.touch()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_tts_stt_receipts_merge_without_releasing_live_sibling(client, auth, tmp_path):
    from ttakkari.research.resources import RESOURCE_KEY, checkpoint_resource

    _, ids = await seed_runs()
    adapter, child_python = ProbeAdapter(), executable(tmp_path)
    ready = [tmp_path / f"ready-{i}" for i in range(2)]
    release = [tmp_path / f"release-{i}" for i in range(2)]
    first_done = asyncio.Event()

    async def invoke(index, module):
        await model_process.run_json_worker(module=module, python=child_python,
            request={"ready": str(ready[index]), "release": str(release[index])}, timeout=15)
        if index == 0:
            first_done.set()

    async def execute(**kwargs):
        await checkpoint_resource({"kind": "host_gpu", "state": "released", "launch": {"synthetic": True}})
        await asyncio.gather(invoke(0, TTS), invoke(1, STT))
        return AdapterRunResult(status="succeeded", raw_output={}, environment_snapshot={})

    adapter.execute_run = execute
    task = asyncio.create_task(runner.execute_run_instance(ids[0], adapter))
    try:
        await asyncio.gather(*(wait_file(p) for p in ready))
        release[0].touch()
        await asyncio.wait_for(first_done.wait(), 5)
        snapshot = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()["environment_snapshot"]
        receipt = snapshot[WORKERS_KEY]
        assert receipt["state"] == "owned"
        assert sorted(child["state"] for child in receipt["workers"].values()) == ["exited", "running"]
        assert snapshot[RESOURCE_KEY]["launch"] == {"synthetic": True}
        release[1].touch()
        await task
        snapshot = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()["environment_snapshot"]
        assert snapshot[WORKERS_KEY]["state"] == "released"
        assert len(snapshot[WORKERS_KEY]["workers"]) == 2
        assert snapshot[RESOURCE_KEY]["launch"] == {"synthetic": True}
    finally:
        for p in release:
            p.touch()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("observation", ["absent", "present", "unknown"])
@pytest.mark.parametrize("shape", ["valid", "wrong_token", "invalid_module", "missing_pid"])
async def test_recovery_observes_only_fenced_child_and_never_signals(client, auth, monkeypatch, observation, shape):
    from ttakkari import host_gpu_guard
    from ttakkari.research.resources import RECOVERY_KEY

    _, ids = await seed_runs()
    owner = ExecutionOwner.acquire()
    token, worker_id = owner.metadata["token"], str(uuid.uuid4())
    worker = {"module": TTS, "pid": 123456, "parent_pid": 123455, "started_at": 123.5, "state": "running"}
    if shape == "invalid_module":
        worker["module"] = "unrelated.module"
    if shape == "missing_pid":
        worker.pop("pid")
    checkpoint = {"contract": 1, "owner_token": str(uuid.uuid4()) if shape == "wrong_token" else token,
                  "state": "owned", "workers": {worker_id: worker}}
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        row.adapter_id, row.status = "tts_synthesis", "running"
        row.raw_output = {"saved": "before_owner_death"}
        row.environment_snapshot = {**owner.snapshot({}), WORKERS_KEY: checkpoint}
        await db.commit()
    owner.close()
    observations = []

    def observe(pid, start):
        observations.append((pid, start))
        return host_gpu_guard.ProcState(observation)

    def no_signals(*args):
        pytest.fail("recovery must never signal a model child by PID")

    monkeypatch.setattr(host_gpu_guard, "process_state", observe)
    monkeypatch.setattr(os, "kill", no_signals)
    assert await runner.recover_orphaned_runs() == 1
    data = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()
    assert data["status"] == "failed" and data["error_code"] == "owner_lost"
    assert data["raw_output"] == {"saved": "before_owner_death"}
    recovery = data["environment_snapshot"][RECOVERY_KEY]
    assert recovery["status"] == ("verified" if shape == "valid" and observation == "absent" else "unverified")
    assert observations == ([(123456, 123.5)] if shape == "valid" else [])
    assert await runner.recover_orphaned_runs() == 0


@pytest.mark.parametrize("module", [TTS, STT])
@pytest.mark.parametrize("user_cancelled", [False, True])
@pytest.mark.parametrize("native", [False, True])
async def test_actual_owner_death_recovers_persisted_child_without_replay(client, auth, tmp_path, monkeypatch, module, user_cancelled, native):
    # The app's periodic recovery must not race the explicitly staged observation.
    foreground, recover = asyncio.current_task(), runner._recover_orphaned_runs

    async def foreground_only():
        return await recover() if asyncio.current_task() is foreground else 0

    monkeypatch.setattr(runner, "_recover_orphaned_runs", foreground_only)
    _, ids = await seed_runs()
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        row.adapter_id, row.raw_output = "tts_synthesis", {"saved": "original"}
        await db.commit()
    ready, release = tmp_path / "ready", tmp_path / "release"
    child_python = executable(tmp_path, native=native)
    source = (
        "import asyncio,uuid\nfrom pathlib import Path\n"
        "from ttakkari.model_process import run_json_worker\n"
        "from ttakkari.research import runner\n"
        "from ttakkari.research.adapters.base import AdapterRunResult\n"
        "class Adapter:\n"
        "    def get_adapter(self, key): return self\n"
        "    def to_measurements(self, result, config): return []\n"
        "    async def execute_run(self, **kwargs):\n"
        f"        r=await run_json_worker(module={module!r},python=Path({str(child_python)!r}),"
        f"request={{'ready':{str(ready)!r},'release':{str(release)!r}}},timeout=20)\n"
        "        return AdapterRunResult(status='succeeded',raw_output=r,environment_snapshot={})\n"
        f"asyncio.run(runner.execute_run_instance(uuid.UUID({str(ids[0])!r}),Adapter()))\n"
    )
    env = {k: v for k, v in os.environ.items() if k.startswith("TTAKKARI_") or k in ("PATH", "TMPDIR", "LANG")}
    env["PYTHONPATH"] = str(Path(model_process.__file__).resolve().parents[1])
    env["TTAKKARI_HOME"] = str(get_settings().home)
    env["TMPDIR"] = str(tmp_path)
    parent = await asyncio.create_subprocess_exec(sys.executable, "-c", source, env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await wait_file(ready)
        before = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()
        checkpoint = before["environment_snapshot"][WORKERS_KEY]
        [child] = checkpoint["workers"].values()
        assert child["parent_pid"] == parent.pid and child["module"] == module
        # A live owner cannot be recovered just because the DB row says running.
        assert await runner.recover_orphaned_runs() == 0
        if user_cancelled:
            assert await runner.cancel_run(ids[0], "researcher stop")
        parent.kill()
        await parent.wait()
        async with asyncio.timeout(6):
            while True:
                try:
                    os.kill(child["pid"], 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.01)
        assert await runner.recover_orphaned_runs() == 1
        after = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()
        assert after["raw_output"] == {"saved": "original"}
        assert after["status"] == ("cancelled" if user_cancelled else "failed")
        assert after["error_code"] == ("cancelled" if user_cancelled else "owner_lost")
        if user_cancelled:
            assert after["error_message"] == "researcher stop"
        recovery = after["environment_snapshot"]["research_resource_recovery"]
        assert recovery["status"] == "verified" and recovery["evidence"]["signals_sent"] is False
        assert after["environment_snapshot"][WORKERS_KEY] == checkpoint
        assert await runner.recover_orphaned_runs() == 0
        resumed = await client.get(f"/api/research/studies/{after['study_id']}/resume", headers=auth)
        assert resumed.status_code == 200
        recovery_events = [e for e in resumed.json()["timeline"]
            if e["kind"] == ("resource_recovery" if user_cancelled else "execution_finished")
            and e["payload"].get("run_id") == str(ids[0])]
        assert len(recovery_events) == 1
        assert recovery_events[0]["payload"]["external_resource_cleanup"] == "verified"
    finally:
        release.touch()
        if parent.returncode is None:
            parent.kill()
        await parent.wait()
