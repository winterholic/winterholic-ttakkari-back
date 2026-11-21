"""Stage3 lifecycle/consumer contracts; model fixtures are not inference evidence."""

import fcntl
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_creative_v2_core_integration import direction
from test_creative_v2_dispatch import approved, upload_all
from test_creative_v2_dispatch import stores as _stores

from ttakkari.creative.v2 import dispatch
from ttakkari.creative.v2.planner import ProductionDirection


@pytest.fixture
def stores(tmp_path):
    return _stores.__wrapped__(tmp_path)


def request():
    from ttakkari.creative.v2.storyboard import StoryboardRequest

    return StoryboardRequest(
        brief="기존 영상으로 세 장면의 짧은 소개 영상을 만든다",
        engine="codex",
        sources=[
            {
                "id": "source-1",
                "locator": "user-provided:reuse",
                "text": "기존 영상의 동작을 보존한다.",
            }
        ],
    )


def output():
    return {
        "summary": "기존 영상의 동작을 보존하는 소개",
        "catalog_ids": ["C08.three_act"],
        "knowledge": [
            {
                "source_id": "source-1",
                "quote": "동작을 보존한다",
                "application": "원본 영상을 재사용한다",
            }
        ],
        "scenes": [
            {
                "scene_id": f"i{i}",
                "title": f"장면 {i}",
                "narration": "",
                "seconds": 3,
                "strategy": "slideshow",
                "rationale": "기존 영상 재생과 자막만 합성한다",
                "source_ids": ["source-1"],
                "alternatives": [
                    {
                        "strategy": "generative_video",
                        "rationale": "이미 있는 생성 영상에만 사용 가능",
                    }
                ],
                "shots": [
                    {
                        "shot_id": f"shot-{i}",
                        "action": "기존 영상의 프레임 재생",
                        "layers": ["visual", "captions", "audio"],
                    }
                ],
            }
            for i in range(3)
        ],
    }


def test_storyboard_requires_grounded_sources_alternatives_and_explicit_review(stores):
    from ttakkari.creative.v2 import storyboard as sb

    plans, _ = stores
    body = request()
    raw = json.dumps(output(), ensure_ascii=False)
    draft = sb.parse_storyboard(body, raw)
    with pytest.raises(ValueError, match="review"):
        sb.materialize(
            plans,
            body,
            raw,
            run_id=str(uuid.uuid4()),
            model="fixture",
            reviewed_output_sha256="0" * 64,
        )
    rid = str(uuid.uuid4())
    plan = sb.materialize(
        plans,
        body,
        raw,
        run_id=rid,
        model="fixture",
        reviewed_output_sha256=sb.output_hash(raw),
    )
    assert not plan["render_gate"] and plan["approval"] is None
    assert plan["director"]["run_id"] == rid and plan["director"][
        "output_sha256"
    ] == sb.output_hash(raw)
    assert (
        plan["scenes"][0]["strategy_rationale"]["alternatives"][0]["reason"]
        == "이미 있는 생성 영상에만 사용 가능"
    )
    again = sb.materialize(
        plans,
        body,
        raw,
        run_id=rid,
        model="fixture",
        reviewed_output_sha256=sb.output_hash(raw),
    )
    assert again["id"] == plan["id"] and again["revision"] == plan["revision"]
    bad = output()
    bad["knowledge"][0]["quote"] = "출처에 없는 주장"
    with pytest.raises(ValueError):
        sb.parse_storyboard(body, json.dumps(bad))
    bad = output()
    bad["scenes"][0]["alternatives"] = []
    with pytest.raises(ValueError):
        sb.parse_storyboard(body, json.dumps(bad))
    assert draft.scenes[0].strategy == "slideshow"


