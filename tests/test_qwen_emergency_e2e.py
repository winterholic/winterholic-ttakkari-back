"""전 클라우드 쿨다운 + ready Qwen에서 작업·기획 세션이 Qwen으로 이어서 동작한다.

반례 1(초기 비상 분기 추락)·2(trigger=emergency)·5(Qwen 실패 시 클라우드 대기)를
전용 테스트 DB 통합 시나리오로 고정한다. 모델 없이 고정 가짜 qwen-worker로만 돈다.
"""
import asyncio
import json
import shlex
import uuid
from pathlib import Path

import pytest
from helpers import run_events, start_run, wait_run

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles, worker_routing
from ttakkari.runs.manager import QwenLocalAdapter
from ttakkari.runs.manager import manager as run_manager


def _write_emergency_worker(tmp_path: Path, *, run_result: dict | str | None = None,
                            exit_code: int = 0) -> Path:
    """doctor는 ready, run은 고정 성공/실패 JSON 한 줄. task 파일은 스냅샷으로 남긴다."""
    worker = tmp_path / "bin/qwen-worker"
    snapshot = tmp_path / "task-snapshot.txt"
    worker.parent.mkdir(parents=True, exist_ok=True)
    if run_result is None:
        run_result = {"schema": 1, "run_id": "fixture", "status": "success", "exit_code": 0,
                      "session_id": "qwen-session", "result_text": "fixture complete",
                      "files": {"added": [], "modified": [], "deleted": []},
                      "metrics": {"host_peaks": {"server_rss_max_bytes": 1024}, "elapsed_seconds": 0.1}}
    result_line = run_result if isinstance(run_result, str) else json.dumps(run_result)
    script = (
        "#!/bin/sh\n"
        "if [ \"$1\" = doctor ]; then "
        "printf '%s\\n' '{\"contract\":\"qwen-local-readiness-v1\",\"checks\":{\"seatbelt\":true,\"hostmon\":true,\"fixture\":true}}'; exit 0; fi\n"
        "prev=\"\"; task=\"\"\n"
        "for a in \"$@\"; do if [ \"$prev\" = \"--task-file\" ]; then task=\"$a\"; fi; prev=\"$a\"; done\n"
        f"if [ ! -f '{snapshot}' ] || grep -q \"역할=worker\" \"$task\"; then cp \"$task\" '{snapshot}'; fi\n"
        f"printf '%s\\n' {shlex.quote(result_line)}\n"
        f"exit {exit_code}\n"
    )
    worker.write_text(script)
    worker.chmod(0o755)
    return worker


async def _cooldown_all_cloud(client, auth) -> list[str]:
    """개인 클라우드 계정(Claude·Codex·Antigravity)을 만들어 전부 쿨다운한다."""
    existing = (await client.get("/api/accounts", headers=auth)).json()
    ids = [a["id"] for a in existing]
    for name, engine in (("c", "claude"), ("x", "codex"), ("agy", "antigravity")):
        created = await client.post("/api/accounts", json={"name": f"qwen-e2e-{name}", "engine": engine},
                                    headers=auth)
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    for aid in ids:
        profiles.cool_down(uuid.UUID(aid), 3600)
    return ids


async def _spawn_worker(client, orch_auth, *, task_type: str, prompt: str = "emergency fixture task") -> dict:
    created = await client.post("/api/orchestrator/workers", headers=orch_auth, json={
        "title": "Emergency Qwen fixture", "prompt": prompt, "task_type": task_type,
    })
    assert created.status_code == 201, created.text
    return created.json()


def _routing_receipts(events: list[dict]) -> list[dict]:
    return [e["payload"] for e in events if e["type"] == "worker.routing"]


