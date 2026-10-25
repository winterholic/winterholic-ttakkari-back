"""작업 기록 저장소와 시작·취소·상태 조회. 기록은 파일이라 API 재시작과 세대 교체를 견딘다."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

from ttakkari.config import get_settings
from ttakkari.operations import plans

ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "failed", "cancelled", "timeout")
KEEP_TERMINAL = 100
QUEUED_GRACE_SECONDS = 30
RUNNER_MARKER = "ttakkari.operations.runner"


def now() -> str:
    return datetime.now(UTC).isoformat()


def root() -> Path:
    path = get_settings().home / "operations"
    (path / "jobs").mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextmanager
def locked():
    with (root() / "operations.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _path(job_id: str) -> Path:
    return root() / "jobs" / f"{uuid.UUID(job_id)}.json"


def get(job_id: str) -> dict:
    return json.loads(_path(job_id).read_text(encoding="utf-8"))


def all_jobs() -> list[dict]:
    out = []
    for p in (root() / "jobs").glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # 쓰는 중이거나 손상된 파일 하나가 목록 전체를 막지 않는다
    return sorted(out, key=lambda j: j["created_at"])


def write(job: dict) -> dict:
    job["updated_at"] = now()
    path = _path(job["id"])
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    return job


def update(job_id: str, **fields) -> dict:
    with locked():
        return write(get(job_id) | fields)


def process_identity(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, ProcessLookupError):
        return None


def runner_alive(job: dict) -> bool:
    pid, started = job.get("runner_pid"), job.get("runner_started")
    if not pid or started is None:
        return False
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - started) > 2 or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return RUNNER_MARKER in " ".join(proc.cmdline())
    except (psutil.Error, ProcessLookupError):
        return False


def _clean_child_process_group(job: dict) -> None:
    """러너가 비정상 종료(SIGKILL 등)했을 때 자식 프로세스 그룹을 신원 검증 후 정리한다."""
    pid = job.get("child_pid")
    started = job.get("child_started")
    cmdline = job.get("child_cmdline")
    pgid = job.get("child_pgid")
    if not pid or not pgid or started is None:
        return
    verified = False
    try:
        proc = psutil.Process(pid)
        # 신원 검증: create_time 과 cmdline 이 일치하는지
        if abs(proc.create_time() - started) <= 2 and (
            not cmdline or cmdline in " ".join(proc.cmdline()) or " ".join(proc.cmdline()) in cmdline
        ):
            verified = True
    except (psutil.Error, ProcessLookupError):
        # 프로세스 리더가 이미 죽었을 수 있음. pgid 확인
        verified = True

    if not verified:
        return

    # 신원 확인됨: 프로세스 그룹 전체 정리
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            break
        # 자식 프로세스들이 완전히 종료될 때까지 대기
        for _ in range(10):
            try:
                os.killpg(pgid, 0)
                time.sleep(0.05)
            except (ProcessLookupError, PermissionError):
                break


def _audit_finish_sync(job: dict) -> None:
    try:
        from ttakkari.audit import record
        from ttakkari.db import sessionmaker

        async def _do():
            async with sessionmaker()() as db:
                record(
                    db,
                    actor="operation",
                    action="operation.finish",
                    outcome="error",
                    target_type="operation",
                    target_id=job["id"],
                    detail={
                        "kind": job["kind"],
                        "status": job["status"],
                        "error": job.get("error"),
                        "session_id": job.get("requested_by_session"),
                    },
                )
                await db.commit()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do())
        except RuntimeError:
            asyncio.run(_do())
    except Exception:  # noqa: BLE001, S110
        pass


def _reconcile(job: dict) -> dict:
    """러너가 사라졌는데 끝나지 않은 기록은 실패로 남긴다. 결과를 지어내지 않는다."""
    if job["status"] not in ACTIVE or job.get("inline") or runner_alive(job):
        return job
    if job["status"] == "queued":
        age = datetime.now(UTC) - datetime.fromisoformat(job["created_at"])
        if age < timedelta(seconds=QUEUED_GRACE_SECONDS) and not job.get("runner_pid"):
            return job
    # runner_lost: 자식 프로세스 그룹 정리
    _clean_child_process_group(job)
    plans.clean_residuals()
    updated = write(
        job
        | {
            "status": "failed",
            "error": "runner_lost: 러너 프로세스가 끝나기 전에 사라졌습니다.",
            "finished_at": now(),
        }
    )
    if not updated.get("finish_audited"):
        _audit_finish_sync(updated)
        updated = write(updated | {"finish_audited": True})
    return updated


def reconciled(job_id: str) -> dict:
    with locked():
        return _reconcile(get(job_id))


def recent(limit: int = 10) -> list[dict]:
    with locked():
        jobs = [_reconcile(j) for j in all_jobs()]
    return list(reversed(jobs))[:limit]


def view(job: dict) -> dict:
    """API 로 내보내는 필드. 경로·pid·환경은 내지 않는다."""
    keys = ("id", "kind", "status", "created_at", "started_at", "finished_at", "updated_at", "duration_s",
            "exit_code", "error", "summary", "identity", "output_tail", "cancel_requested_at", "inline",
            "requested_by_session", "deduplicated")
    return {k: job[k] for k in keys if k in job}


def _prune() -> None:
    done = [j for j in all_jobs() if j["status"] in TERMINAL]
    for old in done[:-KEEP_TERMINAL]:
        _path(old["id"]).unlink(missing_ok=True)


class Conflict(Exception):
    pass


def create(kind: str, session_id: str, *, inline: bool = False, identity: dict | None = None) -> dict:
    job = {"id": str(uuid.uuid4()), "kind": kind, "status": "running" if inline else "queued",
           "created_at": now(), "inline": inline, "requested_by_session": session_id,
           "identity": identity or {}}
    if inline:
        job["started_at"] = job["created_at"]
    return write(job)


def clean_failed_queue(job_id: str) -> None:
    """예상 외 예외 시 queued 기록 정리 (락 해제)."""
    with locked():
        _path(job_id).unlink(missing_ok=True)


def prepare_heavy(kind: str, session_id: str) -> tuple[dict, bool]:
    """(기록, 기존 작업 재사용 여부). 무거운 작업은 한 번에 하나만 돈다.
    새 작업이면 queued 기록과 계획만 세우고 러너는 띄우지 않는다."""
    with locked():
        active = [j for j in (_reconcile(j) for j in all_jobs()) if j["status"] in ACTIVE and not j.get("inline")]
        if active:
            same = next((j for j in active if j["kind"] == kind), None)
            if same:
                return same, True
            raise Conflict(f"다른 운영 작업이 진행 중입니다: {active[0]['kind']} ({active[0]['id']})")
        job = create(kind, session_id)
        try:
            plan = plans.make_plan(kind, job["id"])
            updated = write(job | {"identity": plan.identity})
            return updated, False
        except Exception:
            _path(job["id"]).unlink(missing_ok=True)
            raise


def spawn_heavy(job_id: str) -> dict:
    """감사 기록 커밋 후 분리된 러너 프로세스를 기동한다."""
    with locked():
        job = get(job_id)
        if job["status"] != "queued":
            raise Conflict("작업이 대기 상태가 아닙니다.")
        plans.clean_residuals()
        try:
            proc = subprocess.Popen(
                plans.runner_argv(job_id),
                cwd=plans.BACK,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            )
        except Exception as exc:
            _path(job_id).unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise
            raise OSError(f"러너 기동 실패: {exc}") from exc
        write(job | {"runner_pid": proc.pid, "runner_started": process_identity(proc.pid)})
        _prune()
        return get(job_id)


def start_heavy(kind: str, session_id: str) -> tuple[dict, bool]:
    """(기록, 기존 작업 재사용 여부). prepare_heavy 와 spawn_heavy 를 차례로 실행한다."""
    job, reused = prepare_heavy(kind, session_id)
    if reused:
        return job, True
    try:
        job = spawn_heavy(job["id"])
        return job, False
    except Exception:
        clean_failed_queue(job["id"])
        raise


def cancel(job_id: str) -> dict:
    with locked():
        job = _reconcile(get(job_id))
        if job["status"] not in ACTIVE:
            raise Conflict("이미 끝난 작업입니다.")
        job = write(job | {"cancel_requested_at": now()})
        if job.get("inline") or not runner_alive(job):
            updated = write(job | {"status": "cancelled", "finished_at": now()})
            if not updated.get("finish_audited"):
                _audit_finish_sync(updated)
                updated = write(updated | {"finish_audited": True})
            return updated
        os.kill(job["runner_pid"], signal.SIGTERM)  # 러너가 자식 프로세스 그룹을 정리하고 cancelled 로 기록한다
        return job
