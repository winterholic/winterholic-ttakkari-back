"""분리된 러너: `python -m ttakkari.operations.runner <job_id>`. 계획된 스크립트 하나를 시간 제한 안에서 돌린다.

API 프로세스와 별도 세션이라 API 재시작·세대 교체에 죽지 않는다. SIGTERM 은 취소로 처리해 자식 프로세스
그룹을 정리하고 cancelled 로 남긴다. 출력은 scrub 한 뒤 꼬리만 보존하며, 덤프·키 내용은 어디에도 싣지 않는다.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import asyncpg

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


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _execute(plan: plans.Plan, out) -> tuple[str, int | None]:
    """(status, exit_code). 출력은 파일로 받아 파이프가 차서 멈추는 일을 피한다."""
    # nice 로 낮은 우선순위: 백업이 다른 사용자 Run 의 CPU 를 빼앗지 않게 한다
    proc = subprocess.Popen(["/usr/bin/nice", "-n", "10", *plan.argv], cwd=plans.BACK, env=plan.env,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + plan.timeout
    while proc.poll() is None:
        if _cancelled:
            _kill_group(proc)
            return "cancelled", proc.returncode
        if time.monotonic() > deadline:
            _kill_group(proc)
            return "timeout", proc.returncode
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


async def _audit(job: dict) -> str | None:
    try:
        from ttakkari.audit import record
        from ttakkari.db import sessionmaker
        async with sessionmaker()() as db:
            record(db, actor="operation", action="operation.finish",
                   outcome="ok" if job["status"] == "succeeded" else "error", target_type="operation",
                   target_id=job["id"],
                   detail={"kind": job["kind"], "status": job["status"], "exit_code": job.get("exit_code"),
                           "duration_s": job.get("duration_s"), "session_id": job.get("requested_by_session")})
            await db.commit()
        return None
    except Exception as exc:  # noqa: BLE001 -- 감사 실패가 작업 결과 기록을 막지 않는다. 사실만 남긴다
        return type(exc).__name__


async def _run(job_id: str) -> int:
    with jobs.locked():
        job = jobs.get(job_id)
        if job["status"] != "queued":
            return 0  # 시작 전에 취소됐거나 이미 처리됨
        job = jobs.write(job | {"status": "running", "started_at": jobs.now(), "runner_pid": os.getpid(),
                                "runner_started": jobs.process_identity(os.getpid())})
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
            status, code = _execute(plan, out)
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
    except Exception as exc:  # noqa: BLE001 -- 어떤 오류도 기록에 남기고 끝낸다
        status, error = "failed", f"runner_error: {type(exc).__name__}"
    finally:
        if plan and plan.cleanup_dir:
            root = (plans.backup_dir() / plans.VERIFY_DIR).resolve()
            if plan.cleanup_dir.resolve().parent == root:
                shutil.rmtree(plan.cleanup_dir, ignore_errors=True)
    tail = scrub(_redact_paths(output, plan), []) if plan else ""
    finished = {"status": status, "exit_code": code, "summary": summary,
                "error": scrub(error) if error else (f"종료 코드 {code}" if status == "failed" else None),
                "output_tail": tail[-OUTPUT_TAIL_CHARS:], "finished_at": jobs.now(),
                "duration_s": round(time.monotonic() - started, 2)}
    with jobs.locked():
        final = jobs.write(jobs.get(job_id) | finished)
    audit_error = await _audit(final)
    if audit_error:
        jobs.update(job_id, audit_error=audit_error)
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
