"""호스트 운영 작업(operations): 허용 호출·권한·임의 입력 거절·단일 실행·취소/시간초과·실패 보존·민감정보 비노출.

실제 통합은 conftest 의 격리 임시 PostgreSQL 클러스터에서 scripts/*.sh 를 그대로 돈다(운영 DB 접속 없음).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from authz03_canary import ensure_authz03
from sqlalchemy import func, select, text

from ttakkari import backup_status
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import AuditLog
from ttakkari.operations import jobs, plans, runner
from ttakkari.orchestrator.runtime import issue_token

pytestmark = [pytest.mark.security]

HAS_VENV = (plans.BACK / ".venv/bin/python").is_file()
needs_venv = pytest.mark.skipif(not HAS_VENV, reason="운영 스크립트가 쓰는 .venv 가 없는 체크아웃")


@pytest.fixture
async def env(make_dir):
    async with sessionmaker()() as db:
        return await ensure_authz03(db, make_dir)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """작업 기록·백업 디렉터리·앱 키를 테스트마다 비운다."""
    import shutil
    home = get_settings().home
    shutil.rmtree(home / "operations", ignore_errors=True)
    (home / "backup.key").unlink(missing_ok=True)
    backups = tmp_path / "backups"
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    yield backups
    shutil.rmtree(home / "operations", ignore_errors=True)


def _hdr(session_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(session_id)}"}


async def _wait(client, hdr, job_id, seconds=180):
    for _ in range(seconds * 4):
        body = (await client.get(f"/api/agent/operations/{job_id}", headers=hdr)).json()
        if body["status"] not in jobs.ACTIVE:
            return body
        await asyncio.sleep(0.25)
    raise AssertionError(f"작업이 끝나지 않았다: {body}")


async def _start(client, hdr, kind):
    r = await client.post("/api/agent/operations", json={"kind": kind}, headers=hdr)
    return r


# ---- 권한·입력 거절 ----

async def test_work_session_cannot_use_operations(client, env):
    work = _hdr(env.work_session_id)
    assert (await _start(client, work, "runtime_report")).status_code == 403
    assert (await client.get("/api/agent/operations", headers=work)).status_code == 403
    assert (await client.get(f"/api/agent/operations/{uuid.uuid4()}", headers=work)).status_code == 403
    assert (await client.post(f"/api/agent/operations/{uuid.uuid4()}/cancel", headers=work)).status_code == 403


async def test_unauthenticated_call_is_rejected(client):
    assert (await client.post("/api/agent/operations", json={"kind": "backup_status"})).status_code in (401, 403)


@pytest.mark.parametrize("payload", [
    {"kind": "bash"}, {"kind": "backup_db; rm -rf /"}, {"kind": "backup_db", "path": "/etc"},
    {"kind": "backup_db", "command": "id"}, {"kind": "restore_rehearsal", "target_db": "ttakkari"},
    {"kind": "restore_rehearsal", "db_url": "postgresql://x/y"}, {"kind": "data_snapshot", "source": "/Users"},
    {},
])
async def test_arbitrary_input_is_rejected(client, env, payload):
    r = await client.post("/api/agent/operations", json=payload, headers=_hdr(env.personal_session_id))
    assert r.status_code == 422
    assert not list((get_settings().home / "operations" / "jobs").glob("*.json"))


def test_heavy_kind_outside_allowlist_is_refused_by_plan():
    with pytest.raises(plans.Refused) as exc:
        plans.make_plan("bash", str(uuid.uuid4()))
    assert exc.value.status == 422


# ---- 읽기 전용 즉시 작업 ----

async def test_inline_reports_are_recorded(client, env):
    hdr = _hdr(env.personal_session_id)
    runtime = (await _start(client, hdr, "runtime_report")).json()
    assert runtime["status"] == "succeeded" and runtime["inline"] is True
    assert {"this_process", "control", "generations", "runs_running", "recent_deploys"} <= set(runtime["summary"])
    perf = (await _start(client, hdr, "performance_baseline")).json()
    assert perf["status"] == "succeeded"
    s = perf["summary"]
    assert s["synthetic"] and s["readonly"] and s["db_roundtrip"]["n"] == 20 and s["event_loop_lag"]["n"] == 20
    state = (await _start(client, hdr, "backup_status")).json()["summary"]
    assert state["db"] is None and state["data"] is None and state["key_source"] is None
    listing = (await client.get("/api/agent/operations", headers=hdr)).json()
    assert {j["kind"] for j in listing["jobs"]} == {"runtime_report", "performance_baseline", "backup_status"}
    assert set(listing["kinds"]) == set(plans.KINDS)
    async with sessionmaker()() as db:
        audited = (await db.execute(select(func.count()).select_from(AuditLog)
                                    .where(AuditLog.action == "operation.request",
                                           AuditLog.actor == "agent"))).scalar()
    assert audited >= 3


# ---- 단일 실행·취소·상태 보존 ----

def _active_job(kind: str, **extra) -> dict:
    job = jobs.create(kind, str(uuid.uuid4()))
    return jobs.write(job | {"status": "running", "runner_pid": os.getpid(), "runner_started": 1.0, **extra})


async def test_duplicate_and_conflicting_starts(client, env, monkeypatch):
    monkeypatch.setattr(jobs, "runner_alive", lambda job: True)
    hdr = _hdr(env.personal_session_id)
    running = _active_job("backup_db")
    again = await _start(client, hdr, "backup_db")
    assert again.status_code == 202 and again.json()["id"] == running["id"] and again.json()["deduplicated"]
    other = await _start(client, hdr, "restore_rehearsal")
    assert other.status_code == 409 and "backup_db" in other.text
    # 읽기 전용 즉시 작업은 무거운 작업과 별개로 돈다
    assert (await _start(client, hdr, "backup_status")).status_code == 202


async def test_cancel_signals_runner_and_keeps_record(client, env, monkeypatch):
    monkeypatch.setattr(jobs, "runner_alive", lambda job: True)
    sent = []
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    job = _active_job("data_snapshot")
    hdr = _hdr(env.personal_session_id)
    r = await client.post(f"/api/agent/operations/{job['id']}/cancel", headers=hdr)
    assert r.status_code == 200 and r.json()["cancel_requested_at"] and r.json()["status"] == "running"
    assert sent == [(os.getpid(), jobs.signal.SIGTERM)]
    assert (await client.post(f"/api/agent/operations/{uuid.uuid4()}/cancel", headers=hdr)).status_code == 404
    assert (await client.get("/api/agent/operations/not-a-uuid", headers=hdr)).status_code == 404


async def test_cancel_queued_job_before_runner_starts(client, env):
    job = jobs.create("data_snapshot", str(uuid.uuid4()))  # 러너 pid 가 아직 없는 queued 기록
    hdr = _hdr(env.personal_session_id)
    body = (await client.post(f"/api/agent/operations/{job['id']}/cancel", headers=hdr)).json()
    assert body["status"] == "cancelled" and body["finished_at"]
    assert (await client.post(f"/api/agent/operations/{job['id']}/cancel", headers=hdr)).status_code == 409


async def test_lost_runner_is_preserved_as_failed_not_succeeded(client, env):
    job = jobs.write(jobs.create("backup_db", str(uuid.uuid4())) | {"status": "running", "runner_pid": 2 ** 22,
                                                                      "runner_started": 1.0})
    body = (await client.get(f"/api/agent/operations/{job['id']}", headers=_hdr(env.personal_session_id))).json()
    assert body["status"] == "failed" and body["error"].startswith("runner_lost") and body["finished_at"]


# ---- 러너: 시간 초과·취소·실패 보존·비밀 마스킹 ----

def _fake_plan(script: str, timeout=30) -> plans.Plan:
    return plans.Plan("backup_db", ["/bin/bash", "-c", script], {"PATH": "/usr/bin:/bin"}, timeout)


def test_runner_times_out_and_kills_process_group():
    out = __import__("tempfile").TemporaryFile()
    status, _ = runner._execute(_fake_plan("sleep 60 & sleep 60", timeout=1), out)
    assert status == "timeout"


def test_runner_cancel_flag_stops_child(monkeypatch):
    monkeypatch.setattr(runner, "_cancelled", True)
    out = __import__("tempfile").TemporaryFile()
    status, _ = runner._execute(_fake_plan("sleep 60"), out)
    assert status == "cancelled"


async def test_failed_run_keeps_error_and_masks_secrets(monkeypatch):
    secret = "hunter2-very-secret-value"
    key = get_settings().home / "backup.key"
    home_marker = str(get_settings().home)
    script = (f"echo 'password={secret}'; echo 'DB=postgresql://admin:{secret}@db.example/x'; "
              f"echo 'path={home_marker}/backup.key'; echo boom >&2; exit 7")
    monkeypatch.setattr(plans, "make_plan", lambda kind, job_id: _fake_plan(script))
    job = jobs.create("backup_db", "s")
    assert await runner._run(job["id"]) == 0
    done = jobs.get(job["id"])
    assert done["status"] == "failed" and done["exit_code"] == 7 and done["finished_at"]
    blob = json.dumps(done, ensure_ascii=False)
    assert secret not in blob and home_marker not in blob and "boom" in done["output_tail"]
    assert "<home>" in done["output_tail"] and not key.exists()


async def test_run_never_starts_twice(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "s") | {"status": "cancelled", "finished_at": jobs.now()})
    called = []
    monkeypatch.setattr(plans, "make_plan", lambda *a: called.append(a))
    assert await runner._run(job["id"]) == 0 and not called


# ---- 복원 안전 ----

async def test_restore_refuses_remote_database(client, env, monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", "postgresql+asyncpg://user:pw@prod.example.com:5432/ttakkari")
    r = await _start(client, _hdr(env.personal_session_id), "restore_rehearsal")
    assert r.status_code == 409 and "로컬" in r.text
    assert "pw" not in r.text and "prod.example.com" not in r.text


def test_restore_manifest_must_live_in_backup_dir(isolated_state, tmp_path):
    outside = tmp_path / "ttakkari-20260101-000000-1.manifest.json"
    outside.write_text(json.dumps({"encrypted": True}))
    isolated_state.mkdir()
    (isolated_state / "latest.json").write_text(json.dumps({"created_at": "x", "manifest": str(outside)}))
    with pytest.raises(plans.Refused):
        plans.latest_manifest("db")
    link = isolated_state / "ttakkari-20260101-000000-1.manifest.json"
    link.symlink_to(outside)
    (isolated_state / "latest.json").write_text(json.dumps({"created_at": "x", "manifest": str(link)}))
    with pytest.raises(plans.Refused):
        plans.latest_manifest("db")


def test_restore_target_is_fixed_isolated_name(isolated_state, monkeypatch):
    isolated_state.mkdir()
    manifest = isolated_state / "ttakkari-20260101-000000-1.manifest.json"
    manifest.write_text(json.dumps({"encrypted": False, "tables": {}}))
    (isolated_state / "latest.json").write_text(json.dumps({"created_at": "x", "manifest": str(manifest)}))
    monkeypatch.setattr(plans, "_need", lambda *a: None)
    job_id = str(uuid.uuid4())
    plan = plans.make_plan("restore_rehearsal", job_id)
    target = plan.identity["target_db"]
    assert target.startswith(plans.RESTORE_DB_PREFIX) and target != plans.local_database()[1]
    assert plan.argv[plan.argv.index("--target-db") + 1] == target
    assert "TTAKKARI_DATABASE_URL" in plan.env and "--db-url" not in plan.argv


def test_snapshot_never_includes_home_or_backup_dir(monkeypatch, isolated_state):
    monkeypatch.setattr(plans, "_need", lambda *a: None)
    get_settings().data_dir.mkdir(parents=True, exist_ok=True)
    plan = plans.make_plan("data_snapshot", str(uuid.uuid4()))
    source = plan.argv[plan.argv.index("--source") + 1]
    assert source == str(get_settings().data_dir.resolve()) and source != str(get_settings().home)
    monkeypatch.setenv("BACKUP_DIR", str(get_settings().data_dir / "inside"))
    with pytest.raises(plans.Refused):
        plans.make_plan("data_snapshot", str(uuid.uuid4()))


# ---- 실제 격리 통합: 암호화 백업 → 격리 복원 → 데이터 스냅샷 → 격리 검증 ----

@needs_venv
async def test_backup_restore_snapshot_end_to_end(client, env, isolated_state):
    hdr = _hdr(env.personal_session_id)
    async with sessionmaker()() as db:
        rows_before = (await db.execute(text("SELECT count(*) FROM sessions"))).scalar()
    created = await _start(client, hdr, "backup_db")
    assert created.status_code == 202, created.text
    done = await _wait(client, hdr, created.json()["id"])
    assert done["status"] == "succeeded", done
    s = done["summary"]
    assert s["encrypted"] is True and s["tables"] > 10 and s["rows"] > 0 and len(s["sha256"]) == 64
    assert list(isolated_state.glob("ttakkari-*.dump.enc")) and not list(isolated_state.glob("ttakkari-*.dump"))
    key = get_settings().home / "backup.key"
    assert key.stat().st_mode & 0o777 == 0o600
    secret = key.read_text().strip()

    restored = await _start(client, hdr, "restore_rehearsal")
    rdone = await _wait(client, hdr, restored.json()["id"])
    assert rdone["status"] == "succeeded", rdone
    r = rdone["summary"]
    assert r["tables_matched"] == s["tables"] and r["tables_mismatched"] == 0
    assert r["isolated_db"].startswith(plans.RESTORE_DB_PREFIX) and r["target_db_dropped"]
    async with sessionmaker()() as db:
        left = (await db.execute(text("SELECT count(*) FROM pg_database WHERE datname = :n"),
                                 {"n": r["isolated_db"]})).scalar()
        assert left == 0
        assert (await db.execute(text("SELECT count(*) FROM sessions"))).scalar() == rows_before

    data_dir = get_settings().data_dir
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts" / "a.txt").write_text("hello snapshot")
    snap = await _wait(client, hdr, (await _start(client, hdr, "data_snapshot")).json()["id"])
    assert snap["status"] == "succeeded", snap
    assert snap["summary"]["encrypted"] and snap["summary"]["file_count"] >= 1
    verify = await _wait(client, hdr, (await _start(client, hdr, "snapshot_verify")).json()["id"])
    assert verify["status"] == "succeeded", verify
    assert verify["summary"]["files_verified"] == snap["summary"]["file_count"]
    assert not any((isolated_state / plans.VERIFY_DIR).glob("*"))

    # 어디에도 키·평문 덤프 경로가 새지 않는다
    every = "".join(p.read_text() for p in (get_settings().home / "operations" / "jobs").glob("*.json"))
    every += json.dumps([done, rdone, snap, verify])
    assert secret not in every and str(isolated_state) not in every
    status = backup_status.backup_status()
    assert status["db"]["encrypted"] and status["data"]["encrypted"]

    # 감사: 요청과 종료가 모두 남는다
    async with sessionmaker()() as db:
        actions = (await db.execute(select(AuditLog.action, AuditLog.outcome)
                                    .where(AuditLog.target_id == created.json()["id"]))).all()
    assert ("operation.request", "ok") in actions and ("operation.finish", "ok") in actions
