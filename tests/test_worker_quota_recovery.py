"""Deterministic regression tests for worker quota waiting, automatic recovery, and local Qwen fallback."""
import asyncio
import os
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import AGENT_LOG, agent_calls
from helpers import wait_run, wait_status
from test_orchestrator import _orch, _session

from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    Run,
    RunStatus,
    Workspace,
    utcnow,
)
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles, usage, worker_routing
from ttakkari.runs.manager import (
    QWEN_LOCAL_ADAPTER,
    _reconsider_quota_waits,
    manager,
    resume_queued,
)


def quota(aid, *, claude=80, gemini=90, argon=70):
    now = time.time()
    windows = [
        usage.Window(f"{group}_{span}", group, round(100 - left), int(now + 1000), left)
        for group, left in [
            ("agy_claude_and_gpt_models", claude),
            ("agy_gemini_models", gemini),
            ("agy_gemini_4_argon", argon),
        ]
        for span in ("5h", "weekly")
    ]
    usage.seed(
        aid,
        usage.Usage(windows=windows, updated_at=now, source="antigravity_cli", credits_balance=0),
        refresh=True,
    )


@pytest.fixture
async def accounts(client, auth):
    await client.get("/api/accounts", headers=auth)
    agy = (await client.post("/api/accounts", json={"name": "agy", "engine": "antigravity"}, headers=auth)).json()
    quota(uuid.UUID(agy["id"]))
    rows = (await client.get("/api/accounts", headers=auth)).json()
    for a in rows:
        if a["engine"] != "antigravity":
            usage.seed(
                uuid.UUID(a["id"]),
                usage.Usage(
                    windows=[usage.Window("five_hour", "5h", 40, int(time.time() + 1000))],
                    updated_at=time.time(),
                ),
            )
    return rows


@pytest.fixture
def synthetic(monkeypatch):
    class SyntheticAdapter(ClaudeAdapter):
        def parse_line(self, obj, state, cwd):
            if obj.get("type") == "rate_limit_event" and obj.get("rate_limit_info", {}).get("status") == "allowed":
                return []
            return super().parse_line(obj, state, cwd)

        def build_command(self, spec, env):
            limit_model = env.get("WORKER_TEST_LIMIT_MODEL")
            if limit_model == spec.model:
                spec = replace(spec, prompt=spec.prompt + "\nCLAUDE_LIMIT")
            return super().build_command(spec, env)

    import importlib
    m = importlib.import_module("ttakkari.runs.manager")
    for engine in ("claude", "codex", "antigravity"):
        monkeypatch.setitem(m.ADAPTERS, engine, SyntheticAdapter())


async def test_qwen_local_routing_contract_and_boundary():
    """Qwen has no auto stage: the only entry is the emergency path (trigger emergency)."""
    # 1. Absent from every auto route (반례 2: 평시 auto에서 명시 지정만).
    for task, stages in worker_routing.ROUTES.items():
        assert not any(engine == "qwen-local" for stage in stages for engine, _ in stage), (
            f"qwen-local must not be an auto stage of {task}"
        )

    # 2. Emergency profile carries the configured model (반례 3의 전제).
    settings = get_settings()
    assert worker_routing.qwen_local_model() == settings.worker_default_models["qwen-local"]["model"]
    prof = worker_routing.emergency_qwen_profile("personal")
    assert (prof.engine, prof.model) == ("qwen-local", worker_routing.qwen_local_model())

    # 3. Model engine recognition
    assert profiles.model_engine("Qwen3.8-Flash-Next-Q2_K") == "qwen-local"

    # 4. Independent verification requirement
    assert worker_routing.is_flash_model("Qwen3.8-Flash-Next-Q2_K") is True
    assert worker_routing.needs_independent_verification("routine", "Qwen3.8-Flash-Next-Q2_K") is True
    assert worker_routing.needs_independent_verification("general", "Qwen3.8-Flash-Next-Q2_K") is True

    # 5. Capacity formatting
    cap = worker_routing.capacity(prof, running=0)
    assert cap["engine"] == "qwen-local"
    assert cap["quota_known"] is True
    assert cap["remaining_to_limit_percent"] == 100.0


