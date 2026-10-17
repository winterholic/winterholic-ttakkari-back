"""Context rotation is enforced at the serial execution boundary, including queued turns."""
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from conftest import agent_calls
from helpers import run_events, run_to_end, start_run, wait_run
from test_orchestrator import _orch, _session

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, utcnow
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import context_usage, desk


def _call(run_id):
    return next(c for c in agent_calls() if any(a.endswith(f"/{run_id}") for a in c["argv"]))


@pytest.mark.parametrize("pct,action", [(49, "reuse"), (50, "review"), (69, "review"), (70, "fresh"), (85, "fresh")])
async def test_lead_rotates_only_at_threshold_and_persists_handoff(client, auth, make_dir, pct, action):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    first = await run_to_end(client, auth, sess["id"], f"CTX{pct} original goal")
    second = await run_to_end(client, auth, sess["id"], "continue next stage")
    assert second["status"] == "succeeded"
    event = next(e for e in await run_events(client, auth, second["id"]) if e["type"] == "context.routing")
    assert event["payload"]["action"] == action
    assert event["payload"]["measurement"]["measured_run_id"] == first["id"]
    argv = _call(second["id"])["argv"]
    assert ("--resume" not in argv) == (action == "fresh")
    if action == "fresh":
        assert "original goal" in argv[argv.index("-p") + 1]
        assert second["usage"]["context_handoff"]["source_run_ids"] == [first["id"]]
        assert second["usage"]["context_handoff"]["text"]


async def test_queued_turn_rotates_after_previous_run_finishes(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    first = await start_run(client, auth, sess["id"], "CTX75 queued original")
    second = await start_run(client, auth, sess["id"], "queued continuation")
    await wait_run(client, auth, first["id"])
    done = await wait_run(client, auth, second["id"])
    assert done["status"] == "succeeded"
    assert "--resume" not in _call(second["id"])["argv"]
    async with sessionmaker()() as db:
        ctx = await context_usage.latest(db, await db.get(ChatSession, UUID(sess["id"])))
    assert ctx["measured_run_id"] == second["id"]  # created before reset, executed after it


async def test_worker_message_without_fresh_flag_also_rotates(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    h = _orch(issue_token(orch["id"]))
    w = (await client.post("/api/orchestrator/workers", json={"title": "long task", "prompt": "CTX72 keep goal", "engine": "claude"}, headers=h)).json()
    wid = w["worker_id"]
    first = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, first["id"])
    response = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt": "continue"}, headers=h)
    assert response.status_code == 202
    second = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[-1]
    done = await wait_run(client, auth, second["id"])
    assert done["status"] == "succeeded"
    assert "--resume" not in _call(second["id"])["argv"]
    assert first["id"] in done["usage"]["context_handoff"]["source_run_ids"]


async def test_board_and_worker_api_expose_measurement_provenance(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    first = await run_to_end(client, auth, sess["id"], "CTX70 original")
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess["id"]))
        ctx = await context_usage.latest(db, s)
        line = await desk._lead_line(db, s)
    assert datetime.fromisoformat(ctx["measured_at"]) == datetime.fromisoformat(first["finished_at"])
    assert ctx["measured_engine"] == first["engine"]
    assert ctx["measured_model"] == first["usage"]["selected_model"]
    assert "70%" in line and ctx["measured_at"] in line and "새 문맥" in line


async def test_old_measurement_is_labelled_stale_but_high_usage_still_rotates(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    first = await run_to_end(client, auth, sess["id"], "CTX71 original")
    async with sessionmaker()() as db:
        r = await db.get(Run, UUID(first["id"]))
        r.finished_at = utcnow() - timedelta(days=2)
        await db.commit()
        ctx = await context_usage.latest(db, await db.get(ChatSession, UUID(sess["id"])))
    assert ctx["measurement_status"] == "stale"
    second = await run_to_end(client, auth, sess["id"], "continue")
    assert "--resume" not in _call(second["id"])["argv"]


async def test_missing_measurement_is_unknown_and_does_not_invent_zero(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    done = await run_to_end(client, auth, sess["id"], "new task")
    event = next(e for e in await run_events(client, auth, done["id"]) if e["type"] == "context.routing")
    assert event["payload"]["action"] == "unknown"
    assert event["payload"]["measurement"] is None


async def test_second_rotation_preserves_original_goal_and_checkpoint_pointer(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    first = await run_to_end(client, auth, sess["id"], "CTX70 original durable goal")
    await run_to_end(client, auth, sess["id"], "second step")
    third = await run_to_end(client, auth, sess["id"], "third step")
    handoff = third["usage"]["context_handoff"]
    assert handoff["original_request"]["run_id"] == first["id"]
    assert "original durable goal" in handoff["text"]
    assert "checkpoint=" in handoff["text"] and "outbox=" in handoff["text"]


async def test_rotation_is_idempotent_when_same_run_resumes(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    await run_to_end(client, auth, sess["id"], "CTX70 goal")
    second = await run_to_end(client, auth, sess["id"], "continue")
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess["id"]))
        r = await db.get(Run, UUID(second["id"]))
        before = dict(s.engine_sessions)
        handoff = r.usage["context_handoff"]
        await context_usage.prepare(db, s, r)
        assert s.engine_sessions == before
        assert r.usage["context_handoff"] == handoff


async def test_measurement_missing_latest_turn_does_not_reuse_older_value(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    await run_to_end(client, auth, sess["id"], "CTX55 first")
    second = await run_to_end(client, auth, sess["id"], "next")
    async with sessionmaker()() as db:
        r = await db.get(Run, UUID(second["id"]))
        r.usage = {}
        await db.commit()
        s = await db.get(ChatSession, UUID(sess["id"]))
        assert await context_usage.latest(db, s) is None


async def test_handoff_does_not_spend_field_budget_on_duplicate_goal(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    first = await run_to_end(client, auth, sess["id"], "original")
    async with sessionmaker()() as db:
        r = await db.get(Run, UUID(first["id"]))
        r.prompt = "goal-" + "g" * 6995
        r.result_text = "result-" + "r" * 6993
        await db.flush()
        s = await db.get(ChatSession, UUID(sess["id"]))
        next_run = Run(session_id=s.id, workspace_id=s.workspace_id, prompt="continue", engine="claude")
        snapshot = await context_usage._snapshot(db, s, next_run)
        assert snapshot["text"].count(r.prompt) == 1
        assert r.result_text in snapshot["text"]


async def test_handoff_prioritizes_latest_result_within_budget(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", engine="claude")
    first = await run_to_end(client, auth, sess["id"], "original")
    second = await run_to_end(client, auth, sess["id"], "next step")
    async with sessionmaker()() as db:
        old = await db.get(Run, UUID(first["id"]))
        old.prompt = "g" * 7000
        old.result_text = "old result-" + "x" * 6990
        new = await db.get(Run, UUID(second["id"]))
        new.result_text = "latest decision-" + "y" * 4000
        await db.flush()
        s = await db.get(ChatSession, UUID(sess["id"]))
        snapshot = await context_usage._snapshot(db, s, Run(session_id=s.id, workspace_id=s.workspace_id, prompt="continue"))
        assert new.result_text in snapshot["text"]
        assert old.result_text not in snapshot["text"]
