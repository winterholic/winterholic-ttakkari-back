"""작업 기록 저장소와 시작·취소·상태 조회. 기록은 파일이라 API 재시작과 세대 교체를 견딘다."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.operations import plans

ACTIVE = ("queued", "running", "cleanup_pending")
TERMINAL = ("succeeded", "failed", "cancelled", "timeout")
KEEP_TERMINAL = 100
QUEUED_GRACE_SECONDS = 30
GROUP_EXIT_WAIT_SECONDS = 2.0
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


@asynccontextmanager
async def audit_locked():
    """감사 커밋을 한 번에 하나씩 하게 하는 프로세스 간 잠금.

    대기를 스레드 풀에서 blocking flock 으로 하면 대기자가 풀을 다 차지해 보유자의 해제 작업이 못 돌아 교착한다.
    LOCK_NB 재시도로 대기하며 스레드를 쓰지 않는다. 취소돼도 finally 에서 핸들을 닫아 잠금이 새지 않는다.
    """
    # 작업 슬롯 잠금(operations.lock)과 분리한다. DB 커밋이 느려도 시작·취소·재조회가 막히지 않게 한다.
    handle = (root() / "audit.lock").open("a")
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        handle.close()  # close 가 flock 도 푼다


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


def _runner_state(job: dict) -> tuple[bool | None, str | None]:
    pid, started = job.get("runner_pid"), job.get("runner_started")
    if not pid or started is None:
        return False, None
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - started) > 2 or proc.status() == psutil.STATUS_ZOMBIE:
            return False, None
        return RUNNER_MARKER in " ".join(proc.cmdline()), None
    except psutil.NoSuchProcess:
        return False, "NoSuchProcess"
    except (psutil.AccessDenied, PermissionError) as exc:
        return None, type(exc).__name__
    except (psutil.Error, OSError) as exc:
        return None, type(exc).__name__


def runner_alive(job: dict) -> bool:
    return _runner_state(job)[0] is True


def _id_gone(value: int) -> bool:
    """value 를 pid 로도 pgid 로도 쓰는 프로세스·그룹이 없음이 확인될 때만 True."""
    try:
        os.killpg(value, 0)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    else:
        return False
    return not psutil.pid_exists(value)


def child_pidfile(job_id: str) -> Path:
    """자식이 fork 직후 exec 전에 스스로 pid 를 적는 파일. 러너가 신원을 기록하기 전에 죽었을 때의 단서다."""
    return root() / "jobs" / f"{uuid.UUID(job_id)}.child"


def _recover_unrecorded_child(job: dict) -> tuple[dict | None, str | None]:
    """(복원한 신원 기록, 차단 사유). 둘 다 None 이면 띄워진 자식이 없었다는 뜻이다."""
    clue = f"child start was in progress but its identity is unrecorded ({_unrecorded_clue(job)})"
    try:
        text = child_pidfile(job["id"]).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, None  # exec 전 단계에 이르지 못했다: 띄워진 자식이 없다
    except OSError as exc:
        return None, f"{clue}; pid file unreadable ({type(exc).__name__})"
    try:
        pid = int(text)
    except ValueError:
        return None, f"{clue}; pid file malformed"
    planned, since = job.get("child_planned_sha256"), job.get("child_starting_at")
    if not planned or since is None:
        return None, f"{clue}; pid file found (pid={pid}) but planned command was not recorded"
    try:
        live = psutil.Process(pid)
        started = live.create_time()
    except psutil.NoSuchProcess:
        return {"child_pid": pid, "child_pgid": pid, "child_started": None, "child_cmdline_sha256": planned}, None
    except (psutil.Error, OSError) as exc:
        return None, f"{clue}; pid file found (pid={pid}) but lookup failed ({type(exc).__name__})"
    if started < since - 2:
        return None, f"{clue}; pid file found (pid={pid}) but that process predates the start (pid reused)"
    return {"child_pid": pid, "child_pgid": pid, "child_started": started, "child_cmdline_sha256": planned}, None


def _unrecorded_clue(job: dict) -> str:
    return (f"job={job['id']}, kind={job.get('kind')}, runner_pid={job.get('runner_pid')}, "
            f"runner_started={job.get('runner_started')}; "
            f"find it with: ps -axo pid,pgid,ppid,lstart,command | grep -E 'scripts/'")


def _clean_child_process_group(job: dict) -> tuple[bool, str | None]:
    """기록된 leader·생성시각·명령행·pgid를 결속 검증한 뒤 그룹을 정리한다.

    러너가 신원을 기록하기 전에 죽었다면 자식이 스스로 적은 pid 파일과 기록해 둔 예정 명령으로 신원을 복원해 같은 검증을 거친다.
    """
    if not job.get("child_pid") and not job.get("child_pgid") and job.get("child_starting"):
        recovered, blocked = _recover_unrecorded_child(job)
        if blocked:
            return False, blocked
        if recovered is None:
            return True, None  # 자식이 띄워지지 않았다
        cleaned, reason = _clean_recorded_child(recovered)
        return cleaned, (f"{reason} [unrecorded child; {_unrecorded_clue(job)}]" if reason else None)
    return _clean_recorded_child(job)


def _clean_recorded_child(job: dict) -> tuple[bool, str | None]:
    pid = job.get("child_pid")
    started = job.get("child_started")
    cmdline = job.get("child_cmdline")
    cmdline_hash = job.get("child_cmdline_sha256")
    pgid = job.get("child_pgid")
    if not pid and not pgid:
        return True, None  # 자식을 띄우기 전에 러너가 사라진 경우: 정리할 대상이 없다
    if not pid or not pgid or started is None or (not cmdline and not cmdline_hash) or pgid != pid:
        # 신원은 불완전해도 기록된 id 가 전부 사라졌다면 신호가 필요 없다. 하나라도 남았거나 확인 못 하면 차단한다
        if all(_id_gone(value) for value in {pid, pgid} if value):
            return True, None
        return False, f"child identity record incomplete or unbound (pid={pid}, pgid={pgid})"
    try:
        proc = psutil.Process(pid)
        actual_started = proc.create_time()
        actual_cmdline = " ".join(proc.cmdline())
        actual_pgid = os.getpgid(pid)
    except psutil.NoSuchProcess:
        try:
            os.killpg(pgid, 0)
            return False, f"recorded child leader missing while process group remains (pid={pid}, pgid={pgid})"
        except ProcessLookupError:
            return True, None
        except PermissionError:
            return False, f"process group existence check denied (pid={pid}, pgid={pgid}, PermissionError)"
        except OSError as exc:
            return False, f"process group existence check failed (pid={pid}, pgid={pgid}, {type(exc).__name__})"
    except (psutil.AccessDenied, PermissionError) as exc:
        return False, f"child identity lookup denied (pid={pid}, pgid={pgid}, {type(exc).__name__})"
    except (psutil.Error, OSError) as exc:
        return False, f"child identity lookup failed (pid={pid}, pgid={pgid}, {type(exc).__name__})"

    actual_hash = hashlib.sha256(actual_cmdline.encode()).hexdigest()
    if (abs(actual_started - started) > 2
            or (cmdline and actual_cmdline != cmdline)
            or (cmdline_hash and actual_hash != cmdline_hash)
            or actual_pgid != pgid):
        return False, f"child identity mismatch (pid={pid}, pgid={pgid})"

    last = "process group remains alive"
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True, None
        except PermissionError:
            return False, f"signal {sig} denied for verified process group (pid={pid}, pgid={pgid}, PermissionError)"
        except OSError as exc:
            return False, f"signal {sig} failed for verified process group (pid={pid}, pgid={pgid}, {type(exc).__name__})"
        gone, last = _wait_group_gone(pgid)
        if gone:
            return True, None
    return False, f"{last} after SIGKILL (pid={pid}, pgid={pgid})"


def _wait_group_gone(pgid: int) -> tuple[bool, str]:
    """신호를 보낸 그룹이 사라질 때까지 기다린다. 회수 전 좀비는 macOS 에서 EPERM 을 돌려주므로 한동안 재시도한다.

    창이 끝나도 EPERM·기타 오류·생존이면 사라졌다고 보지 않고 마지막 사유를 돌려준다.
    """
    last = "process group remains alive"
    deadline = time.monotonic() + GROUP_EXIT_WAIT_SECONDS
    while True:
        try:
            os.killpg(pgid, 0)
            last = "process group remains alive"
        except ProcessLookupError:
            return True, ""
        except PermissionError:
            last = "process group status denied (PermissionError)"
        except OSError as exc:
            last = f"process group status failed ({type(exc).__name__})"
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(0.05)


async def audit_finish(job_id: str) -> dict:
    """DB 감사 커밋 후 파일 marker를 갱신한다. 실패는 pending 상태와 오류로 남긴다."""
    from ttakkari.audit import record
    from ttakkari.db import sessionmaker
    from ttakkari.oplog.scrub import scrub

    async with audit_locked():
        job = get(job_id)
        if job.get("status") not in TERMINAL or job.get("finish_audited"):
            return job
        try:
            async with sessionmaker()() as db:
                await _commit_finish_audit(job, db, record)
            return update(job_id, finish_audited=True, audit_error=None)
        except Exception as exc:  # noqa: BLE001 -- 작업 결과는 보존하고 완료 감사만 재시도 가능하게 둔다
            return update(job_id, finish_audited=False,
                          audit_error=scrub(f"{type(exc).__name__}: {exc}")[:300])


async def _commit_finish_audit(job: dict, db, record_fn) -> None:
    from ttakkari.models import AuditLog

    existing = await db.scalar(select(AuditLog.id).where(
        AuditLog.actor == "operation", AuditLog.action == "operation.finish",
        AuditLog.target_type == "operation", AuditLog.target_id == job["id"],
    ).limit(1))
    if existing is None:
        record_fn(db, actor="operation", action="operation.finish",
                  outcome="ok" if job["status"] == "succeeded" else "error",
                  target_type="operation", target_id=job["id"],
                  detail={"kind": job["kind"], "status": job["status"], "error": job.get("error"),
                          "exit_code": job.get("exit_code"), "duration_s": job.get("duration_s"),
                          "session_id": job.get("requested_by_session")})
    await db.commit()


def _reconcile(job: dict) -> dict:
    """러너가 사라졌는데 끝나지 않은 기록은 실패로 남긴다. 결과를 지어내지 않는다."""
    if job["status"] not in ACTIVE or job.get("inline"):
        return job
    if job["status"] == "queued":
        age = datetime.now(UTC) - datetime.fromisoformat(job["created_at"])
        if age < timedelta(seconds=QUEUED_GRACE_SECONDS) and not job.get("runner_pid"):
            return job
    runner_state, runner_error = _runner_state(job)
    if runner_state is True:
        return job
    if runner_state is None:
        return write(job | {"status": "cleanup_pending", "finished_at": None,
                            "error": f"runner_lost_cleanup_pending: runner identity lookup unavailable "
                                     f"(pid={job.get('runner_pid')}, {runner_error})"})
    # runner_lost: 자식 프로세스 그룹 정리
    cleaned, cleanup_error = _clean_child_process_group(job)
    if not cleaned:
        return write(job | {"status": "cleanup_pending", "finished_at": None,
                            "error": f"runner_lost_cleanup_pending: {cleanup_error}"})
    plans.clean_residuals()
    updated = write(
        job
        | {
            "status": "failed",
            "error": "runner_lost: 러너 프로세스가 끝나기 전에 사라졌습니다.",
            "finished_at": now(),
        }
    )
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
            "requested_by_session", "deduplicated", "audit_error")
    out = {k: job[k] for k in keys if k in job}
    if job.get("status") in TERMINAL:
        out["audit_pending"] = not job.get("finish_audited", False)
    return out


def _prune() -> None:
    done = [j for j in all_jobs() if j["status"] in TERMINAL]
    for old in done[:-KEEP_TERMINAL]:
        _path(old["id"]).unlink(missing_ok=True)
        child_pidfile(old["id"]).unlink(missing_ok=True)


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
        if job["status"] == "cleanup_pending":
            raise Conflict("자식 프로세스 그룹 종료를 확인하지 못해 작업 슬롯이 차단된 상태입니다.")
        job = write(job | {"cancel_requested_at": now()})
        runner_state, _ = _runner_state(job)
        if runner_state is None:
            raise Conflict("러너 신원을 확인할 수 없어 작업 슬롯이 차단된 상태입니다.")
        if job.get("inline") or runner_state is False:
            updated = write(job | {"status": "cancelled", "finished_at": now()})
            return updated
        os.kill(job["runner_pid"], signal.SIGTERM)  # 러너가 자식 프로세스 그룹을 정리하고 cancelled 로 기록한다
        return job
