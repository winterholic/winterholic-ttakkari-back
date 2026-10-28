from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ttakkari.db import sessionmaker
from ttakkari.models import (
    Artifact,
    ChatSession,
    CtxEntity,
    CtxMemory,
    CtxRelation,
    Run,
    RunStatus,
    RuntimeLease,
    Workspace,
)
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.security.auth import issue_access_token

pytestmark = [pytest.mark.security]



@pytest.fixture
async def setup_sessions(make_dir):
    root = make_dir()
    async with sessionmaker()() as db:
        ws = Workspace(id=uuid.uuid4(), name=root.name, root_path=str(root))
        db.add(ws)
        await db.flush()

        personal_session = ChatSession(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            title="Personal Session",
            engine="codex",
            pool="personal",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        work_session = ChatSession(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            title="Work Session",
            engine="codex",
            pool="work",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add_all([personal_session, work_session])
        await db.commit()
    return personal_session, work_session


@pytest.mark.asyncio
async def test_agent_sessions_list_isolation(client, setup_sessions):
    personal_s, work_s = setup_sessions

    # 1. Personal session listing: sees both
    p_token = issue_token(personal_s.id)
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {p_token}"})
    assert resp.status_code == 200
    titles = [s["title"] for s in resp.json()["sessions"]]
    assert "Personal Session" in titles
    assert "Work Session" in titles

    # 2. Work grouping does not revoke owner access to personal sessions
    w_token = issue_token(work_s.id)
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 200
    w_titles = [s["title"] for s in resp.json()["sessions"]]
    assert "Work Session" in w_titles
    assert "Personal Session" in w_titles


@pytest.mark.asyncio
async def test_agent_endpoint_rejects_missing_garbage_and_wrong_audience_tokens(client, setup_sessions):
    personal_session, _ = setup_sessions
    user_token, _ = issue_access_token()
    invalid_headers = [
        {},
        {"Authorization": "Bearer not.a.signed.token"},
        {"Authorization": f"Bearer {user_token}"},
    ]
    for headers in invalid_headers:
        response = await client.get("/api/agent/sessions", headers=headers)
        assert response.status_code == 401

    valid = await client.get(
        "/api/agent/sessions",
        headers={"Authorization": f"Bearer {issue_token(personal_session.id)}"},
    )
    assert valid.status_code == 200


@pytest.mark.asyncio
async def test_agent_read_session_scope_enforcement(client, setup_sessions):
    personal_s, work_s = setup_sessions
    w_token = issue_token(work_s.id)
    p_token = issue_token(personal_s.id)

    # 1. Work session can explicitly read the owner's personal session
    resp = await client.get(f"/api/agent/sessions/{personal_s.id}", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Personal Session"

    # 2. Work session reading work session -> 200 OK
    resp = await client.get(f"/api/agent/sessions/{work_s.id}", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Work Session"

    # 3. Personal session reading personal session -> 200 OK
    resp = await client.get(f"/api/agent/sessions/{personal_s.id}", headers={"Authorization": f"Bearer {p_token}"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Personal Session"


@pytest.mark.asyncio
async def test_work_agent_can_archive_owner_agent_memory_across_groups(client, setup_sessions):
    _, work_s = setup_sessions
    personal_memory = CtxMemory(content="private synthetic memory", scope="personal", source="agent")
    work_memory = CtxMemory(content="work synthetic memory", scope="work", source="agent")
    async with sessionmaker()() as db:
        db.add_all([personal_memory, work_memory])
        await db.commit()

    headers = {"Authorization": f"Bearer {issue_token(work_s.id)}"}
    forbidden = await client.post("/api/agent/forget", headers=headers, json={"id": str(personal_memory.id)})
    assert forbidden.status_code == 200
    allowed = await client.post("/api/agent/forget", headers=headers, json={"id": str(work_memory.id)})
    assert allowed.status_code == 200
    assert allowed.json() == {"forgotten": True}
    async with sessionmaker()() as db:
        stored_personal = await db.get(CtxMemory, personal_memory.id)
        stored_work = await db.get(CtxMemory, work_memory.id)
        assert stored_personal is not None and stored_personal.archived_at is not None
        assert stored_work is not None and stored_work.archived_at is not None


@pytest.mark.asyncio
async def test_work_entity_detail_includes_owner_relations_across_groups(client, setup_sessions):
    _, work_s = setup_sessions
    subject = CtxEntity(type="topic", name="work-subject", scope="work", aliases=[], attributes={})
    private_target = CtxEntity(type="topic", name="personal-target", scope="personal", aliases=[], attributes={})
    async with sessionmaker()() as db:
        db.add_all([subject, private_target])
        await db.flush()
        db.add(CtxRelation(subject_id=subject.id, predicate="related_to", object_id=private_target.id,
                           note="synthetic cross-scope relation", source="agent"))
        await db.commit()

    headers = {"Authorization": f"Bearer {issue_token(work_s.id)}"}
    response = await client.post("/api/agent/entities", headers=headers, json={"name": "work-subject"})
    assert response.status_code == 200
    assert len(response.json()["relations"]) == 1
    assert "personal-target" in response.text


@pytest.mark.asyncio
async def test_agent_stocks_scope_enforcement(client, setup_sessions):
    personal_s, work_s = setup_sessions
    w_token = issue_token(work_s.id)
    p_token = issue_token(personal_s.id)

    # ba4f0220 / c8be9694 이후 소유자 도구는 work 세션에서도 열려 있다(표시 분류 제한 제거).
    resp = await client.get("/api/agent/stocks/tossinvest/status", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 200

    # Personal session calling agent stocks -> 200 OK
    resp = await client.get("/api/agent/stocks/tossinvest/status", headers={"Authorization": f"Bearer {p_token}"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agent_work_artifacts_are_workspace_and_session_scoped(client, setup_sessions, make_dir):
    personal_s, work_s = setup_sessions
    other_root = make_dir()
    async with sessionmaker()() as db:
        other_ws = Workspace(id=uuid.uuid4(), name="other", root_path=str(other_root))
        db.add(other_ws)
        await db.flush()

        def artifact(name: str, workspace_id: uuid.UUID, session_id: uuid.UUID | None,
                     source: str = "agent_output") -> Artifact:
            return Artifact(filename=name, mime_type="text/plain", kind="text", source=source,
                            workspace_id=workspace_id, session_id=session_id, original_path=f"/{name}",
                            rel_path=name, storage_mode="copy", stored_path=f"/{name}", size_bytes=1,
                            sha256="0" * 64, retention_until=None)

        personal_artifact = artifact("personal-private.txt", personal_s.workspace_id, personal_s.id)
        work_artifact = artifact("work-allowed.txt", work_s.workspace_id, work_s.id)
        db.add_all([
            personal_artifact,
            work_artifact,
            artifact("shared-workspace.txt", work_s.workspace_id, None, "user_registered"),
            artifact("unowned-orphan.txt", work_s.workspace_id, None),
            artifact("other-workspace.txt", other_ws.id, None),
        ])
        await db.commit()

    work_headers = {"Authorization": f"Bearer {issue_token(work_s.id)}"}
    personal_headers = {"Authorization": f"Bearer {issue_token(personal_s.id)}"}
    work_resp = await client.get("/api/agent/artifacts", headers=work_headers)
    assert work_resp.status_code == 200
    work_names = {item["filename"] for item in work_resp.json()["artifacts"]}
    assert work_names == {"personal-private.txt", "work-allowed.txt", "shared-workspace.txt",
                          "unowned-orphan.txt", "other-workspace.txt"}
    injected_filter = await client.get(
        "/api/agent/artifacts", headers=work_headers, params={"q": "%' OR 1=1 --"}
    )
    assert injected_filter.status_code == 200
    assert injected_filter.json()["artifacts"] == []
    forbidden_move = await client.patch(
        f"/api/agent/artifacts/{personal_artifact.id}",
        headers=work_headers,
        json={"area": "files"},
    )
    assert forbidden_move.status_code == 409  # owner access still cannot implicitly keep a file
    # 범위 안의 파일이어도 에이전트는 files 로 올릴 수 없다(409). agent 로 두는 것은 허용된다.
    promote = await client.patch(
        f"/api/agent/artifacts/{work_artifact.id}",
        headers=work_headers,
        json={"area": "files"},
    )
    assert promote.status_code == 409
    allowed_move = await client.patch(
        f"/api/agent/artifacts/{work_artifact.id}",
        headers=work_headers,
        json={"area": "agent"},
    )
    assert allowed_move.status_code == 200

    # Personal sessions preserve the existing cross-workspace library listing behavior.
    personal_resp = await client.get("/api/agent/artifacts", headers=personal_headers)
    assert personal_resp.status_code == 200
    personal_names = {item["filename"] for item in personal_resp.json()["artifacts"]}
    assert personal_names == {"personal-private.txt", "work-allowed.txt", "shared-workspace.txt",
                              "unowned-orphan.txt", "other-workspace.txt"}
async def test_read_session_exposes_bounded_wait_diagnostics(client, setup_sessions):
    from ttakkari.runs import events

    personal, _ = setup_sessions
    until = datetime.now(UTC) + timedelta(days=4)
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, personal.id)
        sess.strict_engine = True
        run = Run(session_id=sess.id, workspace_id=sess.workspace_id, engine="codex",
                  status=RunStatus.queued, prompt="review", resume_after=until,
                  resume_handoff="private provider progress")
        db.add(run)
        await db.flush()
        events.append(db, run, "run.waiting", {"reason": "usage_limit", "handoff": "private provider progress"})
        events.append(db, run, "log", {"text": "private diagnostic"})
        await db.commit()
        rid = run.id
    response = await client.get(f"/api/agent/sessions/{personal.id}",
                                headers={"Authorization": f"Bearer {issue_token(personal.id)}"})
    assert response.status_code == 200
    data = response.json()
    assert data["routing"]["strict_engine"] is True
    turn = data["turns"][0]
    assert turn["id"] == str(rid)
    assert datetime.fromisoformat(turn["resume_after"]) == until
    assert turn["last_event_type"] == "log"
    assert turn["lease_owned"] is False
    assert "private provider progress" not in response.text
    assert "private diagnostic" not in response.text


@pytest.mark.parametrize("generation_mode", [False, True])
async def test_read_session_observes_local_dispatch_without_exposing_task_locals(
    client, setup_sessions, monkeypatch, generation_mode,
):
    import asyncio
    from types import SimpleNamespace

    from ttakkari import generation
    from ttakkari.models import RuntimeControl, RuntimeGeneration
    from ttakkari.runs.manager import manager

    personal, _ = setup_sessions
    async with sessionmaker()() as db:
        run = Run(session_id=personal.id, workspace_id=personal.workspace_id,
                  engine="codex", status=RunStatus.queued, prompt="review")
        db.add(run)
        await db.commit()
        rid = run.id
    gate = asyncio.Event()

    async def blocked_dispatch_probe():
        secret = "task-local-private-progress"
        await gate.wait()
        return secret

    task = asyncio.create_task(blocked_dispatch_probe())
    await asyncio.sleep(0)
    until = datetime.now(UTC) + timedelta(hours=1)
    runtime = None
    if generation_mode:
        gid = uuid.uuid4()
        async with sessionmaker()() as db:
            db.add(RuntimeGeneration(id=gid, revision="a" * 40, api_url="http://test",
                                    pid=0, process_started_at=0, state="active"))
            await db.flush()
            db.add(RuntimeControl(id=1, active_generation=gid))
            await db.commit()
        runtime = SimpleNamespace(id=gid, role="api", execution_mode="local", draining=False,
                                  http_inflight=0)
    monkeypatch.setattr(generation, "current", runtime)
    monkeypatch.setattr(manager, "_runtime", None)
    monkeypatch.setattr(manager, "_handles", {rid: SimpleNamespace(task=task)})
    monkeypatch.setattr(manager, "_waiting", {rid: (until.timestamp(), personal.id)})
    monkeypatch.setattr(manager, "_restart_waker", task)
    try:
        response = await client.get(f"/api/agent/sessions/{personal.id}",
            headers={"Authorization": f"Bearer {issue_token(personal.id)}"})
        assert response.status_code == 200
        data = response.json()
        assert data["execution"]["local_coordinator"] is True
        assert data["execution"]["head_run_id"] == str(rid)
        assert data["execution"]["lane"] == "workers"
        assert data["execution"]["lane_leases"] == 0
        assert data["execution"]["lane_limit"] > 0
        assert data["execution"]["restart_waker_exists"] is True
        assert data["execution"]["restart_waker_done"] is False
        obs = data["turns"][0]["local_dispatch"]
        assert obs["task_exists"] is True
        assert obs["task_done"] is False
        assert datetime.fromisoformat(obs["armed_until"]) == until
        assert "blocked_dispatch_probe" in obs["await_chain"]
        assert "wait" in obs["await_chain"]
        assert len(obs["await_chain"]) <= 8
        assert "task-local-private-progress" not in response.text
        assert not task.done()
        assert rid in manager._waiting
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("same_owner", [False, True])
async def test_read_session_reports_lease_owner_match_and_pid_presence(
    client, setup_sessions, monkeypatch, same_owner,
):
    from types import SimpleNamespace

    from ttakkari import generation
    from ttakkari.models import RuntimeControl, RuntimeGeneration
    from ttakkari.runs.manager import manager

    personal, _ = setup_sessions
    owner_id = uuid.uuid4()
    lease_owner_id = owner_id if same_owner else uuid.uuid4()
    async with sessionmaker()() as db:
        for gid in {owner_id, lease_owner_id}:
            db.add(RuntimeGeneration(id=gid, revision="a" * 40, api_url="http://test",
                                    pid=0, process_started_at=0, state="active"))
        await db.flush()
        db.add(RuntimeControl(id=1, active_generation=owner_id))
        run = Run(session_id=personal.id, workspace_id=personal.workspace_id,
                  engine="codex", status=RunStatus.running, prompt="review", pid=4321)
        db.add(run)
        await db.flush()
        db.add(RuntimeLease(run_id=run.id, generation_id=lease_owner_id,
                            session_id=personal.id, lane="workers"))
        await db.commit()
    runtime = SimpleNamespace(id=owner_id, role="api", execution_mode="local", draining=False,
                              http_inflight=0)
    monkeypatch.setattr(generation, "current", runtime)
    monkeypatch.setattr(manager, "_runtime", None)
    monkeypatch.setattr(manager, "_restart_waker", None)
    response = await client.get(f"/api/agent/sessions/{personal.id}",
        headers={"Authorization": f"Bearer {issue_token(personal.id)}"})
    assert response.status_code == 200
    turn = response.json()["turns"][0]
    assert turn["lease_owned"] is True
    assert turn["lease_owner_matches_claim_owner"] is same_owner
    assert turn["pid_recorded"] is True
    assert '"pid": 4321' not in response.text


@pytest.mark.parametrize("execution_mode", ["local", "remote"])
async def test_read_session_does_not_claim_remote_task_absence(
    client, setup_sessions, monkeypatch, execution_mode,
):
    from types import SimpleNamespace

    from ttakkari import generation
    from ttakkari.runs.manager import manager

    personal, _ = setup_sessions
    async with sessionmaker()() as db:
        run = Run(session_id=personal.id, workspace_id=personal.workspace_id,
                  engine="codex", status=RunStatus.queued, prompt="review")
        db.add(run)
        await db.commit()
    runtime = SimpleNamespace(id=uuid.uuid4(), role="api", execution_mode=execution_mode, draining=False,
                              http_inflight=0)
    monkeypatch.setattr(generation, "current", runtime)
    monkeypatch.setattr(manager, "_runtime", None)
    response = await client.get(f"/api/agent/sessions/{personal.id}",
        headers={"Authorization": f"Bearer {issue_token(personal.id)}"})
    assert response.status_code == 200
    assert response.json()["execution"]["local_coordinator"] is False
    assert response.json()["turns"][0]["local_dispatch"] is None


async def test_dispatch_await_chain_is_bounded():
    import asyncio

    from ttakkari.runs.diagnostics import _await_chain

    gate = asyncio.Event()

    async def descend(depth):
        if depth:
            await descend(depth - 1)
        else:
            await gate.wait()

    task = asyncio.create_task(descend(20))
    await asyncio.sleep(0)
    try:
        assert _await_chain(task) == ["descend"] * 8
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
