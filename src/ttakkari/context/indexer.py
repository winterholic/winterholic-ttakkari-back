"""RAG 색인: 등록된 폴더의 마크다운·텍스트를 제목 단위 조각으로 잘라 임베딩한다. 바뀐 파일만 다시 한다."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.broker.policy import is_sensitive, is_within
from ttakkari.config import get_settings
from ttakkari.context.embedder import embed
from ttakkari.models import CtxChunk, CtxDocument, CtxSource, utcnow

log = logging.getLogger(__name__)
CHUNK_CHARS = 1200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 5000
EMBED_BATCH = 32
TEXT_EXT = {".md", ".markdown", ".txt", ".org", ".rst"}
HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$")


def chunk_markdown(text: str) -> list[tuple[str | None, str]]:
    """제목(#~####) 단위로 자르되, 같은 상위 절 안의 짧은 하위 절은 한 조각으로 합친다. (제목 경로, 본문).

    작업 일지처럼 '## 시각 주제' 아래 '### 한줄 요약/작업 내용' 이 반복되는 문서에서, 짧은 하위 절이 따로 떨어지면
    비슷한 조각이 많아져 검색이 흐려진다. 긴 절은 문단 경계에서 다시 자른다.
    """
    sections: list[tuple[list[tuple[int, str]], str | None, list[str]]] = []
    trail: list[tuple[int, str]] = []
    cur: list[str] = []
    sub: str | None = None
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            if "".join(cur).strip():
                sections.append((list(trail), sub, cur))
            level = len(m.group(1))
            while trail and trail[-1][0] >= level:
                trail.pop()
            trail.append((level, m.group(2)))
            sub = None
            cur = []
        else:
            cur.append(line)
    if "".join(cur).strip():
        sections.append((list(trail), sub, cur))

    # 묶는 단위: 가장 얕은 제목 단계. 그 단계 제목이 하나뿐이면(문서 제목) 한 단계 아래까지 본다.
    levels = [lv for tr, _, _ in sections for lv, _ in tr]
    top = min(levels) if levels else 1
    titles_at_top = {t for tr, _, _ in sections for lv, t in tr if lv == top}
    depth = top + 1 if len(titles_at_top) == 1 else top
    merged: list[tuple[str | None, str]] = []
    group_key: tuple | None = None
    group_head: str | None = None
    buf = ""
    for tr, _sub, lines in sections:
        body = "\n".join(lines).strip()
        key = tuple(t for lv, t in tr if lv <= depth)
        head = " > ".join(key) or None
        deeper = " > ".join(t for lv, t in tr if lv > depth)
        piece = (f"[{deeper}]\n" if deeper else "") + body
        if key == group_key and len(buf) + len(piece) <= CHUNK_CHARS:
            buf += "\n\n" + piece
            continue
        if buf.strip():
            merged.append((group_head, buf.strip()))
        group_key, group_head, buf = key, head, piece
    if buf.strip():
        merged.append((group_head, buf.strip()))

    out: list[tuple[str | None, str]] = []
    for h, body in merged:
        if len(body) <= CHUNK_CHARS * 1.5:
            out.append((h, body))
            continue
        buf = ""
        for para in re.split(r"\n\s*\n", body):
            if buf and len(buf) + len(para) > CHUNK_CHARS:
                out.append((h, buf.strip()))
                buf = ""
            buf += para + "\n\n"
            while len(buf) > CHUNK_CHARS * 1.5:
                out.append((h, buf[:CHUNK_CHARS].strip()))
                buf = buf[CHUNK_CHARS:]
        if buf.strip():
            out.append((h, buf.strip()))
    return out


def _files(src: CtxSource) -> list[Path]:
    root = Path(src.path).expanduser()
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(f"폴더가 없어요: {root}")
    home = get_settings().home
    out = []
    for p in sorted(root.glob(src.pattern or "**/*.md")):
        if len(out) >= MAX_FILES:
            break
        hidden = any(part.startswith(".") for part in p.relative_to(root).parts)
        if p.is_file() and p.suffix.lower() in TEXT_EXT and not hidden and not is_sensitive(p) and not is_within(p.resolve(), home):
            out.append(p)
    return out


async def index_source(db: AsyncSession, src: CtxSource) -> dict[str, int]:
    stats = {"files": 0, "updated": 0, "chunks": 0, "removed": 0}
    try:
        files = await asyncio.to_thread(_files, src)
    except OSError as e:
        src.last_error = str(e)
        await db.commit()
        raise
    seen: set[str] = set()
    for f in files:
        stats["files"] += 1
        path = str(f.resolve())
        seen.add(path)
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        if len(raw) > MAX_FILE_BYTES:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        doc = await db.scalar(select(CtxDocument).where(CtxDocument.path == path))
        if doc is not None and doc.sha256 == sha:
            continue
        text = raw.decode("utf-8", errors="replace")
        pieces = chunk_markdown(text)
        title = next((h for h, _ in pieces if h), None) or f.stem
        if doc is None:
            doc = CtxDocument(source_id=src.id, path=path, title=title[:500], sha256=sha)
            db.add(doc)
            await db.flush()
        else:
            await db.execute(delete(CtxChunk).where(CtxChunk.document_id == doc.id))
            doc.sha256, doc.title = sha, title[:500]
        for i in range(0, len(pieces), EMBED_BATCH):
            batch = pieces[i:i + EMBED_BATCH]
            # 조각만으로는 무슨 문서인지 모를 수 있어 파일 이름·제목을 앞에 붙여 임베딩한다.
            vecs = await embed([f"{f.stem} {h or ''}\n{c}" for h, c in batch])
            for j, ((h, c), v) in enumerate(zip(batch, vecs, strict=True)):
                db.add(CtxChunk(document_id=doc.id, ord=i + j, heading=h, content=c, embedding=v))
        doc.chunk_count = len(pieces)
        doc.indexed_at = utcnow()
        stats["updated"] += 1
        stats["chunks"] += len(pieces)
        await db.commit()
    gone = await db.scalars(select(CtxDocument).where(CtxDocument.source_id == src.id, CtxDocument.path.not_in(seen or {""})))
    for d in gone:
        await db.delete(d)
        stats["removed"] += 1
    src.last_indexed_at = utcnow()
    src.last_error = None
    await db.commit()
    return stats


async def index_all(db: AsyncSession) -> int:
    n = 0
    for src in list(await db.scalars(select(CtxSource).where(CtxSource.enabled.is_(True)))):
        try:
            await index_source(db, src)
            n += 1
        except Exception:
            log.exception("indexing %s failed", src.path)
    return n
