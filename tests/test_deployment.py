from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from conftest import agent_calls
from helpers import start_run, wait_run

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import Run
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs.manager import resume_queued


def deployment():
    from ttakkari import deployment

    return deployment


def test_reservation_survives_requester_and_deduplicates(tmp_path):
    m = deployment()
    store = m.JobStore(tmp_path)
    job = store.request("a" * 40)
    assert m.JobStore(tmp_path).get(job["id"])["status"] == "requested"
    assert store.request("a" * 40)["id"] == job["id"]
    with pytest.raises(ValueError, match="진행 중"):
        store.request("b" * 40)
    with pytest.raises(ValueError):
        store.get("../../etc/passwd")


def test_admission_serializes_start_against_restart_and_unlocks(tmp_path):
    gate = deployment().Admission(tmp_path)
    running = gate.acquire()
    assert running is not None
    assert gate.acquire(exclusive=True) is None
    running.close()
    restarting = gate.acquire(exclusive=True)
    assert restarting is not None
    assert gate.acquire() is None
    restarting.close()
    admitted = gate.acquire()
    assert admitted is not None
    admitted.close()


async def test_waits_for_requesting_run_too_then_rechecks_under_lock(
    tmp_path, monkeypatch
):
    m = deployment()
    controller = m.Controller(tmp_path, poll_seconds=0)
    job = controller.store.request("a" * 40)
    counts = iter([1, 0, 1, 0, 0])
    observations = []

    async def count(own):
        observations.append(own)
        return next(counts)

    monkeypatch.setattr(m, "_active_run_count", count)
    lease = await controller.wait_idle(job)
    assert observations == [None] * 5
    assert controller.admission.acquire() is None
    lease.close()


async def test_preparation_failure_never_restarts_and_is_persisted(
    tmp_path, monkeypatch
):
    m = deployment()
    controller = m.Controller(tmp_path, poll_seconds=0)
    job = controller.store.request("a" * 40)
    restarted = []

    async def fail(_job):
        raise RuntimeError("tests failed")

    monkeypatch.setattr(controller, "prepare", fail)
    monkeypatch.setattr(controller, "restart", lambda: restarted.append(True))
    await controller.process(job)
    saved = m.JobStore(tmp_path).get(job["id"])
    assert saved["status"] == "failed" and "tests failed" in saved["error"]
    assert restarted == []
    assert controller.admission.acquire() is not None


async def test_restart_success_requires_requested_revision_and_changed_pid(
    tmp_path, monkeypatch
):
    m = deployment()
    controller = m.Controller(tmp_path, poll_seconds=0, verify_seconds=0.01)
    job = controller.store.request("a" * 40)
    monkeypatch.setattr(
        controller,
        "runtime",
        lambda: asyncio.sleep(0, result={"revision": "a" * 40, "pid": 42}),
    )
    with pytest.raises(RuntimeError, match="버전|PID"):
        await controller.verify(job, old_pid=42)
    monkeypatch.setattr(
        controller,
        "runtime",
        lambda: asyncio.sleep(0, result={"revision": "b" * 40, "pid": 43}),
    )
    with pytest.raises(RuntimeError, match="버전|PID"):
        await controller.verify(job, old_pid=42)
    monkeypatch.setattr(
        controller,
        "runtime",
        lambda: asyncio.sleep(0, result={"revision": "a" * 40, "pid": 43}),
    )
    monkeypatch.setattr(controller, "public_ready", lambda _job: asyncio.sleep(0))
    await controller.verify(job, old_pid=42)


async def test_reserved_restart_blocks_new_runs_without_losing_queue(
    client, auth, new_session
):
    m = deployment()
    gate = m.Admission(get_settings().home / "deploy")
    lease = gate.acquire(exclusive=True)
    assert lease is not None
    try:
        _ws, sess = await new_session()
        run = await start_run(client, auth, sess["id"], "hello after restart")
        await asyncio.sleep(0.1)
        async with sessionmaker()() as db:
            row = await db.get(Run, uuid.UUID(run["id"]))
            assert row.status == "queued"
        assert agent_calls() == []
    finally:
        lease.close()
    await resume_queued()
    result = await wait_run(client, auth, run["id"])
    assert result["status"] == "succeeded"
    assert len(agent_calls()) == 1
    lock = gate.acquire(exclusive=True)
    assert lock is not None
    lock.close()


async def test_version_is_public_and_frozen_at_process_start(client, monkeypatch):
    r = await client.get("/api/version")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    value = r.json()
    assert value["pid"] == os.getpid()
    assert len(value["revision"]) == 40
    monkeypatch.setattr(deployment(), "revision", lambda: "f" * 40)
    assert (await client.get("/api/version")).json() == value