@pytest.mark.asyncio
async def test_all_cloud_limited_routes_worker_to_qwen_with_emergency_trigger(
        client, auth, make_dir, tmp_path, monkeypatch):
    """반례 1·2: 초기 비상 분기가 failed로 떨어지지 않고 trigger=emergency로 Qwen 성공."""
    from ttakkari.runs import manager as run_manager_mod

    worker = _write_emergency_worker(tmp_path)
    monkeypatch.setattr(run_manager_mod, "QWEN_LOCAL_ADAPTER",
                        QwenLocalAdapter(worker_path=worker, enabled=True))
    await _cooldown_all_cloud(client, auth)
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"},
                                headers=auth)).json()
    orch_auth = {"Authorization": f"Bearer {issue_token(parent['id'])}"}
    spawned = await _spawn_worker(client, orch_auth, task_type="routine")
    run = await wait_run(client, auth, spawned["active_run_id"], timeout=30.0)
    assert run["status"] == "succeeded", run
    assert run["engine"] == "qwen-local"
    assert run["model"] == worker_routing.qwen_local_model()
    events = await run_events(client, auth, run["id"])
    receipts = _routing_receipts(events)
    assert receipts, "worker.routing receipt missing"
    assert all(r["trigger"] == "emergency" for r in receipts), receipts
    assert any(r.get("mode") == "worker-handoff" for r in receipts)
    fallback = next(e["payload"] for e in events if e["type"] == "engine.fallback")
    assert fallback["to_engine"] == "qwen-local"
    assert fallback["reason"] == "usage_limit_all_cloud_limited"
    # 반례 7: 작업 파일은 짧은 계약만 담는다(5.6KB 클라우드 노트 없음).
    snapshot = (tmp_path / "task-snapshot.txt").read_text()
    assert "Qwen local 계약" in snapshot
    assert "[User's standing instructions" not in snapshot
    assert "emergency fixture task" in snapshot
    assert len(snapshot) < 2000, len(snapshot)


@pytest.mark.asyncio
async def test_all_cloud_limited_runs_orchestrator_degraded_on_qwen(
        client, auth, make_dir, tmp_path, monkeypatch):
    """반례 1(기획 degraded 경로): 기획 세션도 failed가 아니라 Qwen degraded로 성공."""
    from ttakkari.runs import manager as run_manager_mod

    worker = _write_emergency_worker(tmp_path)
    monkeypatch.setattr(run_manager_mod, "QWEN_LOCAL_ADAPTER",
                        QwenLocalAdapter(worker_path=worker, enabled=True))
    await _cooldown_all_cloud(client, auth)
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    sess = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"},
                              headers=auth)).json()
    started = await start_run(client, auth, sess["id"], "plan the fixture on emergency qwen")
    run = await wait_run(client, auth, started["id"], timeout=30.0)
    assert run["status"] == "succeeded", run
    assert run["engine"] == "qwen-local"
    events = await run_events(client, auth, run["id"])
    receipts = _routing_receipts(events)
    assert receipts and all(r["trigger"] == "emergency" for r in receipts), receipts
    assert any(r.get("mode") == "orchestrator-degraded" for r in receipts)
    snapshot = (tmp_path / "task-snapshot.txt").read_text()
    assert "spawn-queue" in snapshot, "degraded note must be in the orchestrator prompt"


@pytest.mark.asyncio
async def test_emergency_qwen_failure_returns_to_cloud_wait_not_failed(
        client, auth, make_dir, tmp_path, monkeypatch):
    """반례 5: 비상 Qwen 실패는 failed가 아니라 클라우드 초기화 대기로 돌아간다."""
    from ttakkari.runs import manager as run_manager_mod

    worker = _write_emergency_worker(
        tmp_path,
        run_result={"schema": 1, "status": "error", "exit_code": 2, "result_text": None,
                    "error": {"kind": "guard", "message": "qwen blew up"}},
        exit_code=2)
    monkeypatch.setattr(run_manager_mod, "QWEN_LOCAL_ADAPTER",
                        QwenLocalAdapter(worker_path=worker, enabled=True))
    await _cooldown_all_cloud(client, auth)
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"},
                                headers=auth)).json()
    orch_auth = {"Authorization": f"Bearer {issue_token(parent['id'])}"}
    spawned = await _spawn_worker(client, orch_auth, task_type="routine")
    run_id = spawned["active_run_id"]
    try:
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            run = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
            if run["status"] == "queued" and run.get("waiting_limit"):
                break
            assert run["status"] not in {"succeeded", "failed"}, run
            assert asyncio.get_running_loop().time() < deadline, f"no cloud wait: {run['status']}"
            await asyncio.sleep(0.1)
        events = await run_events(client, auth, run_id)
        waiting = next(e["payload"] for e in events if e["type"] == "run.waiting")
        assert waiting["qwen_local"] == {"ready": False, "reason": "qwen_attempt_failed"}
        assert not any(e["type"] == "engine.fallback" and e["payload"].get("reason") == "qwen_failed"
                       for e in events), "no cloud was free, so no qwen->cloud switch"
    finally:
        run_manager._disarm(uuid.UUID(run_id))


