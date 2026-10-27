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
        "creative_v2_auto_assets_status", "creative_v2_auto_assets_start",
        "creative_v2_production_dispatch",
        "creative_v2_production_render", "creative_v2_production_status",
        "creative_v2_production_media_link",
    }
    assert expected <= tools.keys()
    assert "prompt" in tools["comfy_submit"].input_schema["properties"]
    assert "revision" in tools["creative_v2_plan_approve"].input_schema["properties"]
    dispatch_schema = tools["creative_v2_production_dispatch"].input_schema
    assert dispatch_schema["properties"]["caption_mode"]["default"] == "narration"
    assert set(dispatch_schema["properties"]["caption_mode"]["enum"]) == {"narration", "title_only"}
    plan_create_schema = tools["creative_v2_plan_create"].input_schema
    style_items = plan_create_schema["properties"]["style"]["anyOf"][0]["items"]
    assert style_items["$ref"] == "#/$defs/CreativeStyleRef"
    assert plan_create_schema["$defs"]["CreativeStyleRef"]["required"] == ["style_id", "version"]

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

    call.reset_mock()
    await ms.server.call_tool("creative_v2_auto_assets_start", {
        "plan_id": "00000000-0000-0000-0000-000000000001", "revision": 3,
    })
    call.assert_awaited_once_with("POST", "/auto_assets_start", {"data": {
        "plan_id": "00000000-0000-0000-0000-000000000001", "revision": 3,
    }}, prefix="/api/agent/creative-v2")

    call.reset_mock()
    await ms.server.call_tool("creative_v2_production_dispatch", {
        "plan_id": "00000000-0000-0000-0000-000000000001", "revision": 3,
        "caption_mode": "title_only",
    })
    call.assert_awaited_once_with("POST", "/production_dispatch", {"data": {
        "plan_id": "00000000-0000-0000-0000-000000000001", "revision": 3,
        "caption_mode": "title_only",
    }}, prefix="/api/agent/creative-v2")

    call.reset_mock()
    await ms.server.call_tool("creative_v2_plan_create", {
        "brief": "Kafka consumer group 동작을 설명한다.",
        "style": [{"style_id": "neutral_base", "version": "1.0.0"}],
    })
    call.assert_awaited_once_with("POST", "/plan_create", {"data": {
        "brief": "Kafka consumer group 동작을 설명한다.", "name": "", "intent": None,
        "duration": None, "aspect": None,
        "style": [{"style_id": "neutral_base", "version": "1.0.0"}], "resolutions": [],
    }}, prefix="/api/agent/creative-v2")

    from mcp.server.mcpserver.exceptions import ToolError

    call.reset_mock()
    with pytest.raises(ToolError, match="style.0.style_id"):
        await ms.server.call_tool("creative_v2_plan_create", {
            "brief": "Kafka consumer group 동작을 설명한다.",
            "style": [{"entry_id": "C03.vector_motion_graphics"}],
        })
    call.assert_not_awaited()


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
async def test_agent_creative_and_comfy_access_allows_work_sessions():
    from types import SimpleNamespace

    from ttakkari.api.comfy_managed import agent_access as comfy_access
    from ttakkari.api.creative_mcp import agent_access as studio_access

    sid = "00000000-0000-0000-0000-000000000001"

    class Db:
        async def get(self, *_):
            return SimpleNamespace(pool="work")

    assert await comfy_access(sid, Db()) == f"agent:{sid}"
    assert await studio_access(sid, Db()) == "personal"


@pytest.mark.asyncio
async def test_creative_bridge_fails_closed_for_unknown_operations_and_reports_auto_asset_contract():
    from fastapi import HTTPException

    from ttakkari.api.creative_mcp import Command, command

    with pytest.raises(HTTPException) as e:
        await command("shell", Command(data={}), "personal")
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await command("plan", Command(data={"plan_id": "00000000-0000-0000-0000-000000000001", "path": "/tmp/x"}), "personal")
    assert e.value.status_code == 422
    available = await command("auto_assets_status", Command(), "personal")
    assert available["available"] is True
    assert available["supported_recipes"] == ["R21"]
    assert available["cancel_available"] is False