@pytest.mark.parametrize(
    "case",
    [
        "unapproved",
        "stale",
        "cancelled",
        "duplicate",
        "asset_changed",
        "file_tampered",
        "lease",
        "remote",
    ],
)
async def test_render_consumer_admission_is_fenced(stores, tmp_path, monkeypatch, case):
    from ttakkari.creative.v2 import render_worker as rw

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(direction(("slideshow",) * 3)),
    )
    upload_all(plans, plan, tmp_path)
    dispatch.dispatch(plans, studio, plan["id"], 1)
    if case == "unapproved":
        plans.edit(plan["id"], 1, caption_updates={"i0": {"title": "수정"}})
        with pytest.raises(ValueError):
            rw.enqueue(plans, plan["id"], 2)
        return
    first = rw.enqueue(plans, plan["id"], 1)
    assert rw.enqueue(plans, plan["id"], 1)["id"] == first["id"]
    called = []

    def render(*args):
        called.append(1)
        return {"state": "succeeded", "final": {"sha256": "fixture"}}

    monkeypatch.setattr(dispatch, "request_render", render)
    held = None
    if case == "stale":
        plans.edit(plan["id"], 1, caption_updates={"i0": {"title": "수정"}})
    elif case == "cancelled":
        rw.cancel(plans, plan["id"], 1)
    elif case == "asset_changed":
        with dispatch.ProductionStore(plans).tx(plan["id"]) as data:
            data["assets"]["music"]["sha256"] = "tampered"
    elif case == "file_tampered":
        store = dispatch.ProductionStore(plans)
        data = store.read(plan["id"])
        store.asset_path(plan["id"], data["assets"]["music"]).write_bytes(b"tampered")
    elif case == "lease":
        held = (plans._folder(plan["id"]) / "render-consumer.lock").open("a")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif case == "remote":
        monkeypatch.setattr(rw.queued_work, "delegated", lambda: True)
    try:
        await rw.consume_once(plans=plans, studio=studio)
        await rw.consume_once(plans=plans, studio=studio)
    finally:
        if held:
            held.close()
    assert len(called) == (1 if case == "duplicate" else 0)
    data = dispatch.ProductionStore(plans).read(plan["id"])
    state = data["dispatches"][0]["render_request"]["status"]
    assert (
        state
        == {
            "duplicate": "succeeded",
            "stale": "blocked",
            "cancelled": "cancelled",
            "asset_changed": "blocked",
            "file_tampered": "blocked",
            "lease": "queued",
            "remote": "queued",
        }[case]
    )


async def test_existing_asset_consumer_reaches_render_work(
    stores, tmp_path, monkeypatch
):
    from ttakkari.creative.v2 import asset_worker, render_worker

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(direction(("slideshow",) * 3)),
    )
    upload_all(plans, plan, tmp_path)
    dispatch.dispatch(plans, studio, plan["id"], 1)
    render_worker.enqueue(plans, plan["id"], 1)
    monkeypatch.setattr(render_worker, "default_studio", lambda: studio)
    monkeypatch.setattr(
        dispatch, "request_render", lambda *a: {"state": "succeeded", "final": {}}
    )
    assert await asset_worker.consume_once(plans=plans) is True
    assert (
        dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0][
            "render_request"
        ]["status"]
        == "succeeded"
    )


async def test_storyboard_run_reuses_queue_and_resume_preserves_source_context(
    monkeypatch,
):
    from ttakkari.creative.v2 import storyboard_runs as life
    from ttakkari.models import ChatSession, Run, RunStatus

    req = request()
    records = {}
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(side_effect=lambda cls, ident: records.get((cls, ident))),
        flush=AsyncMock(),
        commit=AsyncMock(),
        add=lambda s: None,
    )

    async def queue(db, session, prompt, **kw):
        if session.id is None:
            session.id = uuid.uuid4()
        run = Run(
            id=uuid.uuid4(),
            session_id=session.id,
            status=RunStatus.queued,
            engine=session.engine,
            model=kw.get("model"),
            prompt=prompt,
        )
        kw["on_queued"](run)
        records[(Run, run.id)] = run
        records[(ChatSession, session.id)] = session
        return run

    monkeypatch.setattr(life, "queue_run", queue)
    monkeypatch.setattr(
        life,
        "ensure_default_workspace",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )
    monkeypatch.setattr(
        life,
        "get_settings",
        lambda: SimpleNamespace(
            engine_models={"codex": ["fixture"]},
            worker_default_models={"codex": {"model": "fixture"}},
        ),
    )
    started = await life.start(db, req, request_key="first")
    run = records[(Run, uuid.UUID(started["run_id"]))]
    assert run.usage["creative_storyboard"]["request"]["sources"][0]["id"] == "source-1"
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await life.resume(db, run.id)
    run.status = RunStatus.failed
    run.error = "fixture failure"
    read = await life.read(db, run.id)
    assert read["phase"] == "failed" and read["error"] == "fixture failure"
    resumed = await life.resume(db, run.id, request_key="retry")
    assert resumed["resumed_from"] == str(run.id)
    latest = records[(Run, uuid.UUID(resumed["run_id"]))]
    assert latest.prompt == run.prompt
    latest.status = RunStatus.succeeded
    latest.result_text = json.dumps(output())
    parsed = await life.read(db, latest.id)
    assert parsed["phase"] == "completed_needs_review" and parsed["review_required"]


