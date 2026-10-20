"""범용 지식 도서관(kb) 원본·문서·리비전·패시지·근거 테이블. 개인 맥락 도서관과 분리된다(ADR 0063).

불변 규칙: kb_sources·kb_revisions·kb_passages·kb_evidence 행은 UPDATE 와 DELETE 를 거부한다.
외래키 제약조건은 RESTRICT 로 결속되어 부모 삭제로 인한 연쇄 삭제(CASCADE)가 방지된다.
정규화 및 오프셋 기준: 유니코드 NFKC, LF 줄바꿈('\n'), 0-based Unicode 코드포인트(char) 오프셋 기준.
리비전의 현재성(active)은 가변 포인터가 아니라 문서별 최대 rev_no 로 정한다.
kb_evidence 인용 구간은 INSERT 때 kb_passages.body 와 sha256 을 대조한다.
아래 트리거 DDL 은 Alembic 0055 와 동기화한다(create_all 로 만드는 테스트 DB 용).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class KbSource(Base):
    """수집한 원본 바이트의 정체. (source_key, sha256)마다 한 행. 파일은 해시 저장소에 둔다."""

    __tablename__ = "kb_sources"
    __table_args__ = (
        UniqueConstraint("source_key", "sha256", name="uq_kb_source_bytes"),
        CheckConstraint("byte_size >= 0", name="ck_kb_source_bytes"),
        Index("ix_kb_sources_source_key", "source_key"),
        Index("ix_kb_sources_sha256", "sha256"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_key: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(40), default="file")
    locator: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str | None] = mapped_column(String(120))
    store_path: Mapped[str] = mapped_column(String(500))
    collected_at: Mapped[datetime] = mapped_column(default=utcnow)
    origin_meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KbDocument(Base):
    """정규화 문서의 논리 묶음. 같은 doc_key(수집 계보)의 리비전을 모은다. 가변 껍데기다."""

    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("doc_key", name="uq_kb_documents_doc_key"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    doc_key: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    lang: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class KbRevision(Base):
    """문서 정규화 텍스트의 버전. 새 수집·새 파서는 새 행이다. valid_* 는 유효 시간, created_at 은 기록 시간이다."""

    __tablename__ = "kb_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "rev_no", name="uq_kb_revision_no"),
        CheckConstraint("rev_no > 0", name="ck_kb_revision_no"),
        CheckConstraint("char_count >= 0", name="ck_kb_revision_chars"),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="ck_kb_revision_valid_time",
        ),
        Index("ix_kb_revisions_document_id", "document_id"),
        Index("ix_kb_revisions_source_id", "source_id"),
        Index("ix_kb_revisions_supersedes_id", "supersedes_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="RESTRICT"))
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_sources.id", ondelete="RESTRICT"))
    rev_no: Mapped[int] = mapped_column(Integer)
    parser_version: Mapped[str] = mapped_column(String(60), default="raw-v1")
    body: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    char_count: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[datetime | None]
    valid_to: Mapped[datetime | None]
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kb_revisions.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KbPassage(Base):
    """리비전 본문의 검색·인용 단위 구간. char_* 는 리비전 본문 기준 오프셋이다."""

    __tablename__ = "kb_passages"
    __table_args__ = (
        UniqueConstraint("revision_id", "ord", name="uq_kb_passage_ord"),
        CheckConstraint("ord >= 0", name="ck_kb_passage_ord"),
        CheckConstraint(
            "char_start >= 0 AND char_end > char_start", name="ck_kb_passage_span"
        ),
        Index("ix_kb_passages_revision_id", "revision_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_revisions.id", ondelete="RESTRICT"))
    ord: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KbEvidence(Base):
    """패시지 본문에 대한 인용(span_start 포함·span_end 제외 문자 오프셋). 트리거가 구간 해시를 검증한다."""

    __tablename__ = "kb_evidence"
    __table_args__ = (
        UniqueConstraint("passage_id", "span_start", "span_end", name="uq_kb_evidence_span"),
        CheckConstraint(
            "span_start >= 0 AND span_end > span_start", name="ck_kb_evidence_span"
        ),
        Index("ix_kb_evidence_passage_id", "passage_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    passage_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_passages.id", ondelete="RESTRICT"))
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_immutable_row() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'kb rows are immutable' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql
"""
TRUNCATE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_truncate_deny() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'kb tables cannot be truncated' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql
"""
REVISION_VALIDATE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_validate_revision() RETURNS trigger AS $$
BEGIN
  IF encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') <> NEW.text_sha256 THEN
    RAISE EXCEPTION 'kb revision text_sha256 mismatch' USING ERRCODE='23514';
  END IF;
  IF char_length(NEW.body) <> NEW.char_count THEN
    RAISE EXCEPTION 'kb revision char_count mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
PASSAGE_VALIDATE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_validate_passage() RETURNS trigger AS $$
DECLARE rev_body text;
BEGIN
  SELECT body INTO rev_body FROM kb_revisions WHERE id = NEW.revision_id;
  IF rev_body IS NULL THEN
    RAISE EXCEPTION 'kb revision not found' USING ERRCODE='23514';
  END IF;
  IF NEW.char_start < 0 OR NEW.char_end <= NEW.char_start OR NEW.char_end > char_length(rev_body) THEN
    RAISE EXCEPTION 'kb passage span out of revision bounds' USING ERRCODE='23514';
  END IF;
  IF substring(rev_body FROM NEW.char_start + 1 FOR NEW.char_end - NEW.char_start) <> NEW.body THEN
    RAISE EXCEPTION 'kb passage body mismatch with revision span' USING ERRCODE='23514';
  END IF;
  IF encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') <> NEW.sha256 THEN
    RAISE EXCEPTION 'kb passage sha256 mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
SPAN_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_validate_evidence_span() RETURNS trigger AS $$
DECLARE original text;
BEGIN
  SELECT body INTO original FROM kb_passages WHERE id=NEW.passage_id;
  IF original IS NULL OR NEW.span_start < 0 OR NEW.span_end <= NEW.span_start
      OR NEW.span_end > char_length(original)
      OR encode(sha256(convert_to(substring(original FROM NEW.span_start+1 FOR NEW.span_end-NEW.span_start), 'UTF8')), 'hex') <> NEW.sha256 THEN
    RAISE EXCEPTION 'kb evidence span or digest mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# 불변 테이블 UPDATE/DELETE 차단
for _table in (
    KbSource.__table__,
    KbRevision.__table__,
    KbPassage.__table__,
    KbEvidence.__table__,
):
    event.listen(_table, "after_create", DDL(IMMUTABLE_FUNCTION))
    event.listen(
        _table,
        "after_create",
        DDL(
            f"CREATE TRIGGER {_table.name}_immutable BEFORE UPDATE OR DELETE ON {_table.name} "
            "FOR EACH ROW EXECUTE FUNCTION kb_immutable_row()"
        ),
    )

# 원장 및 부모 문서 테이블 TRUNCATE 차단
for _table in (
    KbSource.__table__,
    KbDocument.__table__,
    KbRevision.__table__,
    KbPassage.__table__,
    KbEvidence.__table__,
):
    event.listen(_table, "after_create", DDL(TRUNCATE_FUNCTION))
    event.listen(
        _table,
        "after_create",
        DDL(
            f"CREATE TRIGGER {_table.name}_truncate BEFORE TRUNCATE ON {_table.name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION kb_truncate_deny()"
        ),
    )

# Revision 삽입 시 해시 및 길이 검증
event.listen(KbRevision.__table__, "after_create", DDL(REVISION_VALIDATE_FUNCTION))
event.listen(
    KbRevision.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER kb_revisions_validate BEFORE INSERT ON kb_revisions "
        "FOR EACH ROW EXECUTE FUNCTION kb_validate_revision()"
    ),
)

# Passage 삽입 시 revision 슬라이스 본문 및 해시 검증
event.listen(KbPassage.__table__, "after_create", DDL(PASSAGE_VALIDATE_FUNCTION))
event.listen(
    KbPassage.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER kb_passages_validate BEFORE INSERT ON kb_passages "
        "FOR EACH ROW EXECUTE FUNCTION kb_validate_passage()"
    ),
)

# Evidence 삽입 시 passage 구간 인용 및 해시 검증
event.listen(KbEvidence.__table__, "after_create", DDL(SPAN_FUNCTION))
event.listen(
    KbEvidence.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER kb_evidence_validate BEFORE INSERT ON kb_evidence "
        "FOR EACH ROW EXECUTE FUNCTION kb_validate_evidence_span()"
    ),
)
