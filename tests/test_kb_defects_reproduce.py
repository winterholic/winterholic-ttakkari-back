"""KB1·KB2 독립 검증(커밋 63cf7e0 기준)에서 지적된 결함의 재현 테스트.

수정 전 코드에서는 각 테스트가 해당 결함 때문에 실패하고, 수정 후에는 통과해야 한다.
"""
import inspect
import os
import shutil
import time

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbEvidence, KbRevision, KbSource
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

# 유예 시간 파라미터가 생기기 전 코드에서도 같은 호출이 돌아가야 결함이 "올바른 이유로" 드러난다.
_HAS_GRACE = "orphan_grace_seconds" in inspect.signature(verify_integrity).parameters
NO_GRACE = {"orphan_grace_seconds": 0, "tmp_grace_seconds": 0} if _HAS_GRACE else {}


@pytest.fixture(autouse=True)
def clean_originals():
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    yield
    if root.exists():
        shutil.rmtree(root)


def _stored_files() -> list[str]:
    return [str(p) for p in originals_root().rglob("*") if p.is_file()]


async def _seed(db, key="file:/tmp/원본.txt", data: bytes | None = None):
    ing = await ingest_source(
        db,
        source_key=key,
        data=BODY.encode() if data is None else data,
        locator=key,
        media_type="text/plain",
        title="원본",
        lang="ko",
    )
    passages = await segment_revision(db, ing.revision_id)
    target = passages[1]
    start = target.body.index("인용할 문장")
    end = start + len("인용할 문장")
    ev = KbEvidence(
        passage_id=target.id,
        span_start=start,
        span_end=end,
        sha256=span_digest(target.body, start, end),
        note="근거",
        created_at=utcnow(),
    )
    db.add(ev)
    await db.flush()
    return ing, target, ev


# --------------------------------------------------------------------------
# 1. P1: clean-orphans 와 수집(flush 후 commit 전) 경쟁
# --------------------------------------------------------------------------
async def test_clean_orphans_does_not_delete_uncommitted_ingest_file():
    """세션 A 가 flush 만 한 상태에서 세션 B 의 정리가 돌아도 파일이 남고, A 커밋 뒤 verify 가 정상이어야 한다."""
    async with sessionmaker()() as session_a:
        ing = await ingest_source(session_a, source_key="file:/tmp/race.txt", data=BODY.encode())
        path = originals_root() / object_relpath(ing.sha256)
        assert path.is_file()

        # 유예 시간을 0 으로 둬서 락만으로 막히는지 본다.
        async with sessionmaker()() as session_b:
            rep = await verify_integrity(session_b, clean_orphans=True, **NO_GRACE)
        assert path.is_file(), "수집 중인 파일은 clean_orphans 에 의해 삭제되면 안 된다"
        assert rep.cleaned_orphans == 0
        if _HAS_GRACE:
            assert rep.clean_skipped, "정리를 건너뛴 이유가 보고돼야 한다"

        await session_a.commit()

    async with sessionmaker()() as session_c:
        after = await verify_integrity(session_c)
        assert after.ok is True, [p.code for p in after.problems]


async def test_clean_orphans_blocks_ingest_until_cleaner_finishes():
    """정리기가 락을 쥔 동안 시작한 수집은 정리가 끝난 뒤에야 파일을 쓴다(반대 방향 경쟁 차단)."""
    import asyncio

    if not _HAS_GRACE:
        pytest.fail("락이 없는 코드: 정리기와 수집이 직렬화되지 않는다")
    async with sessionmaker()() as cleaner:
        rep = await verify_integrity(cleaner, clean_orphans=True, **NO_GRACE)
        assert rep.clean_skipped is None  # 락을 잡았다(트랜잭션이 끝날 때까지 유지)

        async def ingest():
            async with sessionmaker()() as s:
                res = await ingest_source(s, source_key="file:/tmp/wait.txt", data=BODY.encode())
                await s.commit()
                return res

        task = asyncio.create_task(ingest())
        await asyncio.sleep(0.5)
        assert not task.done(), "배타 락을 쥔 정리기가 끝나기 전에는 수집이 진행되면 안 된다"
        await cleaner.rollback()
    res = await asyncio.wait_for(task, 10)
    assert (originals_root() / object_relpath(res.sha256)).is_file()


