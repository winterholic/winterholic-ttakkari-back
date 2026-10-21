"""Authenticated ASGI coverage for the delivery opt-in boundary and repair cap."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from ttakkari.db import sessionmaker
from ttakkari.delivery.merge_queue import _record_validation_failure
from ttakkari.delivery.provenance import register_change_set
from ttakkari.delivery.worktree import run_git
from ttakkari.delivery_models import ChangeSet, ChangeSetStatus
from ttakkari.models import ChatSession, Run, Workspace
from ttakkari.orchestrator.runtime import issue_token


def _git(cwd: Path, *args: str) -> None:
    result = run_git(list(args), cwd)
    assert result.returncode == 0, result.stderr


async def _worker_request(client, auth, root: Path, enabled: bool, monkeypatch, *, title="Implement a small code change"):
    import ttakkari.api.orchestrator as orchestrator_api
    settings = orchestrator_api.get_settings()
    monkeypatch.setattr(orchestrator_api, "get_settings", lambda: settings.model_copy(update={"delivery_pipeline_enabled": enabled}))
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "ASGI Test")
    _git(root, "config", "user.email", "asgi@example.invalid")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "base")
    workspace = (await client.post("/api/workspaces", headers=auth,
        json={"name": root.name, "root_path": str(root)})).json()
    session = (await client.post("/api/sessions", headers=auth,
        json={"workspace_id": workspace["id"], "kind": "orchestrator", "engine": "claude"})).json()
    headers = {"Authorization": f"Bearer {issue_token(uuid.UUID(session['id']))}"}
    response = await client.post("/api/orchestrator/workers", headers=headers, json={
        "title": title, "prompt": "Add a source file", "engine": "qwen-local",
        "model": "Qwen3.8-Flash-Next-Q2_K", "task_type": "routine",
    })
    assert response.status_code == 201, response.text
    return workspace, session, response.json()


@pytest.mark.asyncio
async def test_authenticated_workers_opt_in_isolates_only_writable_git_code_tasks(client, auth, make_dir, monkeypatch):
    root = make_dir()
    workspace, session, worker = await _worker_request(client, auth, root, True, monkeypatch)
    async with sessionmaker()() as db:
        child = await db.get(ChatSession, uuid.UUID(worker["worker_id"]))
        isolated = await db.get(Workspace, child.workspace_id)
        assert child.parent_id == uuid.UUID(session["id"])
        assert child.workspace_id != uuid.UUID(workspace["id"])
        assert Path(isolated.root_path).is_dir()
        from ttakkari.config import get_settings
        assert Path(isolated.root_path).resolve().parent == (get_settings().home / "worktrees").resolve()
        assert Path(isolated.root_path).resolve() in {Path(p).resolve() for p in isolated.extra_writable_roots}
        change = await db.scalar(select(ChangeSet).where(ChangeSet.session_id == child.id))
        assert change is not None and change.worktree_path == isolated.root_path
        assert change.repo_path == str(root.resolve()) and change.status == ChangeSetStatus.registered

    disabled_root = make_dir()
    base_workspace, _, off_worker = await _worker_request(client, auth, disabled_root, False, monkeypatch)
    readonly_root = make_dir()
    read_workspace, _, readonly_worker = await _worker_request(
        client, auth, readonly_root, True, monkeypatch, title="Review and inspect the repository"
    )
    async with sessionmaker()() as db:
        off = await db.get(ChatSession, uuid.UUID(off_worker["worker_id"]))
        readonly = await db.get(ChatSession, uuid.UUID(readonly_worker["worker_id"]))
        assert off.workspace_id == uuid.UUID(base_workspace["id"])
        assert readonly.workspace_id == uuid.UUID(read_workspace["id"])
        assert await db.scalar(select(ChangeSet.id).where(ChangeSet.session_id.in_([off.id, readonly.id]))) is None


@pytest.mark.asyncio
async def test_repair_dispatch_never_queues_run_past_change_set_limit(client, auth, make_dir):
    """The real ChangeSet policy is checked before a repair dispatcher can enqueue a worker Run."""
    root = make_dir()
    workspace = (await client.post("/api/workspaces", headers=auth,
        json={"name": root.name, "root_path": str(root)})).json()
    session = (await client.post("/api/sessions", headers=auth,
        json={"workspace_id": workspace["id"], "kind": "orchestrator", "engine": "claude"})).json()
    async with sessionmaker()() as db:
        cs = await register_change_set(db, session_id=uuid.UUID(session["id"]), run_id=None,
            repo_path=str(root), branch_name="work/test", worktree_path=str(root), base_sha="0" * 40)
        cs.status = ChangeSetStatus.validating_stage1
        cs.repair_attempts = 3
        await db.flush()

        from ttakkari.delivery.cli import _dispatch_repair

        before = await db.scalar(select(func.count()).select_from(Run).where(Run.session_id == uuid.UUID(session["id"])))

        result = await _record_validation_failure(
            db, cs, "Stage 1", "failing test", ("test_regression", "AssertionError"), _dispatch_repair
        )
        assert not result.success
        assert result.status == ChangeSetStatus.rejected
        assert "상한" in result.reason
        after = await db.scalar(select(func.count()).select_from(Run).where(Run.session_id == uuid.UUID(session["id"])))
        assert after == before
        await db.rollback()
