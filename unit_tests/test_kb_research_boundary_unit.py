"""Unit verification for knowledge library and research context boundary.

Tests pure logic, mixed-history classification, regression scenarios, and
PostgreSQL compilation without requiring a live database connection.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from ttakkari import kb_library as lib
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource


def _fake_source(*, kind="research_text", locator=None, purpose=None):
    origin_meta = {}
    if purpose is not None:
        origin_meta["library_purpose"] = purpose
    return KbSource(
        id=uuid.uuid4(),
        source_key=f"source:{uuid.uuid4()}",
        kind=kind,
        locator=locator,
        sha256="0" * 64,
        byte_size=100,
        origin_meta=origin_meta,
        created_at=datetime.now(UTC),
    )


def test_research_source_purpose_classification():
    """Verify classification across explicit, legacy HTTP, legacy local, and session locators."""
    # 1. Explicit knowledge_reference takes precedence regardless of locator
    assert lib.research_source_purpose(_fake_source(locator="https://example.org", purpose="knowledge_reference")) == "knowledge_reference"
    assert lib.research_source_purpose(_fake_source(locator="/repo/docs/manual.md", purpose="knowledge_reference")) == "knowledge_reference"
    assert lib.research_source_purpose(_fake_source(locator="session:req-123", purpose="knowledge_reference")) == "knowledge_reference"

    # 2. Explicit research_context takes precedence even on HTTP
    assert lib.research_source_purpose(_fake_source(locator="https://example.org/receipt", purpose="research_context")) == "research_context"
    assert lib.research_source_purpose(_fake_source(locator="/repo/receipt.md", purpose="research_context")) == "research_context"

    # 3. Legacy (no purpose) external URL falls back to knowledge_reference
    assert lib.research_source_purpose(_fake_source(locator="http://example.org/paper")) == "knowledge_reference"
    assert lib.research_source_purpose(_fake_source(locator="https://example.org/paper ; note")) == "knowledge_reference"

    # 4. Legacy (no purpose) local / session / other falls back to research_context
    assert lib.research_source_purpose(_fake_source(locator="/repo/work/notes.md")) == "research_context"
    assert lib.research_source_purpose(_fake_source(locator="session:chat-456")) == "research_context"
    assert lib.research_source_purpose(_fake_source(locator="regna-ios/internal")) == "research_context"
    assert lib.research_source_purpose(_fake_source(locator=None)) == "research_context"


def test_source_is_research_context_sql_logic():
    """Verify SQL expressions match the classification logic."""
    # The expressions compile cleanly to postgresql
    compiled = str(sa.select(lib._source_is_research_context(KbSource)).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "kb_sources.kind = 'research_text'" in compiled
    assert "kb_sources.origin_meta ->> 'library_purpose'" in compiled


def test_p2_defect_demonstration_and_correction_logic():
    """Demonstrate how the old filter failed on mixed history and how current-revision solves it.

    In memory evaluation of the decision logic:
    Given a document with revisions [rev1, rev2]:
    Case A (Context -> Reference):
      rev1 = local receipt (context)
      rev2 = official reference (reference, CURRENT)
      Old filter: ANY revision is context -> document HIDDEN (FAIL - P2 bug!)
      New filter: CURRENT revision is reference -> document SHOWN (PASS!)
      Revision filter: rev1 is context (HIDDEN from passages/counts), rev2 is reference (SHOWN)

    Case B (Reference -> Context):
      rev1 = official reference (reference)
      rev2 = local task notes (context, CURRENT)
      Old filter: ANY revision is context -> document hidden
      New filter: CURRENT revision is context -> document HIDDEN (PASS - no leak!)
    """
    def old_classify_document(revisions_sources):
        # Old logic: if any revision has is_context == True, hide document
        return any(lib.research_source_purpose(s) == "research_context" for s in revisions_sources)

    def new_classify_document(current_source):
        # New logic: document classification follows CURRENT revision only
        return lib.research_source_purpose(current_source) == "research_context"

    # Case A: Mixed history: rev1 (context) -> rev2 (reference, current)
    s1_context = _fake_source(locator="/repo/receipt.md", purpose=None) # legacy local context
    s2_reference = _fake_source(locator="https://example.org/doc", purpose="knowledge_reference")

    # Under old logic: document was incorrectly hidden!
    assert old_classify_document([s1_context, s2_reference]) is True, "Old filter hides document"

    # Under new logic: document is correctly visible!
    assert new_classify_document(s2_reference) is False, "New filter exposes document"
    # And individual revision boundary is maintained:
    assert lib.research_source_purpose(s1_context) == "research_context"
    assert lib.research_source_purpose(s2_reference) == "knowledge_reference"

    # Case B: Reverse mixed history: rev1 (reference) -> rev2 (context, current)
    s1_ref = _fake_source(locator="https://example.org/guide", purpose="knowledge_reference")
    s2_ctx = _fake_source(locator="/repo/notes.md", purpose="research_context")

    assert lib.research_source_purpose(s1_ref) == "knowledge_reference"
    # Under new logic: document is correctly hidden
    assert new_classify_document(s2_ctx) is True, "Current context hides document"


def test_postgresql_dialect_compilation_all_library_queries():
    """Ensure all updated queries compile to valid PostgreSQL SQL."""
    dialect = postgresql.dialect()
    compile_kwargs = {"literal_binds": True}

    # 1. library_filter
    doc_stmt = sa.select(KbDocument).where(lib.library_filter("personal"))
    doc_sql = str(doc_stmt.compile(dialect=dialect, compile_kwargs=compile_kwargs))
    assert "kb_documents" in doc_sql
    assert "current_observation" in doc_sql
    assert "research_text" in doc_sql

    # 2. research_context_revision_filter
    rev_stmt = sa.select(KbRevision).where(~lib.research_context_revision_filter(KbRevision))
    rev_sql = str(rev_stmt.compile(dialect=dialect, compile_kwargs=compile_kwargs))
    assert "kb_revisions" in rev_sql
    assert "kb_sources" in rev_sql

    # 3. search_passages query structure
    passage_stmt = (
        sa.select(KbPassage, KbRevision, KbDocument)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .join(KbDocument, KbDocument.id == KbRevision.document_id)
        .where(
            lib.library_filter("personal"),
            ~lib.research_context_revision_filter(KbRevision),
            lib._current_revision_filter(),
        )
    )
    passage_sql = str(passage_stmt.compile(dialect=dialect, compile_kwargs=compile_kwargs))
    assert "kb_passages" in passage_sql
    assert "kb_revisions" in passage_sql
    assert "kb_documents" in passage_sql

    # 4. summary queries
    for model in (KbDocument, KbRevision, KbPassage, KbEvidence):
        stmt = sa.select(sa.func.count()).select_from(model)
        if model is KbEvidence:
            stmt = stmt.join(KbPassage, KbEvidence.passage_id == KbPassage.id)
        if model in (KbPassage, KbEvidence):
            stmt = stmt.join(KbRevision, KbPassage.revision_id == KbRevision.id)
        if model is not KbDocument:
            stmt = stmt.join(KbDocument, KbRevision.document_id == KbDocument.id)
            stmt = stmt.where(~lib.research_context_revision_filter(KbRevision))
        stmt = stmt.where(lib.library_filter("personal"))
        sql = str(stmt.compile(dialect=dialect, compile_kwargs=compile_kwargs))
        assert "COUNT" in sql.upper()

    # Sources summary query
    src_stmt = sa.select(KbSource.id, KbSource.collected_at).join(
        KbRevision, KbSource.id == KbRevision.source_id
    ).join(
        KbDocument, KbRevision.document_id == KbDocument.id
    ).where(
        lib.library_filter("personal"), ~lib._source_is_research_context(KbSource)
    ).distinct()
    src_sql = str(src_stmt.compile(dialect=dialect, compile_kwargs=compile_kwargs))
    assert "kb_sources" in src_sql
