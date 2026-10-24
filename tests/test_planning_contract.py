"""Goal completion, continuation and cross-engine recovery regressions (A–E)."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunOrigin, RunStatus
from ttakkari.orchestrator.runtime import issue_token


async def lead(client, auth, make_dir, kind="orchestrator"):
    root = make_dir()
    ws = (await client.post("/api/workspaces", headers=auth,
                           json={"name": root.name, "root_path": str(root)})).json()
    sess = (await client.post("/api/sessions", headers=auth,
                             json={"workspace_id": ws["id"], "kind": kind})).json()
    return sess, {"Authorization": f"Bearer {issue_token(UUID(sess['id']))}"}


def state():
    return {"goal": "Implement and verify the entire V2", "strategy": "Inspect shared causes, batch fixes, then integrate",
            "requirements": [{"id": "A", "text": "Gate A", "status": "done", "evidence": "pytest gate_a: 94 passed"},
                             {"id": "B", "text": "Runtime integration", "status": "pending"}],
            "next_action": "Implement runtime integration", "status": "active"}


async def checkpoint(client, h, value, revision=0):
    return await client.post("/api/orchestrator/work", headers=h,
                             json={"operation": "checkpoint", "revision": revision, "state": value})


async def test_gate_a_is_not_goal_completion(client, auth, make_dir):
    _, h = await lead(client, auth, make_dir)
    value = state()
    value["status"] = "complete"
    r = await checkpoint(client, h, value)
    assert r.status_code == 422, r.text
    assert "B" in r.text
    value["status"] = "active"
    r = await checkpoint(client, h, value)
    assert r.status_code == 200, r.text
    assert r.json()["state"]["requirements"][1]["status"] == "pending"


async def test_replan_preserves_requirements_and_evidence(client, auth, make_dir):
    _, h = await lead(client, auth, make_dir)
    value = state()
    r = await checkpoint(client, h, value)
    assert r.status_code == 200, r.text
    revision = r.json()["revision"]
    value["strategy"] = "Group all SDK defects; run the expensive integration once"
    value["replan_reason"] = "Found common SDK cause"
    updated = await checkpoint(client, h, value, revision)
    assert updated.status_code == 200, updated.text
    assert updated.json()["state"]["requirements"][0]["evidence"].endswith("94 passed")
    value["requirements"] = value["requirements"][:1]
    assert (await checkpoint(client, h, value, updated.json()["revision"])).status_code == 422


async def test_stale_checkpoint_rejected(client, auth, make_dir):
    _, h = await lead(client, auth, make_dir)
    r = await checkpoint(client, h, state())
    assert r.status_code == 200, r.text
    assert (await checkpoint(client, h, state(), 0)).status_code == 409


async def test_contract_survives_context_reset_and_engine_switch(client, auth, make_dir):
    from ttakkari.runs import context_usage
    from ttakkari.runs.manager import _compose_prompt

    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess["id"]))
        context_usage.reset_engine_sessions(s)
        s.engine = "codex"
        await db.commit()
        run = Run(session_id=s.id, workspace_id=s.workspace_id, prompt="next stage", engine="codex")
        note_codex = await _compose_prompt(db, run)
        s.engine = "claude"
        run.engine = "claude"
        note_claude = await _compose_prompt(db, run)
    for note in (note_codex, note_claude):
        assert "Implement runtime integration" in note
        assert "94 passed" in note
        assert "entire V2" in note


async def test_work_contract_is_session_scoped_and_lead_only(client, auth, make_dir):
    _, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    _, other = await lead(client, auth, make_dir)
    r = await client.post("/api/orchestrator/work", headers=other, json={"operation": "get"})
    assert r.status_code == 200 and r.json()["state"] is None
    _, worker_h = await lead(client, auth, make_dir, "chat")
    assert (await checkpoint(client, worker_h, state())).status_code == 403


async def test_partial_gate_automatically_queues_next_and_no_progress_is_bounded(client, auth, make_dir):
    from ttakkari.orchestrator import work

    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    sid = UUID(sess["id"])
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        run = Run(session_id=sid, workspace_id=s.workspace_id, prompt="Gate A only", engine="claude",
                  status=RunStatus.succeeded, origin=RunOrigin.report)
        db.add(run)
        await db.flush()
        next_id = await work.after_run(db, s, run)
        assert next_id is not None
        await db.commit()
    async with sessionmaker()() as db:
        queued = await db.get(Run, next_id)
        assert queued.status == "queued" and "Runtime integration" in queued.prompt
        queued.status = RunStatus.succeeded
        second = await work.after_run(db, await db.get(ChatSession, sid), queued)
        assert second is not None
        await db.commit()
        r2 = await db.get(Run, second)
        r2.status = RunStatus.succeeded
        assert await work.after_run(db, await db.get(ChatSession, sid), r2) is None
        await db.commit()
    result = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert result["state"]["status"] == "blocked"
    assert "progress" in result["state"]["blocker"]


async def test_complete_requires_evidence_and_all_pms_tasks(client, auth, make_dir):
    _, h = await lead(client, auth, make_dir)
    project = (await client.post("/api/pms/projects", headers=auth,
                                json={"name": "V2", "phases": ["Gate A", "Runtime"]})).json()
    value = state()
    value["pms_project_id"] = project["id"]
    for req in value["requirements"]:
        req.update(status="done", evidence="integration receipt: pass")
    value.update(status="complete", next_action="")
    assert (await checkpoint(client, h, value)).status_code == 422
    value.pop("pms_project_id")
    value["requirements"][0]["evidence"] = ""
    assert (await checkpoint(client, h, value)).status_code == 422
    value["requirements"][0]["evidence"] = "gate receipt: pass"
    r = await checkpoint(client, h, value)
    assert r.status_code == 200, r.text


async def test_no_duplicate_when_worker_or_user_run_owns_next_step(client, auth, make_dir):
    from ttakkari.orchestrator import work

    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess["id"]))
        queued = Run(session_id=s.id, workspace_id=s.workspace_id, prompt="new steering", engine="claude",
                     status=RunStatus.queued, origin=RunOrigin.user)
        done = Run(session_id=s.id, workspace_id=s.workspace_id, prompt="done gate", engine="claude",
                   status=RunStatus.succeeded)
        db.add_all([queued, done])
        await db.flush()
        assert await work.after_run(db, s, done) is None
        assert len(list(await db.scalars(select(Run).where(Run.session_id == s.id)))) == 2


async def test_completed_evidence_and_project_binding_cannot_disappear(client, auth, make_dir):
    _, h = await lead(client, auth, make_dir)
    project = (await client.post('/api/pms/projects', headers=auth, json={'name': 'linked'})).json()
    value = state()
    value['pms_project_id'] = project['id']
    assert (await checkpoint(client, h, value)).status_code == 200
    value.pop('pms_project_id')
    assert (await checkpoint(client, h, value, 1)).status_code == 422
    value['pms_project_id'] = project['id']
    value['requirements'][0]['evidence'] = 'different evidence'
    assert (await checkpoint(client, h, value, 1)).status_code == 422


async def test_failed_run_records_exact_blocker_instead_of_silently_stranding_work(client, auth, make_dir):
    from ttakkari.orchestrator import work
    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        run = Run(session_id=s.id, workspace_id=s.workspace_id, prompt='continue', engine='codex',
                  status=RunStatus.failed, error='nested codex: authentication failed before model response')
        db.add(run)
        await db.flush()
        assert await work.after_run(db, s, run) is None
        await db.commit()
    result = (await client.post('/api/orchestrator/work', headers=h, json={'operation': 'get'})).json()
    assert result['state']['status'] == 'blocked'
    assert 'authentication failed' in result['state']['blocker']


async def test_pms_project_cannot_be_done_with_unfinished_leaf(client, auth):
    project = (await client.post('/api/pms/projects', headers=auth,
                                json={'name': 'Gate is intermediate', 'phases': ['Gate', 'Delivery']})).json()
    r = await client.patch(f"/api/pms/projects/{project['id']}", headers=auth, json={'status': 'done'})
    assert r.status_code == 422, r.text


async def test_actual_manager_finish_continues_goal_once_and_stops_after_completion(client, auth, make_dir):
    from ttakkari.runs.manager import manager
    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        run = Run(session_id=s.id, workspace_id=s.workspace_id, prompt='Gate A green', engine='claude',
                  status=RunStatus.running)
        db.add(run)
        await db.flush()
        await manager._finish(db, run, RunStatus.succeeded)
    async with sessionmaker()() as db:
        pending = list(await db.scalars(select(Run).where(Run.session_id == UUID(sess['id']), Run.status == 'queued')))
        assert len(pending) == 1
        pending[0].status = RunStatus.succeeded
        await db.commit()
    value = state()
    value['requirements'][1].update(status='done', evidence='runtime integration: pass')
    value.update(status='complete', next_action='')
    assert (await checkpoint(client, h, value, 1)).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        last = await db.get(Run, pending[0].id)
        await manager._finish(db, last, RunStatus.succeeded)
        assert await db.scalar(select(Run.id).where(Run.session_id == s.id, Run.status == 'queued')) is None


async def test_goal_state_is_in_prompt_not_cache_breaking_system_layer(client, auth, make_dir):
    from ttakkari.runs.manager import _compose_prompt, _system_note
    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        run = Run(session_id=s.id, workspace_id=s.workspace_id, prompt='next', engine='codex')
        assert 'Implement runtime integration' in await _compose_prompt(db, run)
        assert 'Implement runtime integration' not in await _system_note(db, make_dir(), s)


async def test_desk_board_does_not_call_partial_goal_complete(client, auth, make_dir):
    from ttakkari.runs.desk import _lead_line
    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        db.add(Run(session_id=s.id, workspace_id=s.workspace_id, engine='claude', prompt='gate', status='succeeded'))
        await db.flush()
        line = await _lead_line(db, s)
        assert '목표 진행 중' in line and '미완료 1' in line


async def test_live_child_prevents_duplicate_continuation_and_false_completion(client, auth, make_dir):
    from ttakkari.orchestrator import work
    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        worker = ChatSession(workspace_id=s.workspace_id, parent_id=s.id, title='runtime', engine='codex', kind='worker')
        db.add(worker)
        await db.flush()
        db.add(Run(session_id=worker.id, workspace_id=s.workspace_id, prompt='integrate', engine='codex', status='queued'))
        done = Run(session_id=s.id, workspace_id=s.workspace_id, prompt='gate green', engine='claude', status='succeeded')
        db.add(done)
        await db.flush()
        assert await work.after_run(db, s, done) is None
        await db.commit()
    value = state()
    value['requirements'][1].update(status='done', evidence='claim')
    value.update(status='complete', next_action='')
    assert (await checkpoint(client, h, value, 1)).status_code == 422


async def test_active_contract_prevents_idle_archival(client, auth, make_dir):
    from datetime import timedelta

    from ttakkari.models import utcnow
    from ttakkari.runs.lead_archive import _archive_under
    desk_sess, _ = await lead(client, auth, make_dir, 'desk')
    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        s.parent_id = UUID(desk_sess['id'])
        s.updated_at = utcnow() - timedelta(days=10)
        await db.flush()
        assert await _archive_under(db, s.parent_id, utcnow() - timedelta(days=1)) == 0
        assert s.archived_at is None


async def test_finished_result_is_labelled_partial_in_real_desk_report(client, auth, make_dir):
    from ttakkari.runs.manager import _post_lead_report
    desk_sess, _ = await lead(client, auth, make_dir, 'desk')
    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        s.parent_id = UUID(desk_sess['id'])
        run = Run(session_id=s.id, workspace_id=s.workspace_id, prompt='Gate A', engine='codex',
                  status='succeeded', result_text='94 passed')
        db.add(run)
        await db.flush()
        await _post_lead_report(db, s, run)
        report = await db.scalar(select(Run).where(Run.session_id == s.parent_id))
        assert '턴 종료' in report.prompt
        assert '미완료: B' in report.result_text
        assert '94 passed' in report.result_text


async def test_checkpoint_rejects_cross_scope_project(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    project = (await client.post('/api/pms/projects', headers=auth, json={'name': 'private', 'scope': 'personal'})).json()
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        s.pool = 'work'
        await db.commit()
    value = state()
    value['pms_project_id'] = project['id']
    assert (await checkpoint(client, h, value)).status_code == 404


async def test_persisted_resume_schedule_owns_next_step_without_duplicate_run(client, auth, make_dir):
    from datetime import timedelta

    from ttakkari.models import ScheduledAction, utcnow
    from ttakkari.orchestrator import work

    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess['id']))
        db.add(ScheduledAction(action_type='send_message', payload={'session_id': str(s.id), 'prompt': 'resume'},
                               status='pending', due_at=utcnow() + timedelta(minutes=10)))
        run = Run(session_id=s.id, workspace_id=s.workspace_id, engine='codex', prompt='partial', status='succeeded')
        db.add(run)
        await db.flush()
        assert await work.after_run(db, s, run) is None


async def test_dispatch_executes_followups_without_another_user_message(client, auth, make_dir):
    import asyncio
    import time

    from helpers import start_run

    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200
    await start_run(client, auth, sess['id'], 'Gate A checkpoint; original task is still unfinished')
    deadline = time.monotonic() + 15
    while True:
        runs = (await client.get(f"/api/sessions/{sess['id']}/runs", headers=auth)).json()
        if len(runs) >= 3 and all(r['status'] == 'succeeded' for r in runs):
            break
        assert time.monotonic() < deadline, [(r['origin'], r['status']) for r in runs]
        await asyncio.sleep(0.05)
    result = (await client.post('/api/orchestrator/work', headers=h, json={'operation': 'get'})).json()
    assert len(runs) == 3
    assert [r['origin'] for r in runs].count('user') == 1
    assert [r['origin'] for r in runs].count('orchestrator') == 2
    # Fake engine records no work progress: bounded blocking, never a false complete result.
    assert result['state']['status'] == 'blocked'
    assert result['state']['requirements'][1]['status'] == 'pending'


async def test_cancel_run_with_pending_schedule_pauses_goal_and_cancels_contract_resume(client, auth, make_dir):
    from datetime import timedelta

    from ttakkari.models import ScheduledAction, utcnow
    from ttakkari.schedules.service import execute_action

    sess, h = await lead(client, auth, make_dir)
    assert (await checkpoint(client, h, state())).status_code == 200

    # 1. 활성 Run 큐잉
    run_resp = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "work in progress"})
    assert run_resp.status_code == 202
    run_id = run_resp.json()["id"]

    # 2. 계약 재개 예약과 무관한 사용자 예약 등록
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess["id"]))
        resume_sched = ScheduledAction(
            action_type="send_message",
            payload={"session_id": str(s.id), "prompt": "resume", "work_continuation": True},
            status="pending",
            due_at=utcnow() + timedelta(minutes=10),
            description="목표 계약 자동 재개",
        )
        user_sched = ScheduledAction(
            action_type="send_message",
            payload={"session_id": str(s.id), "message": "사용자 리마인더"},
            status="pending",
            due_at=utcnow() + timedelta(minutes=15),
            description="사용자 개인 알림",
        )
        db.add_all([resume_sched, user_sched])
        await db.commit()
        resume_id = resume_sched.id
        user_id = user_sched.id

    # 3. 취소 API 호출
    cancel_resp = await client.post(f"/api/runs/{run_id}/cancel", headers=auth)
    assert cancel_resp.status_code == 200

    # 4. 목표 상태 확인 -> paused여야 함 (기존 버그: active로 남아있었음)
    work_resp = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert work_resp["state"]["status"] == "paused"
    assert "cancelled" in work_resp["state"]["blocker"].lower()

    # 5. 예약 상태 확인 -> 계약 재개 예약은 cancelled, 사용자 예약은 pending 유지
    async with sessionmaker()() as db:
        r_item = await db.get(ScheduledAction, resume_id)
        u_item = await db.get(ScheduledAction, user_id)
        assert r_item.status == "cancelled"
        assert u_item.status == "pending"

    # 6. 예약 실행 시점 검증
    # 계약 재개 예약은 실행 시도 시 취소/스킵됨
    # 사용자 예약은 실행 시 정상적으로 run이 큐잉됨
    user_res = await execute_action(u_item)
    assert "queued_run_id" in user_res

