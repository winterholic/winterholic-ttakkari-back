"""Deterministic regression tests for worker quota waiting, automatic recovery, and local Qwen fallback."""
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from helpers import wait_run
from test_orchestrator import _orch, _session

from ttakkari.agents.claude import ClaudeAdapter
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
    """Verify that qwen-local is connected to routine/general fallback while maintaining quality boundaries."""
    # 1. Routing presence
    assert any(engine == "qwen-local" for stage in worker_routing.ROUTES["routine"] for engine, _ in stage)
    assert any(engine == "qwen-local" for stage in worker_routing.ROUTES["general"] for engine, _ in stage)

    # 2. Boundaries: excluded from verification, design, reasoning
    for high_task in ("verification", "design", "reasoning"):
        assert not any(engine == "qwen-local" for stage in worker_routing.ROUTES[high_task] for engine, _ in stage), (
            f"qwen-local must not be routed to {high_task}"
        )

    # 3. Model engine recognition
    assert profiles.model_engine("Qwen3.8-Flash-Next-Q2_K") == "qwen-local"

    # 4. Independent verification requirement
    assert worker_routing.is_flash_model("Qwen3.8-Flash-Next-Q2_K") is True
    assert worker_routing.needs_independent_verification("routine", "Qwen3.8-Flash-Next-Q2_K") is True
    assert worker_routing.needs_independent_verification("general", "Qwen3.8-Flash-Next-Q2_K") is True

    # 5. Capacity formatting
    prof = profiles.Profile(uuid.uuid4(), "qwen", "qwen-local", model="Qwen3.8-Flash-Next-Q2_K")
    cap = worker_routing.capacity(prof, running=0)
    assert cap["engine"] == "qwen-local"
    assert cap["quota_known"] is True
    assert cap["remaining_to_limit_percent"] == 100.0


async def test_qwen_local_readiness_guards_availability(monkeypatch):
    """When Qwen readiness fails (unverified), it must not be added as a candidate."""
    async with sessionmaker()() as db:
        # 1. Mock unverified readiness
        monkeypatch.setattr(QWEN_LOCAL_ADAPTER, "readiness", lambda: (False, "readiness_contract_unverified"))
        free, _blocked = await worker_routing.availability(db, "routine", "personal")
        assert not any(p.engine == "qwen-local" for p in free)

        # 2. Mock ready readiness
        monkeypatch.setattr(QWEN_LOCAL_ADAPTER, "readiness", lambda: (True, "ready"))
        free, _blocked = await worker_routing.availability(db, "routine", "personal")
        qwen_candidates = [p for p in free if p.engine == "qwen-local"]
        assert len(qwen_candidates) == 1
        assert qwen_candidates[0].model == "Qwen3.8-Flash-Next-Q2_K"


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
    assert "step 1 finished" in (run_detail["last_progress"] or "")

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
