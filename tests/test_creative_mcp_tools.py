from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_creative_and_comfy_tools_are_registered_and_call_scoped_agent_services(monkeypatch):
    from ttakkari.orchestrator import mcp_server as ms

    tools = {tool.name: tool for tool in await ms.server.list_tools()}
    expected = {
        "comfy_profiles", "comfy_readiness", "comfy_jobs", "comfy_submit", "comfy_job",
        "comfy_cancel", "comfy_reconcile", "creative_v2_catalog",
        "creative_v2_capabilities", "creative_v2_recipe_matrix", "creative_v2_registries",
        "creative_v2_plans", "creative_v2_plan", "creative_v2_plan_create",
        "creative_v2_plan_edit", "creative_v2_plan_approve", "creative_v2_production_assets",
        "creative_v2_auto_assets_status", "creative_v2_production_dispatch",
        "creative_v2_production_render", "creative_v2_production_status",
        "creative_v2_production_media_link",
    }
    assert expected <= tools.keys()
    assert "prompt" in tools["comfy_submit"].input_schema["properties"]
    assert "revision" in tools["creative_v2_plan_approve"].input_schema["properties"]

    call = AsyncMock(return_value='{"status":"starting"}')
    monkeypatch.setattr(ms, "_call", call)
    await ms.server.call_tool("comfy_submit", {
        "profile_id": "wan22-5b-draft", "prompt": "scene image", "seed": 9,
        "idempotency_key": "stable-key-0001",
    })
    call.assert_awaited_once_with("POST", "/submit", {
        "profile_id": "wan22-5b-draft", "prompt": "scene image", "seed": 9,
        "idempotency_key": "stable-key-0001",
    }, prefix="/api/agent/comfy", params={})

    call.reset_mock()
    await ms.server.call_tool("creative_v2_plan_approve", {
        "plan_id": "00000000-0000-0000-0000-000000000001", "revision": 3,
    })
    call.assert_awaited_once_with("POST", "/plan_approve", {"data": {
        "plan_id": "00000000-0000-0000-0000-000000000001", "revision": 3,
    }}, prefix="/api/agent/creative-v2")


def test_managed_comfy_owner_filter_hides_jobs_from_other_personal_principals(tmp_path):
    from ttakkari.managed_comfy import ComfyManagedController

    controller = ComfyManagedController(data_dir=tmp_path)
    job = {
        "task_id": "a" * 32, "owner": "session-a", "status": "failed",
        "result": None, "profile": "wan22-5b-draft", "runner_job_id": None,
        "error": None,
    }
    controller.jobs_dir.mkdir(parents=True)
    controller._save(job)
    other = dict(job, task_id="b" * 32, owner="session-b")
    controller._save(other)
    controller._active[job["task_id"]] = {"progress": {"phase": "progress", "pct": 42}}

    current = controller.get(job["task_id"], owner="session-a")
    assert current["task_id"] == job["task_id"]
    assert current["progress"]["pct"] == 42
    page = controller.list_jobs(owner="session-a", limit=1)
    assert page == {"items": [job], "total_count": 1, "offset": 0, "has_more": False, "next_offset": None}
    with pytest.raises(FileNotFoundError):
        controller.get(job["task_id"], owner="session-b")
    with pytest.raises(FileNotFoundError):
        controller.cancel(job["task_id"], owner_task=job["task_id"], owner="session-b")
    with pytest.raises(FileNotFoundError):
        controller.reconcile(job["task_id"], owner_task=job["task_id"], owner="session-b")


