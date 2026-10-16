from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["TTAKKARI_NO_ENV_FILE"] = "1"
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location(
    "luna_benchmark_service", ROOT / "src/ttakkari/runs/luna_benchmark.py"
)
assert SPEC and SPEC.loader


def _module():
    module = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = module
    SPEC.loader.exec_module(module)
    return module


def test_benchmark_contract_is_fixed_to_v2_luna_high_and_90_cases():
    module = _module()
    contract = module.contract()
    assert contract["model"] == "gpt-6-luna"
    assert contract["effort"] == "high"
    assert contract["dataset_cases_n"] == 30
    assert contract["repetitions"] == 3
    assert contract["requested_n"] == 90
    assert contract["max_output_tokens"] is None


def test_checkpoint_records_init_failure_unscored_and_stops_immediately():
    module = _module()
    rows = []
    row = module.record_attempt(
        {"case_id": "common30-01", "repetition": 1},
        {"text": None, "error": "failed to initialize in-process app-server client: Operation not permitted"},
        rows,
    )
    assert row["execution_status"] == "initialization_failed"
    assert row["scored"] is False
    assert row["exact_correct"] is None
    assert len(rows) == 1


def test_checkpoint_summary_uses_only_successful_latency_and_separates_87_90():
    module = _module()
    rows = [
        {"execution_status": "completed", "scored": True, "valid": True, "elapsed_ms": 100,
         "expected_status": "answer", "exact_correct": True, "acceptable_set_correct": True},
        {"execution_status": "timeout", "scored": False, "valid": False, "elapsed_ms": 1500,
         "expected_status": "delegate", "exact_correct": None, "acceptable_set_correct": None},
    ]
    summary = module.summarize(rows)
    assert summary["latency_p50_ms"] == 100
    assert summary["latency_p95_ms"] == 100
    assert summary["hard_exact_n"] == 1
    assert summary["acceptable_n"] == 1
    assert summary["timeouts"] == 1


def test_job_view_pages_raw_results_without_exposing_other_jobs():
    module = _module()
    import uuid
    job = module.Job(uuid.uuid4(), "owner", uuid.uuid4(), uuid.uuid4(),
                     rows=[{"n": n} for n in range(25)])
    page = job.view(offset=20, limit=5)
    assert page["total_rows"] == 25
    assert page["rows_offset"] == 20
    assert page["rows"] == [{"n": n} for n in range(20, 25)]


def test_checkpoint_can_be_read_after_generation_switch_but_not_by_another_owner(tmp_path, monkeypatch):
    import uuid

    from ttakkari.config import get_settings

    module = _module()
    monkeypatch.setenv("TTAKKARI_HOME", str(tmp_path))
    get_settings.cache_clear()
    job_id, generation_id, profile_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    job = module.Job(job_id, "owner-a", generation_id, profile_id, status="running",
                     rows=[{"case_id": "common30-01"}], checkpoint_path=module._checkpoint_path(job_id))
    job.checkpoint()
    module._job = None
    resumed = module.get_job(job_id, "owner-a")
    assert resumed and resumed.rows == [{"case_id": "common30-01"}]
    assert module.get_job(job_id, "owner-b") is None


def test_checkpoint_recovery_preserves_all_raw_rows_across_api_pages(tmp_path, monkeypatch):
    import uuid

    from ttakkari.config import get_settings

    module = _module()
    monkeypatch.setenv("TTAKKARI_HOME", str(tmp_path))
    get_settings.cache_clear()

    for count in (30, 90):
        job_id = uuid.uuid4()
        expected = [{"row_id": f"raw-{n:03d}", "n": n} for n in range(count)]
        job = module.Job(job_id, "owner", uuid.uuid4(), uuid.uuid4(), rows=expected,
                         checkpoint_path=module._checkpoint_path(job_id))
        job.checkpoint()

        module._job = None  # Force restoration from disk with no live Job object.
        restored = module.get_job(job_id, "owner")
        assert restored is not None
        assert restored.view(limit=100)["total_rows"] == count

        recovered_rows = []
        for offset in (0, 20, 40, 60, 80):
            page = restored.view(offset=offset, limit=20)
            assert page["total_rows"] == count
            recovered_rows.extend(page["rows"])
        assert recovered_rows == expected


