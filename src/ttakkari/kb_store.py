"""범용 지식 도서관 KB2: 해시 기반 원본 파일 저장소 + 수집·구간 분리·무결성 점검(ADR 0063).

저장소 배치: <data_dir>/kb/originals/<sha256 앞 2자>/<그다음 2자>/<sha256 전체>.
같은 바이트는 같은 경로에 한 번만 둔다. DB 행과 파일은 sha256 으로 묶는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.models import utcnow

PARSER_VERSION_RAW = "raw-v1"
_MAX_PROBLEMS = 50
# 수집(공유 락)과 고아 정리(배타 락)를 직렬화한다. 파일은 DB 커밋보다 먼저 디스크에 놓이므로,
# 정리기가 커밋 전 파일을 고아로 오인해 지우는 경쟁을 막는다.
KB_ADVISORY_LOCK_ID = 6078174526685872129
# 락 밖에서 파일이 생겼을 가능성(다른 쓰기 경로)에 대비한 mtime 기준 이중 방어.
DEFAULT_ORPHAN_GRACE_SECONDS = 60.0
DEFAULT_TMP_GRACE_SECONDS = 60.0
# DB 컬럼 길이(문자 수). 초과 입력은 파일을 쓰기 전에 거부한다.
_FIELD_LIMITS = {"source_key": 500, "kind": 40, "media_type": 120, "lang": 16, "parser_version": 60}


class KbInputError(ValueError):
    """DB 가 거부할 입력. 파일을 쓰기 전에 던져 고아 파일을 만들지 않는다."""


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


def _blob_intact(path: Path, digest: str, size: int) -> bool:
    """파일이 있고 해시·크기가 DB 가 기대하는 값과 같은가."""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return len(raw) == size and sha256_hex(raw) == digest


def _ensure_blob(path: Path, data: bytes, digest: str) -> bool:
    """원본 blob 이 온전하도록 보장한다. 없거나 손상됐으면 새로 쓰고 True 를 돌려준다."""
    if _blob_intact(path, digest, len(data)):
        return False
    _write_file_atomic(path, data)
    return True


def _has_nul(value: object) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nul(v) for v in value)
    return False


def _validate_ingest_input(data: bytes, fields: dict[str, str | None], origin_meta: dict | None) -> None:
    """DB INSERT 가 실패할 입력을 파일 쓰기 전에 거른다(NUL 은 text·jsonb 모두 저장 불가)."""
    if b"\x00" in data:
        raise KbInputError("원본에 NUL(0x00) 바이트가 있어 저장할 수 없다")
    for name, value in fields.items():
        if value is None:
            continue
        if "\x00" in value:
            raise KbInputError(f"{name} 에 NUL 문자가 있어 저장할 수 없다")
        limit = _FIELD_LIMITS.get(name)
        if limit is not None and len(value) > limit:
            raise KbInputError(f"{name} 이(가) {limit}자를 넘는다: {len(value)}자")
    if origin_meta is not None:
        if _has_nul(origin_meta):
            raise KbInputError("origin_meta 에 NUL 문자가 있어 저장할 수 없다")
        try:
            json.dumps(origin_meta)
        except (TypeError, ValueError) as e:
            raise KbInputError(f"origin_meta 를 JSON 으로 저장할 수 없다: {e}") from e


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

    파일이 행보다 먼저 지워졌거나 손상되었으면 다시 써서 살린다(healed_file). 동시에 같은 바이트를
    넣으면 한쪽이 unique 충돌로 실패한다. 호출자가 재시도하면 기존 행을 받는다.
    """
    _validate_ingest_input(
        data,
        {"source_key": source_key, "kind": kind, "locator": locator, "media_type": media_type,
         "title": title, "lang": lang, "parser_version": parser_version},
        origin_meta,
    )
    # 파일을 쓰기 전에 공유 락을 잡고 커밋/롤백까지 쥔다. 고아 정리기(배타 락)가 이 트랜잭션의
    # 미커밋 파일을 고아로 오인해 지우지 못한다.
    await db.execute(sa_text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": KB_ADVISORY_LOCK_ID})
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
        healed = _ensure_blob(path, data, digest)
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

    # 같은 해시의 손상 blob 이 남아 있으면 덮어써 교체한다(내용 주소 저장소라 새로 써도 안전하다).
    _ensure_blob(root / relpath, data, digest)
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
    # MISSING_FILE | HASH_MISMATCH | SIZE_MISMATCH | ORPHAN_FILE | ABANDONED_TMP_FILE | CORRUPT_FILE
    # | TEXT_HASH_MISMATCH | REVISION_BODY_MISMATCH | PASSAGE_MISMATCH | SPAN_MISMATCH
    code: str
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
    clean_skipped: str | None = None  # 정리를 요청했지만 수행하지 않은 이유

    @property
    def ok(self) -> bool:
        return not self.problems and self.truncated == 0

    def add(self, code: str, detail: str) -> None:
        if len(self.problems) < _MAX_PROBLEMS:
            self.problems.append(VerifyProblem(code, detail))
        else:
            self.truncated += 1


async def verify_integrity(
    db: AsyncSession,
    *,
    root: Path | None = None,
    clean_orphans: bool = False,
    orphan_grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
    tmp_grace_seconds: float = DEFAULT_TMP_GRACE_SECONDS,
) -> VerifyReport:
    """DB 행과 저장소 파일의 정합성을 점검한다.

    clean_orphans 가 True 면 DB 에 없는 고아 파일(ORPHAN_FILE)과 방치된 임시 파일(.kb-tmp-*)을
    디스크에서 지운다. 수집 세션은 파일을 쓰기 전에 공유 advisory lock 을 쥐므로, 정리는 배타 락을
    잡은 동안(호출자 트랜잭션이 끝날 때까지 유지)에만 한다. 락을 못 잡으면(수집 진행 중) 지우지 않고 clean_skipped 에 이유를 남긴다.
    락 밖에서 생긴 파일을 위해 mtime 이 유예 시간보다 어린 파일도 지우지 않는다.
    방치된 .kb-tmp-* 는 유예 시간이 지나면 정리 여부와 무관하게 ABANDONED_TMP_FILE 로 보고한다.
    """
    report = VerifyReport()
    locked = False
    if clean_orphans:
        # 트랜잭션 범위 락이라 호출자가 커밋/롤백하면 풀 연결에 남지 않고 풀린다.
        locked = bool(
            (await db.execute(sa_text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": KB_ADVISORY_LOCK_ID})).scalar()
        )
        if not locked:
            report.clean_skipped = "수집이 진행 중이라 고아 파일 정리를 건너뜀(advisory lock 사용 중)"
    await _verify_into(db, report, root or originals_root(), can_clean=locked,
                       orphan_grace_seconds=orphan_grace_seconds, tmp_grace_seconds=tmp_grace_seconds)
    return report


async def _verify_into(
    db: AsyncSession, report: VerifyReport, base: Path, *,
    can_clean: bool, orphan_grace_seconds: float, tmp_grace_seconds: float,
) -> None:
    sources = (await db.execute(select(KbSource))).scalars().all()
    report.sources = len(sources)
    known: set[str] = set()
    intact_sources: dict[uuid.UUID, KbSource] = {}
    for src in sources:
        known.add(src.sha256)
        path = base / src.store_path
        if not path.is_file():
            report.add("MISSING_FILE", f"{src.id} {src.store_path}")
            continue
        data = path.read_bytes()
        hash_ok = sha256_hex(data) == src.sha256
        if not hash_ok:
            report.add("HASH_MISMATCH", f"{src.id} {src.store_path}")
        if len(data) != src.byte_size:
            report.add("SIZE_MISMATCH", f"{src.id} {src.store_path}")
        if hash_ok:
            intact_sources[src.id] = src
    if base.is_dir():
        now_ts = time.time()
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(base))
            try:
                age = now_ts - path.stat().st_mtime
            except OSError:
                continue
            if path.name.startswith(".kb-tmp-"):
                # 어린 임시 파일은 쓰는 중일 수 있어 건드리지도 보고하지도 않는다.
                if age < tmp_grace_seconds:
                    continue
                if can_clean and _try_unlink(path):
                    report.cleaned_orphans += 1
                else:
                    report.add("ABANDONED_TMP_FILE", rel)
                continue
            report.files += 1
            digest = path.name
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                report.add("CORRUPT_FILE", rel)
                continue
            try:
                raw_bytes = path.read_bytes()
            except OSError:
                report.add("CORRUPT_FILE", rel)
                continue
            if sha256_hex(raw_bytes) != digest:
                report.add("CORRUPT_FILE", rel)
            elif digest not in known:
                if can_clean and age >= orphan_grace_seconds and _try_unlink(path):
                    report.cleaned_orphans += 1
                else:
                    report.add("ORPHAN_FILE", rel)
    revisions = (await db.execute(select(KbRevision))).scalars().all()
    report.revisions = len(revisions)
    bodies: dict[uuid.UUID, str] = {}
    for rev in revisions:
        bodies[rev.id] = rev.body
        if sha256_hex(rev.body.encode("utf-8")) != rev.text_sha256:
            report.add("TEXT_HASH_MISMATCH", str(rev.id))
        # 원본 바이트가 온전할 때만 revision 본문을 원본에서 다시 만든 값과 대조한다.
        # raw 파서만 본문이 normalize_text(원본)로 결정되므로 다른 파서 버전은 대상이 아니다.
        src = intact_sources.get(rev.source_id)
        if src is not None and rev.parser_version == PARSER_VERSION_RAW:
            try:
                original = (base / src.store_path).read_bytes()
            except OSError:
                continue
            if normalize_text(original) != rev.body:
                report.add("REVISION_BODY_MISMATCH", f"{rev.id} source {src.id}")
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


def _try_unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True