async def test_qwen_local_availability_never_lists_qwen_for_any_pool(monkeypatch):
    """availability() is cloud-only; Qwen enters only via _emergency_qwen (반례 2·4)."""
    from types import SimpleNamespace

    from ttakkari.runs import manager as run_manager

    async with sessionmaker()() as db:
        # Ready Qwen still does not appear in auto availability, personal or work.
        monkeypatch.setattr(QWEN_LOCAL_ADAPTER, "readiness", lambda: (True, "ready"))
        for pool in ("personal", "work"):
            free, _blocked = await worker_routing.availability(db, "routine", pool)
            assert not any(p.engine == "qwen-local" for p in free), pool

        def sess(**kw):
            base = {"pool": "personal", "worker_task_type": "routine",
                    "strict_engine": False, "kind": "worker"}
            base.update(kw)
            return SimpleNamespace(**base)

        # Emergency fires for personal routine workers when ready and gated in.
        emergency, state = await run_manager.manager._emergency_qwen(sess(), "routine")
        assert state == "ready" and emergency is not None
        assert (emergency.engine, emergency.model) == ("qwen-local", worker_routing.qwen_local_model())

        # Work pool never leaks to the personal local model (반례 4).
        emergency, state = await run_manager.manager._emergency_qwen(sess(pool="work"), "routine")
        assert (emergency, state) == (None, "ineligible")

        # engine_models gate is honored (반례 3).
        settings = get_settings()
        monkeypatch.setattr(settings, "engine_models", {**settings.engine_models, "qwen-local": []})
        emergency, state = await run_manager.manager._emergency_qwen(sess(), "routine")
        assert (emergency, state) == (None, "qwen_model_gated")

        # Unready Qwen reports its reason instead of a profile.
        monkeypatch.setattr(QWEN_LOCAL_ADAPTER, "readiness", lambda: (False, "readiness_contract_unverified"))
        monkeypatch.setattr(settings, "engine_models", {**settings.engine_models,
                                                        "qwen-local": [worker_routing.qwen_local_model()]})
        emergency, state = await run_manager.manager._emergency_qwen(sess(), "routine")
        assert (emergency, state) == (None, "readiness_contract_unverified")


async def test_quota_wait_saves_handoff_and_frees_slot_and_dispatches_when_recovered(
    client, auth, make_dir, accounts, synthetic, monkeypatch,
):
    """A worker hitting limits saves handoff to db, frees slot, and recovers exactly once upon quota relief."""
    from ttakkari.runs import events as run_events_mod

    # Setup a worker session
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    headers = _orch(issue_token(orch["id"]))
    resp = await client.post(
        "/api/orchestrator/workers",
        json={"title": "quota recovery test", "prompt": "initial prompt", "task_type": "routine"},
        headers=headers,
    )
    assert resp.status_code == 201
    worker_id = uuid.UUID(resp.json()["worker_id"])

    # Simulate run waiting for quota
    until_ts = time.time() + 3600
    handoff_text = "[Previous account handoff]\nProgress so far: step 1 done\n\n"
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, worker_id)
        run = Run(
            session_id=worker_id,
            workspace_id=sess.workspace_id,
            prompt="continue task",
            engine="antigravity",
            model="gemini-3.8-flash-high",
            status=RunStatus.queued,
            resume_after=datetime.fromtimestamp(until_ts, UTC),
            resume_handoff=handoff_text,
        )
        db.add(run)
        await db.flush()
        run_events_mod.append(db, run, "tool.result", {"output": "step 1 finished"})
        run_events_mod.append(db, run, "run.waiting", {
            "until": datetime.fromtimestamp(until_ts, UTC).isoformat(),
            "accounts": [{"name": "agy", "engine": "antigravity", "resets_at": datetime.fromtimestamp(until_ts, UTC).isoformat()}],
            "handoff": handoff_text,
        })
        await db.commit()
        run_id = run.id

    # Arm wait timer in manager
    manager._arm(run_id, worker_id, until_ts)
    assert manager.is_waiting(run_id) is True

    # Check RunOut surface
    run_detail = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
    assert run_detail["status"] == "queued"
    assert run_detail["waiting_limit"] is True
    assert "한도 대기" in run_detail["waiting_reason"]
    assert run_detail["last_progress"] == "도구 결과 수신"

    # Check SessionOut surface
    sess_detail = (await client.get(f"/api/orchestrator/workers/{worker_id}", headers=headers)).json()
    assert sess_detail["waiting_limit"] is True
    assert "한도 대기" in sess_detail["waiting_reason"]

    # Now simulate quota relief: accounts are available, trigger reconsider
    assert run_id not in manager._handles  # Not occupying execution slot!

    # Execute reconsideration
    await _reconsider_quota_waits()

    async with sessionmaker()() as db:
        updated = await db.get(Run, run_id)
        assert updated.resume_after is None, "resume_after must be cleared when quota is recovered"

    # Verify that dispatch was called and manager disarmed
    assert manager.is_waiting(run_id) is False

    # Wait for the run to complete
    done = await wait_run(client, auth, str(run_id))
    assert done["status"] == "succeeded"


