"""범용 지식 도서관 KB2: 해시 기반 원본 파일 저장소 + 수집·구간 분리·무결성 점검(ADR 0063).

저장소 배치: <data_dir>/kb/originals/<sha256 앞 2자>/<그다음 2자>/<sha256 전체>.
같은 바이트는 같은 경로에 한 번만 둔다. DB 행과 파일은 sha256 으로 묶는다.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.models import utcnow

PARSER_VERSION_RAW = "raw-v1"
_MAX_PROBLEMS = 50


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def span_digest(body: str, start: int, end: int) -> str:
    """인용 구간의 다이제스트. DB 트리거(kb_validate_evidence_span)와 같은 정의다."""
    return sha256_hex(body[start:end].encode("utf-8"))


def originals_root() -> Path:
    return get_settings().kb_originals_dir


def object_relpath(digest: str) -> Path:
    return Path(digest[0:2]) / digest[2:4] / digest


def normalize_text(data: bytes) -> str:
    """수집 바이트를 정규화 텍스트로 바꾼다. 결정적이어야 한다(해시·오프셋의 기준)."""
    return unicodedata.normalize("NFKC", data.decode("utf-8", errors="replace")).replace("\r\n", "\n")


def _write_file_atomic(path: Path, data: bytes) -> None:
    """원자적 파일 쓰기: 임시 파일 작성 -> flush -> fsync -> 원자적 rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".kb-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class IngestResult:
    source_id: uuid.UUID
    document_id: uuid.UUID
    revision_id: uuid.UUID
    rev_no: int
    sha256: str
    created: bool
    healed_file: bool = False


async def ingest_source(
    db: AsyncSession,
    *,
    source_key: str,
    data: bytes,
    kind: str = "file",
    locator: str | None = None,
    media_type: str | None = None,
    title: str | None = None,
    lang: str | None = None,
    parser_version: str = PARSER_VERSION_RAW,
    collected_at: datetime | None = None,
    origin_meta: dict | None = None,
) -> IngestResult:
    """원본 바이트를 수집한다. 같은 (source_key, sha256) 재수집은 기존 행을 돌려준다(멱등).

    파일이 행보다 먼저 지워졌으면 다시 써서 살린다(healed_file). 동시에 같은 바이트를
    넣으면 한쪽이 unique 충돌로 실패한다. 호출자가 재시도하면 기존 행을 받는다.
    """
    digest = sha256_hex(data)
    root = originals_root()
    relpath = object_relpath(digest)
    now = utcnow()
    collected = collected_at or now

    existing = (
        await db.execute(select(KbSource).where(KbSource.source_key == source_key, KbSource.sha256 == digest))
    ).scalar_one_or_none()
    if existing is not None:
        path = root / relpath
        healed = False
        if not path.is_file():
            _write_file_atomic(path, data)
            healed = True
        rev = (
            await db.execute(
                select(KbRevision)
                .where(KbRevision.source_id == existing.id)
                .order_by(KbRevision.rev_no.desc())
                .limit(1)
            )
        ).scalar_one()
        return IngestResult(
            source_id=existing.id, document_id=rev.document_id, revision_id=rev.id,
            rev_no=rev.rev_no, sha256=digest, created=False, healed_file=healed,
        )

    _write_file_atomic(root / relpath, data)
    source_id = uuid.uuid4()
    await db.execute(
        insert(KbSource).values(
            id=source_id, source_key=source_key, kind=kind, locator=locator, sha256=digest,
            byte_size=len(data), media_type=media_type, store_path=str(relpath),
            collected_at=collected, origin_meta=origin_meta or {}, created_at=now,
        )
    )
    doc = (await db.execute(select(KbDocument).where(KbDocument.doc_key == source_key))).scalar_one_or_none()
    if doc is None:
        doc_id = uuid.uuid4()
        await db.execute(
            insert(KbDocument).values(
                id=doc_id, doc_key=source_key,
                title=(title or locator or source_key)[:500], lang=lang,
                created_at=now, updated_at=now,
            )
        )
    else:
        doc_id = doc.id
    prev_no = (
        await db.execute(select(func.max(KbRevision.rev_no)).where(KbRevision.document_id == doc_id))
    ).scalar() or 0
    prev = None
    if prev_no:
        prev = (
            await db.execute(
                select(KbRevision).where(KbRevision.document_id == doc_id, KbRevision.rev_no == prev_no)
            )
        ).scalar_one()
    text = normalize_text(data)
    revision_id = uuid.uuid4()
    await db.execute(
        insert(KbRevision).values(
            id=revision_id, document_id=doc_id, source_id=source_id, rev_no=prev_no + 1,
            parser_version=parser_version, body=text, text_sha256=sha256_hex(text.encode("utf-8")),
            char_count=len(text), supersedes_id=prev.id if prev else None, created_at=now,
        )
    )
    await db.flush()
    return IngestResult(
        source_id=source_id, document_id=doc_id, revision_id=revision_id,
        rev_no=prev_no + 1, sha256=digest, created=True,
    )


def split_paragraphs(text: str) -> list[tuple[str | None, str, int, int]]:
    """본문을 빈 줄 기준 문단으로 나눈다. (heading, 본문, 시작, 끝). heading 은 항상 None 이다.

    KB3 분석기가 들어오면 이 단순 분리기는 파서 버전과 함께 교체한다.
    """
    out: list[tuple[str | None, str, int, int]] = []
    pos = 0
    length = len(text)
    while pos < length:
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            break
        start = pos
        end = text.find("\n\n", pos)
        if end == -1:
            end = length
        body = text[start:end].strip()
        if body:
            body_start = text.index(body[0], start, end)
            body_end = body_start + len(body)
            out.append((None, body, body_start, body_end))
        pos = end
    return out