def test_runner_exception_checkpoints_terminal_failure(tmp_path, monkeypatch):
    import asyncio
    import uuid

    from ttakkari.config import get_settings

    module = _module()
    monkeypatch.setenv("TTAKKARI_HOME", str(tmp_path))
    get_settings.cache_clear()
    job_id, generation_id, profile_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    job = module.Job(job_id, "owner", generation_id, profile_id,
                     checkpoint_path=module._checkpoint_path(job_id))
    job.checkpoint()

    class Runtime:
        id = generation_id
        state = "active"

        def work(self, _label):
            class Work:
                async def __aenter__(self):
                    raise RuntimeError("work acquisition failed")

                async def __aexit__(self, *_args):
                    return False

            return Work()

    module.generation.current = Runtime()
    try:
        asyncio.run(module._run(job, object()))
    except RuntimeError as exc:
        assert str(exc) == "work acquisition failed"
    else:
        raise AssertionError("runner exception should propagate")

    module._job = None
    restored = module.get_job(job_id, "owner")
    assert restored is not None
    assert restored.status == "failed"


def test_runner_cancellation_checkpoints_terminal_status(tmp_path, monkeypatch):
    import asyncio
    import uuid

    from ttakkari.config import get_settings

    module = _module()
    monkeypatch.setenv("TTAKKARI_HOME", str(tmp_path))
    get_settings.cache_clear()
    job_id, generation_id, profile_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    job = module.Job(job_id, "owner", generation_id, profile_id,
                     checkpoint_path=module._checkpoint_path(job_id))
    job.checkpoint()
    entered = asyncio.Event()

    class Runtime:
        id = generation_id
        state = "active"

        def work(self, _label):
            class Work:
                async def __aenter__(self):
                    entered.set()
                    await asyncio.Future()

                async def __aexit__(self, *_args):
                    return False

            return Work()

    module.generation.current = Runtime()

    async def cancel_runner():
        task = asyncio.create_task(module._run(job, object()))
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(cancel_runner())
    module._job = None
    restored = module.get_job(job_id, "owner")
    assert restored is not None
    assert restored.status == "cancelled"


def test_cross_generation_singleton_lock_rejects_a_second_start(tmp_path, monkeypatch):
    from ttakkari.config import get_settings

    module = _module()
    monkeypatch.setenv("TTAKKARI_HOME", str(tmp_path))
    get_settings.cache_clear()
    first = module._acquire_singleton()
    try:
        assert first is not None
        assert module._acquire_singleton() is None
    finally:
        first.close()


def test_local_runner_does_not_launch_a_nested_codex_process(capsys):
    runner_spec = importlib.util.spec_from_file_location(
        "local_luna_runner", ROOT / "experiments/qwen35-router/common30-v2/run_luna_codex.py"
    )
    assert runner_spec and runner_spec.loader
    module = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(module)
    assert not hasattr(module, "subprocess")
    module.main()
    assert "MCP 도구 start_luna_common30_v2" in capsys.readouterr().out


def test_codex_service_command_disables_shell_web_hooks_and_mcp_and_uses_schema(tmp_path, monkeypatch):
    import uuid

    from ttakkari.config import get_settings
    from ttakkari.runs import desk
    from ttakkari.runs.profiles import Profile

    config_dir = tmp_path / "profile"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[mcp_servers.example]\ncommand="unused"\n')
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    monkeypatch.setenv("TTAKKARI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TTAKKARI_CODEX_BIN", "/usr/bin/codex")
    get_settings.cache_clear()
    prof = Profile(uuid.uuid4(), "personal codex", "codex", config_dir)
    argv = desk._codex_cmd(prof, {"model": "gpt-6-luna", "effort": "high"}, "system", "prompt",
                           tmp_path, output_schema=schema)
    assert argv.count("--disable") >= 3
    assert "shell_tool" in argv and "web_search_request" in argv and "hooks" in argv
    assert "mcp_servers.example.enabled=false" in argv
    assert "approval_policy=\"never\"" in argv
    assert "--output-schema" in argv and str(schema) in argv