@pytest.mark.parametrize("trailing", ["log", "usage", "run.status"])
async def test_recovered_account_rechecks_wait_after_diagnostic_events(
    new_session, accounts, monkeypatch, trailing,
):
    from ttakkari.models import SessionKind
    from ttakkari.runs import events as run_events

    ws, session = await new_session()
    until = utcnow() + timedelta(days=4)
    handoff = "previous progress must survive"
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(session["id"]))
        sess.kind, sess.engine, sess.worker_task_type = SessionKind.worker, "auto", "verification"
        run = Run(session_id=sess.id, workspace_id=uuid.UUID(ws["id"]), prompt="review",
                  engine="antigravity", model="claude-sonnet-5-5-high", status=RunStatus.queued,
                  resume_after=until, resume_handoff=handoff)
        db.add(run)
        await db.flush()
        run_events.append(db, run, "run.waiting", {"until": until.isoformat(), "handoff": handoff})
        payload = {"status": "queued", "pool_timeout_retry": True} if trailing == "run.status" else {"text": "diagnostic"}
        run_events.append(db, run, trailing, payload)
        await db.commit()
        rid, sid = run.id, sess.id
    dispatch = AsyncMock()
    monkeypatch.setattr(manager, "dispatch", dispatch)
    manager._arm(rid, sid, until.timestamp())

    await _reconsider_quota_waits()

    async with sessionmaker()() as db:
        saved = await db.get(Run, rid)
        assert saved.resume_handoff == handoff
        assert saved.status == RunStatus.queued
        if trailing == "run.status":
            assert saved.resume_after == until  # A newer retry backoff is not a quota wait.
            dispatch.assert_not_awaited()
        else:
            assert saved.resume_after is None
            assert not manager.is_waiting(rid)
            dispatch.assert_awaited_once_with(sid)


async def test_past_resume_after_is_cleared_on_resume_queued():
    """If resume_after is in the past upon server restart, it should be cleared and dispatched immediately."""
    async with sessionmaker()() as db:
        ws = Workspace(name="test-ws", root_path=f"/tmp/test-ws-{uuid.uuid4()}")
        db.add(ws)
        await db.flush()
        sess = ChatSession(workspace_id=ws.id, title="past wait test", engine="codex", model="gpt-6-luna")
        db.add(sess)
        await db.flush()
        past_time = utcnow() - timedelta(minutes=10)
        run = Run(
            session_id=sess.id,
            workspace_id=ws.id,
            prompt="stale wait prompt",
            engine="codex",
            model="gpt-6-luna",
            status=RunStatus.queued,
            resume_after=past_time,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    # Call resume_queued
    resumed_count = await resume_queued()
    assert resumed_count >= 1

    async with sessionmaker()() as db:
        updated = await db.get(Run, run_id)
        assert updated.resume_after is None


@pytest.mark.parametrize("stored_wait", ["cleared", "expired", "future_backoff"])
async def test_dispatch_uses_committed_wait_instead_of_stale_memory(new_session, monkeypatch, stored_wait):
    from ttakkari.runs.manager import RunManager

    ws, session = await new_session()
    sid = uuid.UUID(session["id"])
    deadline = {"cleared": None, "expired": utcnow() - timedelta(seconds=1),
                "future_backoff": utcnow() + timedelta(hours=1)}[stored_wait]
    async with sessionmaker()() as db:
        row = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="resume",
                  engine="antigravity", model="claude-sonnet-5-5-high", status=RunStatus.queued,
                  resume_after=deadline, resume_handoff="keep the original progress")
        db.add(row)
        await db.commit()
        rid = row.id
    m = RunManager()
    started = []
    monkeypatch.setattr(m, "start", lambda rid: started.append(rid) or True)
    m._arm(rid, sid, (utcnow() + timedelta(days=4)).timestamp())
    waker = m._wakers[rid]
    try:
        result = await m.dispatch(sid)
        if stored_wait == "future_backoff":
            assert result is None and started == []
            assert m.is_waiting(rid)
        else:
            assert result == rid and started == [rid]
            assert not m.is_waiting(rid)
        async with sessionmaker()() as db:
            saved = await db.get(Run, rid)
            assert saved.resume_handoff == "keep the original progress"
            assert saved.resume_after == (deadline if stored_wait == "future_backoff" else None)
    finally:
        m._disarm(rid)
        await asyncio.gather(waker, return_exceptions=True)


