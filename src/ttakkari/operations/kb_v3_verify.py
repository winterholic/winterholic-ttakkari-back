"""지식 도서관(KB v3) L2 활성화 및 상충 파이프라인 실검증 호스트 운영 작업 (kb_v3_verify).

고정 kind 전용: generic 쉘/경로/DB URL/프롬프트/계정 설정 입력을 받지 않으며 오직 db 세션만 인자로 받는다.
1개 공개 합성 PDF + 2개 서로 상충하는 30일/90일 공개 정책 문서를 제품 ingest 파이프라인으로 등록하고,
자동 워커를 구동하여 L2 추출 및 정본·상충 해소 단계를 finite 시간 상한 내에서 검증한다.
결과는 비밀값/원문/프롬프트를 배제한 채 counts, IDs, job 단계 결과만 반환한다.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_library import ensure_document_access, register_text
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
)
from ttakkari.kb_parsers import create_default_registry
from ttakkari.kb_parsers.base import ParseStatus
from ttakkari.kb_pipeline import (
    STAGE_EXTRACTING,
    enqueue_pipeline_job,
    retry_pipeline_job,
)
from ttakkari.kb_pipeline.types import JOB_STATUS_REGISTERED, PIPELINE_STAGES
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.kb_store import ingest_source, segment_revision
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

# 멱등 식별자 접두사: production DB 에 남아도 테스트 산출물임이 명확히 드러남
KEY_PREFIX = "synthetic:kb_v3_verify:"
KEY_PDF = f"{KEY_PREFIX}pdf_spec"
KEY_POLICY_30D = f"{KEY_PREFIX}policy_30d"
KEY_POLICY_90D = f"{KEY_PREFIX}policy_90d"

# 내장 공개 합성 PDF (sample_spec.pdf 1359 bytes)
_EMBEDDED_PDF_B64 = (
    "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFn"
    "ZXMKL0NvdW50IDMKL0tpZHMgWyA0IDAgUiA1IDAgUiA2IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9n"
    "Ci9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Ci9Gb250IDw8Ci9GMSA8"
    "PAovVHlwZSAvRm9udAovU3VidHlwZSAvVHlwZTEKL0Jhc2VGb250IC9IZWx2ZXRpY2EKPj4KPj4KPj4KL01lZGlhQm94IFsgMC4w"
    "IDAuMCA1OTUgODQyIF0KL1BhcmVudCAyIDAgUgovQ29udGVudHMgPDwKL0xlbmd0aCAxMTIKPj4Kc3RyZWFtCkJUIC9GMSAxMiBU"
    "ZiA3MiA3MTIgVGQgKFBhZ2UgMTogU3BlY2lmaWNhdGlvbiBkZXRhaWxzIGZvciBLdWJlcm5ldGVzIGNsdXN0ZXIgZGVwbG95bWVu"
    "dCBhbmQgYXJjaGl0ZWN0dXJlLikgVGogRVQKZW5kc3RyZWFtCj4+CmVuZG9iago1IDAgb2JqCjw8Ci9UeXBlIC9QYWdlCi9SZXNv"
    "dXJjZXMgPDwKL0ZvbnQgPDwKL0YxIDw8Ci9UeXBlIC9Gb250Ci9TdWJ0eXBlIC9UeXBlMQovQmFzZUZvbnQgL0hlbHZldGljYQo+"
    "Pgo+Pgo+PgovTWVkaWFCb3ggWyAwLjAgMC4wIDU5NSA4NDIgXQovUGFyZW50IDIgMCBSCi9Db250ZW50cyA8PAovTGVuZ3RoIDEx"
    "Mgo+PgpzdHJlYW0KQlQgL0YxIDEyIFRmIDcyIDcxMiBUZCAoUGFnZSAyOiBTcGVjaWZpY2F0aW9uIGRldGFpbHMgZm9yIEt1YmVy"
    "bmV0ZXMgY2x1c3RlciBkZXBsb3ltZW50IGFuZCBhcmNoaXRlY3R1cmUuKSBUaiBFVAplbmRzdHJlYW0KPj4KZW5kb2JqCjYgMCBv"
    "YmoKPDwKL1R5cGUgL1BhZ2UKL1Jlc291cmNlcyA8PAovRm9udCA8PAovRjEgPDwKL1R5cGUgL0ZvbnQKL1N1YnR5cGUgL1R5cGUx"
    "Ci9CYXNlGb250IC9IZWx2ZXRpY2EKPj4KPj4KPj4KL01lZGlhQm94IFsgMC4wIDAuMCA1OTUgODQyIF0KL1BhcmVudCAyIDAgUgov"
    "Q29udGVudHMgPDwKL0xlbmd0aCAxMTIKPj4Kc3RyZWFtCkJUIC9GMSAxMiBUZiA3MiA3MTIgVGQgKFBhZ2UgMzogU3BlY2lmaWNh"
    "dGlvbiBkZXRhaWxzIGZvciBLdWJlcm5ldGVzIGNsdXN0ZXIgZGVwbG95bWVudCBhbmQgYXJjaGl0ZWN0dXJlLikgVGogRVQKZW5k"
    "c3RyZWFtCj4+CmVuZG9iagp4cmVmCjAgNwowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMTUgMDAwMDAgbiAKMDAwMDAwMDA1"
    "NCAwMDAwMCBuIAowMDAwMDAwMTI1IDAwMDAwIG4gCjAwMDAwMDAxNzQgMDAwMDAgbiAKMDAwMDAwMDQ5NyAwMDAwMCBuIAowMDAw"
    "MDAwODIwIDAwMDAwIG4gCnRyYWlsZXIKPDwKL1NpemUgNwovUm9vdCAzIDAgUgovSW5mbyAxIDAgUgo+PgpzdGFydHhyZWYKMTE0"
    "MwolJUVPRgo="
)

# 공개 상충 정책 문서 2건: 동일 엔터티(Return Policy / 반품 규정)에 대해 동일 predicate 에서 서로 다른 규정값(30일 vs 90일)을 명시
TEXT_POLICY_30D = """# 반품 규정 (30일 정책)

