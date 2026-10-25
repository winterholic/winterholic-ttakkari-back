"""분리된 러너: `python -m ttakkari.operations.runner <job_id>`. 계획된 스크립트 하나를 시간 제한 안에서 돌린다.

API 프로세스와 별도 세션이라 API 재시작·세대 교체에 죽지 않는다. SIGTERM 은 취소로 처리해 자식 프로세스
그룹을 정리하고 cancelled 로 남긴다. 출력은 scrub 한 뒤 꼬리만 보존하며, 덤프·키 내용은 어디에도 싣지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import asyncpg
import psutil

from ttakkari import backup_status
from ttakkari.config import get_settings
from ttakkari.operations import jobs, plans
from ttakkari.oplog.scrub import scrub

OUTPUT_TAIL_CHARS = 1500
MISMATCH_LIST_LIMIT = 20
_TARGET = re.compile(rf"^{plans.RESTORE_DB_PREFIX}[0-9a-f]{{12}}$")
_VERIFIED = re.compile(r"스냅샷 검증 통과: (\d+)개 파일 일치")

_cancelled = False


def _on_term(_signum, _frame) -> None:
    global _cancelled
    _cancelled = True


def _libpq(url: str) -> str:
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9.-]*)(\+[a-zA-Z0-9_.-]+)?://", r"\1://", url)


_TERM_GRACE = 15
_KILL_GRACE = 5
GROUP_UNCONFIRMED = "cleanup_pending"


class GroupCleanupUnconfirmed(Exception):
    """자식 신원을 기록하지 못해 선정리했지만 그룹 소멸을 확인하지 못했다."""


def _child_setup(pidfile: bytes | None):
    """fork 직후 exec 전에 실행한다. 우선순위를 낮추고, 러너가 신원을 기록하기 전에 죽는 경우를 위해 pid 를 적어 둔다."""
    def setup() -> None:
        os.setpriority(os.PRIO_PROCESS, 0, 10)
        if pidfile:
            fd = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, b"%d" % os.getpid())
            os.close(fd)
    return setup


def _group_gone(proc: subprocess.Popen) -> bool:
    """리더를 회수한 뒤 그룹 전체가 사라졌는지 본다. 권한 거부·기타 오류는 사라졌다고 보지 않는다."""
    proc.poll()  # 회수하지 않은 좀비는 macOS 에서 killpg(pgid, 0) 이 EPERM 을 돌려준다
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _kill_group(proc: subprocess.Popen) -> bool:
    """TERM 뒤 KILL 로 그룹 전체를 정리하고, 그룹이 실제로 사라졌을 때만 True 를 돌려준다.

    리더만 죽는 것으로는 충분하지 않다. 리더가 TERM 에 죽어도 TERM 을 무시하는 손자는 남을 수 있다.
    """
    for sig, grace in ((signal.SIGTERM, _TERM_GRACE), (signal.SIGKILL, _KILL_GRACE)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return _group_gone(proc)
        except OSError:
            continue
        deadline = time.monotonic() + grace
        while True:
            if _group_gone(proc):
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return _group_gone(proc)


def _execute(plan: plans.Plan, out, *, job_id: str | None = None) -> tuple[str, int | None]:
    """(status, exit_code). 출력은 파일로 받아 파이프가 차서 멈추는 일을 피한다.

    취소·시간 초과에서 그룹 종료를 확인하지 못하면 cancelled/timeout 대신 cleanup_pending 을 돌려준다.
    """
    if job_id:
        # Popen 과 신원 기록 사이에 러너가 죽어도 reconcile 이 미기록 자식을 성공으로 오해하지 않게 먼저 표식을 남긴다
        jobs.update(job_id, child_starting=True, child_starting_at=time.time(),
                    child_planned_sha256=hashlib.sha256(" ".join(plan.argv).encode()).hexdigest())
    # 낮은 우선순위: 백업이 다른 사용자 Run 의 CPU 를 빼앗지 않게 한다. nice 래퍼는 exec 로 cmdline 이 바뀌어
    # 신원 해시가 어긋나므로 쓰지 않고 자식이 exec 하기 전에 우선순위만 낮춘다.
    pidfile = os.fsencode(jobs.child_pidfile(job_id)) if job_id else None
    proc = subprocess.Popen(plan.argv, cwd=plans.BACK, env=plan.env, preexec_fn=_child_setup(pidfile),  # noqa: PLW1509
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    # 자식 프로세스 신원 기록 (SIGKILL 시 정리용). Popen 은 exec 가 끝난 뒤 돌아오므로 cmdline 이 안정된 값이다
    if job_id:
        try:
            p = psutil.Process(proc.pid)
            started = p.create_time()
            cmdline = " ".join(p.cmdline())
        except (psutil.Error, ProcessLookupError):
            started = None
            cmdline = None
        try:
            jobs.update(job_id, child_pid=proc.pid, child_pgid=proc.pid, child_started=started,
                        child_cmdline_sha256=hashlib.sha256(cmdline.encode()).hexdigest() if cmdline else None,
                        child_starting=False)
        except Exception as exc:
            if not _kill_group(proc):  # 기록하지 못한 자식을 남기지 않는다. 정리를 확인하지 못하면 슬롯을 유지한다
                raise GroupCleanupUnconfirmed(type(exc).__name__) from exc
            raise
        jobs.child_pidfile(job_id).unlink(missing_ok=True)

    deadline = time.monotonic() + plan.timeout
    while proc.poll() is None:
        if _cancelled:
            return ("cancelled" if _kill_group(proc) else GROUP_UNCONFIRMED), proc.returncode
        if time.monotonic() > deadline:
            return ("timeout" if _kill_group(proc) else GROUP_UNCONFIRMED), proc.returncode
        time.sleep(0.25)
    return ("succeeded" if proc.returncode == 0 else "failed"), proc.returncode


async def _drop_leftover(target: str) -> bool:
    """이 작업이 만든 격리 DB 가 남았으면 지운다. 이름 규칙(접두사+12자리 hex)이 맞는 DB 만 대상이다."""
    if not _TARGET.match(target):
        return False
    url, prod = plans.local_database()
    if target == prod:
        return False
    conn = await asyncpg.connect(dsn=_libpq(url), database="postgres", timeout=10)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target):
            return False
        await conn.execute(f'DROP DATABASE "{target}"')
        return True
    finally:
        await conn.close()


async def drop_all_leftover_dbs() -> list[str]:
    """이전 작업이 남긴 모든 격리 DB(이름 규칙 일치)를 시작 시 정리한다."""
    url, prod = plans.local_database()
    conn = await asyncpg.connect(dsn=_libpq(url), database="postgres", timeout=10)
    dropped = []
    try:
        rows = await conn.fetch("SELECT datname FROM pg_database WHERE datname LIKE $1", f"{plans.RESTORE_DB_PREFIX}%")
        for row in rows:
            name = row["datname"]
            if _TARGET.match(name) and name != prod:
                await conn.execute(f'DROP DATABASE "{name}"')
                dropped.append(name)
        return dropped
    except Exception:  # noqa: BLE001
        return dropped
    finally:
        await conn.close()


def _secure_rmtree(target: Path) -> bool:
    try:
        if not target.exists():
            return True
        for root, dirs, files in os.walk(target, topdown=False):
            for name in files:
                fpath = Path(root) / name
                try:
                    fpath.chmod(0o600)
                    with open(fpath, "wb") as f:
                        f.write(b"\x00" * fpath.stat().st_size)
                    fpath.unlink(missing_ok=True)
                except OSError:
                    pass
            for name in dirs:
                try:
                    (Path(root) / name).rmdir()
                except OSError:
                    pass
        target.rmdir()
        return not target.exists()
    except Exception:  # noqa: BLE001
        return not target.exists()


def _status_section(section: str) -> dict | None:
    return backup_status.backup_status()[section]


def _summarize(plan: plans.Plan, status: str, output: str, before: dict | None) -> tuple[str, dict, str | None]:
    """(status, summary, error). 성공 표지가 없으면 종료 코드 0 이어도 성공으로 보지 않는다."""
    summary: dict = {}
    if status != "succeeded":
        return status, summary, None
    if plan.kind in ("backup_db", "data_snapshot"):
        section = "db" if plan.kind == "backup_db" else "data"
        latest = _status_section(section)
        if not latest or (before and latest.get("created_at") == before.get("created_at")):
            return "failed", summary, "백업 상태 파일이 갱신되지 않았습니다."
        summary = {**latest, "kind_section": section}
        if not latest.get("encrypted"):
            return "failed", summary, "암호화되지 않은 산출물이라 실패로 처리합니다."
    elif plan.kind == "restore_rehearsal":
        lines = output.splitlines()
        matched = sum(1 for ln in lines if ln.startswith("MATCH "))
        bad = [ln.split(":")[0].removeprefix("MISMATCH ") for ln in lines if ln.startswith("MISMATCH ")]
        summary = {"tables_matched": matched, "tables_mismatched": len(bad),
                   "mismatched": bad[:MISMATCH_LIST_LIMIT], "isolated_db": plan.identity["target_db"],
                   "target_db_dropped": any("리허설 DB 삭제" in ln for ln in lines)}
        if bad or "복원 리허설 통과" not in output or matched == 0:
            return "failed", summary, "복원 리허설 통과 표지가 없거나 행 수가 맞지 않습니다."
    else:
        found = _VERIFIED.search(output)
        summary = {"files_verified": int(found.group(1)) if found else 0}
        if not found:
            return "failed", summary, "스냅샷 검증 통과 표지가 없습니다."
    return status, summary, None


def _redact_paths(text: str, plan: plans.Plan) -> str:
    for value, label in ((str(plans.backup_dir()), "<backup_dir>"), (str(get_settings().home), "<home>"),
                         (str(plans.BACK), "<release>")):
        text = text.replace(value, label)
    return text


async def _run(job_id: str) -> int:
    with jobs.locked():
        job = jobs.get(job_id)
        if job["status"] != "queued":
            return 0  # 시작 전에 취소됐거나 이미 처리됨
        job = jobs.write(job | {"status": "running", "started_at": jobs.now(), "runner_pid": os.getpid(),
                                "runner_started": jobs.process_identity(os.getpid())})

    # 시작 시 이전 비정상 종료 잔여물(격리 DB 및 평문 임시 파일) 정리
    try:
        await drop_all_leftover_dbs()
    except Exception:  # noqa: BLE001, S110
        pass
    plans.clean_residuals()

    started = time.monotonic()
    plan: plans.Plan | None = None
    status, code, output, error, summary = "failed", None, "", None, {}
    try:
        plan = plans.make_plan(job["kind"], job_id)
        before = _status_section("db" if job["kind"] == "backup_db" else "data") \
            if job["kind"] in ("backup_db", "data_snapshot") else None
        if plan.cleanup_dir:
            plan.cleanup_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile() as out:
            status, code = _execute(plan, out, job_id=job_id)
            out.seek(0, os.SEEK_END)
            out.seek(max(0, out.tell() - 200_000))
            output = out.read().decode("utf-8", "replace")
        status, summary, error = _summarize(plan, status, output, before)
        if plan.kind == "restore_rehearsal":
            try:
                if await _drop_leftover(plan.identity["target_db"]):
                    summary["leftover_isolated_db_dropped"] = True
            except Exception as exc:  # noqa: BLE001
                error = (error or "") + f" 격리 DB 정리 확인 실패: {type(exc).__name__}"
    except plans.Refused as exc:
        status, error = "failed", f"refused: {exc}"
    except GroupCleanupUnconfirmed:
        status = GROUP_UNCONFIRMED
    except Exception as exc:  # noqa: BLE001 -- 어떤 오류도 기록에 남기고 끝낸다
        status, error = "failed", f"runner_error: {type(exc).__name__}"
    finally:
        if plan and plan.cleanup_dir:
            root = (plans.backup_dir() / plans.VERIFY_DIR).resolve()
            if plan.cleanup_dir.resolve().parent == root:
                cleaned_ok = _secure_rmtree(plan.cleanup_dir)
                if not cleaned_ok and plan.cleanup_dir.exists():
                    if status == "succeeded":
                        status = "failed"
                    error = (error + " " if error else "") + "임시 평문 검증 디렉터리 정리 실패(잔여물 보존)"
                    summary["cleanup_dir_retained"] = True

    if status == GROUP_UNCONFIRMED:
        error = "cleanup_pending: 취소·시간 초과 뒤에도 자식 프로세스 그룹 종료를 확인하지 못했습니다(작업 슬롯 유지)"
    tail = scrub(_redact_paths(output, plan), []) if plan else ""
    finished = {"status": status, "exit_code": code, "summary": summary,
                "error": scrub(error) if error else (f"종료 코드 {code}" if status == "failed" else None),
                "output_tail": tail[-OUTPUT_TAIL_CHARS:],
                "finished_at": None if status == GROUP_UNCONFIRMED else jobs.now(),
                "duration_s": round(time.monotonic() - started, 2)}
    with jobs.locked():
        jobs.write(jobs.get(job_id) | finished | {"finish_audited": False})
    await jobs.audit_finish(job_id)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        job_id = str(uuid.UUID(argv[1]))
    except ValueError:
        return 2
    signal.signal(signal.SIGTERM, _on_term)
    return asyncio.run(_run(job_id))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