async def test_clean_orphans_keeps_files_younger_than_grace():
    """락 밖에서 생긴 최근 고아 파일도 기본 유예 시간 안에서는 지우지 않는다. 오래되면 지운다."""
    digest = sha256_hex(b"stray")
    path = originals_root() / object_relpath(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stray")

    async with sessionmaker()() as db:
        rep = await verify_integrity(db, clean_orphans=True)  # 기본 유예
        assert path.is_file(), "방금 쓴 파일은 기본 유예 시간 안에서 지우면 안 된다"
        assert rep.cleaned_orphans == 0
        assert [p.code for p in rep.problems] == ["ORPHAN_FILE"]

    old = time.time() - 3600
    os.utime(path, (old, old))
    async with sessionmaker()() as db:
        rep = await verify_integrity(db, clean_orphans=True)
        assert rep.cleaned_orphans == 1
        assert not path.exists()


# --------------------------------------------------------------------------
# 2. P2: TRUNCATE 차단
# --------------------------------------------------------------------------
@pytest.mark.parametrize("table", ["kb_evidence", "kb_passages", "kb_revisions", "kb_sources", "kb_documents"])
async def test_truncate_is_blocked_for_ledger_and_parent_tables(table):
    async with sessionmaker()() as db:
        await _seed(db)
        await db.commit()

    async with sessionmaker()() as db:
        with pytest.raises(DBAPIError, match="cannot be truncated"):
            await db.execute(text(f"TRUNCATE {table} CASCADE"))
        await db.rollback()

    async with sessionmaker()() as db:
        for t in ("kb_sources", "kb_documents", "kb_revisions", "kb_passages", "kb_evidence"):
            assert await db.scalar(text(f"select count(*) from {t}")) >= 1, f"{t} 가 비워졌다"


# --------------------------------------------------------------------------
# 3. P2: 손상 blob 복구와 방치된 .kb-tmp-* 정리
# --------------------------------------------------------------------------
@pytest.mark.parametrize("corrupt", [b"corrupted-blob-content", None], ids=["different-size", "same-size"])
async def test_reingest_replaces_corrupt_blob(corrupt):
    async with sessionmaker()() as db:
        ing, _t, _e = await _seed(db)
        await db.commit()

    path = originals_root() / object_relpath(ing.sha256)
    raw = BODY.encode()
    path.write_bytes(corrupt if corrupt is not None else bytes(len(raw)))  # 같은 길이의 0 바이트

    async with sessionmaker()() as db:
        rep = await verify_integrity(db)
        assert rep.ok is False
        assert any(p.code in ("HASH_MISMATCH", "CORRUPT_FILE") for p in rep.problems)

        again = await ingest_source(db, source_key="file:/tmp/원본.txt", data=raw)
        assert again.created is False
        assert again.healed_file is True
        assert path.read_bytes() == raw

        rep_after = await verify_integrity(db)
        assert rep_after.ok is True, [p.code for p in rep_after.problems]


async def test_reingest_replaces_corrupt_blob_without_db_row():
    """DB 행은 롤백됐고 같은 해시 경로에 손상 파일만 남은 경우에도 새 수집이 올바른 바이트로 교체한다."""
    raw = BODY.encode()
    path = originals_root() / object_relpath(sha256_hex(raw))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"junk")

    async with sessionmaker()() as db:
        await ingest_source(db, source_key="file:/tmp/fresh.txt", data=raw)
        await db.commit()
    assert path.read_bytes() == raw
    async with sessionmaker()() as db:
        assert (await verify_integrity(db)).ok is True


async def test_abandoned_tmp_file_is_reported_and_cleaned_but_fresh_one_is_left():
    stale = originals_root() / "ab" / "cd" / ".kb-tmp-abandoned"
    fresh = originals_root() / "ab" / "cd" / ".kb-tmp-inflight"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"abandoned")
    fresh.write_bytes(b"in flight")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    async with sessionmaker()() as db:
        rep = await verify_integrity(db)
        assert rep.ok is False
        assert [p.code for p in rep.problems] == ["ABANDONED_TMP_FILE"]
        assert stale.exists()

        rep_clean = await verify_integrity(db, clean_orphans=True)
        assert rep_clean.cleaned_orphans == 1
        assert not stale.exists(), "오래된 임시 파일은 정리되어야 한다"
        assert fresh.exists(), "방금 만든 임시 파일은 쓰는 중일 수 있어 남겨야 한다"
        assert rep_clean.ok is True


