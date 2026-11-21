"""MCP bridge keeps pace with the director/render-worker API contracts."""

import pytest
from pydantic import ValidationError
from test_creative_v2_dispatch import approved, upload_all
from test_creative_v2_dispatch import stores as _stores_fixture
from test_creative_v2_stage3 import direction

from ttakkari.creative.v2 import dispatch
from ttakkari.creative.v2.planner import ProductionDirection


@pytest.fixture
def stores(tmp_path):
    return _stores_fixture.__wrapped__(tmp_path)


def _bind(monkeypatch, plans, studio):
    from ttakkari.api import creative_v2 as api

    monkeypatch.setattr(api, "default_plan_store", lambda: plans)
    monkeypatch.setattr(api, "default_studio", lambda: studio)
    monkeypatch.setattr(dispatch.queued_work, "delegated", lambda: True)


async def _call(operation, **data):
    from ttakkari.api import creative_mcp as mcp

    return await mcp.command(operation, mcp.Command(data=data), "personal")


async def test_mcp_render_of_director_plan_on_delegated_executor_queues_instead_of_crashing(
    stores, tmp_path, monkeypatch
):
    plans, studio = stores
    plan = approved(
        plans, director=ProductionDirection.model_validate(direction(("slideshow",) * 3))
    )
    upload_all(plans, plan, tmp_path)
    _bind(monkeypatch, plans, studio)
    await _call("production_dispatch", plan_id=plan["id"], revision=1)

    queued = await _call("production_render", plan_id=plan["id"], revision=1)
    assert queued["render_request"]["status"] == "queued"
    assert queued["render_request"].get("retry") in (None, False)
    assert not studio.list()


async def test_mcp_render_accepts_retry_and_cancel_for_director_plans(
    stores, tmp_path, monkeypatch
):
    plans, studio = stores
    plan = approved(
        plans, director=ProductionDirection.model_validate(direction(("slideshow",) * 3))
    )
    upload_all(plans, plan, tmp_path)
    _bind(monkeypatch, plans, studio)
    await _call("production_dispatch", plan_id=plan["id"], revision=1)
    await _call("production_render", plan_id=plan["id"], revision=1)

    cancelled = await _call("production_render_cancel", plan_id=plan["id"], revision=1)
    assert cancelled["status"] == "cancelled"
    retried = await _call("production_render", plan_id=plan["id"], revision=1, retry=True)
    assert retried["render_request"]["status"] == "queued"


async def test_mcp_render_without_retry_keeps_existing_call_shape(stores, tmp_path, monkeypatch):
    from ttakkari.api import creative_mcp as mcp

    plans, studio = stores
    plan = approved(plans)
    upload_all(plans, plan, tmp_path)
    _bind(monkeypatch, plans, studio)
    monkeypatch.setattr(dispatch.queued_work, "delegated", lambda: False)
    await _call("production_dispatch", plan_id=plan["id"], revision=1)

    rendered = await _call("production_render", plan_id=plan["id"], revision=1)
    assert rendered["plan_id"] == plan["id"]
    assert rendered["state"]
    assert "production_render" in mcp.AVAILABLE_OPERATIONS


async def test_mcp_exposes_review_repair_and_revision_operations(stores, tmp_path, monkeypatch):
    from fastapi import HTTPException

    from ttakkari.api import creative_mcp as mcp

    plans, studio = stores
    plan = approved(plans)
    _bind(monkeypatch, plans, studio)
    for operation in (
        "production_review", "production_repair_plan", "production_repair",
        "production_revision", "production_result_link", "production_render_cancel",
    ):
        assert operation in mcp.AVAILABLE_OPERATIONS

    with pytest.raises(HTTPException) as unknown:
        await _call("production_review", plan_id=plan["id"], revision=1, retry=True)
    assert unknown.value.status_code == 422
    with pytest.raises(HTTPException) as missing:
        await _call("production_revision", plan_id=plan["id"], revision=1)
    assert missing.value.status_code == 404
    with pytest.raises(ValidationError, match="defects"):
        await _call("production_repair_plan", plan_id=plan["id"], revision=1, defects=[])