async def test_agent_reserves_deploy_without_writing_protected_state_directly(
    client, auth, new_session, monkeypatch, tmp_path
):
    m = deployment()
    _ws, sess = await new_session()
    headers = {"Authorization": f"Bearer {issue_token(uuid.UUID(sess['id']))}"}
    monkeypatch.setattr(m, "revision", lambda: "a" * 40)
    monkeypatch.setattr(m, "ensure_revision", lambda _sha: None)
    monkeypatch.setattr(m, "state_dir", lambda: tmp_path)
    assert (
        await client.post("/api/agent/deploy", json={}, headers=auth)
    ).status_code == 401
    assert (await client.post("/api/agent/deploy", json={})).status_code == 401
    r = await client.post(
        "/api/agent/deploy", json={"revision": "a" * 40}, headers=headers
    )
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "requested"
    assert (await client.get(f"/api/agent/deploy/{job['id']}", headers=headers)).json()[
        "id"
    ] == job["id"]
    assert (
        await client.post(
            "/api/agent/deploy", json={"revision": "bad"}, headers=headers
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/agent/deploy", json={"skip_tests": True}, headers=headers
        )
    ).status_code == 422


async def test_generation_reservations_accept_active_runs_and_an_existing_deployment(
    client, auth, new_session, monkeypatch, tmp_path
):
    from helpers import wait_status

    m = deployment()
    _ws, sess = await new_session()
    run = await start_run(client, auth, sess["id"], "SLEEP")
    await wait_status(client, auth, run["id"], "running")
    headers = {"Authorization": f"Bearer {issue_token(uuid.UUID(sess['id']))}"}
    # The activation marker overrides the install default of idle.
    monkeypatch.setattr(m, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(get_settings(), "deploy_strategy", "idle")
    (tmp_path / "generations-enabled.json").write_text('{"protocol":1,"strategy":"generations"}')
    monkeypatch.setattr(m, "ensure_revision", lambda _sha: None)
    first = m.JobStore(tmp_path).request("a" * 40)
    m.JobStore(tmp_path).update(first["id"], status="draining")
    try:
        response = await client.post("/api/agent/deploy", json={"revision": "b" * 40}, headers=headers)
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["strategy"] == "generations" and job["status"] == "requested"
        assert job["id"] != first["id"]
        assert (await client.get(f"/api/runs/{run['id']}", headers=auth)).json()["status"] == "running"
        assert m.JobStore(tmp_path).get(first["id"])["status"] == "draining"
    finally:
        await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)
        await wait_run(client, auth, run["id"])


async def test_deferred_queue_resumes_automatically(client, auth, new_session):
    m = deployment()
    gate = m.Admission(get_settings().home / "deploy")
    lease = gate.acquire(exclusive=True)
    try:
        _ws, sess = await new_session()
        run = await start_run(client, auth, sess["id"], "automatic resume")
        await asyncio.sleep(0.05)
        assert agent_calls() == []
    finally:
        lease.close()
    assert (await wait_run(client, auth, run["id"]))["status"] == "succeeded"


async def test_cancel_before_first_tick_releases_admission(monkeypatch):
    from ttakkari.runs.manager import RunManager

    m = deployment()
    manager = RunManager()
    rid = uuid.uuid4()
    assert manager.start(rid)
    gate = m.Admission(get_settings().home / "deploy")
    assert gate.acquire(exclusive=True) is None
    task = manager._handles[rid].task
    await manager.cancel(rid)
    await asyncio.gather(task, return_exceptions=True)
    lease = gate.acquire(exclusive=True)
    assert lease is not None
    lease.close()


async def test_restart_verification_failure_releases_gate_and_preserves_last_good(
    tmp_path, monkeypatch
):
    m = deployment()
    controller = m.Controller(tmp_path, poll_seconds=0)
    job = controller.store.request("a" * 40)
    controller.phase(job, "waiting")
    (tmp_path / "current").write_text("b" * 40 + "\n")
    (tmp_path / "previous").write_text("c" * 40 + "\n")
    monkeypatch.setattr(m, "ensure_revision", lambda _: None)
    monkeypatch.setattr(m, "_active_run_count", lambda _: asyncio.sleep(0, result=0))
    monkeypatch.setattr(
        controller, "runtime", lambda: asyncio.sleep(0, result={"pid": 12})
    )
    monkeypatch.setattr(controller, "command", lambda *_args: asyncio.sleep(0))
    monkeypatch.setattr(controller, "restart", lambda: None)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("wrong version")

    monkeypatch.setattr(controller, "verify", fail)
    await controller.process(job)
    assert controller.store.get(job["id"])["status"] == "failed"
    assert (tmp_path / "current").read_text().strip() == "b" * 40
    assert (tmp_path / "previous").read_text().strip() == "c" * 40
    lease = controller.admission.acquire(exclusive=True)
    assert lease is not None
    lease.close()


