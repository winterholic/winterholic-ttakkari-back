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


async def test_immutable_tables_reject_delete():
    """검토 지적 1: 원장 행(sources, revisions, passages, evidence)의 DELETE 는 트리거로 거부된다."""
    async with sessionmaker()() as db:
        ing, target, ev = await _seed_evidence(db)
        got_ev = await db.get(KbEvidence, ev.id)
        await db.delete(got_ev)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()

        ing, target, ev = await _seed_evidence(db)
        passage = await db.get(KbPassage, target.id)
        await db.delete(passage)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()

        ing, target, ev = await _seed_evidence(db)
        rev = await db.get(KbRevision, ing.revision_id)
        await db.delete(rev)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()

        ing, target, ev = await _seed_evidence(db)
        src = await db.get(KbSource, ing.source_id)
        await db.delete(src)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_parent_deletion_blocked_by_restrict():
    """검토 지적 1: 부모 문서 삭제 시 자식 리비전이 있으면 ON DELETE RESTRICT 로 거부된다."""
    async with sessionmaker()() as db:
        ing, _target, _ev = await _seed_evidence(db)
        doc = await db.get(KbDocument, ing.document_id)
        await db.delete(doc)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_clean_orphans_cleans_unreferenced_blobs():
    """검토 지적 8: DB 트랜잭션 롤백 등으로 남은 고아 파일은 clean_orphans=True 로 정리된다."""
    async with sessionmaker()() as db:
        ing, _target, _ev = await _seed_evidence(db)
        path = originals_root() / object_relpath(ing.sha256)
        assert path.is_file()
        # DB 트랜잭션을 롤백하여 DB 행은 지우고 파일만 남긴다(고아 파일 생성 시나리오)
        await db.rollback()

    async with sessionmaker()() as db:
        # 1. 일반 점검: ORPHAN_FILE 탐지
        report = await verify_integrity(db)
        assert report.ok is False
        assert [p.code for p in report.problems] == ["ORPHAN_FILE"]
        assert path.is_file()

        # 2. 고아 정리 실행: 파일이 삭제되고 cleaned_orphans 가 1 증가
        clean_report = await verify_integrity(db, clean_orphans=True)
        assert clean_report.cleaned_orphans == 1
        assert not path.is_file()

        # 3. 정리 후 재점검: 정합성 정상
        after = await verify_integrity(db)
        assert after.ok is True
        assert len(after.problems) == 0


async def test_nfd_input_normalizes_to_nfkc_and_evidence_span_is_stable():
    """검토 지적 7: NFD 조합형 한글도 NFKC 로 정규화되어 오프셋과 구간 sha256 이 안정적으로 검증된다."""
    import unicodedata
    nfd_text = unicodedata.normalize("NFD", "첫째 문단이다.\n\n둘째 문단 인용.\n")
    async with sessionmaker()() as db:
        ing = await ingest_source(db, source_key="file:/tmp/nfd.txt", data=nfd_text.encode("utf-8"),
                                  locator="/tmp/nfd.txt", media_type="text/plain", title="NFD", lang="ko")
        rev = await db.get(KbRevision, ing.revision_id)
        # 본문은 NFKC(완성형)로 정규화되어 있어야 한다
        assert unicodedata.is_normalized("NFKC", rev.body)
        passages = await segment_revision(db, ing.revision_id)
        assert len(passages) == 2
        p2 = passages[1]
        target_str = "둘째 문단"
        start = p2.body.index(target_str)
        end = start + len(target_str)
        ev = KbEvidence(passage_id=p2.id, span_start=start, span_end=end,
                        sha256=span_digest(p2.body, start, end), note="NFD 정규화 검증",
                        created_at=utcnow())
        db.add(ev)
        await db.flush()
        assert ev.id is not None
        await db.rollback()