async def segment_revision(db: AsyncSession, revision_id: uuid.UUID) -> list[KbPassage]:
    """리비전 본문을 패시지로 나눠 저장한다. 재실행해도 같은 행이 남는다(멱등)."""
    rev = await db.get(KbRevision, revision_id)
    if rev is None:
        raise KeyError(f"kb revision not found: {revision_id}")
    now = utcnow()
    for ordinal, (_heading, body, start, end) in enumerate(split_paragraphs(rev.body)):
        stmt = insert(KbPassage).values(
            id=uuid.uuid4(), revision_id=revision_id, ord=ordinal, heading=None, body=body,
            char_start=start, char_end=end, sha256=sha256_hex(body.encode("utf-8")), created_at=now,
        )
        stmt = stmt.on_conflict_do_nothing(constraint="uq_kb_passage_ord")
        await db.execute(stmt)
    await db.flush()
    rows = (
        await db.execute(
            select(KbPassage).where(KbPassage.revision_id == revision_id).order_by(KbPassage.ord)
        )
    ).scalars().all()
    return list(rows)


@dataclass
class VerifyProblem:
    code: str  # MISSING_FILE | HASH_MISMATCH | SIZE_MISMATCH | ORPHAN_FILE | CORRUPT_FILE | TEXT_HASH_MISMATCH | PASSAGE_MISMATCH | SPAN_MISMATCH
    detail: str


@dataclass
class VerifyReport:
    sources: int = 0
    revisions: int = 0
    passages: int = 0
    evidence: int = 0
    files: int = 0
    problems: list[VerifyProblem] = field(default_factory=list)
    truncated: int = 0
    cleaned_orphans: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems and self.truncated == 0

    def add(self, code: str, detail: str) -> None:
        if len(self.problems) < _MAX_PROBLEMS:
            self.problems.append(VerifyProblem(code, detail))
        else:
            self.truncated += 1


async def verify_integrity(
    db: AsyncSession, *, root: Path | None = None, clean_orphans: bool = False
) -> VerifyReport:
    """DB 행과 저장소 파일의 정합성을 점검한다.
    
    clean_orphans 가 True 면 DB 에 없는 고아 파일(ORPHAN_FILE)을 디스크에서 안전하게 제거한다.
    원자성 순서: 파일 작성(fsync) -> 원자적 rename -> DB insert -> DB commit.
    파일 작성 후 DB 트랜잭션 롤백/실패로 남은 파일은 이 점검의 고아 정리로 복구된다.
    """
    base = root or originals_root()
    report = VerifyReport()
    sources = (await db.execute(select(KbSource))).scalars().all()
    report.sources = len(sources)
    known: set[str] = set()
    for src in sources:
        known.add(src.sha256)
        path = base / src.store_path
        if not path.is_file():
            report.add("MISSING_FILE", f"{src.id} {src.store_path}")
            continue
        data = path.read_bytes()
        if sha256_hex(data) != src.sha256:
            report.add("HASH_MISMATCH", f"{src.id} {src.store_path}")
        if len(data) != src.byte_size:
            report.add("SIZE_MISMATCH", f"{src.id} {src.store_path}")
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith(".kb-tmp-"):
                continue
            report.files += 1
            digest = path.name
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                report.add("CORRUPT_FILE", str(path.relative_to(base)))
                continue
            if sha256_hex(path.read_bytes()) != digest:
                report.add("CORRUPT_FILE", str(path.relative_to(base)))
            elif digest not in known:
                if clean_orphans:
                    try:
                        path.unlink()
                        report.cleaned_orphans += 1
                    except OSError:
                        report.add("ORPHAN_FILE", str(path.relative_to(base)))
                else:
                    report.add("ORPHAN_FILE", str(path.relative_to(base)))
    revisions = (await db.execute(select(KbRevision))).scalars().all()
    report.revisions = len(revisions)
    bodies: dict[uuid.UUID, str] = {}
    for rev in revisions:
        bodies[rev.id] = rev.body
        if sha256_hex(rev.body.encode("utf-8")) != rev.text_sha256:
            report.add("TEXT_HASH_MISMATCH", str(rev.id))
    passages = (await db.execute(select(KbPassage))).scalars().all()
    report.passages = len(passages)
    passage_bodies: dict[uuid.UUID, str] = {}
    for passage in passages:
        passage_bodies[passage.id] = passage.body
        if sha256_hex(passage.body.encode("utf-8")) != passage.sha256:
            report.add("PASSAGE_MISMATCH", str(passage.id))
            continue
        parent = bodies.get(passage.revision_id)
        if parent is None or parent[passage.char_start:passage.char_end] != passage.body:
            report.add("PASSAGE_MISMATCH", f"{passage.id} offsets")
    rows = (await db.execute(select(KbEvidence))).scalars().all()
    report.evidence = len(rows)
    for ev in rows:
        body = passage_bodies.get(ev.passage_id)
        if body is None or span_digest(body, ev.span_start, ev.span_end) != ev.sha256:
            report.add("SPAN_MISMATCH", str(ev.id))
    return report