def test_unverified_producers_are_blocked_without_runtime(stores, monkeypatch):
    from ttakkari.creative.v2 import engines, render_worker

    plans, _ = stores
    directed = direction(("code_motion",) * 3)
    for s in directed["scenes"]:
        s["preset"] = "kafka_flow"
    plan = approved(plans, director=ProductionDirection.model_validate(directed))
    monkeypatch.setattr(
        engines.ModuleAdapter,
        "preflight",
        lambda *_: pytest.fail("forbidden producer preflight"),
    )
    assert render_worker.enqueue(plans, plan["id"], 1)["status"] == "blocked"
    assert not dispatch.ProductionStore(plans).read(plan["id"])["dispatches"]


async def test_remote_render_api_queues_without_running_native_preflight(
    stores, tmp_path, monkeypatch
):
    import httpx
    from fastapi import FastAPI

    from ttakkari.api import creative_v2 as api
    from ttakkari.creative.v2 import engines
    from ttakkari.security.auth import require_user

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(direction(("slideshow",) * 3)),
    )
    upload_all(plans, plan, tmp_path)
    monkeypatch.setattr(dispatch.queued_work, "delegated", lambda: True)
    monkeypatch.setattr(
        engines.V1SlideshowAdapter,
        "preflight",
        lambda *_: pytest.fail("API must defer runtime preflight"),
    )
    monkeypatch.setattr(api, "default_plan_store", lambda: plans)
    monkeypatch.setattr(api, "default_studio", lambda: studio)
    app = FastAPI()
    app.include_router(api.router)
    base = f"/api/creative/v2/plans/{plan['id']}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(base + "/production/render", json={"revision": 1})
        ).status_code == 401
        app.dependency_overrides[require_user] = lambda: "owner"
        dispatched = await client.post(base + "/production", json={"revision": 1})
        assert dispatched.status_code == 201, dispatched.text
        result = await client.post(base + "/production/render", json={"revision": 1})
        assert result.status_code == 202, result.text
        assert result.json()["render_request"]["status"] == "queued"
        cancel = await client.post(
            base + "/production/render/cancel", json={"revision": 1}
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "cancelled"
    assert not studio.list()


async def test_adoption_binds_database_run_prompt_and_accepts_only_succeeded_output(
    stores,
):
    from fastapi import HTTPException

    from ttakkari.creative.v2 import storyboard as sb
    from ttakkari.creative.v2 import storyboard_runs as life
    from ttakkari.models import ChatSession, Run, RunStatus

    plans, _ = stores
    req = request()
    sid = uuid.uuid4()
    session = ChatSession(id=sid, pool="personal", engine="codex")
    run = Run(
        id=uuid.uuid4(),
        session_id=sid,
        engine="codex",
        model="fixture",
        prompt="forged",
        status=RunStatus.succeeded,
        result_text=json.dumps(output()),
        usage={},
    )
    records = {(Run, run.id): run, (ChatSession, sid): session}
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda cls, ident: records.get((cls, ident))),
        commit=AsyncMock(),
    )
    with pytest.raises(HTTPException):
        await life.adopt(db, run.id, req)
    run.prompt = sb.build_prompt(req)
    adopted = await life.adopt(db, run.id, req)
    assert adopted["phase"] == "completed_needs_review"
    run.status = RunStatus.cancelled
    with pytest.raises(HTTPException):
        await life.accept(
            db, plans, run.id, reviewed_output_sha256=adopted["output_sha256"]
        )
    assert (await life.read(db, run.id))["phase"] == "cancelled"
    run.status = RunStatus.succeeded
    plan = await life.accept(
        db, plans, run.id, reviewed_output_sha256=adopted["output_sha256"]
    )
    assert not plan["render_gate"]
    run.result_text = json.dumps({**output(), "summary": "changed after review"})
    with pytest.raises(ValueError):
        await life.accept(
            db, plans, run.id, reviewed_output_sha256=adopted["output_sha256"]
        )