async def test_controller_recovers_verified_restart_without_restarting_again(
    tmp_path, monkeypatch
):
    m = deployment()
    controller = m.Controller(tmp_path, poll_seconds=0)
    job = controller.store.request("a" * 40)
    controller.phase(job, "verifying", old_pid=12, previous_revision="b" * 40)
    monkeypatch.setattr(controller, "verify", lambda *_args, **_kw: asyncio.sleep(0))
    restarted = []
    monkeypatch.setattr(controller, "restart", lambda: restarted.append(True))
    await controller.process(controller.store.get(job["id"]))
    assert restarted == []
    assert controller.store.get(job["id"])["status"] == "succeeded"
    assert (tmp_path / "current").read_text().strip() == "a" * 40
    assert (tmp_path / "previous").read_text().strip() == "b" * 40


async def test_company_session_can_validate_deployment_without_pool_denial(client, auth, make_dir):
    ws = (
        await client.post(
            "/api/workspaces",
            json={"name": "company", "root_path": str(make_dir())},
            headers=auth,
        )
    ).json()
    sess = (
        await client.post(
            "/api/sessions",
            json={"workspace_id": ws["id"], "pool": "work"},
            headers=auth,
        )
    ).json()
    headers = {"Authorization": f"Bearer {issue_token(uuid.UUID(sess['id']))}"}
    assert (
        await client.post(
            "/api/agent/deploy", json={"revision": "a" * 40}, headers=headers
        )
    ).status_code == 409
    assert (
        await client.get(f"/api/agent/deploy/{uuid.uuid4()}", headers=headers)
    ).status_code == 404


def test_multiple_running_leases_do_not_release_each_other(tmp_path):
    gate = deployment().Admission(tmp_path)
    first, second = gate.acquire(), gate.acquire()
    assert first is not None and second is not None
    first.close()
    assert gate.acquire(exclusive=True) is None
    second.close()
    lease = gate.acquire(exclusive=True)
    assert lease is not None
    lease.close()


async def test_controller_process_death_releases_admission(tmp_path):
    import sys

    gate = deployment().Admission(tmp_path)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; from pathlib import Path; from ttakkari.deployment import Admission; "
        "lease=Admission(Path(sys.argv[1])).acquire(exclusive=True); "
        'print("ready", flush=True); sys.stdin.read()',
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(proc.stdout.readline(), 5) == b"ready\n"
        assert gate.acquire() is None
    finally:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)
    lease = gate.acquire()
    assert lease is not None
    lease.close()


def test_generations_reservation_ignores_dirty_tree_and_moved_head(monkeypatch, tmp_path):

    m = deployment()
    from conftest import git as _git
    git = lambda *a: _git(tmp_path, *a)
    git("init", "-q")
    (tmp_path / "f").write_text("1")
    git("add", "f")
    git("commit", "-qm", "one")
    first = git("rev-parse", "HEAD")
    (tmp_path / "f").write_text("2")
    git("commit", "-qam", "two")  # 다른 세션이 HEAD 를 앞으로 옮긴 상황
    (tmp_path / "dirty").write_text("x")  # 다른 세션의 미커밋 파일
    monkeypatch.setattr(m, "resolve_source_root", lambda: tmp_path)

    monkeypatch.setattr(m, "strategy", lambda: "generations")
    m.ensure_revision(first)  # 409 가 나지 않는다
    with pytest.raises(ValueError):
        m.ensure_revision("0" * 40)

    monkeypatch.setattr(m, "strategy", lambda: "legacy")
    with pytest.raises(ValueError):
        m.ensure_revision(first)


@pytest.mark.parametrize("published", [True, False])
def test_generations_reservation_of_a_commit_outside_local_head(monkeypatch, tmp_path, published):
    from conftest import git

    m = deployment()
    git(tmp_path, "init", "-q")
    (tmp_path / "version").write_text("base")
    git(tmp_path, "add", "version")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "version").write_text("published candidate")
    git(tmp_path, "commit", "-qam", "candidate")
    candidate = git(tmp_path, "rev-parse", "HEAD")
    ref = "refs/remotes/origin/main" if published else "refs/heads/unfinished-feature"
    git(tmp_path, "update-ref", ref, candidate)
    git(tmp_path, "checkout", "--detach", base)
    (tmp_path / "version").write_text("local concurrent work")
    git(tmp_path, "commit", "-qam", "local")
    local_head = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "dirty").write_text("preserve me")
    monkeypatch.setattr(m, "resolve_source_root", lambda: tmp_path)
    monkeypatch.setattr(m, "strategy", lambda: "generations")

    if published:
        m.ensure_revision(candidate)
    else:
        with pytest.raises(ValueError):
            m.ensure_revision(candidate)
    assert git(tmp_path, "rev-parse", "HEAD") == local_head
    assert (tmp_path / "dirty").read_text() == "preserve me"
    assert (tmp_path / "version").read_text() == "local concurrent work"
