"""지식 도서관(KB v3) 온톨로지·엔터티·관계·주장·정본·충돌 모델.

원칙:
1. 불변 원장 보호: 원장(kb_documents, kb_revisions, kb_passages, kb_evidence)을 참조하는
   모든 외래키는 RESTRICT 로 결속되어 원장 불변성(immutability)을 훼손하지 않는다.
2. 근거 없는 관계 확정 차단: kb_relations 의 status 가 'active' 일 때는 반드시 1개 이상의
   검증된 근거(evidence_count >= 1)와 실제 kb_relation_evidence 행이 보장되어야 한다(DB CheckConstraint 및 진입점).
3. provenance/visibility 격리: 회사(work) 문서 증거로부터 생성된 엔터티·주장·관계·정본은
   origin_scope='work', visibility='work_only' 로 격리되어 global 공간에 누출되지 않는다.
   기본값은 fail-closed('work_only')이며, derive_derived_visibility 헬퍼를 통해 원천 문서 접근에서 유도한다.
4. 유니코드 정규화 보존: norm_form(NFC/NFKC)을 명시하여 파서 버전에 따른 정규화 계보를 유지한다.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from ttakkari.models import Base, utcnow


def derive_derived_visibility(sources: Sequence[Any]) -> str:
    """원천 문서의 접근 레코드나 visibility/scope 목록으로부터 파생 온톨로지 객체의 visibility를 결정.

    Fail-closed 원칙:
    1. 원천 중 단 하나라도 'work_only' 또는 scope/origin_scope == 'work'가 있으면 'work_only'.
    2. 원천 정보가 비어있거나 불분명하면 'work_only' (fail-closed 안전망).
    3. 모든 원천이 명시적으로 'global' (또는 scope == 'personal')일 때만 'global'.
    """
    if not sources:
        return "work_only"

    for item in sources:
        if item is None:
            return "work_only"
        if isinstance(item, str):
            if item in ("work_only", "work"):
                return "work_only"
        elif hasattr(item, "visibility") or hasattr(item, "scope") or hasattr(item, "origin_scope"):
            vis = getattr(item, "visibility", None)
            sc = getattr(item, "scope", None)
            orig = getattr(item, "origin_scope", None)
            if vis == "work_only" or sc == "work" or orig == "work":
                return "work_only"
        else:
            return "work_only"

    return "global"


class KbEntity(Base):
    """개념·엔터티 정본 노드. 4단계 결정적 해소와 가시성 격리를 지원한다."""

    __tablename__ = "kb_entities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "visibility",
            "entity_kind",
            "norm_name",
            name="uq_kb_entities_norm",
        ),
        CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_entities_visibility"
        ),
        CheckConstraint(
            "origin_scope IN ('personal', 'work')", name="ck_kb_entities_origin_scope"
        ),
        CheckConstraint(
            "(origin_scope != 'work') OR (visibility = 'work_only')",
            name="ck_kb_entities_scope_visibility",
        ),
        CheckConstraint(
            "status IN ('active', 'merged', 'archived')", name="ck_kb_entities_status"
        ),
        CheckConstraint(
            "entity_kind IN ('concept', 'technology', 'system', 'api', 'symbol', 'standard', 'organization', 'person', 'document_topic')",
            name="ck_kb_entities_kind",
        ),
        CheckConstraint(
            "domain IN ('dev', 'infra', 'ai', 'security', 'business', 'science', 'general')",
            name="ck_kb_entities_domain",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0", name="ck_kb_entities_confidence"
        ),
        CheckConstraint("mention_count >= 0", name="ck_kb_entities_mention_count"),
        CheckConstraint("document_count >= 0", name="ck_kb_entities_doc_count"),
        Index("ix_kb_entities_norm_name", "norm_name"),
        Index("ix_kb_entities_kind_domain", "entity_kind", "domain"),
        Index("ix_kb_entities_visibility", "visibility"),
        Index("ix_kb_entities_origin_scope", "origin_scope"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), default="personal")
    origin_scope: Mapped[str] = mapped_column(String(16), default="personal")
    visibility: Mapped[str] = mapped_column(String(16), default="work_only")
    canonical_name: Mapped[str] = mapped_column(String(200))
    norm_name: Mapped[str] = mapped_column(String(200))
    norm_form: Mapped[str] = mapped_column(String(16), default="NFKC")
    entity_kind: Mapped[str] = mapped_column(String(32), default="concept")
    domain: Mapped[str] = mapped_column(String(32), default="general")
    summary: Mapped[str] = mapped_column(Text, default="")
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    status: Mapped[str] = mapped_column(String(16), default="active")
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def __init__(self, **kwargs):
        if "visibility" not in kwargs:
            kwargs["visibility"] = "work_only"
        super().__init__(**kwargs)


class KbEntityAlias(Base):
    """한·영·약어 별칭 사전."""

    __tablename__ = "kb_entity_aliases"
    __table_args__ = (
        UniqueConstraint("entity_id", "norm_alias", name="uq_kb_entity_alias"),
        CheckConstraint(
            "alias_kind IN ('canonical', 'synonym', 'abbrev', 'translation')",
            name="ck_kb_entity_alias_kind",
        ),
        CheckConstraint(
            "source IN ('seed', 'extracted', 'user_verified')",
            name="ck_kb_entity_alias_source",
        ),
        Index("ix_kb_entity_aliases_norm", "norm_alias"),
        Index("ix_kb_entity_aliases_entity_id", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    norm_alias: Mapped[str] = mapped_column(String(200), nullable=False)
    norm_form: Mapped[str] = mapped_column(String(16), default="NFKC")
    lang: Mapped[str | None] = mapped_column(String(8), default=None)
    alias_kind: Mapped[str] = mapped_column(String(24), default="synonym")
    source: Mapped[str] = mapped_column(String(24), default="extracted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class KbDocumentEntity(Base):
    """문서 패시지와 엔터티 출현 연결 (역색인 및 그래프 브리지)."""

    __tablename__ = "kb_document_entities"
    __table_args__ = (
        CheckConstraint(
            "role IN ('primary_topic', 'defined', 'mentioned')",
            name="ck_kb_doc_entities_role",
        ),
        CheckConstraint(
            "salience >= 0.0 AND salience <= 1.0", name="ck_kb_doc_entities_salience"
        ),
        Index("ix_kb_doc_entities_entity", "entity_id", "document_id"),
        Index("ix_kb_doc_entities_doc", "document_id", "salience"),
    )

    passage_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_passages.id", ondelete="RESTRICT"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_evidence.id", ondelete="RESTRICT"), default=None
    )
    role: Mapped[str] = mapped_column(String(24), default="mentioned")
    salience: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class KbAssertion(Base):
    """구조화된 지식 주장 (Claim). 모든 주장은 근거 원장(kb_evidence)과 결속된다."""

    __tablename__ = "kb_assertions"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_assertions_visibility"
        ),
        CheckConstraint(
            "origin_scope IN ('personal', 'work')", name="ck_kb_assertions_origin_scope"
        ),
        CheckConstraint(
            "(origin_scope != 'work') OR (visibility = 'work_only')",
            name="ck_kb_assertions_scope_visibility",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'disputed', 'rejected')",
            name="ck_kb_assertions_status",
        ),
        CheckConstraint(
            "modality IN ('fact', 'recommendation', 'requirement', 'deprecated')",
            name="ck_kb_assertions_modality",
        ),
        CheckConstraint(
            "polarity IN ('positive', 'negative')", name="ck_kb_assertions_polarity"
        ),
        CheckConstraint(
            "trust_score >= 0.0 AND trust_score <= 1.0", name="ck_kb_assertions_trust"
        ),
        CheckConstraint(
            "extractor_tier IN ('L0', 'L1', 'L2', 'human')",
            name="ck_kb_assertions_extractor",
        ),
        Index(
            "ix_kb_assertions_subject_pred", "subject_entity_id", "predicate", "status"
        ),
        Index("ix_kb_assertions_object_entity", "object_entity_id"),
        Index("ix_kb_assertions_revision", "revision_id"),
        Index("ix_kb_assertions_visibility", "visibility"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    origin_scope: Mapped[str] = mapped_column(String(16), default="personal")
    visibility: Mapped[str] = mapped_column(String(16), default="work_only")
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(String(64), nullable=False)
    object_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), default=None
    )
    object_value: Mapped[str] = mapped_column(Text, nullable=False)
    norm_object_value: Mapped[str] = mapped_column(String(240), nullable=False)
    modality: Mapped[str] = mapped_column(String(16), default="fact")
    polarity: Mapped[str] = mapped_column(String(16), default="positive")
    applies_condition: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    trust_score: Mapped[float] = mapped_column(Float, default=0.7)
    freshness_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    status: Mapped[str] = mapped_column(String(16), default="active")
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False)
    extractor_tier: Mapped[str] = mapped_column(String(8), default="L0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    def __init__(self, **kwargs):
        if "visibility" not in kwargs:
            kwargs["visibility"] = "work_only"
        super().__init__(**kwargs)


class KbAssertionEvidence(Base):
    """주장과 근거 원장의 다대다 매핑. 원장 행 삭제는 RESTRICT 로 거부된다."""

    __tablename__ = "kb_assertion_evidence"
    __table_args__ = (
        CheckConstraint(
            "relation IN ('supports', 'refutes', 'qualifies')",
            name="ck_kb_assertion_evidence_rel",
        ),
    )

    assertion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_assertions.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_evidence.id", ondelete="RESTRICT"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(16), default="supports")


class KbRelationEvidence(Base):
    """관계(kb_relations)와 증거 원장(kb_evidence)의 결속. 원장 행 삭제는 RESTRICT 로 보호된다."""

    __tablename__ = "kb_relation_evidence"
    __table_args__ = (
        CheckConstraint(
            "relation IN ('supports', 'refutes', 'qualifies')",
            name="ck_kb_relation_evidence_rel",
        ),
        Index("ix_kb_relation_evidence_evidence_id", "evidence_id"),
    )

    relation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_relations.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_evidence.id", ondelete="RESTRICT"), primary_key=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    relation: Mapped[str] = mapped_column(String(16), default="supports")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class KbRelation(Base):
    """엔터티 간 명시적 관계 에지. 근거 없는 관계는 active 상태가 될 수 없다."""

    __tablename__ = "kb_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            name="uq_kb_relation_edge",
        ),
        CheckConstraint("evidence_count >= 0", name="ck_kb_relation_evidence_count"),
        CheckConstraint(
            "(status != 'active') OR (evidence_count >= 1)",
            name="ck_kb_relation_active_needs_evidence",
        ),
        CheckConstraint(
            "status IN ('candidate', 'active', 'disputed')",
            name="ck_kb_relation_status",
        ),
        CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_relation_visibility"
        ),
        CheckConstraint(
            "origin_scope IN ('personal', 'work')", name="ck_kb_relation_origin_scope"
        ),
        CheckConstraint(
            "(origin_scope != 'work') OR (visibility = 'work_only')",
            name="ck_kb_relation_scope_visibility",
        ),
        CheckConstraint(
            "relation_type IN ('part_of', 'manages', 'depends_on', 'uses', 'implements', 'alternative_to', 'related_to')",
            name="ck_kb_relation_type",
        ),
        Index("ix_kb_relations_source", "source_entity_id", "weight"),
        Index("ix_kb_relations_target", "target_entity_id", "weight"),
        Index("ix_kb_relations_visibility", "visibility"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    origin_scope: Mapped[str] = mapped_column(String(16), default="personal")
    visibility: Mapped[str] = mapped_column(String(16), default="work_only")
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), nullable=False
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(48), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    representative_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_evidence.id", ondelete="RESTRICT"), default=None
    )
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def __init__(self, **kwargs):
        if "evidence_count" not in kwargs:
            kwargs["evidence_count"] = 0
        if "status" not in kwargs:
            kwargs["status"] = "candidate"
        if "visibility" not in kwargs:
            kwargs["visibility"] = "work_only"
        if "weight" not in kwargs:
            kwargs["weight"] = 1.0
        if "origin_scope" not in kwargs:
            kwargs["origin_scope"] = "personal"
        super().__init__(**kwargs)


class KbEntityCanonical(Base):
    """개념별 다중 문서 통합 정본 카드."""

    __tablename__ = "kb_entity_canonicals"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('global', 'work_only')",
            name="ck_kb_entity_canonicals_visibility",
        ),
        CheckConstraint("version_no > 0", name="ck_kb_entity_canonicals_version"),
        Index("ix_kb_entity_canonicals_visibility", "visibility"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), primary_key=True
    )
    visibility: Mapped[str] = mapped_column(String(16), default="work_only")
    canonical_definition: Mapped[str] = mapped_column(Text, nullable=False)
    key_facts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    source_document_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    has_open_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def __init__(self, **kwargs):
        if "visibility" not in kwargs:
            kwargs["visibility"] = "work_only"
        super().__init__(**kwargs)


class KbConflict(Base):
    """지식 상충(상반 주장/버전 분기) 및 엔터티 모호성 관리."""

    __tablename__ = "kb_conflicts"
    __table_args__ = (
        CheckConstraint(
            "conflict_kind IN ('value_contradiction', 'polarity_contradiction', 'version_divergence', 'ambiguous_entity')",
            name="ck_kb_conflicts_kind",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved_a', 'resolved_b', 'resolved_both_valid', 'merged', 'kept_separate')",
            name="ck_kb_conflicts_status",
        ),
        CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_conflicts_visibility"
        ),
        Index("ix_kb_conflicts_status", "status", "conflict_kind", "created_at"),
        Index("ix_kb_conflicts_entity_id", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    visibility: Mapped[str] = mapped_column(String(16), default="work_only")
    conflict_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_entities.id", ondelete="RESTRICT"), default=None
    )
    predicate: Mapped[str | None] = mapped_column(String(64), default=None)
    assertion_a_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_assertions.id", ondelete="RESTRICT"), default=None
    )
    assertion_b_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_assertions.id", ondelete="RESTRICT"), default=None
    )
    status: Mapped[str] = mapped_column(String(32), default="open")
    resolution_note: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    def __init__(self, **kwargs):
        if "visibility" not in kwargs:
            kwargs["visibility"] = "work_only"
        super().__init__(**kwargs)


async def activate_relation_with_evidence(
    session: AsyncSession,
    relation: KbRelation,
    evidence_id: uuid.UUID,
    *,
    confidence: float = 1.0,
    relation_kind: str = "supports",
) -> KbRelationEvidence:
    """실제 kb_relation_evidence 행을 추가하고, evidence_count를 동기화하여 관계를 active 상태로 안전하게 전환하는 서비스 계층 단일 진입점.

    근거 없는 active 전환을 방지한다.
    """
    from ttakkari.kb_models import KbEvidence

    ev = await session.get(KbEvidence, evidence_id)
    if ev is None:
        raise ValueError(
            f"Cannot activate relation {relation.id}: evidence {evidence_id} not found in kb_evidence"
        )

    rel_ev = KbRelationEvidence(
        relation_id=relation.id,
        evidence_id=evidence_id,
        confidence=confidence,
        relation=relation_kind,
    )
    session.add(rel_ev)
    await session.flush()

    count_res = await session.scalar(
        select(func.count())
        .select_from(KbRelationEvidence)
        .where(KbRelationEvidence.relation_id == relation.id)
    )
    relation.evidence_count = count_res or 1
    relation.representative_evidence_id = evidence_id
    relation.status = "active"
    return rel_ev


def activate_relation_with_evidence_sync(
    session: Session,
    relation: KbRelation,
    evidence_id: uuid.UUID,
    *,
    confidence: float = 1.0,
    relation_kind: str = "supports",
) -> KbRelationEvidence:
    """동기 세션용 관계 근거 결속 및 active 전환 서비스 계층 단일 진입점."""
    from ttakkari.kb_models import KbEvidence

    ev = session.get(KbEvidence, evidence_id)
    if ev is None:
        raise ValueError(
            f"Cannot activate relation {relation.id}: evidence {evidence_id} not found in kb_evidence"
        )

    rel_ev = KbRelationEvidence(
        relation_id=relation.id,
        evidence_id=evidence_id,
        confidence=confidence,
        relation=relation_kind,
    )
    session.add(rel_ev)
    session.flush()

    count_res = session.scalar(
        select(func.count())
        .select_from(KbRelationEvidence)
        .where(KbRelationEvidence.relation_id == relation.id)
    )
    relation.evidence_count = count_res or 1
    relation.representative_evidence_id = evidence_id
    relation.status = "active"
    return rel_ev