# --------------------------------------------------------------------------
# 4. P3: passage·revision 정합성을 DB 가 강제하고 verify 가 원본 바이트와 대조
# --------------------------------------------------------------------------
async def test_db_rejects_revision_with_wrong_hash_or_length():
    async with sessionmaker()() as db:
        ing, _t, _e = await _seed(db)
        await db.commit()

    good = "정상 본문"
    cases = {
        "text_sha256": (good, "0" * 64, len(good)),
        "char_count": (good, sha256_hex(good.encode()), len(good) + 1),
    }
    for label, (body, digest, count) in cases.items():
        async with sessionmaker()() as db:
            with pytest.raises(DBAPIError, match=label):
                await db.execute(
                    text(
                        "INSERT INTO kb_revisions(id, document_id, source_id, rev_no, parser_version, body,"
                        " text_sha256, char_count, created_at) VALUES (gen_random_uuid(), :d, :s, 99, 'raw-v1',"
                        " :b, :h, :c, now())"
                    ),
                    {"d": ing.document_id, "s": ing.source_id, "b": body, "h": digest, "c": count},
                )
            await db.rollback()


async def test_db_rejects_passage_that_differs_from_revision_slice():
    async with sessionmaker()() as db:
        ing, _t, _e = await _seed(db)
        await db.commit()

    fake = "mismatched_passage_body"
    async with sessionmaker()() as db:
        rev_len = await db.scalar(text("select char_count from kb_revisions where id=:r"), {"r": ing.revision_id})
        cases = [
            ("body mismatch", fake, sha256_hex(fake.encode()), 0, len(fake)),
            ("out of revision bounds", fake, sha256_hex(fake.encode()), 0, rev_len + 5),
            ("sha256 mismatch", BODY[:5], "0" * 64, 0, 5),
        ]
    for label, body, digest, start, end in cases:
        async with sessionmaker()() as db:
            with pytest.raises(DBAPIError, match=label):
                await db.execute(
                    text(
                        "INSERT INTO kb_passages(id, revision_id, ord, heading, body, char_start, char_end, sha256,"
                        " created_at) VALUES (gen_random_uuid(), :r, 99, NULL, :b, :s, :e, :h, now())"
                    ),
                    {"r": ing.revision_id, "b": body, "s": start, "e": end, "h": digest},
                )
            await db.rollback()


async def test_verify_compares_revision_body_with_source_bytes():
    """원본 바이트는 온전한데 revision 본문이 그 정규화 결과와 다르면 REVISION_BODY_MISMATCH 로 잡아야 한다."""
    async with sessionmaker()() as db:
        ing, _t, _e = await _seed(db)
        await db.commit()

    forged = "변조된 본문"
    async with sessionmaker()() as db:
        # 트리거를 우회하는 수정(DB 관리자 권한 사고)을 흉내낸다. 본문 해시는 일관되게 맞춰 해시 검사만으론 안 걸리게 한다.
        await db.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await db.execute(
            text("UPDATE kb_revisions SET body=:b, text_sha256=:h, char_count=:c WHERE id=:i"),
            {"b": forged, "h": sha256_hex(forged.encode()), "c": len(forged), "i": ing.revision_id},
        )
        await db.commit()

    async with sessionmaker()() as db:
        rep = await verify_integrity(db)
        codes = {p.code for p in rep.problems}
        assert "REVISION_BODY_MISMATCH" in codes, codes
        assert "TEXT_HASH_MISMATCH" not in codes


# --------------------------------------------------------------------------
# 5. P3: 입력 사전 검증(NUL 등)과 파일 미생성
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"data": b"hello\x00world\n\nsecond"},
        {"data": b"ok", "source_key": "file:/tmp/n\x00ul.txt"},
        {"data": b"ok", "title": "제\x00목"},
        {"data": b"ok", "locator": "/tmp/\x00x"},
        {"data": b"ok", "origin_meta": {"k": ["a\x00b"]}},
        {"data": b"ok", "source_key": "k" * 501},
        {"data": b"ok", "lang": "x" * 17},
    ],
    ids=["data-nul", "key-nul", "title-nul", "locator-nul", "meta-nul", "key-too-long", "lang-too-long"],
)
async def test_invalid_input_is_rejected_before_any_file_is_written(kwargs):
    params = {"source_key": "file:/tmp/bad.txt", **kwargs}
    async with sessionmaker()() as db:
        with pytest.raises(ValueError):
            await ingest_source(db, **params)
        await db.rollback()

    assert _stored_files() == [], "거부된 입력이 디스크에 고아 파일을 남기면 안 된다"
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbSource)) == 0
        assert await db.scalar(select(func.count()).select_from(KbRevision)) == 0

