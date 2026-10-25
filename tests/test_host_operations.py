"""호스트 운영 작업(operations): 허용 호출·권한·임의 입력 거절·단일 실행·취소/시간초과·실패 보존·민감정보 비노출.

실제 통합은 conftest 의 격리 임시 PostgreSQL 클러스터에서 scripts/*.sh 를 그대로 돈다(운영 DB 접속 없음).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace

import psutil
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


async def test_non_personal_pool_cannot_use_operations(client, env, monkeypatch):
    from ttakkari.api import operations
    monkeypatch.setattr(operations, "_session", lambda db, sid: asyncio.sleep(0, result=SimpleNamespace(pool="shared")))
    hdr = _hdr(env.personal_session_id)
    assert (await _start(client, hdr, "runtime_report")).status_code == 403
    assert (await client.get("/api/agent/operations", headers=hdr)).status_code == 403


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
    assert runtime["audit_pending"] is False
    assert {"this_process", "control", "generations", "runs_running", "recent_deploys"} <= set(runtime["summary"])
    perf = (await _start(client, hdr, "performance_baseline")).json()
    assert perf["status"] == "succeeded", perf.get("error")
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
        finish_audited = (await db.execute(select(func.count()).select_from(AuditLog)
                                           .where(AuditLog.action == "operation.finish",
                                                  AuditLog.actor == "operation"))).scalar()
    assert audited >= 3
    assert finish_audited >= 3


async def test_inline_failure_is_finished_and_audited(client, env, monkeypatch):
    from ttakkari.operations import inline

    async def fail(*_):
        raise RuntimeError("read-only probe failure")

    monkeypatch.setattr(inline, "run", fail)
    response = await _start(client, _hdr(env.personal_session_id), "runtime_report")
    body = response.json()
    assert response.status_code == 202
    assert body["status"] == "failed" and body["audit_pending"] is False
    assert body["error"].startswith("RuntimeError")
    async with sessionmaker()() as db:
        row = await db.scalar(select(AuditLog.id).where(
            AuditLog.action == "operation.finish", AuditLog.target_id == body["id"]))
    assert row is not None


# ---- 단일 실행·취소·상태 보존 ----

def _active_job(kind: str, **extra) -> dict:
    job = jobs.create(kind, str(uuid.uuid4()))
    return jobs.write(job | {"status": "running", "runner_pid": os.getpid(), "runner_started": 1.0, **extra})


async def test_duplicate_and_conflicting_starts(client, env, monkeypatch):
    monkeypatch.setattr(jobs, "_runner_state", lambda job: (True, None))
    hdr = _hdr(env.personal_session_id)
    running = _active_job("backup_db")
    again = await _start(client, hdr, "backup_db")
    assert again.status_code == 202 and again.json()["id"] == running["id"] and again.json()["deduplicated"]
    other = await _start(client, hdr, "restore_rehearsal")
    assert other.status_code == 409 and "backup_db" in other.text
    # 읽기 전용 즉시 작업은 무거운 작업과 별개로 돈다
    assert (await _start(client, hdr, "backup_status")).status_code == 202


def test_start_heavy_unexpected_exception_cleans_queue(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated unexpected runner spawn failure")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(Exception) as exc:
        jobs.start_heavy("backup_db", "test-session")
    assert "simulated" in str(exc.value)

    # 대기 큐에 작업이 남아있지 않고 락이 해제되어 있어야 한다
    assert not any(j["status"] == "queued" for j in jobs.all_jobs())
    assert not any(j.get("kind") == "backup_db" and j["status"] in jobs.ACTIVE for j in jobs.all_jobs())


async def test_cancel_signals_runner_and_keeps_record(client, env, monkeypatch):
    monkeypatch.setattr(jobs, "_runner_state", lambda job: (True, None))
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
    async with sessionmaker()() as db:
        actions = (await db.execute(select(AuditLog.action, AuditLog.outcome)
                                    .where(AuditLog.target_id == job["id"]))).all()
    assert ("operation.cancel", "ok") in actions
    assert ("operation.finish", "error") in actions


async def test_lost_runner_is_preserved_as_failed_not_succeeded(client, env):
    job = jobs.write(jobs.create("backup_db", str(uuid.uuid4())) | {"status": "running", "runner_pid": 2 ** 22,
                                                                      "runner_started": 1.0})
    body = (await client.get(f"/api/agent/operations/{job['id']}", headers=_hdr(env.personal_session_id))).json()
    assert body["status"] == "failed" and body["error"].startswith("runner_lost") and body["finished_at"]
    assert body["audit_pending"] is False


def _reap_in_background(proc: subprocess.Popen) -> threading.Thread:
    """실제 러너가 죽으면 고아 자식은 init(launchd)이 바로 회수한다. 테스트의 부모는 따로 회수해 같은 조건을 만든다.

    macOS 는 회수되지 않은 좀비만 남은 그룹에 killpg(pgid, 0) 이 EPERM 을 돌려주므로 회수 전에는 종료 확인이 불가능하다.
    """
    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()
    return reaper


def test_runner_sigkill_cleans_orphaned_child_and_residuals():
    # 1. 자식 프로세스를 별도 세션/그룹으로 기동
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        p = psutil.Process(proc.pid)
        child_started = p.create_time()
        child_cmdline = " ".join(p.cmdline())

        # 2. 러너가 SIGKILL 로 사망했다고 가정한 작업 기록 생성
        job_id = str(uuid.uuid4())
        jobs.write({
            "id": job_id, "kind": "restore_rehearsal", "status": "running",
            "created_at": jobs.now(), "started_at": jobs.now(),
            "runner_pid": 2 ** 22, "runner_started": 1.0,  # 존재하지 않는 러너 pid
            "child_pid": proc.pid, "child_pgid": proc.pid,
            "child_started": child_started, "child_cmdline": child_cmdline,
        })

        # 3. 작업 전용 0700 디렉터리에 잔여 평문 덤프 모의 파일 생성
        ops_tmp = plans.ops_tmp()
        stale_file = ops_tmp / "rehearse-decrypt-mock123"
        stale_file.write_text("sensitive decrypted data")
        assert stale_file.is_file()

        # 자식이 살아있음을 확인
        assert psutil.pid_exists(proc.pid)

        # 4. reconciled 실행 -> 러너 사망 감지, 신원 검증 후 자식 프로세스 그룹 정리
        _reap_in_background(proc)
        reconciled = jobs.reconciled(job_id)
        assert reconciled["status"] == "failed", reconciled.get("error")
        assert "runner_lost" in reconciled["error"]
        assert not stale_file.exists()

        # 5. 자식 프로세스가 종료되었음을 확인
        for _ in range(20):
            if not psutil.pid_exists(proc.pid) or proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None

        # 6. 시작 시 잔여 평문 파일 정리 함수 독립 검증
        stale_file2 = ops_tmp / "rehearse-decrypt-mock456"
        stale_file2.write_text("another plain")
        cleaned = plans.clean_residuals()
        assert not stale_file2.exists()
        assert cleaned["cleaned_files"] >= 1
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


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
    assert done["finish_audited"] is True
    blob = json.dumps(done, ensure_ascii=False)
    assert secret not in blob and home_marker not in blob and "boom" in done["output_tail"]
    assert "<home>" in done["output_tail"] and not key.exists()


def test_snapshot_verify_retains_residual_on_cleanup_failure(monkeypatch, tmp_path):
    target_dir = tmp_path / "verify_leftover"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "plain.txt").write_text("secret")

    plan = plans.Plan("snapshot_verify", ["true"], {}, 30, cleanup_dir=target_dir)
    monkeypatch.setattr(runner, "_secure_rmtree", lambda p: False)
    monkeypatch.setattr(runner, "_execute", lambda p, out, **kw: ("succeeded", 0))
    monkeypatch.setattr(runner, "_summarize", lambda p, s, o, b: ("succeeded", {"files_verified": 1}, None))

    job = jobs.create("snapshot_verify", "test-session")
    monkeypatch.setattr(plans, "make_plan", lambda k, j: plan)
    monkeypatch.setattr(plans, "backup_dir", lambda: tmp_path)
    monkeypatch.setattr(plans, "VERIFY_DIR", "")

    asyncio.run(runner._run(job["id"]))
    done = jobs.get(job["id"])
    assert done["status"] == "failed"
    assert "임시 평문 검증 디렉터리 정리 실패" in done["error"]
    assert done["summary"].get("cleanup_dir_retained") is True
    assert target_dir.exists()


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

    assert not plans.is_local_url("postgresql://localhost/ttakkari?host=remote.example.com")
    assert not plans.is_local_url("postgresql://localhost/ttakkari?hostaddr=10.0.0.1")
    assert not plans.is_local_url("postgresql://localhost/ttakkari?service=secret_remote")
    assert not plans.is_local_url("postgresql:///ttakkari", environ={"PGHOST": "remote.example.com"})
    assert not plans.is_local_url("postgresql:///ttakkari", environ={"PGHOSTADDR": "192.168.1.100"})
    assert not plans.is_local_url("postgresql:///ttakkari", environ={"PGSERVICE": "remote_service"})
    assert plans.is_local_url("postgresql://localhost/ttakkari")
    assert plans.is_local_url("postgresql://127.0.0.1:5432/ttakkari")
    assert plans.is_local_url("postgresql:///ttakkari")
    assert plans.is_local_url("postgresql:///ttakkari?host=/tmp")


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


def test_atomic_key_creation_and_fingerprint(tmp_path):
    key_path = tmp_path / "test.key"
    assert not key_path.exists()
    plans._create_key(key_path)
    assert key_path.is_file()
    assert key_path.stat().st_mode & 0o777 == 0o600
    content1 = key_path.read_text()
    assert len(content1.strip()) > 30

    plans._create_key(key_path)
    assert key_path.read_text() == content1

    fp = plans.key_fingerprint(key_path)
    assert len(fp) == 16 and all(c in "0123456789abcdef" for c in fp)


def test_runbook_contains_key_loss_warning():
    runbook = (plans.BACK / "docs/security/backup-runbook.md").read_text(encoding="utf-8")
    assert "키 파일을 분실하면" in runbook
    assert "복호화할 수 없다" in runbook
    assert "backup.key" in runbook
    assert "key_fingerprint" in runbook


def test_backup_status_preserves_only_key_fingerprint_from_status_files():
    digest = "a" * 16
    db = backup_status.sanitize("db", {"created_at": "20261025", "encrypted": True,
                                       "key_fingerprint": digest, "manifest": "/private/secret.json"})
    data = backup_status.sanitize("data", {"created_at": "20261025", "encrypted": True,
                                           "key_fingerprint": digest, "archive": "/private/secret.enc"})
    assert db["key_fingerprint"] == digest
    assert data["key_fingerprint"] == digest
    assert "manifest" not in db and "archive" not in data


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
    assert len(s.get("key_fingerprint", "")) == 16, s
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
    assert snap["summary"]["key_fingerprint"] == s["key_fingerprint"]
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

# ---- P0 회귀: 프로세스 그룹 정리와 finish audit의 durable marker ----

def test_reconcile_keeps_heavy_slot_when_child_identity_is_unavailable(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0,
        "child_pid": 2 ** 22 + 1, "child_pgid": 2 ** 22 + 1,
        "child_started": 123.0, "child_cmdline": "expected-child --flag",
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))
    monkeypatch.setattr(jobs.psutil, "Process", lambda _: (_ for _ in ()).throw(psutil.AccessDenied()))

    reconciled = jobs.reconciled(job["id"])

    assert reconciled["status"] == "cleanup_pending"
    assert "identity" in reconciled["error"]
    assert not reconciled.get("finished_at")
    assert any(j["id"] == job["id"] for j in jobs.all_jobs() if j["status"] in jobs.ACTIVE)


def test_reconcile_keeps_slot_when_kill_is_denied_or_group_remains(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0,
        "child_pid": 2 ** 22 + 1, "child_pgid": 2 ** 22 + 1,
        "child_started": 123.0, "child_cmdline": "expected-child --flag",
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))

    class Child:
        def create_time(self): return 123.0
        def cmdline(self): return ["expected-child", "--flag"]

    monkeypatch.setattr(jobs.psutil, "Process", lambda _: Child())
    monkeypatch.setattr(jobs.os, "getpgid", lambda _: job["child_pgid"])
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(PermissionError())
                        if sig != 0 else None)
    reconciled = jobs.reconciled(job["id"])
    assert reconciled["status"] == "cleanup_pending"
    assert "PermissionError" in reconciled["error"]
    assert not reconciled.get("finished_at")


def test_reconcile_keeps_slot_when_group_survives_sigkill(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0,
        "child_pid": 2 ** 22 + 1, "child_pgid": 2 ** 22 + 1,
        "child_started": 123.0, "child_cmdline": "expected-child --flag",
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))

    class Child:
        def create_time(self): return 123.0
        def cmdline(self): return ["expected-child", "--flag"]

    monkeypatch.setattr(jobs.psutil, "Process", lambda _: Child())
    monkeypatch.setattr(jobs.os, "getpgid", lambda _: job["child_pgid"])
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(jobs.time, "sleep", lambda _: None)
    reconciled = jobs.reconciled(job["id"])
    assert reconciled["status"] == "cleanup_pending"
    assert "remains alive" in reconciled["error"]


def test_reconcile_does_not_signal_group_when_recorded_identity_mismatches(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0,
        "child_pid": 2 ** 22 + 1, "child_pgid": 2 ** 22 + 1,
        "child_started": 123.0, "child_cmdline": "expected-child --flag",
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))

    class ReusedPid:
        def create_time(self): return 456.0
        def cmdline(self): return ["unrelated", "process"]

    sent = []
    monkeypatch.setattr(jobs.psutil, "Process", lambda _: ReusedPid())
    monkeypatch.setattr(jobs.os, "getpgid", lambda _: job["child_pgid"])
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    reconciled = jobs.reconciled(job["id"])
    assert reconciled["status"] == "cleanup_pending"
    assert "identity mismatch" in reconciled["error"]
    assert sent == []


def test_reconcile_after_actual_runner_sigkill_handles_verified_child_group(tmp_path):
    child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    runner_proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        identity = psutil.Process(child.pid)
        job = jobs.write(jobs.create("backup_db", "session") | {
            "status": "running", "runner_pid": runner_proc.pid,
            "runner_started": psutil.Process(runner_proc.pid).create_time(),
            "child_pid": child.pid, "child_pgid": os.getpgid(child.pid),
            "child_started": identity.create_time(), "child_cmdline": " ".join(identity.cmdline()),
        })
        os.kill(runner_proc.pid, signal.SIGKILL)
        runner_proc.wait(timeout=2)
        assert runner_proc.returncode == -signal.SIGKILL

        reaped = _reap_in_background(child)
        reconciled = jobs.reconciled(job["id"])
        assert reconciled["status"] == "failed", reconciled
        assert "runner_lost" in reconciled["error"]
        reaped.join(timeout=2)
        assert child.returncode == -signal.SIGTERM
    finally:
        for proc in (child, runner_proc):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass


def test_reconcile_blocks_when_child_record_lacks_start_time_or_cmdline(monkeypatch):
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))
    sent = []
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    base = {"status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0,
            "child_pid": 2 ** 22 + 1, "child_pgid": 2 ** 22 + 1}
    for extra in ({"child_cmdline": "x"}, {"child_started": 123.0}):  # 시작 시각 없음 / 명령행 없음
        job = jobs.write(jobs.create("backup_db", "session") | base | extra)
        reconciled = jobs.reconciled(job["id"])
        assert reconciled["status"] == "cleanup_pending" and "incomplete" in reconciled["error"]
        jobs.write(reconciled | {"status": "cancelled", "finished_at": jobs.now()})
    assert sent == []


def test_reconcile_without_recorded_child_finishes_as_runner_lost(monkeypatch):
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0})
    assert jobs.reconciled(job["id"])["status"] == "failed"


def test_cleanup_pending_finalizes_after_group_is_confirmed_gone(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0,
        "child_pid": 2 ** 22 + 1, "child_pgid": 2 ** 22 + 1,
        "child_started": 123.0, "child_cmdline": "expected-child --flag",
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))
    monkeypatch.setattr(jobs.psutil, "Process", lambda _: (_ for _ in ()).throw(psutil.NoSuchProcess(1)))
    state = {"denied": True}

    def probe(pgid, sig):
        if state["denied"]:
            raise PermissionError()
        raise ProcessLookupError()

    monkeypatch.setattr(jobs.os, "killpg", probe)
    assert jobs.reconciled(job["id"])["status"] == "cleanup_pending"
    state["denied"] = False  # 좀비가 회수돼 그룹이 실제로 사라진 뒤의 재조회
    final = jobs.reconciled(job["id"])
    assert final["status"] == "failed" and final["error"].startswith("runner_lost") and final["finished_at"]


def test_reconcile_blocks_when_leader_is_gone_but_recorded_group_exists(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22, "runner_started": 1.0,
        "child_pid": 2 ** 22 + 1, "child_pgid": 2 ** 22 + 1,
        "child_started": 123.0, "child_cmdline": "expected-child --flag",
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))
    monkeypatch.setattr(jobs.psutil, "Process", lambda _: (_ for _ in ()).throw(psutil.NoSuchProcess(1)))
    calls = []

    def group_probe(pgid, sig):
        calls.append((pgid, sig))
        if sig != 0:
            raise AssertionError("must not signal group without live leader identity")

    monkeypatch.setattr(jobs.os, "killpg", group_probe)
    reconciled = jobs.reconciled(job["id"])
    assert reconciled["status"] == "cleanup_pending"
    assert "leader missing while process group remains" in reconciled["error"]
    assert calls == [(job["child_pgid"], 0)]


def test_cleanup_pending_keeps_heavy_slot_and_cannot_be_cancelled(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "cleanup_pending", "error": "runner_lost_cleanup_pending: identity unavailable",
        "runner_pid": 2 ** 22, "runner_started": 1.0,
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (False, None))
    monkeypatch.setattr(jobs, "_clean_child_process_group", lambda _: (False, "identity unavailable"))
    with pytest.raises(jobs.Conflict, match="슬롯이 차단"):
        jobs.cancel(job["id"])
    with pytest.raises(jobs.Conflict, match="backup_db"):
        jobs.prepare_heavy("restore_rehearsal", "other-session")
    assert jobs.get(job["id"])["status"] == "cleanup_pending"


def test_runner_identity_lookup_failure_blocks_reconciliation(monkeypatch):
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "running", "runner_pid": 2 ** 22 + 2, "runner_started": 123.0,
    })
    monkeypatch.setattr(jobs, "_runner_state", lambda _: (None, "AccessDenied"))
    reconciled = jobs.reconciled(job["id"])
    assert reconciled["status"] == "cleanup_pending"
    assert "runner identity lookup unavailable" in reconciled["error"]
    assert "AccessDenied" in reconciled["error"]
    assert not reconciled.get("finished_at")


async def test_cancel_queued_audit_failure_leaves_finish_pending_then_status_retries(client, env, monkeypatch):
    job = jobs.create("data_snapshot", str(env.personal_session_id))
    real_finish = jobs.audit_finish

    async def failed_commit(job_id):
        return jobs.update(job_id, finish_audited=False,
                           audit_error="RuntimeError: simulated audit commit failure")

    monkeypatch.setattr(jobs, "audit_finish", failed_commit)
    body = (await client.post(f"/api/agent/operations/{job['id']}/cancel",
                              headers=_hdr(env.personal_session_id))).json()
    saved = jobs.get(job["id"])
    assert body["status"] == "cancelled"
    assert saved.get("finish_audited") is not True
    assert "RuntimeError" in saved.get("audit_error", "")

    monkeypatch.setattr(jobs, "audit_finish", real_finish)
    retried = (await client.get(f"/api/agent/operations/{job['id']}",
                                headers=_hdr(env.personal_session_id))).json()
    assert retried["status"] == "cancelled"
    assert retried["audit_pending"] is False
    assert jobs.get(job["id"])["finish_audited"] is True


async def test_finish_audit_marker_follows_commit_and_retry_is_idempotent(monkeypatch):
    from ttakkari import db as db_module
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "failed", "finished_at": jobs.now(), "error": "runner_error: ValueError",
        "finish_audited": False,
    })
    added = []
    commits = []

    class AuditSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def scalar(self, _): return 41 if commits else None
        def add(self, row): added.append(row)
        async def commit(self):
            assert jobs.get(job["id"]).get("finish_audited") is not True
            await asyncio.sleep(0)
            commits.append(True)

    monkeypatch.setattr(db_module, "sessionmaker", lambda: lambda: AuditSession())
    first, second = await asyncio.gather(jobs.audit_finish(job["id"]), jobs.audit_finish(job["id"]))
    assert commits == [True] and first["finish_audited"] and second["finish_audited"] and len(added) == 1


async def test_finish_audit_database_commit_failure_keeps_marker_pending_then_retries(monkeypatch):
    from ttakkari import db as db_module
    job = jobs.write(jobs.create("backup_db", "session") | {
        "status": "failed", "finished_at": jobs.now(), "error": "runner_error: ValueError",
        "finish_audited": False,
    })

    class FailedSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def scalar(self, _): return None
        def add(self, _): pass
        async def commit(self): raise RuntimeError("deliberate commit failure")

    monkeypatch.setattr(db_module, "sessionmaker", lambda: lambda: FailedSession())
    failed = await jobs.audit_finish(job["id"])
    assert failed["finish_audited"] is False
    assert "RuntimeError" in failed["audit_error"]

    class SuccessfulSession(FailedSession):
        async def commit(self): pass

    monkeypatch.setattr(db_module, "sessionmaker", lambda: lambda: SuccessfulSession())
    retried = await jobs.audit_finish(job["id"])
    assert retried["finish_audited"] is True and retried["audit_error"] is None
