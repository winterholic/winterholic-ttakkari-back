import asyncio
import os
from types import SimpleNamespace

import pytest

from ttakkari.meetings.local_stt import runner


def settings(tmp_path, source):
    python = tmp_path / "fake-worker"
    python.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    python.chmod(0o755)
    return SimpleNamespace(
        home=tmp_path,
        local_stt_python=python,
        local_stt_model_path=tmp_path,
        local_stt_timeout_seconds=1,
        local_stt_max_audio_seconds=60,
        local_stt_cpu_threads=2,
    )


async def test_supervisor_waits_for_exit_and_records_it(tmp_path):
    s = settings(
        tmp_path,
        'import json,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\nPath(sys.argv[-1]).write_text(json.dumps({"metrics":{},"pid":__import__("os").getpid()}))\n',
    )
    result = await runner.run_worker(tmp_path / "a.wav", {}, s)
    assert result["metrics"]["process_exited"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(result["pid"], 0)


async def test_timeout_kills_and_reaps_child(tmp_path):
    pid_file = tmp_path / "pid"
    s = settings(
        tmp_path,
        f"import os,time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )
    with pytest.raises(TimeoutError):
        await runner.run_worker(tmp_path / "a.wav", {}, s)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_cancellation_also_reaps_child(tmp_path):
    pid_file = tmp_path / "pid"
    s = settings(
        tmp_path,
        f"import os,time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
    )
    s.local_stt_timeout_seconds = 30
    task = asyncio.create_task(runner.run_worker(tmp_path / "a.wav", {}, s))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_nonzero_exit_is_failure_not_empty_result(tmp_path):
    s = settings(tmp_path, "raise SystemExit(2)\n")
    with pytest.raises(RuntimeError):
        await runner.run_worker(tmp_path / "a.wav", {}, s)


async def test_enhancement_configuration_is_forwarded_and_secrets_are_not(
    tmp_path, monkeypatch
):
    s = settings(
        tmp_path,
        'import json,sys,os\nfrom pathlib import Path\nr=json.load(sys.stdin)\nPath(sys.argv[-1]).write_text(json.dumps({"request":r,"env":dict(os.environ),"metrics":{}}))\n',
    )
    s.local_stt_diarization_model_path = tmp_path / "community-1"
    s.local_stt_diarization_device = "mps"
    s.local_stt_separation_model_path = tmp_path / "sepformer"
    monkeypatch.setenv("HF_TOKEN", "do-not-forward")
    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "1")
    r = await runner.run_worker(tmp_path / "a.wav", {}, s)
    assert r["request"]["diarization_model_path"] == str(
        s.local_stt_diarization_model_path
    )
    assert r["request"]["diarization_device"] == "mps"
    assert r["request"]["separation_model_path"] == str(
        s.local_stt_separation_model_path
    )
    assert r["env"]["PYTHONPATH"] == str(__import__("pathlib").Path(runner.__file__).resolve().parents[3])
    assert "HF_TOKEN" not in r["env"]
    assert r["env"]["PYANNOTE_METRICS_ENABLED"] == "0"
    assert r["env"]["HF_HOME"].startswith(r["env"]["HOME"])
    assert r["env"]["HOME"] != os.environ["HOME"]