@pytest.mark.asyncio
async def test_plan_create_rejects_catalog_entries_as_style_refs_with_422_without_echoing_input(monkeypatch):
    from fastapi import HTTPException

    from ttakkari.api import creative_v2
    from ttakkari.api.creative_mcp import Command, command

    plan_create = AsyncMock()
    monkeypatch.setattr(creative_v2, "plan_create", plan_create)

    with pytest.raises(HTTPException) as exc:
        await command("plan_create", Command(data={
            "brief": "Kafka consumer group 동작을 세 장면으로 설명한다.",
            "style": [{"entry_id": "C03.vector_motion_graphics"}],
        }), "personal")

    assert exc.value.status_code == 422
    assert "style.0.style_id" in exc.value.detail
    assert "style.0.version" in exc.value.detail
    assert "C03.vector_motion_graphics" not in exc.value.detail
    plan_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_create_accepts_versioned_style_refs(monkeypatch):
    from ttakkari.api import creative_v2
    from ttakkari.api.creative_mcp import Command, command

    plan_create = AsyncMock(return_value={"id": "plan-1", "revision": 1})
    monkeypatch.setattr(creative_v2, "plan_create", plan_create)

    result = await command("plan_create", Command(data={
        "brief": "Kafka consumer group 동작을 세 장면으로 설명한다.",
        "style": [{"style_id": "neutral_base", "version": "1.0.0"}],
    }), "personal")

    assert result == {"id": "plan-1", "revision": 1}
    model, owner = plan_create.await_args.args
    assert [(ref.style_id, ref.version) for ref in model.style] == [("neutral_base", "1.0.0")]
    assert owner == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize(("brief", "seconds", "style_id"), [
    ("Kafka consumer group의 작동 원리를 한국어로 설명한다.", 20, "anime_cel"),
    ("교토 골목을 걷는 여행 브이로그를 만든다.", 60, "sf_world"),
    ("휴대용 커피 메이커의 장점을 소개한다.", 150, "neutral_base"),
    ("비 오는 밤의 짧은 감성 이야기를 만든다.", 60, "horror_lighting"),
    ("오늘의 과학 뉴스를 간결하게 요약한다.", 150, "cute_3d"),
])
async def test_plan_create_contract_accepts_varied_briefs_lengths_and_styles(
    monkeypatch, brief, seconds, style_id,
):
    from ttakkari.api import creative_v2
    from ttakkari.api.creative_mcp import Command, command

    plan_create = AsyncMock(return_value={"id": "plan-1", "revision": 1})
    monkeypatch.setattr(creative_v2, "plan_create", plan_create)

    await command("plan_create", Command(data={
        "brief": brief,
        "duration": {"mode": "preferred", "seconds": seconds},
        "aspect": "16:9",
        "style": [{"style_id": style_id, "version": "1.0.0"}],
    }), "personal")

    model, _owner = plan_create.await_args.args
    assert model.duration.seconds == seconds
    assert [(ref.style_id, ref.version) for ref in model.style] == [(style_id, "1.0.0")]


@pytest.mark.asyncio
async def test_auto_asset_start_and_retry_reuse_approved_revision_route(monkeypatch):
    import uuid
    from unittest.mock import AsyncMock

    from ttakkari.api import creative_v2
    from ttakkari.api.creative_mcp import Command, command

    plan_id = "00000000-0000-0000-0000-000000000001"
    start = AsyncMock(return_value={"plan_id": plan_id, "plan_revision": 3, "status": "queued"})
    status = AsyncMock(return_value={"asset_generation": {"status": "failed"}})
    monkeypatch.setattr(creative_v2, "production_generate_assets", start)
    monkeypatch.setattr(creative_v2, "production_get", status)

    result = await command("auto_assets_start", Command(data={"plan_id": plan_id, "revision": 3}), "personal")
    assert result["status"] == "queued"
    assert start.await_args.args[0] == uuid.UUID(plan_id)
    assert start.await_args.args[1].revision == 3

    start.reset_mock()
    result = await command("auto_assets_retry", Command(data={"plan_id": plan_id, "revision": 3}), "personal")
    assert result["status"] == "queued"
    status.assert_awaited_once()
    assert start.await_args.args[1].revision == 3


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



def _motion_doc(version="1.0.0", animation="pan", transition="cut", caption="center_highlight"):
    return {
        "style_id": "probe_motion",
        "version": version,
        "parent": {"style_id": "neutral_base", "version": "1.0.0"},
        "overrides": [
            {"field": "motion_language", "reason": "장면 움직임과 전환을 편별로 구분한다"},
            {"field": "visual_identity", "reason": "자막 스타일을 편별로 구분한다"},
        ],
        "motion_language": {"text": "느린 패닝과 컷 전환", "params": {"animation": animation, "transition": transition}},
        "visual_identity": {"text": "중앙 강조 자막", "params": {"caption_style": caption}},
    }


