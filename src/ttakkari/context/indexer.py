"""Model-free RAG ingestion. Preserve heading context and reindex changed files only."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.broker.policy import is_sensitive, is_within
from ttakkari.config import get_settings
from ttakkari.knowledge.store import import_text
from ttakkari.knowledge_models import KnowledgeRevision
from ttakkari.models import CtxChunk, CtxDocument, CtxSource, utcnow

log = logging.getLogger(__name__)
CHUNK_CHARS = 1200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 5000
EXCLUDED_DIRS = {"node_modules", "vendor", "target", "dist", "build", "coverage", "__pycache__", "venv"}
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
    root = Path(src.path).expanduser().resolve()
    home = get_settings().home
    if is_sensitive(root) or is_within(root, home):
        raise ValueError("민감 경로는 색인할 수 없어요.")
    if root.is_file():
        if root.suffix.lower() not in TEXT_EXT:
            raise ValueError("텍스트 문서만 색인할 수 있어요.")
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(f"폴더가 없어요: {root}")
    out = []
    pattern = src.pattern or "**/*.md"
    def scan_error(error: OSError) -> None:
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=scan_error):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in EXCLUDED_DIRS
                         and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            p = Path(directory) / name
            rel = p.relative_to(root)
            matches = rel.match(pattern) or (pattern.startswith("**/") and rel.match(pattern[3:]))
            if (name.startswith(".") or not matches or p.suffix.lower() not in TEXT_EXT
                    or p.is_symlink() or not is_within(p.resolve(), root) or is_sensitive(p)):
                continue
            out.append(p)
            if len(out) > MAX_FILES:
                raise ValueError(f"색인 파일 상한 {MAX_FILES}개를 넘었어요. 출처를 나눠 주세요.")
    return out


async def index_source(db: AsyncSession, src: CtxSource) -> dict[str, int]:
    stats = {"files": 0, "updated": 0, "chunks": 0, "removed": 0, "errors": 0}
    try:
        files = await asyncio.to_thread(_files, src)
    except (OSError, ValueError) as e:
        src.last_error = str(e)
        await db.commit()
        raise
    seen: set[str] = set()
    failures: list[str] = []
    for f in files:
        stats["files"] += 1
        path = str(f.resolve())
        seen.add(path)
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"파일 크기 상한 {MAX_FILE_BYTES}바이트 초과")
            raw = f.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError(f"파일 크기 상한 {MAX_FILE_BYTES}바이트 초과")
            text = raw.decode("utf-8")
            if "\0" in text:
                raise ValueError("unsupported_nul_in_source")
        except (OSError, ValueError) as exc:
            stats["errors"] += 1
            failures.append(f"{f}: {exc}")
            log.warning("context index skipped %s: %s", f, exc)
            continue
        sha = hashlib.sha256(raw).hexdigest()
        doc = await db.scalar(select(CtxDocument).where(CtxDocument.path == path))
        if doc is not None and doc.source_id != src.id:
            stats["errors"] += 1
            failures.append(f"{f}: 다른 출처에서 이미 색인된 문서")
            continue
        if doc is not None and doc.sha256 == sha:
            await import_text(db, source_key=f'indexed-document:{doc.id}', body=text,
                              scope=src.scope, source_id=src.id, locator=path,
                              sensitivity='sensitive' if src.category == 'health' else 'normal',
                              origin='indexed_document')
            await db.commit()
            continue
        pieces = chunk_markdown(text)
        title = next((h for h, _ in pieces if h), None) or f.stem
        if doc is None:
            doc = CtxDocument(source_id=src.id, path=path, title=title[:500], sha256=sha)
            db.add(doc)
            await db.flush()
        else:
            await db.execute(delete(CtxChunk).where(CtxChunk.document_id == doc.id))
            doc.sha256, doc.title = sha, title[:500]
        for i, (h, c) in enumerate(pieces):
            db.add(CtxChunk(document_id=doc.id, ord=i, heading=h, content=c))
        await import_text(db, source_key=f'indexed-document:{doc.id}', body=text,
                          scope=src.scope, source_id=src.id, locator=path,
                          sensitivity='sensitive' if src.category == 'health' else 'normal',
                          origin='indexed_document')
        doc.chunk_count = len(pieces)
        doc.indexed_at = utcnow()
        stats["updated"] += 1
        stats["chunks"] += len(pieces)
        await db.commit()
    gone = await db.scalars(select(CtxDocument).where(CtxDocument.source_id == src.id, CtxDocument.path.not_in(seen or {""})))
    for d in gone:
        await db.execute(update(KnowledgeRevision).where(
            KnowledgeRevision.source_key == f'indexed-document:{d.id}').values(active=False))
        await db.delete(d)
        stats["removed"] += 1
    src.last_indexed_at = utcnow()
    src.last_error = "\n".join(failures)[:4000] or None
    await db.commit()
    return stats


async def index_all(db: AsyncSession) -> int:
    from ttakkari.context import index_queue
    n = 0
    for src in list(await db.scalars(select(CtxSource).where(CtxSource.enabled.is_(True)))):
        # 수동 색인 소비자가 이미 돌리는 출처는 건너뛴다. 같은 출처를 두 곳에서 동시에 쓰지 않는다.
        sid = src.id
        if not await index_queue.claim(sid):
            continue
        try:
            await index_source(db, src)
            n += 1
        except Exception:
            log.exception("indexing %s failed", src.path)
        finally:
            await index_queue.release(sid)
    return n
