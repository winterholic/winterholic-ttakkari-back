"""Opt-in actual CPU ML owner-crash evidence; never collected by default CI.

Uses isolated Run PostgreSQL and test-created owners only. The executable wrapper
repeats real inference for at most 30s after its first real result, so the owner can
be killed while the loaded model is working. This is not a native GIL claim.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from test_research_runner_lifecycle import seed_runs

from ttakkari import model_process
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.research import runner
from ttakkari.research.models import Run
from ttakkari.research.resources import WORKERS_KEY

pytestmark = pytest.mark.skipif(
    os.getenv("TTAKKARI_RESEARCH_LIVE_OWNER") != "1", reason="actual CPU ML opt-in",
)
ROOT = Path("/Users/winterholic/development/projects/winterholic-ttakkari/back")
TTS = "ttakkari.tts.worker_supertonic"
STT = "ttakkari.meetings.local_stt.worker"


def actual_executable(tmp_path, module, ready):
    """Instrument real inference, without replacing its output with a stub."""
    python = ROOT / (".tts-runtime/supertonic/bin/python" if module == TTS
                     else ".stt-runtime/venv/bin/python")
    assert python.is_file()
    common = (
        f"#!{python}\n"
        "import importlib,json,os,time\nfrom pathlib import Path\n"
        f"worker=importlib.import_module({module!r})\n"
        f"ready=Path({str(ready)!r})\n"
    )
    if module == TTS:
        hook = (
            "original=worker._Engine.synthesize\n"
            "def actual(self,*args,**kwargs):\n"
            "    result=original(self,*args,**kwargs)\n"
            "    assert len(result[0])>0\n"
            "    ready.write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid(),"
            "'real_samples':len(result[0]),'runtime':self.ort_version,'python':os.sys.version}))\n"
            "    deadline=time.monotonic()+30\n"
            "    while time.monotonic()<deadline: result=original(self,*args,**kwargs)\n"
            "    return result\n"
            "worker._Engine.synthesize=actual\n"
        )
    else:
        hook = (
            "import faster_whisper,ctranslate2\n"
            "original=faster_whisper.WhisperModel.transcribe\n"
            "def actual(self,*args,**kwargs):\n"
            "    segments,info=original(self,*args,**kwargs)\n"
            "    segments=list(segments)\n"
            "    assert segments\n"
            "    ready.write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid(),"
            "'real_segments':len(segments),'runtime':ctranslate2.__version__,'python':os.sys.version}))\n"
            "    deadline=time.monotonic()+30\n"
            "    while time.monotonic()<deadline:\n"
            "        iterator,info=original(self,*args,**kwargs)\n"
            "        segments=list(iterator)\n"
            "    return iter(segments),info\n"
            "faster_whisper.WhisperModel.transcribe=actual\n"
        )
    path = tmp_path / "actual-ml-worker"
    path.write_text(common + hook + "raise SystemExit(worker.main())\n")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("module,user_cancelled", [(TTS, False), (STT, True)])
async def test_actual_cpu_model_owner_crash_preserves_run(client, auth, tmp_path, monkeypatch, module, user_cancelled):
    foreground, recover = asyncio.current_task(), runner._recover_orphaned_runs

    async def foreground_only():
        return await recover() if asyncio.current_task() is foreground else 0

    monkeypatch.setattr(runner, "_recover_orphaned_runs", foreground_only)
    _, ids = await seed_runs()
    rid = ids[0]
    original_raw = {"saved": "before_actual_model_owner_crash", "module": module}
    async with sessionmaker()() as db:
        row = await db.get(Run, rid)
        row.adapter_id, row.raw_output = "tts_synthesis", original_raw
        await db.commit()
    ready = tmp_path / "real-inference.json"
    executable = actual_executable(tmp_path, module, ready)
    if module == TTS:
        request = {"model_dir": str(ROOT / ".tts-runtime/supertonic/assets"),
                   "out_dir": str(tmp_path / "audio"), "voice": "M1", "speed": 1.,
                   "steps": 2, "lang": "ko", "cpu_threads": 2,
                   "chunks": ["안녕하세요. 소유자 종료를 확인합니다."]}
    else:
        prior = json.loads(Path("docs/research-lab/evidence/research-memory-live-e1cfa8b4.json").read_text())
        audio = Path(prior["normal"]["raw_output"]["audio_chunks"][0]["path"])
        assert audio.is_file(), "reuse existing WAV; do not silently synthesize a replacement"
        request = {"audio_path": str(audio), "model_path": str(ROOT / ".stt-runtime/faster-whisper-large-v3"),
                   "cpu_threads": 2, "profile": "baseline"}
    assert Path(request.get("model_dir", request.get("model_path"))).is_dir()
    source = (
        "import asyncio,uuid\nfrom pathlib import Path\n"
        "from ttakkari.model_process import run_json_worker\n"
        "from ttakkari.research import runner\n"
        "from ttakkari.research.adapters.base import AdapterRunResult\n"
        "class Adapter:\n"
        "    def get_adapter(self,key): return self\n"
        "    def to_measurements(self,result,config): return []\n"
        "    async def execute_run(self,**kwargs):\n"
        f"        r=await run_json_worker(module={module!r},python=Path({str(executable)!r}),"
        f"request={request!r},timeout=110)\n"
        "        return AdapterRunResult(status='succeeded',raw_output=r,environment_snapshot={})\n"
        f"asyncio.run(runner.execute_run_instance(uuid.UUID({str(rid)!r}),Adapter()))\n"
    )
    env = {k: v for k, v in os.environ.items() if k.startswith("TTAKKARI_") or k in ("PATH", "TMPDIR", "LANG")}
    env.update(PYTHONPATH=str(Path(model_process.__file__).resolve().parents[1]),
               TTAKKARI_HOME=str(get_settings().home), TMPDIR=str(tmp_path))
    stderr = tmp_path / "owner.stderr"
    parent = None
    with stderr.open("wb") as errors:
        parent = await asyncio.create_subprocess_exec(sys.executable, "-c", source, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=errors)
        try:
            async with asyncio.timeout(100):
                while not ready.exists():
                    assert parent.returncode is None, stderr.read_text()[-2000:]
                    await asyncio.sleep(.02)
            actual = json.loads(ready.read_text())
            before_response = await client.get(f"/api/research/runs/{rid}", headers=auth)
            assert before_response.status_code == 200
            before = before_response.json()
            checkpoint = before["environment_snapshot"][WORKERS_KEY]
            [child] = checkpoint["workers"].values()
            assert child["pid"] == actual["pid"] and child["parent_pid"] == actual["parent"] == parent.pid
            assert child["module"] == module and child["state"] == "running"
            assert await runner.recover_orphaned_runs() == 0
            if user_cancelled:
                assert await runner.cancel_run(rid, "researcher actual ML stop")
            killed_at = time.monotonic()
            parent.kill()
            await parent.wait()
            async with asyncio.timeout(6):
                while True:
                    try:
                        os.kill(child["pid"], 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(.01)
            exit_seconds = time.monotonic() - killed_at
            assert await runner.recover_orphaned_runs() == 1
            after_response = await client.get(f"/api/research/runs/{rid}", headers=auth)
            assert after_response.status_code == 200
            after = after_response.json()
            async with sessionmaker()() as db:
                row = await db.get(Run, rid)
                assert row.raw_output == after["raw_output"] == original_raw
                assert row.environment_snapshot == after["environment_snapshot"]
            assert after["status"] == ("cancelled" if user_cancelled else "failed")
            assert after["error_code"] == ("cancelled" if user_cancelled else "owner_lost")
            if user_cancelled:
                assert after["error_message"] == "researcher actual ML stop"
            recovery = after["environment_snapshot"]["research_resource_recovery"]
            assert recovery["status"] == "verified" and recovery["evidence"]["signals_sent"] is False
            assert after["environment_snapshot"][WORKERS_KEY] == checkpoint
            assert await runner.recover_orphaned_runs() == 0
            response = await client.get(f"/api/research/studies/{after['study_id']}/resume", headers=auth)
            assert response.status_code == 200
            resumed = response.json()
            events = [e for e in resumed["timeline"]
                      if e["kind"] == ("resource_recovery" if user_cancelled else "execution_finished")
                      and e["payload"].get("run_id") == str(rid)]
            assert len(events) == 1 and events[0]["payload"]["external_resource_cleanup"] == "verified"
            report_path = Path(os.environ["RESEARCH_OWNER_LIVE_REPORT"])
            report = json.loads(report_path.read_text()) if report_path.exists() else {"cases": []}
            report["cases"].append({"module": module, "user_cancelled": user_cancelled,
                "actual_inference": actual, "exit_seconds": exit_seconds, "before": before,
                "after": after, "resume": resumed, "request": request,
                "wrapper_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()})
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(f"actual ML owner crash: {module}, {after['error_code']}, exit={exit_seconds:.3f}s, cleanup=verified")
        finally:
            if parent.returncode is None:
                parent.kill()
            await parent.wait()
            # The real inference loop is finite. Do not signal a PID loaded from DB.
            if ready.exists():
                pid = json.loads(ready.read_text())["pid"]
                async with asyncio.timeout(115):
                    while True:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        await asyncio.sleep(.02)