@pytest.mark.asyncio
async def test_ineligible_design_task_waits_without_qwen_reason(
        client, auth, make_dir, tmp_path, monkeypatch):
    """반례 5: 자격 미달(design)인데 대기 사유에 qwen_local ready:false가 찍히지 않는다."""
    from ttakkari.runs import manager as run_manager_mod

    worker = _write_emergency_worker(tmp_path)
    monkeypatch.setattr(run_manager_mod, "QWEN_LOCAL_ADAPTER",
                        QwenLocalAdapter(worker_path=worker, enabled=True))
    await _cooldown_all_cloud(client, auth)
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"},
                                headers=auth)).json()
    orch_auth = {"Authorization": f"Bearer {issue_token(parent['id'])}"}
    spawned = await _spawn_worker(client, orch_auth, task_type="design")
    run_id = spawned["active_run_id"]
    try:
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            run = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
            if run["status"] == "queued" and run.get("waiting_limit"):
                break
            assert run["status"] not in {"succeeded", "failed"}, run
            assert asyncio.get_running_loop().time() < deadline, f"no wait: {run['status']}"
            await asyncio.sleep(0.1)
        events = await run_events(client, auth, run_id)
        waiting = next(e["payload"] for e in events if e["type"] == "run.waiting")
        assert "qwen_local" not in waiting, waiting
    finally:
        run_manager._disarm(uuid.UUID(run_id))


@pytest.mark.asyncio
async def test_degraded_orchestrator_spawn_queue_replays_as_workers(
        client, auth, make_dir, tmp_path, monkeypatch):
    """degraded 기획의 spawn-queue intent가 클라우드 회복 뒤 작업 세션으로 뜬다."""
    from ttakkari.runs import manager as run_manager_mod

    worker = _write_emergency_worker(tmp_path)
    monkeypatch.setattr(run_manager_mod, "QWEN_LOCAL_ADAPTER",
                        QwenLocalAdapter(worker_path=worker, enabled=True))
    await _cooldown_all_cloud(client, auth)
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    sess = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"},
                              headers=auth)).json()
    started = await start_run(client, auth, sess["id"], "plan then queue intents")
    run = await wait_run(client, auth, started["id"], timeout=30.0)
    assert run["status"] == "succeeded", run
    async with sessionmaker()() as db:
        sess_row = await db.get(ChatSession, uuid.UUID(sess["id"]))
        run_row = await db.get(Run, uuid.UUID(run["id"]))
        from pathlib import Path as _Path
        qdir = _Path(run_row.outbox_path) / worker_routing.QWEN_SPAWN_QUEUE_DIR
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "01-small-fix.json").write_text(json.dumps(
            {"title": "작은 수정", "prompt": "replayed fixture task", "engine": "auto",
             "task_type": "routine"}), encoding="utf-8")
        (qdir / "02-bad.json").write_text(json.dumps({"title": "", "prompt": "x"}), encoding="utf-8")
        await run_manager._replay_spawn_queue(db, sess_row, run_row, _Path(run_row.outbox_path))
        from sqlalchemy import select as _select
        titles = list(await db.scalars(
            _select(ChatSession.title).where(ChatSession.parent_id == uuid.UUID(sess["id"]))))
    assert "작은 수정" in titles, titles
    assert (qdir / "done" / "01-small-fix.json").exists()
    assert (qdir / "invalid" / "02-bad.json").exists()