async def test_repeated_wait_commits_timer_even_when_notification_is_deduplicated(new_session):
    from sqlalchemy import func, select

    from ttakkari.models import RunEvent
    from ttakkari.runs import events as run_events
    from ttakkari.runs.manager import RunManager
    from ttakkari.runs.profiles import Profile

    ws, session = await new_session()
    sid = uuid.UUID(session["id"])
    until = utcnow() + timedelta(days=4)
    async with sessionmaker()() as db:
        run = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="review", engine="antigravity",
                  status=RunStatus.queued, resume_after=None, resume_handoff="same progress")
        db.add(run)
        await db.flush()
        run_events.append(db, run, "run.waiting", {"until": until.isoformat(), "handoff": "same progress"})
        await db.commit()
        rid = run.id
    m = RunManager()
    profile = Profile(uuid.uuid4(), "blocked account", "antigravity", model="claude-sonnet-5-5-high")
    try:
        async with sessionmaker()() as db:
            run, sess = await db.get(Run, rid), await db.get(ChatSession, sid)
            await m._wait_for_reset(db, run, sess, [(profile, until.timestamp())])
            # No caller commit: _wait_for_reset must persist before arming, even on a repeat.
        async with sessionmaker()() as db:
            saved = await db.get(Run, rid)
            assert saved.resume_after == until
            assert saved.resume_handoff == "same progress"
            assert await db.scalar(select(func.count()).select_from(RunEvent).where(
                RunEvent.run_id == rid, RunEvent.type == "run.waiting")) == 1
    finally:
        waker = m._wakers.get(rid)
        m._disarm(rid)
        if waker:
            await asyncio.gather(waker, return_exceptions=True)


async def test_quota_status_is_consistent_and_progress_summary_hides_payload_data(
    client, auth, make_dir,
):
    """Expired-but-not-dispatched waits stay consistent; summaries never copy private event text."""
    from ttakkari.models import SessionKind
    from ttakkari.orchestrator.runtime import issue_token
    from ttakkari.runs import events as run_events_mod

    orch = await _session(client, auth, make_dir, kind="orchestrator")
    parent_id = uuid.UUID(orch["id"])
    worker_headers = _orch(issue_token(parent_id))
    until = utcnow() - timedelta(minutes=5)
    private_output = "Bearer sk-test-private token; contact winter@example.com"
    async with sessionmaker()() as db:
        parent = await db.get(ChatSession, parent_id)
        sess = ChatSession(workspace_id=parent.workspace_id, title="quota status", engine="auto",
                           worker_task_type="routine", kind=SessionKind.worker, parent_id=parent_id)
        db.add(sess)
        await db.flush()
        worker_id = sess.id
        run = Run(
            session_id=worker_id,
            workspace_id=sess.workspace_id,
            prompt="resume safely",
            engine="antigravity",
            model="gemini-3.8-flash-high",
            status=RunStatus.queued,
            resume_after=until,
        )
        db.add(run)
        await db.flush()
        run_events_mod.append(db, run, "tool.result", {"output": private_output})
        run_events_mod.append(db, run, "run.waiting", {
            "until": private_output,
            "accounts": [{"name": "private-account-name", "engine": "antigravity"}],
            "handoff": private_output,
        })
        await db.commit()
        run_id = run.id

    run_out = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
    session_out = (await client.get(f"/api/sessions/{worker_id}", headers=auth)).json()
    worker_out = (await client.get(f"/api/orchestrator/workers/{worker_id}", headers=worker_headers)).json()

    assert run_out["status"] == session_out["last_run_status"] == "queued"
    assert worker_out["active_run_id"] == str(run_id)
    assert run_out["waiting_limit"] is session_out["waiting_limit"] is worker_out["waiting_limit"] is True
    assert "private-account-name" not in (run_out["waiting_reason"] or "")
    for response in (run_out, session_out, worker_out):
        serialized = str(response)
        assert "sk-test-private" not in serialized
        assert "winter@example.com" not in serialized
    assert run_out["last_progress"] == "도구 결과 수신"
    assert session_out["last_progress"] == worker_out["last_progress"] == "도구 결과 수신"

    # Once recovery clears the timer, a stale run.waiting event must not label a slot-queued Run as quota-waiting.
    async with sessionmaker()() as db:
        row = await db.get(Run, run_id)
        row.resume_after = None
        await db.commit()
    run_out = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
    session_out = (await client.get(f"/api/sessions/{worker_id}", headers=auth)).json()
    worker_out = (await client.get(f"/api/orchestrator/workers/{worker_id}", headers=worker_headers)).json()
    assert run_out["status"] == session_out["last_run_status"] == worker_out["status"] == "queued"
    assert run_out["waiting_limit"] is session_out["waiting_limit"] is worker_out["waiting_limit"] is False


