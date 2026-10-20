"""KB1+KB2: 수집 멱등성·불변 트리거·인용 구간 해시·무결성 점검."""

import shutil

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.kb_store import (
    ingest_source,
    object_relpath,
    originals_root,
    segment_revision,
    sha256_hex,
    span_digest,
    verify_integrity,
)
from ttakkari.models import utcnow

BODY = "첫째 문단이다.\n\n둘째 문단에는 인용할 문장이 있다.\n\n셋째 문단이다.\n"


@pytest.fixture(autouse=True)
def clean_originals():
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    yield
    if root.exists():
        shutil.rmtree(root)


async def _ingest(db, key="file:/tmp/원본.txt", data: bytes | None = None):
    return await ingest_source(db, source_key=key, data=BODY.encode() if data is None else data,
                               locator=key, media_type="text/plain", title="원본", lang="ko")


async def test_ingest_is_idempotent_and_stores_content_addressed_file():
    async with sessionmaker()() as db:
        first = await _ingest(db)
        assert first.created is True
        assert first.rev_no == 1
        second = await _ingest(db)
        assert second.created is False
        assert (second.source_id, second.document_id, second.revision_id) == \
            (first.source_id, first.document_id, first.revision_id)
        for model in (KbSource, KbDocument, KbRevision):
            assert await db.scalar(select(func.count()).select_from(model)) == 1
        path = originals_root() / object_relpath(first.sha256)
        assert path.is_file()
        assert path.read_bytes() == BODY.encode()
        src = await db.get(KbSource, first.source_id)
        assert src.byte_size == len(BODY.encode())
        assert src.store_path == str(object_relpath(first.sha256))
        rev = await db.get(KbRevision, first.revision_id)
        assert rev.char_count == len(BODY)
        assert rev.supersedes_id is None
        await db.rollback()


async def test_ingest_changed_bytes_creates_new_revision_with_lineage():
    async with sessionmaker()() as db:
        first = await _ingest(db)
        changed = await _ingest(db, data=(BODY + "추가.\n").encode())
        assert changed.created is True
        assert changed.document_id == first.document_id
        assert changed.source_id != first.source_id
        assert changed.rev_no == 2
        rev = await db.get(KbRevision, changed.revision_id)
        assert rev.supersedes_id == first.revision_id
        assert await db.scalar(select(func.count()).select_from(KbSource)) == 2
        await db.rollback()


async def test_ingest_heals_missing_file():
    async with sessionmaker()() as db:
        first = await _ingest(db)
        path = originals_root() / object_relpath(first.sha256)
        path.unlink()
        healed = await _ingest(db)
        assert healed.created is False
        assert healed.healed_file is True
        assert path.read_bytes() == BODY.encode()
        await db.rollback()


async def test_segment_is_idempotent_with_stable_offsets():
    async with sessionmaker()() as db:
        ing = await _ingest(db)
        first = await segment_revision(db, ing.revision_id)
        assert [p.body for p in first] == ["첫째 문단이다.", "둘째 문단에는 인용할 문장이 있다.", "셋째 문단이다."]
        rev = await db.get(KbRevision, ing.revision_id)
        for p in first:
            assert rev.body[p.char_start:p.char_end] == p.body
            assert p.sha256 == sha256_hex(p.body.encode())
        second = await segment_revision(db, ing.revision_id)
        assert [p.id for p in second] == [p.id for p in first]
        await db.rollback()


async def _seed_evidence(db):
    ing = await _ingest(db)
    passages = await segment_revision(db, ing.revision_id)
    target = passages[1]
    start = target.body.index("인용할 문장")
    end = start + len("인용할 문장")
    ev = KbEvidence(passage_id=target.id, span_start=start, span_end=end,
                    sha256=span_digest(target.body, start, end), note="테스트",
                    created_at=utcnow())
    db.add(ev)
    await db.flush()
    return ing, target, ev


async def test_evidence_accepts_valid_span_and_rejects_bad_digest_or_range():
    async with sessionmaker()() as db:
        _ing, target, ev = await _seed_evidence(db)
        assert ev.id is not None
        bad = KbEvidence(passage_id=target.id, span_start=0, span_end=2,
                         sha256="0" * 64, created_at=utcnow())
        db.add(bad)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
        _ing2, target2, _ev2 = await _seed_evidence(db)
        out = KbEvidence(passage_id=target2.id, span_start=0, span_end=len(target2.body) + 5,
                         sha256="0" * 64, created_at=utcnow())
        db.add(out)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_immutable_tables_reject_update():
    async with sessionmaker()() as db:
        ing, target, ev = await _seed_evidence(db)
        src = await db.get(KbSource, ing.source_id)
        src.locator = "바꿈"
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()

        ing, target, ev = await _seed_evidence(db)
        rev = await db.get(KbRevision, ing.revision_id)
        rev.parser_version = "바꿈"
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()

        ing, target, ev = await _seed_evidence(db)
        passage = await db.get(KbPassage, target.id)
        passage.heading = "바꿈"
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()

        ing, target, ev = await _seed_evidence(db)
        got = await db.get(KbEvidence, ev.id)
        got.note = "바꿈"
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_document_stays_mutable():
    async with sessionmaker()() as db:
        ing = await _ingest(db)
        doc = await db.get(KbDocument, ing.document_id)
        doc.title = "새 제목"
        await db.flush()
        assert (await db.get(KbDocument, ing.document_id)).title == "새 제목"
        await db.rollback()


async def test_verify_ok_then_reports_file_problems():
    async with sessionmaker()() as db:
        ing, _target, _ev = await _seed_evidence(db)
        report = await verify_integrity(db)
        assert report.ok, [p.detail for p in report.problems]
        assert (report.sources, report.revisions, report.passages, report.evidence) == (1, 1, 3, 1)
        path = originals_root() / object_relpath(ing.sha256)
        raw = path.read_bytes()
        path.unlink()
        missing = await verify_integrity(db)
        assert missing.ok is False
        assert [p.code for p in missing.problems] == ["MISSING_FILE"]
        path.write_bytes(b"corrupt")
        corrupt = await verify_integrity(db)
        assert {p.code for p in corrupt.problems} == {"HASH_MISMATCH", "SIZE_MISMATCH", "CORRUPT_FILE"}
        path.write_bytes(raw)
        bad = originals_root() / object_relpath("b" * 64)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"x")
        corrupt_report = await verify_integrity(db)
        assert "CORRUPT_FILE" in {p.code for p in corrupt_report.problems}
        bad.unlink()
        orphan_body = b"ownerless-bytes"
        orphan = originals_root() / object_relpath(sha256_hex(orphan_body))
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(orphan_body)
        orphan_report = await verify_integrity(db)
        assert "ORPHAN_FILE" in {p.code for p in orphan_report.problems}
        await db.rollback()