@pytest.fixture
def style_store(tmp_path, monkeypatch):
    from ttakkari.api import creative_mcp, creative_v2
    from ttakkari.creative.v2.style_dna import StyleStore, seed_docs

    store = StyleStore(tmp_path / "styles", seeds=seed_docs())
    monkeypatch.setattr(creative_mcp, "default_style_store", lambda: store)
    monkeypatch.setattr(creative_v2, "default_style_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_style_tools_are_registered_with_safe_schemas():
    from ttakkari.orchestrator import mcp_server as ms
    from ttakkari.orchestrator.tool_policy import CREATIVE_TOOLS, READ_ONLY_TOOLS

    tools = {tool.name: tool for tool in await ms.server.list_tools()}
    assert {"creative_v2_styles", "creative_v2_style", "creative_v2_style_put"} <= tools.keys()
    assert {"creative_v2_styles", "creative_v2_style"} <= READ_ONLY_TOOLS
    assert "creative_v2_style_put" in CREATIVE_TOOLS and "creative_v2_style_put" not in READ_ONLY_TOOLS
    # 경로·URL·임의 실행 입력이 없고, DNA 문서도 서버의 엄격한 계약으로 제한해야 한다.
    assert set(tools["creative_v2_style_put"].input_schema["properties"]) == {"doc", "dry_run"}
    doc_schema = tools["creative_v2_style_put"].input_schema["$defs"]["StyleDNADoc"]
    assert doc_schema["additionalProperties"] is False
    assert doc_schema["required"] == ["style_id", "version"]
    assert "additionalProperties" not in tools["creative_v2_style_put"].input_schema["properties"]["doc"]
    assert set(tools["creative_v2_style"].input_schema["properties"]) == {"style_id", "version"}


@pytest.mark.asyncio
async def test_style_dna_can_be_registered_previewed_and_pinned_through_the_agent_bridge(style_store):
    from fastapi import HTTPException

    from ttakkari.api.creative_mcp import Command, command

    listing = await command("styles", Command(), "personal")
    controls = listing["render_controls"]["controls"]
    assert controls["animation"] == {
        "facet": "motion_language", "param": "animation",
        "allowed": ["still", "zoom_in", "zoom_out", "pan", "pan_down", "pulse"],
    }
    assert controls["transition"]["allowed"] == ["fade", "cut"]
    assert controls["caption_style"]["facet"] == "visual_identity"
    assert {s["style_id"] for s in listing["styles"]} >= {"neutral_base", "anime_cel"}

    dry = await command("style_put", Command(data={"doc": _motion_doc(), "dry_run": True}), "personal")
    assert dry["valid"] is True and dry["dry_run"] is True
    assert dry["render_effects"]["applied"] == {
        "animation": "pan", "transition": "cut", "caption_style": "center_highlight",
    }
    assert style_store.versions("probe_motion") == []  # dry run 은 저장하지 않는다.

    saved = await command("style_put", Command(data={"doc": _motion_doc()}), "personal")
    assert saved["saved"] is True and saved["ref"] == "probe_motion@1.0.0"
    read = await command("style", Command(data={"style_id": "probe_motion", "version": "latest"}), "personal")
    assert read["ref"] == "probe_motion@1.0.0"
    assert read["render_effects"]["applied"]["animation"] == "pan"
    assert read["dna"]["motion_language"]["params"] == {"animation": "pan", "transition": "cut"}

    # 같은 내용은 멱등, 같은 버전의 다른 내용과 시드 덮어쓰기는 거부한다.
    again = await command("style_put", Command(data={"doc": _motion_doc()}), "personal")
    assert again["saved"] is True
    with pytest.raises(HTTPException) as changed:
        await command("style_put", Command(data={"doc": _motion_doc(animation="zoom_out")}), "personal")
    assert changed.value.status_code == 422 and "불변" in changed.value.detail
    seed = _motion_doc()
    seed["style_id"], seed["version"] = "anime_cel", "1.0.0"
    with pytest.raises(HTTPException):
        await command("style_put", Command(data={"doc": seed}), "personal")
    assert style_store.versions("anime_cel") == ["1.0.0"]


@pytest.mark.asyncio
async def test_style_preview_reports_values_the_renderer_does_not_accept_instead_of_claiming_support(style_store):
    from ttakkari.api.creative_mcp import Command, command

    dry = await command("style_put", Command(data={
        "doc": _motion_doc(animation="dolly", transition="wipe", caption="neon"), "dry_run": True,
    }), "personal")

    assert dry["valid"] is True  # 저장 가능한 DNA 와 렌더러가 소비하는 값은 별개다.
    assert dry["render_effects"]["applied"] == {}
    assert any("animation" in i and "dolly" in i for i in dry["render_effects"]["ignored"])
    assert any("transition" in i and "wipe" in i for i in dry["render_effects"]["ignored"])


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"doc": {**_motion_doc(), "script": "rm -rf /"}},
    {"doc": {**_motion_doc(), "reference_assets": [{"kind": "image", "ref": "../../etc/passwd", "sha256": "a" * 64}]}},
    {"doc": _motion_doc(), "path": "/tmp/x"},
    {"doc": _motion_doc(), "dry_run": "yes"},
    {"doc": None},
])
async def test_style_put_rejects_unknown_fields_unsafe_refs_and_bad_flags(style_store, data):
    from fastapi import HTTPException

    from ttakkari.api.creative_mcp import Command, command

    with pytest.raises(HTTPException) as exc:
        await command("style_put", Command(data=data), "personal")
    assert exc.value.status_code == 422
    assert "rm -rf" not in exc.value.detail
    assert style_store.versions("probe_motion") == []


@pytest.mark.asyncio
async def test_style_read_rejects_non_slug_ids(style_store):
    from fastapi import HTTPException

    from ttakkari.api.creative_mcp import Command, command

    for data in ({"style_id": "../x"}, {"style_id": "neutral_base", "version": "1"}):
        with pytest.raises(HTTPException) as exc:
            await command("style", Command(data=data), "personal")
        assert exc.value.status_code == 422