반품 규정(Return Policy) refers to standard customer service guarantee allowing refunds within 30 days.

반품 규정(Return Policy)은 구매일로부터 30일 이내에 반드시 환불을 보장하는 표준 고객 규정이다.
"""

TEXT_POLICY_90D = """# 반품 규정 (90일 정책)

반품 규정(Return Policy) refers to standard customer service guarantee allowing refunds within 90 days.

반품 규정(Return Policy)은 구매일로부터 90일 이내에 반드시 환불을 보장하는 표준 고객 규정이다.
"""

TERMINAL_JOB_STATUSES = ("ready", "degraded", "failed", "held")
MAX_VERIFY_WAIT_SECONDS = 30.0


def _get_pdf_bytes() -> bytes:
    fixture_path = (
        Path(__file__).resolve().parents[3] / "tests/fixtures/kb_v3/sample_spec.pdf"
    )
    if fixture_path.is_file():
        try:
            return fixture_path.read_bytes()
        except OSError:
            pass
    return base64.b64decode(_EMBEDDED_PDF_B64)


async def check_schema_support(db: AsyncSession) -> bool:
    """generation/runtime 에 KB v3 핵심 테이블이 배포되어 있는지 확인한다."""
    try:
        bind = db.bind
        dialect_name = bind.dialect.name if bind else "postgresql"
        if dialect_name == "sqlite":
            check = await db.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                    "('kb_pipeline_jobs', 'kb_documents', 'kb_conflicts', 'kb_assertions', 'kb_assertion_evidence')"
                )
            )
            rows = check.fetchall()
            return len(rows) >= 3

        check = await db.execute(
            text(
                "SELECT to_regclass('kb_pipeline_jobs'), "
                "to_regclass('kb_documents'), "
                "to_regclass('kb_conflicts')"
            )
        )
        row = check.first()
        return bool(row and all(r is not None for r in row))
    except Exception:  # noqa: BLE001
        return False


async def verify_kb_v3(
    db: AsyncSession,
    *,
    session_factory: Any | None = None,
    worker: Any | None = None,
    max_wait_seconds: float = MAX_VERIFY_WAIT_SECONDS,
    job_id: str | None = None,
) -> dict[str, Any]:
    """KB v3 실제 파이프라인 검증 오퍼레이션 실행.
    
    1. 스키마 확인
    2. 공개 합성 3문서(PDF 1건 + 30일/90일 정책 2건) 등록 및 enqueue
    3. old L2 disabled 문서 필요 시 retry
    4. 글로벌 backlog 를 건드리지 않는 대상 3개 잡 전용 관측 루프 (남은 시간 finite await)
    5. DB 조회 기반 집계 및 엄격한 성공 게이트 판정
    """
    # 1. 스키마 확인
    if not await check_schema_support(db):
        return {
            "status": "unsupported",
            "reason": "kb_v3_schema_missing",
            "message": "KB v3 database tables are not present in current runtime/generation",
        }

    s = get_settings()
    is_l2_disabled = s.kb_l2_adapter == "disabled"
    sf = session_factory or sessionmaker()

    # 2. 공개 합성 3문서 등록
    # 2-1. 공개 합성 PDF 1건
    pdf_bytes = _get_pdf_bytes()
    parsed_pdf = create_default_registry().parse(
        pdf_bytes, filename="sample_spec.pdf", content_type="application/pdf"
    )
    if parsed_pdf.status != ParseStatus.SUCCEEDED.value:
        return {
            "status": "failed",
            "reason": f"pdf_parse_failed:{parsed_pdf.error_code}",
            "detail": parsed_pdf.error_detail,
        }

    parser_name = (parsed_pdf.metadata or {}).get("parser", "pdf_pypdf_v1")
    parser_ver = (parsed_pdf.metadata or {}).get("version", "1.0")
    parser_version_str = f"{parser_name}-{parser_ver}"

    res_pdf = await ingest_source(
        db,
        source_key=KEY_PDF,
        data=pdf_bytes,
        kind="upload",
        locator="sample_spec.pdf",
        media_type="application/pdf",
        title="[공개 합성] 시스템 사양서 (PDF)",
        lang="ko",
        parser_version=parser_version_str,
        parser_name=parser_name,
        normalized_body=parsed_pdf.normalized_body,
        origin_meta={"via": "kb_v3_verify"},
        allow_binary=True,
        original_filename="sample_spec.pdf",
    )
    await ensure_document_access(db, res_pdf.document_id, scope="personal")
    pdf_job = await db.scalar(
        select(KbPipelineJob).where(KbPipelineJob.revision_id == res_pdf.revision_id)
    )
    if res_pdf.created or pdf_job is None:
        await segment_revision(
            db, res_pdf.revision_id, locator_spans=parsed_pdf.locator_spans
        )
        await enqueue_pipeline_job(
            db, document_id=res_pdf.document_id, revision_id=res_pdf.revision_id
        )

    # 2-2. 상충 30일 정책 문서
    res_30 = await register_text(
        db,
        title="[공개 합성] 반품 규정 (30일)",
        text=TEXT_POLICY_30D,
        source_key=KEY_POLICY_30D,
        lang="ko",
        locator=None,
        scope="personal",
        origin_extra={"via": "kb_v3_verify"},
    )

    # 2-3. 상충 90일 정책 문서
    res_90 = await register_text(
        db,
        title="[공개 합성] 반품 규정 (90일)",
        text=TEXT_POLICY_90D,
        source_key=KEY_POLICY_90D,
        lang="ko",
        locator=None,
        scope="personal",
        origin_extra={"via": "kb_v3_verify"},
    )
    # 이미 commit 된 자료는 일반 제품 queue 소유로 명시
    await db.commit()

    rev_ids = [res_pdf.revision_id, res_30.revision_id, res_90.revision_id]
    doc_ids = [res_pdf.document_id, res_30.document_id, res_90.document_id]

    # 3. 기존 문서가 old L2 disabled 인 경우 필요 단계(extracting) 정상 재처리(reprocess)
    existing_jobs = list(
        (
            await db.scalars(
                select(KbPipelineJob).where(KbPipelineJob.revision_id.in_(rev_ids))
            )
        ).all()
    )
    for job in existing_jobs:
        stages = {
            st.stage: st
            for st in (
                await db.scalars(
                    select(KbPipelineStage).where(KbPipelineStage.job_id == job.id)
                )
            ).all()
        }
        ext = stages.get(STAGE_EXTRACTING)
        # 이전 실행에서 L2 가 꺼져 있어 degraded 였는데 현재 L2 가 활성화되어 있다면 정상 재처리
        if (
            ext is not None
            and ext.status == "degraded"
            and (ext.detail or {}).get("l2", {}).get("reason") == "l2_disabled"
            and not is_l2_disabled
        ):
            await retry_pipeline_job(sf, job.id, from_stage=STAGE_EXTRACTING)
    await db.commit()

    # 4. 관측 루프: 글로벌 worker.pump / 공유 _tasks gather 제거
    # 운영 백로그는 일체 추가 처리하지 않고, 정확히 이 3개 job_ids 만 관측한다.
    deadline = time.monotonic() + max_wait_seconds
    all_terminal = False

    while time.monotonic() < deadline:
        await db.rollback()
        jobs = list(
            (
                await db.scalars(
                    select(KbPipelineJob).where(KbPipelineJob.revision_id.in_(rev_ids))
                )
            ).all()
        )
        if len(jobs) == 3 and all(j.status in TERMINAL_JOB_STATUSES for j in jobs):
            all_terminal = True
            break

        # 테스트 등에서 명시적으로 worker 인자가 전달된 경우에만 대상 잡 전용 처리
        if worker is not None:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            target_job_ids = [j.id for j in jobs if j.status not in TERMINAL_JOB_STATUSES]
            if target_job_ids and hasattr(worker, "session_factory"):
                step_timeout = min(remaining, 5.0)
                claimed: tuple[uuid.UUID, uuid.UUID] | None = None
                now_dt = utcnow()
                async with worker.session_factory() as w_db:
                    c_stmt = (
                        select(KbPipelineJob)
                        .where(
                            and_(
                                KbPipelineJob.id.in_(target_job_ids),
                                KbPipelineJob.status.in_(
                                    (JOB_STATUS_REGISTERED, *PIPELINE_STAGES)
                                ),
                                or_(
                                    KbPipelineJob.claimed_by.is_(None),
                                    KbPipelineJob.lease_expires_at <= now_dt,
                                ),
                            )
                        )
                        .order_by(KbPipelineJob.created_at.asc())
                        .limit(1)
                    )
                    c_job = await w_db.scalar(c_stmt)
                    if c_job is not None:
                        new_token = uuid.uuid4()
                        c_job.attempt_token = new_token
                        c_job.claimed_by = getattr(
                            worker, "worker_id", "kb-verify-worker"
                        )
                        c_job.claimed_at = now_dt
                        c_job.lease_expires_at = now_dt + timedelta(
                            seconds=getattr(worker, "lease_seconds", 300.0)
                        )
                        c_job.attempt_count += 1
                        c_job.status = c_job.current_stage
                        await w_db.commit()
                        claimed = (c_job.id, new_token)

                if claimed is not None:
                    job_id_claimed, token_claimed = claimed
                    if hasattr(worker, "_run_job_wrapper"):
                        await asyncio.wait_for(
                            worker._run_job_wrapper(job_id_claimed, token_claimed),
                            timeout=step_timeout,
                        )
                    elif hasattr(worker, "orchestrator"):
                        await asyncio.wait_for(
                            worker.orchestrator.execute_job(
                                job_id_claimed, token_claimed
                            ),
                            timeout=step_timeout,
                        )
            elif hasattr(worker, "pump"):
                try:
                    await asyncio.wait_for(worker.pump(), timeout=min(remaining, 1.0))
                except Exception as exc:  # noqa: BLE001
                    log.debug("test pump error: %s", exc)
            else:
                await asyncio.sleep(0.05)
        else:
            # 실 런타임: 실 런타임 백그라운드 워커가 큐를 처리하므로 오퍼레이션은 순수 관측만 수행
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 0.2))

    # 5. DB 조회 기반 검증 결과 집계 (결과 하드코딩/수동 삽입 금지)
    # 실제 DB 의 문서 수
    actual_doc_count = (
        await db.scalar(
            select(func.count(KbDocument.id)).where(KbDocument.id.in_(doc_ids))
        )
        or 0
    )

    # 패시지 수
    passages_count = (
        await db.scalar(
            select(func.count(KbPassage.id)).where(KbPassage.revision_id.in_(rev_ids))
        )
        or 0
    )

    # 엔터티 수
    entity_ids = list(
        (
            await db.scalars(
                select(KbDocumentEntity.entity_id)
                .where(KbDocumentEntity.document_id.in_(doc_ids))
                .distinct()
            )
        ).all()
    )

    # 주장(assertion) 조회
    assertions = list(
        (
            await db.scalars(
                select(KbAssertion).where(KbAssertion.revision_id.in_(rev_ids))
            )
        ).all()
    )
    assertions_count = len(assertions)

    # 근거(evidence) 수
    evidence_count = (
        await db.scalar(
            select(func.count(KbAssertionEvidence.evidence_id))
            .join(KbAssertion, KbAssertion.id == KbAssertionEvidence.assertion_id)
            .where(KbAssertion.revision_id.in_(rev_ids))
        )
        or 0
    )

    # Verify the intended L2 policy claims, not arbitrary definition sentences.
    def policy_claim(a: KbAssertion, revision_id: uuid.UUID, days: int) -> bool:
        day_value = rf"{days}\s*(?:일|days?|d)?"
        return (
            a.revision_id == revision_id
            and a.predicate in {"refund_period", "refund_period_days"}
            and a.status in {"active", "disputed"}
            and a.extractor_tier == "L2"
            and a.polarity == "positive"
            and a.modality in {"fact", "requirement"}
            and a.object_entity_id is None
            and re.fullmatch(day_value, a.object_value.strip(), re.IGNORECASE) is not None
            and re.fullmatch(day_value, a.norm_object_value.strip(), re.IGNORECASE) is not None
        )

    assertions_30 = [a for a in assertions if policy_claim(a, res_30.revision_id, 30)]
    assertions_90 = [a for a in assertions if policy_claim(a, res_90.revision_id, 90)]
    policy_days = {a.id: days for claims, days in ((assertions_30, 30), (assertions_90, 90)) for a in claims}
    supported_claims: set[uuid.UUID] = set()
    exact_quotes_valid = False
    quotes = list((await db.execute(
        select(
            KbAssertion.id, KbAssertion.revision_id, KbAssertionEvidence.relation,
            KbEvidence.span_start, KbEvidence.span_end, KbEvidence.sha256,
            KbPassage.body, KbPassage.revision_id,
        )
        .select_from(KbAssertion)
        .join(KbAssertionEvidence, KbAssertionEvidence.assertion_id == KbAssertion.id)
        .join(KbEvidence, KbEvidence.id == KbAssertionEvidence.evidence_id)
        .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
        .where(KbAssertion.revision_id.in_(rev_ids))
    )).all())
    all_spans_valid = bool(quotes)
    for assertion_id, revision_id, relation, start, end, sha, body, passage_revision in quotes:
        if (
            not body or start is None or end is None
            or not 0 <= start < end <= len(body)
            or passage_revision != revision_id
        ):
            all_spans_valid = False
            continue
        quote = body[start:end]
        if not quote.strip() or hashlib.sha256(quote.encode("utf-8")).hexdigest() != sha:
            all_spans_valid = False
            continue
        # Evidence must support this claim and contain the refund period it asserts.
        days = policy_days.get(assertion_id)
        if (
            days is not None and relation == "supports"
            and re.search(rf"(?<!\d){days}\s*(?:일|days?\b)", quote, re.IGNORECASE)
            and re.search(r"환불|refund", quote, re.IGNORECASE)
        ):
            supported_claims.add(assertion_id)
    exact_quotes_valid = all_spans_valid and set(policy_days) <= supported_claims

    conflicts: list[KbConflict] = []
    if entity_ids:
        conflicts = list((await db.scalars(select(KbConflict).where(
            KbConflict.entity_id.in_(entity_ids),
            KbConflict.status.in_(("open", "disputed")),
        ))).all())

    # The conflict must reference exactly the current, source-backed 30/90 pair.
    policy_conflict_valid = any(
        a30.subject_entity_id == a90.subject_entity_id
        and a30.predicate == a90.predicate
        and a30.applies_condition == a90.applies_condition
        and a30.id in supported_claims and a90.id in supported_claims
        and c.conflict_kind == "value_contradiction"
        and c.entity_id == a30.subject_entity_id
        and c.predicate == a30.predicate
        and {c.assertion_a_id, c.assertion_b_id} == {a30.id, a90.id}
        for a30 in assertions_30 for a90 in assertions_90 for c in conflicts
    )

    # 각 잡의 단계별 상태 및 실 L2 호출 성공 증거 확인
    final_jobs = list(
        (
            await db.scalars(
                select(KbPipelineJob).where(KbPipelineJob.revision_id.in_(rev_ids))
            )
        ).all()
    )
    jobs_summary = []
    has_failed = False
    has_held = False
    has_degraded = False
    all_ready = (len(final_jobs) == 3)
    l2_calls_verified = not is_l2_disabled

    for j in final_jobs:
        if j.status != "ready":
            all_ready = False
        if j.status == "failed":
            has_failed = True
        elif j.status == "held":
            has_held = True
        elif j.status == "degraded":
            has_degraded = True

        stages = list(
            (
                await db.scalars(
                    select(KbPipelineStage).where(KbPipelineStage.job_id == j.id)
                )
            ).all()
        )
        stages_map: dict[str, str] = {}
        stage_details: dict[str, Any] = {}
        extracting_succeeded = False

        for st in stages:
            stages_map[st.stage] = st.status
            if st.status == "failed":
                has_failed = True
            elif st.status == "held":
                has_held = True
            elif st.status == "degraded":
                has_degraded = True

            if st.stage == STAGE_EXTRACTING and isinstance(st.detail, dict):
                l2_info = st.detail.get("l2", {})
                calls_made = l2_info.get("calls_made", 0)
                if (
                    st.status == "succeeded"
                    and l2_info.get("adapter") != "disabled"
                    and l2_info.get("available") is True
                    and calls_made > 0
                ):
                    extracting_succeeded = True

                stage_details["l2"] = {
                    "adapter": l2_info.get("adapter"),
                    "available": l2_info.get("available"),
                    "reason": l2_info.get("reason"),
                    "calls_made": calls_made,
                }

        if not is_l2_disabled and not extracting_succeeded:
            l2_calls_verified = False

        jobs_summary.append(
            {
                "job_id": str(j.id),
                "status": j.status,
                "stages": stages_map,
                "details": stage_details,
            }
        )

    # 6. 엄격한 성공 게이트 판정
    reason = None
    if not all_terminal:
        overall_status = "partial"
        reason = "timeout_waiting_terminal_jobs"
    elif has_failed:
        overall_status = "failed"
        reason = "pipeline_jobs_failed"
    elif has_held:
        overall_status = "held"
        reason = "pipeline_jobs_held"
    elif is_l2_disabled or has_degraded:
        overall_status = "degraded"
        reason = "l2_disabled_or_degraded"
    elif not all_ready or actual_doc_count < 3 or len(final_jobs) < 3:
        overall_status = "partial"
        reason = "jobs_or_docs_not_ready"
    elif not l2_calls_verified:
        overall_status = "degraded"
        reason = "l2_extraction_calls_unverified"
    elif not exact_quotes_valid:
        overall_status = "degraded"
        reason = "exact_quote_missing_or_unverified"
    elif not policy_conflict_valid:
        overall_status = "degraded"
        reason = "30d_vs_90d_policy_conflict_unverified"
    else:
        overall_status = "succeeded"

    res = {
        "status": overall_status,
        "l2_adapter": s.kb_l2_adapter,
        "l2_configured_model": s.kb_l2_subscription_model,
        "counts": {
            "documents": actual_doc_count,
            "passages": passages_count,
            "entities": len(entity_ids),
            "assertions": assertions_count,
            "evidence": evidence_count,
            "conflicts": len(conflicts),
        },
        "conflict_ids": [str(c.id) for c in conflicts],
        "jobs": jobs_summary,
    }
    if reason:
        res["reason"] = reason

    return res


def main(argv: list[str]) -> int:
    """러너 프로세스에서 호출되는 CLI 진입점: python -m ttakkari.operations.kb_v3_verify <job_id>"""
    import json
    import sys

    job_id = argv[1] if len(argv) > 1 else None

    async def _run_cli() -> int:
        from ttakkari.db import sessionmaker

        async with sessionmaker()() as db:
            result = await verify_kb_v3(db, job_id=job_id)
            # 민감정보가 배제된 최종 요약 JSON 출력
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return 0 if result.get("status") == "succeeded" else 1

    return asyncio.run(_run_cli())


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv))