async def test_qwen_disabled_fails_without_activation_mutation(monkeypatch):
    from fastapi import HTTPException

    from ttakkari.creative.v2 import storyboard_runs as life
    from ttakkari.models import ChatSession

    req = request().model_copy(update={"engine": "qwen-local"})
    monkeypatch.setattr(
        life,
        "get_settings",
        lambda: SimpleNamespace(
            engine_models={"qwen-local": ["fixture"]}, worker_default_models={}
        ),
    )
    monkeypatch.setattr(
        life.qwen_activation,
        "status",
        AsyncMock(return_value={"ready": False, "reason": "disabled"}),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(life, "queue_run", enqueue)
    with pytest.raises(HTTPException) as exc:
        await life._queue(SimpleNamespace(), ChatSession(engine="qwen-local"), req)
    assert exc.value.status_code == 503 and exc.value.detail["reason"] == "disabled"
    enqueue.assert_not_called()


def test_render_running_cancel_does_not_lie_about_execution(
    stores, tmp_path, monkeypatch
):
    from ttakkari.creative.v2 import render_worker as rw

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(direction(("slideshow",) * 3)),
    )
    upload_all(plans, plan, tmp_path)
    dispatch.dispatch(plans, studio, plan["id"], 1)
    rw.enqueue(plans, plan["id"], 1)
    with dispatch.ProductionStore(plans).tx(plan["id"]) as data:
        data["dispatches"][0]["render_request"]["status"] = "running"
    with pytest.raises(ValueError, match="cannot be cancelled"):
        rw.cancel(plans, plan["id"], 1)
    assert (
        dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0][
            "render_request"
        ]["status"]
        == "running"
    )


async def test_storyboard_http_auth_strict_input_and_cancel_reuse(stores, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from ttakkari.api import creative_v2 as api
    from ttakkari.api import sessions
    from ttakkari.db import get_db
    from ttakkari.security.auth import require_user

    plans, _ = stores
    monkeypatch.setattr(api, "default_plan_store", lambda: plans)
    app = FastAPI()
    app.include_router(api.router)

    async def fake_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = fake_db
    rid = uuid.uuid4()
    started = {"run_id": str(rid), "status": "queued"}
    monkeypatch.setattr(api.storyboard_runs, "start", AsyncMock(return_value=started))
    monkeypatch.setattr(api.storyboard_runs, "assert_owned_run", AsyncMock())
    cancelled = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr(sessions, "cancel_run", cancelled)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/api/creative/v2/storyboard/runs",
                json=request().model_dump(mode="json"),
            )
        ).status_code == 401
        app.dependency_overrides[require_user] = lambda: "owner"
        response = await client.post(
            "/api/creative/v2/storyboard/runs", json=request().model_dump(mode="json")
        )
        assert response.status_code == 202 and response.json()["run_id"] == str(rid)
        forged = await client.post(
            f"/api/creative/v2/storyboard/runs/{rid}/accept",
            json={
                "reviewed_output_sha256": "a" * 64,
                "result_text": "forged client output",
            },
        )
        assert forged.status_code == 422
        response = await client.post(f"/api/creative/v2/storyboard/runs/{rid}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        cancelled.assert_awaited_once()


async def test_draining_preserves_queued_render_without_claim(
    stores, tmp_path, monkeypatch
):
    from ttakkari.creative.v2 import render_worker as rw

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(direction(("slideshow",) * 3)),
    )
    upload_all(plans, plan, tmp_path)
    dispatch.dispatch(plans, studio, plan["id"], 1)
    rw.enqueue(plans, plan["id"], 1)
    monkeypatch.setattr(rw.queued_work, "draining", lambda: True)
    monkeypatch.setattr(
        dispatch,
        "request_render",
        lambda *_: pytest.fail("draining must not start a renderer"),
    )
    assert await rw.consume_once(plans=plans, studio=studio) is False
    assert (
        dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0][
            "render_request"
        ]["status"]
        == "queued"
    )


async def test_executor_timeout_is_recorded_failed_without_duplicate_retry(
    stores, tmp_path, monkeypatch
):
    import subprocess

    from ttakkari.creative.v2 import render_worker as rw

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(direction(("slideshow",) * 3)),
    )
    upload_all(plans, plan, tmp_path)
    dispatch.dispatch(plans, studio, plan["id"], 1)
    rw.enqueue(plans, plan["id"], 1)
    calls = []

    def timeout(*args):
        calls.append(1)
        raise subprocess.TimeoutExpired("ffmpeg-fixture", 1)

    monkeypatch.setattr(dispatch, "request_render", timeout)
    assert await rw.consume_once(plans=plans, studio=studio) is True
    assert await rw.consume_once(plans=plans, studio=studio) is False
    request_state = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0][
        "render_request"
    ]
    assert (
        request_state["status"] == "failed"
        and "TimeoutExpired" in request_state["error"]
    )
    assert len(calls) == 1