def test_managed_comfy_idempotency_reuses_task_and_rejects_changed_arguments(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ttakkari.managed_comfy import ComfyManagedController

    spawned = []
    controller = ComfyManagedController(data_dir=tmp_path, spawn=lambda *args: spawned.append(args))
    lease = SimpleNamespace(
        lease_id="lease", generation_token="generation", runtime_instance="runtime",
        heartbeat=lambda **_: None,
    )
    monkeypatch.setattr(controller.manager, "acquire", lambda **_: lease)
    first = controller.prepare(
        "wan22-5b-draft", prompt="same", seed=4, owner="session-a", idempotency_key="stable-key-0001",
    )
    retry = controller.prepare(
        "wan22-5b-draft", prompt="same", seed=4, owner="session-a", idempotency_key="stable-key-0001",
    )
    assert first["task_id"] == retry["task_id"]
    controller.start(first["task_id"])
    controller.start(first["task_id"])
    assert len(spawned) == 1
    with pytest.raises(ValueError, match="different task arguments"):
        controller.prepare(
            "wan22-5b-draft", prompt="changed", seed=4,
            owner="session-a", idempotency_key="stable-key-0001",
        )


@pytest.mark.asyncio
async def test_agent_creative_and_comfy_access_rejects_work_sessions():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from ttakkari.api.comfy_managed import agent_access as comfy_access
    from ttakkari.api.creative_mcp import agent_access as studio_access

    sid = "00000000-0000-0000-0000-000000000001"

    class Db:
        async def get(self, *_):
            return SimpleNamespace(pool="work")

    with pytest.raises(HTTPException) as e:
        await comfy_access(sid, Db())
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await studio_access(sid, Db())
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_creative_bridge_fails_closed_for_unknown_operations_and_unmerged_auto_assets():
    from fastapi import HTTPException

    from ttakkari.api.creative_mcp import Command, command

    with pytest.raises(HTTPException) as e:
        await command("shell", Command(data={}), "personal")
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await command("plan", Command(data={"plan_id": "00000000-0000-0000-0000-000000000001", "path": "/tmp/x"}), "personal")
    assert e.value.status_code == 422
    unavailable = await command("auto_assets_status", Command(), "personal")
    assert unavailable["available"] is False
    assert "unmerged" in unavailable["reason"]


@pytest.mark.asyncio
async def test_creative_bridge_reuses_plan_service_and_revision_gate(monkeypatch):
    from unittest.mock import AsyncMock

    from ttakkari.api import creative_v2
    from ttakkari.api.creative_mcp import Command, command

    approve = AsyncMock(return_value={"id": "plan", "revision": 4, "approval": {"revision": 4}})
    monkeypatch.setattr(creative_v2, "plan_approve", approve)
    result = await command(
        "plan_approve",
        Command(data={"plan_id": "00000000-0000-0000-0000-000000000001", "revision": 4}),
        "personal",
    )
    assert result["approval"]["revision"] == 4
    approve.assert_awaited_once()
    assert approve.await_args.args[1].revision == 4


def test_agent_comfy_api_binds_submit_to_personal_session_and_idempotency(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ttakkari.api import comfy_managed as api

    class Settings:
        data_dir = None

    class Service:
        def __init__(self):
            self.prepare_args = None
            self.started = []

        def prepare(self, profile, **kwargs):
            self.prepare_args = (profile, kwargs)
            return {"task_id": "a" * 32, "profile": profile, "status": "starting"}

        def start(self, task_id):
            self.started.append(task_id)

    service = Service()
    monkeypatch.setattr(api, "get_settings", lambda: Settings())
    monkeypatch.setattr(api, "default_controller", lambda _data_dir: service)
    app = FastAPI()
    app.include_router(api.agent_router)
    app.dependency_overrides[api.agent_access] = lambda: "agent:session-1"
    client = TestClient(app)
    response = client.post("/api/agent/comfy/submit", json={
        "profile_id": "wan22-5b-draft", "prompt": "scene", "seed": 6,
        "idempotency_key": "request-00001",
    })
    assert response.status_code == 202
    assert service.prepare_args == ("wan22-5b-draft", {
        "prompt": "scene", "seed": 6, "owner": "agent:session-1", "idempotency_key": "request-00001",
    })
    assert service.started == ["a" * 32]


def test_comfy_readiness_reports_no_device_as_not_ready_and_is_explicitly_host_measured(tmp_path, monkeypatch):
    from ttakkari.api import comfy_managed as api

    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '{\"torch\":\"test\",\"mps\":false,\"cuda\":false}'\n")
    executable.chmod(0o755)
    monkeypatch.setattr(api, "COMFY_ROOT", tmp_path)
    result = api._host_readiness()
    assert result["source"] == "backend_host"
    assert result["runtime"] == str(executable)
    assert result["status"] == "no_supported_device"
    assert result["ready"] is False
    assert result["device"] is None
    assert result["measured_at"]

