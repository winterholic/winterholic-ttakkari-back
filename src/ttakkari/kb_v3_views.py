"""KB v3 사용자 API 의 문서·요약·검색 뷰(KB3-402).

응답 모양은 프론트 계약(front-kb-v3 KB_V3_CONTRACT_NOTES.md, kbTypes.ts)에 맞춘다. 기존 응답 필드는 그대로 두고 v3 필드만 더한다.
모든 조회는 호출자가 넘긴 KbAccessContext 하나로 문서·엔터티·주장·관계를 거른다(kb_access 단일 규칙).
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ttakkari import kb_library as lib
from ttakkari import kb_retrieval
from ttakkari.collector.control_models import KbObservation
from ttakkari.kb_access import (
    KbAccessContext,
    assertion_access_filter,
    entity_access_filter,
    entity_alias_access_filter,
    passage_access_filter,
    relation_access_filter,
)
from ttakkari.kb_domains import entity_domains_for_topic, topic_for_entity_domains
from ttakkari.kb_jobs import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    STATUS_GROUPS,
    job_view,
    revision_metrics,
)
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbEntityCanonical,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage

SINGLE_PASSAGE_CHARS = 1200  # 설계 §2.2 max_chars: 이보다 긴 문서가 패시지 하나면 통짜 적재다
SUMMARY_CHARS = 320


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# --------------------------------------------------------------------------------------------
# 근거 인용
# --------------------------------------------------------------------------------------------
async def evidence_refs(
    db: AsyncSession, evidence_ids: list[uuid.UUID], access: KbAccessContext,
) -> dict[uuid.UUID, dict[str, Any]]:
    """근거 id -> 원문 인용 참조. 접근 가능한 패시지의 근거만 돌려준다(없는 id 는 빠진다)."""
    if not evidence_ids:
        return {}
    rows = await db.execute(
        select(KbEvidence, KbPassage, KbRevision, KbDocument)
        .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .join(KbDocument, KbDocument.id == KbRevision.document_id)
        .where(KbEvidence.id.in_(evidence_ids), passage_access_filter(access, KbPassage))
    )
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for ev, passage, rev, doc in rows.all():
        out[ev.id] = {
            "evidence_id": str(ev.id),
            "passage_id": str(passage.id),
            "quote": passage.body[ev.span_start:ev.span_end],
            "start_offset": ev.span_start,
            "end_offset": ev.span_end,
            "document_id": str(doc.id),
            "document_title": doc.title,
            "revision_id": str(rev.id),
            "rev_no": rev.rev_no,
            "page_no": passage.page_no,
            "slide_no": passage.slide_no,
            "section_path": list(passage.section_path or []),
        }
    return out


async def assertion_views(db: AsyncSession, assertions: list[KbAssertion], access: KbAccessContext) -> list[dict[str, Any]]:
    if not assertions:
        return []
    subjects = {
        e.id: e.canonical_name
        for e in await db.scalars(select(KbEntity).where(KbEntity.id.in_({a.subject_entity_id for a in assertions})))
    }
    pairs = (await db.execute(
        select(KbAssertionEvidence.assertion_id, KbAssertionEvidence.evidence_id, KbAssertionEvidence.relation).where(
            KbAssertionEvidence.assertion_id.in_([a.id for a in assertions])
        )
    )).all()
    refs = await evidence_refs(db, list({ev for _, ev, _ in pairs}), access)
    by_assertion: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for a_id, ev_id, relation in pairs:
        if ev_id in refs:
            by_assertion.setdefault(a_id, []).append({**refs[ev_id], "relation": relation})
    return [
        {
            "id": str(a.id),
            "text": f"{subjects.get(a.subject_entity_id, '?')} — {a.predicate}: {a.object_value}",
            "subject": subjects.get(a.subject_entity_id),
            "predicate": a.predicate,
            "object": a.object_value,
            "status": a.status,
            "polarity": a.polarity,
            "modality": a.modality,
            "is_canonical": a.is_canonical,
            "extractor_tier": a.extractor_tier,
            "entity_id": str(a.subject_entity_id),
            "applies_condition": a.applies_condition or {},
            "evidence": by_assertion.get(a.id, []),
        }
        for a in assertions
    ]


# --------------------------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------------------------
def _job_scope(access: KbAccessContext):
    return select(KbDocument.id).where(KbDocument.id == KbPipelineJob.document_id, lib.scope_filter("personal", access)).exists()


async def summary_v3(db: AsyncSession, access: KbAccessContext) -> dict[str, Any]:
    """통합 규모·처리 현황·v3 지표. 접근 가능한 문서와 그 파생물만 센다."""
    base = await lib.summary(db, access=access)
    counts: dict[str, Any] = dict(base["counts"])

    counts["entities"] = await db.scalar(
        select(func.count()).select_from(KbEntity).where(KbEntity.status == "active", entity_access_filter(access, KbEntity))
    ) or 0
    counts["assertions"] = await db.scalar(
        select(func.count()).select_from(KbAssertion).where(
            KbAssertion.status.in_(("active", "disputed")), assertion_access_filter(access, KbAssertion)
        )
    ) or 0
    counts["relations"] = await db.scalar(
        select(func.count()).select_from(KbRelation).where(relation_access_filter(access, KbRelation))
    ) or 0
    candidate_relations = await db.scalar(
        select(func.count()).select_from(KbRelation).where(
            relation_access_filter(access, KbRelation, include_candidates=True), KbRelation.status == "candidate"
        )
    ) or 0

    from ttakkari.kb_ontology import list_conflicts_with_total

    _, open_conflicts = await list_conflicts_with_total(db, access=access, status="open", offset=0, limit=1)
    counts["open_conflicts"] = open_conflicts

    status_rows = (await db.execute(
        select(KbPipelineJob.status, func.count()).where(_job_scope(access)).group_by(KbPipelineJob.status)
    )).all()
    by_status = {status: n for status, n in status_rows}
    counts["pipeline_running"] = sum(by_status.get(s, 0) for s in ACTIVE_STATUSES)
    counts["pipeline_failed"] = by_status.get("failed", 0)

    # v3 지표: 통짜 패시지 문서, 처리 이력이 없는 문서(백필 대상), 임베딩 적용 범위
    revisions = [
        (rev.id, rev.char_count)
        for rev in [await lib._latest_revision(db, d_id) for d_id in await db.scalars(
            select(KbDocument.id).where(lib.scope_filter("personal", access)))]
        if rev is not None
    ]
    rev_ids = [r for r, _ in revisions]
    passage_counts: dict[uuid.UUID, int] = {}
    job_revs: set[uuid.UUID] = set()
    if rev_ids:
        passage_counts = dict((await db.execute(
            select(KbPassage.revision_id, func.count()).where(KbPassage.revision_id.in_(rev_ids)).group_by(KbPassage.revision_id)
        )).all())
        job_revs = set(await db.scalars(select(KbPipelineJob.revision_id).where(KbPipelineJob.revision_id.in_(rev_ids))))
    single = sum(1 for r, chars in revisions if passage_counts.get(r, 0) == 1 and chars > SINGLE_PASSAGE_CHARS)
    without_job = sum(1 for r, _ in revisions if r not in job_revs)
    retrieval = await kb_retrieval.status(db, access_context=access)
    indexed, total = retrieval["indexed"], retrieval["total"]

    return {
        "counts": counts,
        "last_collected_at": base["last_collected_at"],
        "retrieval": retrieval,
        "pipeline": {
            "registered": by_status.get("registered", 0),
            "running": counts["pipeline_running"],
            "ready": by_status.get("ready", 0),
            "degraded": by_status.get("degraded", 0),
            "held": by_status.get("held", 0),
            "failed": by_status.get("failed", 0),
            "by_status": by_status,
        },
        "v3": {
            "relations_active": counts["relations"],
            "relations_candidate": candidate_relations,
            "single_passage_documents": single,
            "documents_without_pipeline_job": without_job,
            "embedding_coverage": {"indexed": indexed, "total": total, "ratio": round(indexed / total, 4) if total else None},
        },
        # 한 공간으로 응답했다는 사실과 이 호출자가 볼 수 있는 가시성 범위(개인/회사 선택 파라미터 없음)
        "access": {"view": "unified", "visibilities": list(access.allowed_visibilities), "caller_role": access.caller_role},
    }


# --------------------------------------------------------------------------------------------
# 문서 목록
# --------------------------------------------------------------------------------------------
def _current_job_exists(statuses: tuple[str, ...] | None):
    cond = [KbPipelineJob.document_id == KbDocument.id, KbPipelineJob.revision_id == KbRevision.id, lib._current_revision_filter()]
    if statuses is not None:
        cond.append(KbPipelineJob.status.in_(statuses))
    return select(KbPipelineJob.id).join(KbRevision, KbRevision.id == KbPipelineJob.revision_id).where(*cond).correlate(KbDocument).exists()


def _domain_filter(domain: str, access: KbAccessContext):
    explicit = select(KbRevision.id).join(KbSource, KbSource.id == KbRevision.source_id).where(
        KbRevision.document_id == KbDocument.id, KbSource.source_meta["domain"].astext == domain
    ).correlate(KbDocument).exists()
    # entity_access_filter 가 안쪽에서 KbDocumentEntity 를 쓰므로 바깥 쿼리는 별칭을 써서 자동 상관(auto-correlation)을 피한다.
    link, ent = aliased(KbDocumentEntity), aliased(KbEntity)
    derived = (
        select(link.entity_id)
        .join(ent, ent.id == link.entity_id)
        .where(
            link.document_id == KbDocument.id,
            ent.domain.in_(entity_domains_for_topic(domain)),
            entity_access_filter(access, ent),
        )
        .correlate(KbDocument)
        .exists()
    )
    return or_(explicit, derived)


async def _doc_extras(
    db: AsyncSession, access: KbAccessContext, rows: list[dict[str, Any]],
) -> None:
    """목록 행에 v3 필드(domain·source_kind·pipeline_status·entity_count·assertion_count)를 한꺼번에 붙인다."""
    rev_by_doc = {r["id"]: r["current_revision"]["id"] for r in rows if r.get("current_revision")}
    rev_ids = [uuid.UUID(v) for v in rev_by_doc.values()]
    sources: dict[uuid.UUID, tuple[str, dict]] = {}
    jobs: dict[uuid.UUID, KbPipelineJob] = {}
    entity_counts: dict[uuid.UUID, int] = {}
    assertion_counts: dict[uuid.UUID, int] = {}
    domain_counts: dict[uuid.UUID, Counter] = {}
    if rev_ids:
        for rev_id, kind, meta in (await db.execute(
            select(KbRevision.id, KbSource.kind, KbSource.source_meta).join(KbSource, KbSource.id == KbRevision.source_id)
            .where(KbRevision.id.in_(rev_ids))
        )).all():
            sources[rev_id] = (kind, meta or {})
        jobs = {j.revision_id: j for j in await db.scalars(select(KbPipelineJob).where(KbPipelineJob.revision_id.in_(rev_ids)))}
        link, ent = aliased(KbDocumentEntity), aliased(KbEntity)
        for rev_id, domain, n in (await db.execute(
            select(link.revision_id, ent.domain, func.count(func.distinct(ent.id)))
            .join(ent, ent.id == link.entity_id)
            .where(link.revision_id.in_(rev_ids), entity_access_filter(access, ent))
            .group_by(link.revision_id, ent.domain)
        )).all():
            entity_counts[rev_id] = entity_counts.get(rev_id, 0) + n
            domain_counts.setdefault(rev_id, Counter())[domain] += n
        assertion_counts = dict((await db.execute(
            select(KbAssertion.revision_id, func.count()).where(
                KbAssertion.revision_id.in_(rev_ids), KbAssertion.status.in_(("active", "disputed")),
                assertion_access_filter(access, KbAssertion),
            ).group_by(KbAssertion.revision_id)
        )).all())
    for row in rows:
        rev_id = uuid.UUID(rev_by_doc[row["id"]]) if row["id"] in rev_by_doc else None
        kind, meta = sources.get(rev_id, (None, {}))
        job = jobs.get(rev_id) if rev_id else None
        row["domain"] = meta.get("domain") or topic_for_entity_domains(domain_counts.get(rev_id, {}))
        row["domain_source"] = "user" if meta.get("domain") else ("entities" if row["domain"] else None)
        row["source_kind"] = kind
        row["pipeline_status"] = job.status if job else None
        row["pipeline_job_id"] = str(job.id) if job else None
        row["hold_reason"] = job.hold_reason if job else None
        row["entity_count"] = entity_counts.get(rev_id, 0) if rev_id else 0
        row["assertion_count"] = assertion_counts.get(rev_id, 0) if rev_id else 0


async def list_documents_v3(
    db: AsyncSession, access: KbAccessContext, *, q: str | None, domain: str | None, pipeline_status: str | None,
    limit: int, offset: int,
) -> dict[str, Any]:
    stmt = select(KbDocument).where(lib.scope_filter("personal", access))
    if q and q.strip():
        pattern = q.strip()
        stmt = stmt.where(
            KbDocument.title.icontains(pattern, autoescape=True) | KbDocument.doc_key.icontains(pattern, autoescape=True)
        )
    if domain:
        stmt = stmt.where(_domain_filter(domain, access))
    if pipeline_status:
        if pipeline_status == "none":
            stmt = stmt.where(~_current_job_exists(None))
        elif pipeline_status in STATUS_GROUPS:
            stmt = stmt.where(_current_job_exists(STATUS_GROUPS[pipeline_status]))
        elif pipeline_status in ALL_STATUSES:
            stmt = stmt.where(_current_job_exists((pipeline_status,)))
        else:
            raise ValueError("kb_pipeline_status_invalid")
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    docs = list(await db.scalars(stmt.order_by(KbDocument.updated_at.desc(), KbDocument.id).offset(offset).limit(limit)))
    items = [await lib._document_row(db, doc) for doc in docs]
    await _doc_extras(db, access, items)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# --------------------------------------------------------------------------------------------
# 문서 상세
# --------------------------------------------------------------------------------------------
async def _published_at(db: AsyncSession, document_id: uuid.UUID, revision_id: uuid.UUID, source_id: uuid.UUID) -> str | None:
    observed = await db.scalar(
        select(KbObservation.event_time).where(KbObservation.document_id == document_id, KbObservation.revision_id == revision_id)
        .order_by(KbObservation.event_time.desc()).limit(1)
    )
    if observed is not None:
        return observed.isoformat()
    collected = await db.scalar(select(KbSource.collected_at).where(KbSource.id == source_id))
    return collected.isoformat() if collected else None


def _extractive_summary(canonical_definition: str | None, passages: list[KbPassage]) -> tuple[str | None, str | None]:
    """요약. 모델이 쓴 문장이 아니다: 정본 정의가 있으면 그것, 없으면 첫 본문 문단의 앞부분(extractive)이다."""
    if canonical_definition:
        return canonical_definition[:SUMMARY_CHARS], "canonical_definition"
    for passage in passages:
        if passage.chunk_kind not in ("prose", "list"):
            continue
        text = " ".join(line.strip() for line in passage.body.splitlines() if line.strip() and not line.lstrip().startswith("#"))
        if len(text) >= 20:
            return (text[:SUMMARY_CHARS] + ("…" if len(text) > SUMMARY_CHARS else "")), "extractive_first_passage"
    return None, None


async def document_detail_v3(
    db: AsyncSession, access: KbAccessContext, document_id: uuid.UUID, *, revision_id: uuid.UUID | None,
    passage_limit: int, passage_offset: int,
) -> dict[str, Any]:
    base = await lib.get_document(
        db, document_id, revision_id=revision_id, passage_limit=passage_limit, passage_offset=passage_offset, access=access
    )
    selected = uuid.UUID(base["selected_revision_id"])
    sel_rev = await db.get(KbRevision, selected)
    row = await lib._document_row(db, await db.get(KbDocument, document_id))
    await _doc_extras(db, access, [row])

    # 엔터티(이 리비전에 출현한 접근 가능 엔터티)
    link = aliased(KbDocumentEntity)
    ent_rows = (await db.execute(
        select(KbEntity, func.max(link.salience), func.count(link.passage_id), func.bool_or(link.role == "defined"))
        .join(link, link.entity_id == KbEntity.id)
        .where(link.revision_id == selected, entity_access_filter(access, KbEntity), KbEntity.status == "active")
        .group_by(KbEntity.id)
        .order_by(func.max(link.salience).desc(), func.count(link.passage_id).desc(), KbEntity.canonical_name)
    )).all()
    entities = [
        {"id": str(e.id), "name": e.canonical_name, "entity_kind": e.entity_kind, "domain": e.domain,
         "salience": round(float(sal), 3), "mention_count": int(n), "defined_here": bool(defined)}
        for e, sal, n, defined in ent_rows
    ]

    # 주장
    assertions = list(await db.scalars(
        select(KbAssertion).where(
            KbAssertion.revision_id == selected, KbAssertion.status.in_(("active", "disputed", "superseded")),
            assertion_access_filter(access, KbAssertion),
        ).order_by(KbAssertion.is_canonical.desc(), KbAssertion.created_at, KbAssertion.id)
    ))
    assertion_out = await assertion_views(db, assertions, access)

    # 관계(이 리비전의 근거가 받치는 관계). 확정과 후보를 구분해 같이 보여 준다.
    rel_rows = (await db.execute(
        select(KbRelation, KbRelationEvidence.evidence_id)
        .join(KbRelationEvidence, KbRelationEvidence.relation_id == KbRelation.id)
        .join(KbEvidence, KbEvidence.id == KbRelationEvidence.evidence_id)
        .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
        .where(KbPassage.revision_id == selected, relation_access_filter(access, KbRelation, include_candidates=True))
        .order_by(KbRelation.status, KbRelation.created_at, KbRelation.id)
    )).all()
    relations: dict[uuid.UUID, dict[str, Any]] = {}
    ev_ids = list({ev for _, ev in rel_rows})
    refs = await evidence_refs(db, ev_ids, access)
    names = {
        e.id: e for e in await db.scalars(
            select(KbEntity).where(KbEntity.id.in_({x for r, _ in rel_rows for x in (r.source_entity_id, r.target_entity_id)}))
        )
    }
    for rel, ev_id in rel_rows:
        src, tgt = names.get(rel.source_entity_id), names.get(rel.target_entity_id)
        if src is None or tgt is None:
            continue
        item = relations.setdefault(rel.id, {
            "id": str(rel.id), "predicate": rel.relation_type, "relation_type": rel.relation_type,
            "source": {"id": str(src.id), "name": src.canonical_name},
            "target": {"id": str(tgt.id), "name": tgt.canonical_name},
            "status": rel.status, "candidate": rel.status != "active", "evidence_count": rel.evidence_count, "evidence": [],
        })
        if ev_id in refs:
            item["evidence"].append(refs[ev_id])

    # 충돌(이 리비전의 주장이 걸린 충돌)
    assertion_ids = [a.id for a in assertions]
    conflicts: list[dict[str, Any]] = []
    if assertion_ids:
        from ttakkari.kb_ontology import list_conflicts_with_total

        wanted = {str(i) for i in assertion_ids}
        listed, _ = await list_conflicts_with_total(db, access=access, status="all", offset=0, limit=100)
        for c in listed:
            if {str(c.assertion_a_id), str(c.assertion_b_id)} & wanted:
                ent = await db.get(KbEntity, c.entity_id)
                conflicts.append({"id": str(c.id), "kind": c.conflict_kind, "status": c.status, "entity_name": ent.canonical_name if ent else None})

    # 처리 이력(리비전별 작업)과 현재 리비전 작업의 단계 상태
    jobs = list(await db.scalars(
        select(KbPipelineJob).where(KbPipelineJob.document_id == document_id).order_by(KbPipelineJob.created_at.desc())
    ))
    rev_nos = {r["id"]: r["rev_no"] for r in base["revisions"]}
    stage_rows: dict[uuid.UUID, list[KbPipelineStage]] = {}
    if jobs:
        for st in await db.scalars(select(KbPipelineStage).where(KbPipelineStage.job_id.in_([j.id for j in jobs]))):
            stage_rows.setdefault(st.job_id, []).append(st)
    computed = await revision_metrics(db, access, [j.revision_id for j in jobs])
    views = {
        j.id: job_view(j, stage_rows.get(j.id, []), title=base["title"], rev_no=rev_nos.get(str(j.revision_id)),
                       computed=computed.get(j.revision_id))
        for j in jobs
    }
    pipeline = next((views[j.id] for j in jobs if j.revision_id == selected), None)
    history = [
        {"job_id": str(j.id), "revision_id": str(j.revision_id), "rev_no": rev_nos.get(str(j.revision_id)), "status": j.status,
         "current_stage": j.current_stage, "hold_reason": j.hold_reason, "metrics": views[j.id]["metrics"],
         "created_at": _iso(j.created_at), "completed_at": _iso(j.completed_at)}
        for j in jobs
    ]

    # 요약
    top_entity = ent_rows[0][0] if ent_rows else None
    canonical_definition = None
    if top_entity is not None:
        canonical_definition = await db.scalar(
            select(KbEntityCanonical.canonical_definition).where(
                KbEntityCanonical.entity_id == top_entity.id, KbEntityCanonical.visibility.in_(access.allowed_visibilities)
            )
        )
    first_passages = list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == selected).order_by(KbPassage.ord).limit(8)))
    summary_text, summary_source = _extractive_summary(canonical_definition, first_passages)

    # 버전별 처리 상태와 게시/수집 시각
    for rev in base["revisions"]:
        job = next((j for j in jobs if str(j.revision_id) == rev["id"]), None)
        rev["pipeline_status"] = job.status if job else None
        rev["published_at"] = await _published_at(db, document_id, uuid.UUID(rev["id"]), sel_rev.source_id) if rev["id"] == str(selected) else None

    # 패시지 v3 필드는 lib 가 이미 붙였다(section_path 등). 선택 리비전의 소스 링크도 같이 둔다.
    selected_rev = next(r for r in base["revisions"] if r["id"] == str(selected))
    return {
        **base,
        "domain": row["domain"],
        "domain_source": row["domain_source"],
        "source_kind": row["source_kind"],
        "pipeline_status": row["pipeline_status"],
        "summary": summary_text,
        "summary_source": summary_source,
        "source": selected_rev.get("source"),
        "entities": entities,
        "assertions": assertion_out,
        "relations": list(relations.values()),
        "conflicts": conflicts,
        "pipeline": pipeline,
        "history": history,
    }


# --------------------------------------------------------------------------------------------
# 통합 검색(프론트 계약 모양으로 변환)
# --------------------------------------------------------------------------------------------
def _fact_strings(key_facts: list[Any]) -> list[str]:
    out = []
    for fact in key_facts or []:
        if isinstance(fact, dict):
            out.append(f"{fact.get('predicate', '')}: {fact.get('value', '')}".strip(": "))
        else:
            out.append(str(fact))
    return out


async def _card_v3(db: AsyncSession, access: KbAccessContext, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """검색의 정본 카드(기존 키 유지)에 프론트 계약 키(id·name·aliases·canonical·neighbors 등)를 더한다."""
    if not cards:
        return []
    ids = [uuid.UUID(c["entity_id"]) for c in cards]
    aliases: dict[uuid.UUID, list[str]] = {}
    for ent_id, alias in (await db.execute(
        select(KbEntityAlias.entity_id, KbEntityAlias.alias)
        .join(KbEntity, KbEntity.id == KbEntityAlias.entity_id)
        .where(KbEntityAlias.entity_id.in_(ids), entity_alias_access_filter(access, KbEntity, KbEntityAlias))
        .order_by(KbEntityAlias.alias)
    )).all():
        aliases.setdefault(ent_id, []).append(alias)
    entities = {e.id: e for e in await db.scalars(select(KbEntity).where(KbEntity.id.in_(ids)))}
    conflict_counts = dict((await db.execute(
        select(KbConflict.entity_id, func.count()).where(
            KbConflict.entity_id.in_(ids), KbConflict.status == "open", KbConflict.visibility.in_(access.allowed_visibilities)
        ).group_by(KbConflict.entity_id)
    )).all())
    out = []
    for card in cards:
        ent_id = uuid.UUID(card["entity_id"])
        ent = entities.get(ent_id)
        neighbors = []
        for rel in card.get("relations") or []:
            outgoing = rel["source_entity"]["id"] == card["entity_id"]
            other = rel["target_entity"] if outgoing else rel["source_entity"]
            neighbors.append({
                "entity_id": other["id"], "name": other["canonical_name"], "predicate": rel["relation_type"],
                "direction": "out" if outgoing else "in", "status": rel.get("status", "active"), "candidate": bool(rel.get("candidate")),
            })
        out.append({
            **card,
            "id": card["entity_id"],
            "name": card["canonical_name"],
            "aliases": aliases.get(ent_id, []),
            # 근거 있는 정본이 아직 없는 개념은 canonical 을 null 로 둔다(프론트는 canonical?: null 을 처리한다).
            "canonical": None if card.get("has_canonical") is False else {
                "summary": card.get("canonical_definition") or "", "key_facts": _fact_strings(card.get("key_facts")), "updated_at": None,
            },
            "neighbors": neighbors,
            "open_conflict_count": conflict_counts.get(ent_id, 1 if card.get("has_open_conflict") else 0),
            "document_count": ent.document_count if ent else None,
        })
    return out


async def search_v3(
    db: AsyncSession, access: KbAccessContext, *, query: str, mode: str, limit: int, offset: int,
    document_id: uuid.UUID | None, current_only: bool, domain: str | None, entity_id: uuid.UUID | None,
    include_candidates: bool,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if domain:
        filters["domain"] = domain
    if entity_id:
        filters["entity_id"] = str(entity_id)
    result = await kb_retrieval.search(
        db, query=query, limit=limit, offset=offset, document_id=document_id, current_only=current_only,
        access_context=access, filters=filters or None, mode=mode, include_candidates=include_candidates,
        include_uncanonical_entities=True,
    )
    result["matched_entities"] = await _card_v3(db, access, result.get("matched_entities") or [])
    for item in result["items"]:
        item["section_path"] = list(item.get("section_path") or [])
        meta = item.get("locator_meta") or {}
        item.setdefault("page_no", meta.get("page_no"))
        item.setdefault("slide_no", meta.get("slide_no"))
        item.setdefault("time_start_ms", meta.get("time_start_ms"))
    return result


__all__ = ["assertion_views", "document_detail_v3", "evidence_refs", "list_documents_v3", "search_v3", "summary_v3"]
