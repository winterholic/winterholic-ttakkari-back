"""에이전트의 승인된 호스트 운영 작업(백업·격리 복원·소유권/성능 확인). 개인 세션만, 종류 허용 목록만."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.operations import inline, jobs, plans
from ttakkari.oplog.scrub import scrub
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession

router = APIRouter(prefix="/api/agent/operations", tags=["agent"])

DESCRIPTIONS = {
    "runtime_report": "실행 소유권: 활성/실행기 세대, lease·프로세스 그룹 수, 최근 배포 예약 (읽기)",
    "performance_baseline": "짧은 합성 기준 성능: DB 왕복·이벤트 루프 지연·호스트 압박 (읽기)",
    "backup_status": "마지막 DB 백업·데이터 스냅샷 시각·개수·해시와 키 출처 (읽기)",
    "backup_db": "운영 DB 읽기 전용 pg_dump 새 사본을 암호화해 앱 백업 디렉터리에 저장",
    "data_snapshot": "앱 data 디렉터리 암호화 스냅샷 새 사본(설정·계정·키는 포함하지 않음)",
    "restore_rehearsal": "최근 DB 백업을 격리 임시 DB 에 복원해 테이블 행 수를 대조하고 임시 DB 를 지움(운영 DB 불변)",
    "snapshot_verify": "최근 데이터 스냅샷을 격리 임시 위치에 풀어 파일 해시를 대조하고 지움",
    "collection_scope_preview": "스키마·활성 세대·이전 세대 종료를 확인하고 단일 도서관 통합을 실행한 뒤 롤백",
    "collection_scope_apply": "이전 세대 종료 후 기존 이력·접근 pool을 보존해 단일 도서관 통합 적용",
}


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["runtime_report", "performance_baseline", "backup_status", "backup_db", "data_snapshot",
                  "restore_rehearsal", "snapshot_verify", "collection_scope_preview", "collection_scope_apply"]


async def personal(sid: uuid.UUID, db: Db) -> None:
    await _session(db, sid)


def _job_id(job_id: str) -> str:
    try:
        return str(uuid.UUID(job_id))
    except ValueError as exc:
        raise HTTPException(404, "운영 작업이 없습니다.") from exc


@router.get("")
async def list_operations(sid: AgentSession, db: Db) -> dict:
    await personal(sid, db)
    recent = await asyncio.to_thread(jobs.recent, 10)
    for job in recent:
        if job.get("status") in jobs.TERMINAL and not job.get("finish_audited"):
            await jobs.audit_finish(job["id"])
    recent = await asyncio.gather(*(asyncio.to_thread(jobs.get, j["id"]) for j in recent))
    return {"kinds": DESCRIPTIONS, "jobs": [jobs.view(j) for j in recent]}


@router.post("", status_code=202)
async def start_operation(body: StartIn, sid: AgentSession, db: Db) -> dict:
    await personal(sid, db)
    if body.kind in plans.INLINE:
        job = await asyncio.to_thread(jobs.create, body.kind, str(sid), inline=True)
        record(db, actor="agent", action="operation.request", target_type="operation", target_id=job["id"],
               detail={"kind": body.kind, "session_id": str(sid), "inline": True})
        await db.commit()
        try:
            summary = await inline.run(body.kind, db)
            fields = {"status": "succeeded", "summary": summary}
        except Exception as exc:  # noqa: BLE001 -- 실패도 기록에 남긴다
            fields = {"status": "failed", "error": scrub(f"{type(exc).__name__}: {exc}")[:500]}
        job = await asyncio.to_thread(jobs.update, job["id"], finished_at=jobs.now(), finish_audited=False, **fields)
        job = await jobs.audit_finish(job["id"])
        return jobs.view(job | {"deduplicated": False})

    try:
        job, reused = await asyncio.to_thread(jobs.prepare_heavy, body.kind, str(sid))
    except jobs.Conflict as exc:
        record(db, actor="agent", action="operation.request", outcome="error", target_type="operation",
               detail={"kind": body.kind, "session_id": str(sid), "reason": "conflict"})
        await db.commit()
        raise HTTPException(409, str(exc)) from exc
    except plans.Refused as exc:
        record(db, actor="agent", action="operation.request", outcome="error", target_type="operation",
               detail={"kind": body.kind, "session_id": str(sid), "reason": str(exc)[:200]})
        await db.commit()
        raise HTTPException(exc.status, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"작업 준비 실패: {type(exc).__name__}") from exc

    if reused:
        record(db, actor="agent", action="operation.request", target_type="operation", target_id=job["id"],
               detail={"kind": body.kind, "session_id": str(sid), "deduplicated": True})
        await db.commit()
        return jobs.view(job | {"deduplicated": True})

    try:
        record(db, actor="agent", action="operation.request", target_type="operation", target_id=job["id"],
               detail={"kind": body.kind, "session_id": str(sid), "deduplicated": False})
        await db.commit()
    except Exception:
        await asyncio.to_thread(jobs.clean_failed_queue, job["id"])
        raise

    try:
        job = await asyncio.to_thread(jobs.spawn_heavy, job["id"])
    except OSError as exc:
        raise HTTPException(503, f"작업 러너를 시작하지 못했습니다: {type(exc).__name__}") from exc
    except Exception as exc:
        raise HTTPException(500, f"작업 러너 시작 실패: {type(exc).__name__}") from exc

    return jobs.view(job | {"deduplicated": False})


@router.get("/{job_id}")
async def operation_status(job_id: str, sid: AgentSession, db: Db) -> dict:
    await personal(sid, db)
    try:
        job = await asyncio.to_thread(jobs.reconciled, _job_id(job_id))
        if job.get("status") in jobs.TERMINAL and not job.get("finish_audited"):
            job = await jobs.audit_finish(job["id"])
        return jobs.view(job)
    except FileNotFoundError as exc:
        raise HTTPException(404, "운영 작업이 없습니다.") from exc


@router.post("/{job_id}/cancel")
async def cancel_operation(job_id: str, sid: AgentSession, db: Db) -> dict:
    await personal(sid, db)
    try:
        job = await asyncio.to_thread(jobs.cancel, _job_id(job_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, "운영 작업이 없습니다.") from exc
    except jobs.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    record(db, actor="agent", action="operation.cancel", target_type="operation", target_id=job["id"],
           detail={"kind": job["kind"], "session_id": str(sid)})
    await db.commit()
    if job.get("status") in jobs.TERMINAL and not job.get("finish_audited"):
        job = await jobs.audit_finish(job["id"])
    return jobs.view(job)
