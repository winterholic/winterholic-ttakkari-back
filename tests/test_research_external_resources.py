"""Actual isolated PostgreSQL with synthetic host resource probes; no GPU launch."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from test_research_runner_lifecycle import ProbeAdapter, seed_runs

from ttakkari.db import sessionmaker
from ttakkari.research import runner
from ttakkari.research.adapters.base import AdapterRunResult
from ttakkari.research.models import Run


def launch_identity():
    return {"launch_id": uuid.uuid4().hex, "supervisor_pid": 1234, "supervisor_start": 123.5,
            "server_pid": 1235, "server_start": 124.5}


async def test_resource_checkpoint_is_visible_before_adapter_finishes(client, auth):
    from ttakkari.research.resources import RESOURCE_KEY, checkpoint_resource

    _, ids = await seed_runs()
    adapter = ProbeAdapter()
    launch = launch_identity()

    async def execute(**kwargs):
        await checkpoint_resource({"kind": "host_gpu", "launch": launch, "state": "owned"})
        adapter.entered.set()
        await adapter.release.wait()
        return AdapterRunResult(status="succeeded", raw_output={"checkpoint_test": True}, environment_snapshot={})

    adapter.execute_run = execute
    task = asyncio.create_task(runner.execute_run_instance(ids[0], adapter))
    try:
        await asyncio.wait_for(adapter.entered.wait(), 1)
        data = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()
        checkpoint = data["environment_snapshot"][RESOURCE_KEY]
        assert checkpoint["launch"] == launch
        assert checkpoint["owner_token"] == data["environment_snapshot"]["research_execution_owner"]["token"]
        assert data["status"] == "running"
    finally:
        adapter.release.set()
        result = await task
    assert result.environment_snapshot[RESOURCE_KEY] == checkpoint


@pytest.mark.parametrize("shape", ["valid", "wrong_token", "missing_identity", "tts", "absent", "wrong_adapter", "work_scope"])
async def test_orphan_cleanup_uses_only_verified_resource_checkpoint(client, auth, monkeypatch, shape):
    from ttakkari.research.resources import RESOURCE_KEY

    _, ids = await seed_runs()
    token = str(uuid.uuid4())
    launch = launch_identity()
    resource = {"contract": 1, "owner_token": token, "kind": "host_gpu", "state": "owned", "launch": launch}
    if shape == "wrong_token":
        resource["owner_token"] = str(uuid.uuid4())
    elif shape == "missing_identity":
        resource["launch"] = {"launch_id": launch["launch_id"]}
    elif shape == "tts":
        resource["kind"] = "tts_worker"
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        row.adapter_id = "retrieval_eval" if shape == "wrong_adapter" else "llm_inference"
        if shape == "work_scope":
            from ttakkari.research.models import Study
            (await db.get(Study, row.study_id)).scope = "work"
        row.status = "running"
        row.raw_output = {"saved": "before_crash"}
        row.environment_snapshot = {"research_execution_owner": {"contract": 1, "token": token, "generation_id": None}}
        if shape != "absent":
            row.environment_snapshot = {**row.environment_snapshot, RESOURCE_KEY: resource}
        await db.commit()
    from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
    calls = []

    def cleanup(host, data_dir, recorded_launch):
        calls.append(recorded_launch)
        return {"attempted": True, "success": True, "launch_id": recorded_launch["launch_id"], "stopped": True}

    monkeypatch.setattr(LlmInferenceAdapter, "_cleanup_owned_launch", staticmethod(cleanup))
    assert await runner.recover_orphaned_runs() == 1
    assert calls == ([launch] if shape == "valid" else [])
    data = (await client.get(f"/api/research/runs/{ids[0]}", headers=auth)).json()
    assert data["status"] == "failed" and data["error_code"] == "owner_lost"
    assert data["raw_output"] == {"saved": "before_crash"}
    assert data["environment_snapshot"]["research_resource_recovery"]["status"] == (
        "verified" if shape == "valid" else "unverified"
    )
    assert await runner.recover_orphaned_runs() == 0
    assert calls == ([launch] if shape == "valid" else [])


async def test_resource_checkpoint_cannot_overwrite_replaced_execution_owner(client):
    from ttakkari.research.resources import RESOURCE_KEY, checkpoint_resource

    _, ids = await seed_runs()
    adapter = ProbeAdapter()
    replacement = {"contract": 1, "token": str(uuid.uuid4()), "generation_id": None}

    async def execute(**kwargs):
        async with sessionmaker()() as db:
            row = await db.get(Run, ids[0])
            row.environment_snapshot = {"research_execution_owner": replacement}
            row.raw_output = {"new_owner": True}
            await db.commit()
        await checkpoint_resource({"kind": "host_gpu", "launch": launch_identity(), "state": "owned"})
        pytest.fail("stale checkpoint was accepted")

    adapter.execute_run = execute
    result = await runner.execute_run_instance(ids[0], adapter)
    assert result.raw_output == {"new_owner": True}
    assert RESOURCE_KEY not in result.environment_snapshot
    assert result.environment_snapshot["research_execution_owner"] == replacement


@pytest.mark.parametrize("process_state", ["present", "unknown", "absent"])
async def test_replaced_gpu_launch_never_stops_the_replacement(monkeypatch, process_state):
    from ttakkari import host_gpu
    from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
    from ttakkari.research.resources import RESOURCE_KEY, recover_resource

    launch, token, refusals = launch_identity(), str(uuid.uuid4()), []

    class ReplacedHost:
        def stop(self, expected_launch_id=None):
            refusals.append(expected_launch_id)
            raise host_gpu.LaunchMismatch("synthetic other launch; refusing before signals")

    monkeypatch.setattr(host_gpu, "HostGpu", lambda settings: ReplacedHost())
    monkeypatch.setattr(LlmInferenceAdapter, "_launch_processes_absent", staticmethod(
        lambda recorded: (process_state == "absent", {"supervisor": process_state, "server": process_state})
    ))
    result = await recover_resource({
        "research_execution_owner": {"contract": 1, "token": token},
        RESOURCE_KEY: {"contract": 1, "owner_token": token, "kind": "host_gpu", "launch": launch},
    })
    assert refusals == [launch["launch_id"]]
    assert result["evidence"]["skipped_reason"] == "launch_replaced"
    assert result["evidence"]["stopped"] is False
    assert result["status"] == ("verified" if process_state == "absent" else "unverified")


def test_lock_observation_error_is_unverified_not_held_or_free(monkeypatch):
    from types import SimpleNamespace

    from ttakkari.research import resources

    row = SimpleNamespace(id=uuid.uuid4(), experiment_id=uuid.uuid4(), adapter_id="llm_inference", status="running",
                          environment_snapshot={"research_execution_owner": {"contract": 1, "token": str(uuid.uuid4())}})

    def unavailable(token):
        raise PermissionError("synthetic observation refusal")

    monkeypatch.setattr(resources, "probe", unavailable)
    locks, observation = resources.observe_execution_owners([row], truncated=False)
    assert locks == []
    assert observation["unverified_run_ids"] == [str(row.id)]


@pytest.mark.parametrize("cleanup_succeeds", [True, False])
async def test_orphan_cleanup_after_user_cancel_preserves_status_and_reason(client, monkeypatch, cleanup_succeeds):
    from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
    from ttakkari.research.ownership import ExecutionOwner
    from ttakkari.research.resources import RESOURCE_KEY

    _, ids = await seed_runs()
    owner, launch, calls = ExecutionOwner.acquire(), launch_identity(), []

    def cleanup(host, data_dir, recorded):
        calls.append(recorded["launch_id"])
        return {"success": cleanup_succeeds, "attempted": True, "stopped": cleanup_succeeds}

    monkeypatch.setattr(LlmInferenceAdapter, "_cleanup_owned_launch", staticmethod(cleanup))
    try:
        async with sessionmaker()() as db:
            row = await db.get(Run, ids[0])
            row.adapter_id, row.status, row.error_code = "llm_inference", "cancelled", "cancelled"
            row.error_message = "Researcher stop"
            row.raw_output = {"saved": True}
            row.environment_snapshot = {**owner.snapshot({}), RESOURCE_KEY: {
                "contract": 1, "owner_token": owner.metadata["token"], "kind": "host_gpu",
                "state": "owned", "launch": launch,
            }}
            await db.commit()
        assert await runner.recover_orphaned_runs() == 0
        assert calls == []
    finally:
        owner.close()
    assert await runner.recover_orphaned_runs() == 1
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        assert row.status == row.error_code == "cancelled"
        assert row.error_message == "Researcher stop"
        assert row.raw_output == {"saved": True}
        assert row.environment_snapshot["research_resource_recovery"]["status"] == (
            "verified" if cleanup_succeeds else "unverified"
        )
    assert await runner.recover_orphaned_runs() == 0
    assert calls == [launch["launch_id"]]


async def test_shared_readiness_keeps_unknown_lease_for_dashboard(client, monkeypatch):
    from types import SimpleNamespace

    from ttakkari.research import service
    from ttakkari.research.adapters import registry
    from ttakkari.research.adapters.base import AdapterEnvironmentStatus

    adapter = SimpleNamespace(id="llm_inference", name_ko="synthetic", version="test", domains=[],
                              supported_parameters=[], required_inputs=[],
                              check_environment=lambda **kwargs: AdapterEnvironmentStatus(
                                  adapter_id="llm_inference", ready=False, execution_mode="host_supervisor",
                                  details={"lease_currently_held": None},
                              ))
    monkeypatch.setattr(registry, "get_adapter_registry", lambda: SimpleNamespace(list_adapters=lambda: [adapter]))
    _, lease = await service.adapter_readiness("personal")
    assert lease == {"held": None}