async def test_real_fake_provider_limit_persists_wait_and_releases_process_and_lane(
    client, auth, new_session, synthetic, monkeypatch,
):
    """Exercise a provider limit through an actual child process and verify durable wait cleanup."""
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, Run
    from ttakkari.runs.manager import manager

    _, sess = await new_session(engine="claude")
    async with sessionmaker()() as db:
        row = await db.get(ChatSession, uuid.UUID(sess["id"]))
        row.strict_engine = True
        await db.commit()

    lane = manager.sem("workers")
    lane_before = lane._value
    reset_at = int(time.time()) + 3600
    lock_path = Path(f"{AGENT_LOG}.lock")
    lock_path.write_text(str(reset_at))
    monkeypatch.setenv("WORKER_TEST_LIMIT_MODEL", "sonnet")
    try:
        response = await client.post(
            f"/api/sessions/{sess['id']}/runs", json={"prompt": "LIMIT_LOCK resume after reset"}, headers=auth,
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        deadline = time.monotonic() + 10
        while True:
            current = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
            if current["waiting_limit"]:
                break
            assert time.monotonic() < deadline, f"provider did not enter quota wait: {current}"
            await asyncio.sleep(0.05)

        async with sessionmaker()() as db:
            run = await db.get(Run, uuid.UUID(run_id))
            assert run.status == RunStatus.queued
            assert run.resume_after is not None
            assert 0 <= run.resume_after.timestamp() - reset_at <= 60
            assert run.resume_handoff
            assert run.pid is None
        assert await wait_status(client, auth, run_id, "queued")
        assert uuid.UUID(run_id) not in manager._handles
        assert lane._value == lane_before
        child_pid = agent_calls()[-1]["pid"]
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"quota-limited child process {child_pid} remained alive")
    finally:
        lock_path.unlink(missing_ok=True)


async def test_future_quota_wait_survives_restart_and_generation_change_without_attempts(
    client, auth, make_dir, monkeypatch, tmp_path,
):
    """A future quota wait with zero resume attempts stays queued when the active generation changes."""
    from test_generations import generations

    from ttakkari import generation
    from ttakkari.generation import activate
    from ttakkari.runs import events as run_events_mod
    from ttakkari.runs import manager as manager_mod

    ws = await client.post("/api/workspaces", json={"name": "future quota", "root_path": str(make_dir())},
                           headers=auth)
    assert ws.status_code == 201
    sess_resp = await client.post("/api/sessions", json={"workspace_id": ws.json()["id"]}, headers=auth)
    assert sess_resp.status_code == 201
    session_id = uuid.UUID(sess_resp.json()["id"])
    until = utcnow() + timedelta(hours=3)
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, session_id)
        run = Run(session_id=session_id, workspace_id=sess.workspace_id, prompt="resume at reset",
                  engine="codex", model="gpt-6-luna", status=RunStatus.queued,
                  resume_attempts=0, resume_after=until)
        db.add(run)
        await db.flush()
        run_events_mod.append(db, run, "run.waiting", {"until": until.isoformat(), "handoff": "preserve"})
        await db.commit()
        run_id = run.id

    async def unavailable(*_args, **_kwargs):
        return [], []

    monkeypatch.setattr(manager_mod, "_available_profiles", unavailable)
    async with generations(tmp_path) as (old, new):
        await activate(old.id, expected=None)
        await activate(new.id, expected=old.id)
        monkeypatch.setattr(generation, "current", new)
        monkeypatch.setattr(manager, "_runtime", new)
        await resume_queued()

        async with sessionmaker()() as db:
            run = await db.get(Run, run_id)
            assert run.status == RunStatus.queued
            assert run.resume_attempts == 0
            assert run.resume_after is not None and run.resume_after > utcnow()
        assert manager.is_waiting(run_id)
        assert run_id not in manager._handles
        assert not agent_calls()
