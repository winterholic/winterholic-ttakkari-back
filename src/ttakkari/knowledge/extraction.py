"""Resumable, budgeted extraction protocol consumed by the caller's existing model.

No additional provider is silently invoked. The calling agent accounts for its own
inference usage. Every unit must receive a disposition, including nontechnical data.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from ttakkari.knowledge import store
from ttakkari.knowledge_models import KnowledgeExtractionJob as J
from ttakkari.knowledge_models import KnowledgeRevision as R
from ttakkari.knowledge_models import KnowledgeUnit as U
from ttakkari.models import CtxSource, Run, utcnow

MAX_UNITS = 100
MAX_CHARS = 20_000
DEFAULT_VERSION = "evidence-extraction-v2"
MAX_RETRIES = 3
RETRYABLE_REASONS = {
    "model_output_invalid",
    "transient_timeout",
    "connection_reset",
    "temporary_provider_error",
}
INSTRUCTIONS = (
    "모든 입력에 처리결과와 처리 사유를 주세요. 발화자·시점·분기와 사실/제안/부정/추론을 구분하고 "
    "기술·건강·가치관·정체성·진로를 도메인 때문에 제외하지 마세요. "
    "관계는 정확한 원문 quote와 0-based end-exclusive start/end로만 제출하세요. "
    "사용자 확정·기억 pinned 승격을 하지 마세요. 애매한 항목은 추론/질문 후보로 두세요. "
    "unsupported/error에는 구체적인 reason을 남기고 동일한 접근 거부를 그대로 재시도하지 마세요."
)


async def execution_context(db, session_id=None):
    if session_id is None:
        return {
            "actor": "user_or_operator",
            "run_id": None,
            "model": None,
            "inference": "unreported",
        }
    run = await db.scalar(
        select(Run)
        .where(Run.session_id == session_id, Run.status == "running")
        .order_by(Run.created_at.desc(), Run.id)
        .limit(1)
    )
    return {
        "actor": "agent",
        "session_id": str(session_id),
        "run_id": str(run.id) if run else None,
        "engine": run.engine if run else None,
        "model": run.model if run else None,
        "effort": run.effort if run else None,
        "profile": run.profile if run else None,
        "cost_usd_observed": run.cost_usd if run else None,
        "cost_scope": "entire_run_not_extraction",
        "observation_scope": "server_run_record",
        "observed_at": utcnow().isoformat(),
    }


def job_out(j: J) -> dict:
    return {
        "id": str(j.id),
        "revision_id": str(j.revision_id),
        "extractor_version": j.extractor_version,
        "status": j.status,
        "run_id": str(j.run_id) if j.run_id else None,
        "parent_job_id": j.manifest.get("parent_job_id"),
        "root_job_id": j.manifest.get("root_job_id", str(j.id)),
        "retry_attempt": j.manifest.get("retry_attempt", 0),
        "cursor": j.cursor,
        "counts": j.counts,
        "error": j.error,
        "updated_at": j.updated_at.isoformat(),
    }


async def get_job(db, jid: uuid.UUID, scope: str = "all", *, lock: bool = False):
    query = (
        select(J)
        .join(R, R.id == J.revision_id)
        .outerjoin(CtxSource, CtxSource.id == R.source_id)
    )
    query = query.where(J.id == jid, *store.visible(scope, current=False))
    if lock:
        query = query.with_for_update(of=J).execution_options(populate_existing=True)
    job = await db.scalar(query)
    if not job:
        raise store.KnowledgeError("extraction_not_found", 404)
    return job


async def start(
    db,
    revision_id: uuid.UUID,
    extractor_version: str = DEFAULT_VERSION,
    *,
    scope: str = "all",
    execution: dict | None = None,
):
    revision = await db.scalar(
        select(R)
        .outerjoin(CtxSource, CtxSource.id == R.source_id)
        .where(R.id == revision_id, *store.visible(scope, current=False))
    )
    if not revision:
        raise store.KnowledgeError("revision_not_found", 404)
    await store._source_lock(db, f"extraction:{revision_id}:{extractor_version}")
    job = await db.scalar(
        select(J).where(
            J.revision_id == revision_id, J.extractor_version == extractor_version
        )
    )
    if job:
        return job
    ids = [
        str(i)
        for i in await db.scalars(
            select(U.id)
            .where(U.revision_id == revision_id)
            .order_by(U.source_offset, U.id)
        )
    ]
    job = J(
        revision_id=revision_id,
        extractor_version=extractor_version,
        run_id=uuid.UUID(execution["run_id"])
        if execution and execution.get("run_id")
        else None,
        manifest={
            "unit_ids": ids,
            "initial_execution": execution or await execution_context(db),
        },
        counts={
            "total": len(ids),
            "accounted": 0,
            "candidates": 0,
            "no_claim": 0,
            "unsupported": 0,
            "errors": 0,
        },
    )
    db.add(job)
    await db.flush()
    return job


async def next_batch(
    db, jid: uuid.UUID, *, scope: str = "all", limit: int = 20
) -> dict:
    job = await get_job(db, jid, scope)
    revision = await db.get(R, job.revision_id)
    ids = job.manifest["unit_ids"][job.cursor : job.cursor + min(limit, MAX_UNITS)]
    result, characters = [], 0
    for uid in ids:
        unit, rev = await store.get_unit(db, uuid.UUID(uid), scope)
        if result and characters + len(unit.body) > MAX_CHARS:
            break
        result.append(store.unit_out(unit, rev))
        characters += len(unit.body)
    return {
        **job_out(job),
        "units": result,
        "characters": characters,
        "sensitivity": revision.sensitivity,
        "account_usage": "inference_owned_by_calling_agent",
        "prompt_version": DEFAULT_VERSION,
        "instructions": INSTRUCTIONS,
        "prompt_sha256": store.digest(INSTRUCTIONS),
    }


async def submit(
    db,
    jid: uuid.UUID,
    expected_cursor: int,
    results: list[dict],
    *,
    scope: str = "all",
    execution: dict | None = None,
):
    job = await get_job(db, jid, scope, lock=True)
    fingerprint = store.digest(
        json.dumps(
            [
                {k: v for k, v in r.items() if k != "reason" or v is not None}
                for r in results
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )
    if job.cursor != expected_cursor:
        if (
            job.manifest.get("last_cursor") == expected_cursor
            and job.manifest.get("last_sha256") == fingerprint
        ):
            return job
        raise store.KnowledgeError("stale_extraction_cursor", 409)
    if job.status in ("succeeded", "cancelled"):
        raise store.KnowledgeError("extraction_closed", 409)
    if not results or len(results) > MAX_UNITS:
        raise store.KnowledgeError("empty_or_oversized_batch")
    ids = [str(r["unit_id"]) for r in results]
    expected = job.manifest["unit_ids"][
        expected_cursor : expected_cursor + len(results)
    ]
    if len(set(ids)) != len(ids) or ids != expected:
        raise store.KnowledgeError("batch_unit_accounting_mismatch")
    if len(results) < len(expected):
        raise store.KnowledgeError("batch_unit_accounting_mismatch")
    # Validate the entire batch before storing any claim. Caller rollback makes errors atomic.
    chars = 0
    for result in results:
        unit, _revision = await store.get_unit(
            db, uuid.UUID(str(result["unit_id"])), scope
        )
        chars += len(unit.body)
        if (
            result["outcome"] in ("error", "unsupported")
            and not (result.get("reason") or "").strip()
        ):
            raise store.KnowledgeError("failure_disposition_requires_reason")
        if result["outcome"] == "candidates" and not result["claims"]:
            raise store.KnowledgeError("candidate_disposition_requires_claim")
        if result["outcome"] != "candidates" and result["claims"]:
            raise store.KnowledgeError("unexpected_claims_for_disposition")
        for claim in result["claims"]:
            await store.add_assertion(db, unit_id=unit.id, scope=scope, **claim)
    if chars > MAX_CHARS:
        raise store.KnowledgeError("batch_character_budget_exceeded")
    counts = dict(job.counts)
    counts["accounted"] += len(results)
    for result in results:
        counts["candidates"] += len(result["claims"])
        for name in ("no_claim", "unsupported"):
            counts[name] += int(result["outcome"] == name)
        counts["errors"] += int(result["outcome"] == "error")
    job.counts, job.cursor = counts, expected_cursor + len(results)
    job.manifest = {
        **job.manifest,
        "last_cursor": expected_cursor,
        "last_sha256": fingerprint,
        "batches": [
            *job.manifest.get("batches", []),
            {
                "cursor": expected_cursor,
                "end_cursor": job.cursor,
                "input_sha256": fingerprint,
                "prompt_version": DEFAULT_VERSION,
                "prompt_sha256": store.digest(INSTRUCTIONS),
                "execution": execution or await execution_context(db),
                "recorded_at": utcnow().isoformat(),
            },
        ],
        "dispositions": {
            **job.manifest.get("dispositions", {}),
            **{
                str(r["unit_id"]): {
                    "outcome": r["outcome"],
                    "claim_count": len(r["claims"]),
                    "reason": r.get("reason"),
                }
                for r in results
            },
        },
    }
    job.status = (
        ("partial" if counts["errors"] else "succeeded")
        if job.cursor == counts["total"]
        else "running"
    )
    job.error = "unit_errors_recorded" if counts["errors"] else None
    job.updated_at = utcnow()
    await db.flush()
    return job


async def details(db, jid, scope="all"):
    job = await get_job(db, jid, scope)
    return {
        **job_out(job),
        "dispositions": job.manifest.get("dispositions", {}),
        "batches": job.manifest.get("batches", []),
        "initial_execution": job.manifest.get("initial_execution"),
    }


async def retry_failed(db, jid, *, scope="all", execution=None):
    parent = await get_job(db, jid, scope, lock=True)
    if parent.status != "partial" or parent.cursor != parent.counts["total"]:
        raise store.KnowledgeError("retry_requires_finished_partial_job", 409)
    attempt = parent.manifest.get("retry_attempt", 0) + 1
    if attempt > MAX_RETRIES:
        raise store.KnowledgeError("extraction_retry_budget_exceeded", 409)
    ids = [
        uid
        for uid in parent.manifest["unit_ids"]
        if parent.manifest.get("dispositions", {}).get(uid, {}).get("outcome")
        == "error"
        and (parent.manifest.get("dispositions", {}).get(uid, {}).get("reason") or "")
        .split(":", 1)[0]
        .strip()
        .lower()
        in RETRYABLE_REASONS
    ]
    if not ids:
        raise store.KnowledgeError("no_retryable_units", 409)
    # A repeat request names the same child; a further attempt must name that child's id.
    version = (
        "retry-" + store.digest(str(parent.id) + ":" + parent.extractor_version)[:40]
    )
    existing = await db.scalar(
        select(J).where(
            J.revision_id == parent.revision_id, J.extractor_version == version
        )
    )
    if existing:
        return existing
    j = J(
        revision_id=parent.revision_id,
        extractor_version=version,
        run_id=uuid.UUID(execution["run_id"])
        if execution and execution.get("run_id")
        else None,
        manifest={
            "unit_ids": ids,
            "parent_job_id": str(parent.id),
            "root_job_id": parent.manifest.get("root_job_id", str(parent.id)),
            "base_extractor_version": parent.manifest.get(
                "base_extractor_version", parent.extractor_version
            ),
            "retry_attempt": attempt,
            "initial_execution": execution or await execution_context(db),
        },
        counts={
            "total": len(ids),
            "accounted": 0,
            "candidates": 0,
            "no_claim": 0,
            "unsupported": 0,
            "errors": 0,
        },
    )
    db.add(j)
    await db.flush()
    return j
