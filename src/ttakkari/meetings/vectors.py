"""회의록 → 맥락 도서관 벡터 색인(ADR 0021).

회의 한 건을 요약·결정·액션·쟁점·논의·전사 구간 조각으로 자르고 임베딩해 meeting_chunks 에 둔다. 정본은 meetings·meeting_items 이고
조각은 언제든 지우고 다시 만든다. 검색은 ADR 0009 의 어휘 점수에 벡터 유사도를 더한다(어휘로 못 찾는 다른 표현을 보완).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context import embedder
from ttakkari.context.search import lexical, scope_clause, terms
from ttakkari.db import sessionmaker
from ttakkari.models import CtxEntity, Meeting, MeetingChunk, MeetingItem, utcnow

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
WINDOW = 700  # 전사 조각 길이(글자). e5 입력 한도(512 토큰) 안쪽
OVERLAP = 120
BATCH = 32
# e5 코사인 유사도는 관련 없는 문장도 0.75 안팎이라, 이 값을 넘는 만큼만 점수로 친다(10-13 실측: 관련 0.81~0.84, 무관 0.76).
SIM_FLOOR = 0.78
SIM_ONLY_MIN = 0.80  # 어휘로는 안 걸리고 벡터로만 걸린 조각을 결과에 넣는 최소 유사도
KIND_LABEL = {"summary": "요약", "decision": "결정", "action": "액션", "open_issue": "미해결 쟁점", "discussion": "논의",
              "transcript": "전사"}

_pending: dict[uuid.UUID, asyncio.Task] = {}
_lock = asyncio.Lock()  # 모델 하나를 여러 회의가 동시에 돌리지 않게


@dataclass
class Piece:
    kind: str
    ord: int
    heading: str | None
    content: str


def _when(m: Meeting) -> str:
    return (m.held_at or m.created_at).astimezone(KST).strftime("%Y-%m-%d")


def split_transcript(text: str) -> list[str]:
    """줄 경계를 살려 WINDOW 글자 안팎으로 자른다. 앞 조각 끝을 OVERLAP 만큼 겹쳐 문맥이 끊기지 않게 한다."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: list[str] = []
    cur = ""
    for ln in lines:
        while len(ln) > WINDOW:  # 줄바꿈 없는 긴 전사
            head, ln = ln[:WINDOW], ln[WINDOW - OVERLAP:]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
        if cur and len(cur) + len(ln) + 1 > WINDOW:
            out.append(cur)
            cur = cur[-OVERLAP:].split("\n", 1)[-1] if "\n" in cur[-OVERLAP:] else ""
        cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


def build_pieces(m: Meeting, items: list[MeetingItem], project: str | None) -> list[Piece]:
    """조각마다 회의 일자·제목·프로젝트를 앞에 붙여 혼자 읽혀도(검색 결과로 떠도) 출처를 알게 한다."""
    head = f"[회의 {_when(m)} '{m.title}'" + (f" · {project}" if project else "") + "]"
    out: list[Piece] = []
    if m.summary:
        out.append(Piece("summary", 0, "요약", f"{head} 요약: {m.summary}"))
    for it in sorted(items, key=lambda i: (i.kind, i.position)):
        extra = " ".join(x for x in (
            f"(담당 {it.owner})" if it.owner else "", f"(기한 {it.due})" if it.due else "",
            "(완료)" if it.status == "done" else "(취소)" if it.status == "cancelled" else "") if x)
        body = it.text + (f" — {it.detail}" if it.detail else "")
        out.append(Piece(it.kind, it.position, KIND_LABEL.get(it.kind), f"{head} {KIND_LABEL.get(it.kind, it.kind)}: {body} {extra}".strip()))
    if m.transcript:
        parts = split_transcript(m.transcript)
        for i, p in enumerate(parts):
            out.append(Piece("transcript", i, f"전사 {i + 1}/{len(parts)}", f"{head} 전사 {i + 1}/{len(parts)}\n{p}"))
    return out


async def reindex(meeting_id: uuid.UUID) -> None:
    """회의 조각을 새로 만든다. 임베딩은 스레드에서 돌고, 끝나면 한 트랜잭션으로 교체한다(검색이 반쯤 빈 상태를 보지 않게)."""
    async with sessionmaker()() as db:
        m = await db.get(Meeting, meeting_id)
        if m is None:
            return
        items = list(await db.scalars(select(MeetingItem).where(MeetingItem.meeting_id == m.id)))
        project = await db.get(CtxEntity, m.entity_id) if m.entity_id else None
        pieces = build_pieces(m, items, project.name if project else None)
        scope, entity_id, stamp = m.scope, m.entity_id, m.updated_at
        m.vector_status, m.vector_error = "pending", None
        await db.commit()
    vectors: list[list[float] | None] = [None] * len(pieces)
    model = None
    try:
        if embedder.enabled() and pieces:
            async with _lock:
                done: list[list[float]] = []
                for i in range(0, len(pieces), BATCH):
                    done += await embedder.embed([p.content for p in pieces[i:i + BATCH]])
            vectors, model = list(done), embedder.model_name()
    except Exception as e:
        log.exception("meeting %s embedding failed", meeting_id)
        async with sessionmaker()() as db:
            await db.execute(update(Meeting).where(Meeting.id == meeting_id).values(
                vector_status="failed", vector_error=f"임베딩 실패: {type(e).__name__}"[:500]))
            await db.commit()
        return
    async with sessionmaker()() as db:
        m = await db.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
        if m is None:
            return
        await db.execute(delete(MeetingChunk).where(MeetingChunk.meeting_id == meeting_id))
        db.add_all(MeetingChunk(meeting_id=meeting_id, kind=p.kind, ord=p.ord, heading=p.heading, content=p.content,
                                scope=scope, entity_id=entity_id, embedding=v, model=model)
                   for p, v in zip(pieces, vectors, strict=True))
        m.vector_status, m.vector_chunks, m.vector_error, m.embedded_at = "ready", len(pieces), None, utcnow()
        m.updated_at = stamp  # 색인은 회의 내용 변경이 아니다
        await db.commit()
    log.info("meeting %s indexed: %d chunks (%s)", meeting_id, len(pieces), model or "lexical only")


def schedule(meeting_id: uuid.UUID, delay: float = 1.0) -> None:
    """변경 직후 여러 번 불려도(전사 저장 → 분석 완료 → 프로젝트 변경) 마지막 한 번만 돈다."""
    old = _pending.pop(meeting_id, None)
    if old is not None and not old.done():
        old.cancel()

    async def _go() -> None:
        try:
            await asyncio.sleep(delay)
            await reindex(meeting_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("meeting %s reindex crashed", meeting_id)
        finally:
            if _pending.get(meeting_id) is task:
                _pending.pop(meeting_id, None)

    task = asyncio.create_task(_go())
    _pending[meeting_id] = task


async def resume_pending() -> int:
    """서버가 색인 도중 재시작했거나, 벡터가 아직 없는 회의를 다시 색인한다."""
    async with sessionmaker()() as db:
        ids = list(await db.scalars(select(Meeting.id).where(
            Meeting.vector_status.in_(("pending", "none")), or_(Meeting.transcript.is_not(None), Meeting.summary.is_not(None)))))
    for i in ids:
        schedule(i, delay=2.0)
    return len(ids)


# ---- 검색

@dataclass
class ChunkHit:
    chunk: MeetingChunk
    meeting: Meeting
    score: float
    similarity: float | None


async def search(db: AsyncSession, query: str, k: int = 8, *, scope: str = "all", entity_id: uuid.UUID | None = None,
                 kinds: tuple[str, ...] | None = None, meeting_id: uuid.UUID | None = None) -> list[ChunkHit]:
    """어휘(ADR 0009) + 벡터 혼합. 회의당 최대 3조각."""
    query = query.strip()
    words = terms(query)
    if not query:
        return []
    k = min(max(k, 1), 30)
    pool = max(k * 5, 30)
    base = [scope_clause(MeetingChunk.scope, scope)]
    if entity_id:
        base.append(MeetingChunk.entity_id == entity_id)
    if kinds:
        base.append(MeetingChunk.kind.in_(kinds))
    if meeting_id:
        base.append(MeetingChunk.meeting_id == meeting_id)
    sims: dict[uuid.UUID, float] = {}
    qvec = None
    if embedder.enabled():
        try:
            qvec = (await embedder.embed([query], query=True))[0]
        except Exception:
            log.exception("query embedding failed; lexical only")
    if qvec is not None:
        dist = MeetingChunk.embedding.cosine_distance(qvec)
        for cid, d in (await db.execute(select(MeetingChunk.id, dist).where(*base, MeetingChunk.embedding.is_not(None))
                                        .order_by(dist).limit(pool))).all():
            sims[cid] = 1.0 - float(d)
    score_lex = None
    lex_ids: dict[uuid.UUID, float] = {}
    if words:
        pred, score_lex = lexical(MeetingChunk.content, query, words)
        for cid, sc in (await db.execute(select(MeetingChunk.id, score_lex).where(*base, pred)
                                         .order_by(score_lex.desc()).limit(pool))).all():
            lex_ids[cid] = float(sc)
    ids = set(lex_ids) | {cid for cid, s in sims.items() if s >= SIM_ONLY_MIN}
    if not ids:
        return []
    if qvec is not None:
        missing = [cid for cid in ids if cid not in sims]
        if missing:
            dist = MeetingChunk.embedding.cosine_distance(qvec)
            for cid, d in (await db.execute(select(MeetingChunk.id, dist).where(MeetingChunk.id.in_(missing),
                                                                               MeetingChunk.embedding.is_not(None)))).all():
                sims[cid] = 1.0 - float(d)
    rows = (await db.execute(select(MeetingChunk, Meeting).join(Meeting, Meeting.id == MeetingChunk.meeting_id)
                             .where(MeetingChunk.id.in_(ids)))).all()
    hits = []
    for c, m in rows:
        sim = sims.get(c.id)
        vec = max(0.0, (sim or 0.0) - SIM_FLOOR) * 8.0
        # 전사 구간보다 정리된 결정·액션·요약을 조금 앞에 둔다.
        bonus = 0.0 if c.kind == "transcript" else 0.15
        hits.append(ChunkHit(c, m, lex_ids.get(c.id, 0.0) + vec + bonus, sim))
    hits.sort(key=lambda h: h.score, reverse=True)
    per: dict[uuid.UUID, int] = {}
    out = []
    for h in hits:
        if per.get(h.meeting.id, 0) >= 3:
            continue
        per[h.meeting.id] = per.get(h.meeting.id, 0) + 1
        out.append(h)
        if len(out) == k:
            break
    return out


def hit_out(h: ChunkHit) -> dict:
    m = h.meeting
    return {"meeting_id": str(m.id), "meeting_title": m.title, "held_at": (m.held_at or m.created_at).isoformat(),
            "scope": m.scope, "kind": h.chunk.kind, "heading": h.chunk.heading, "text": h.chunk.content[:1200],
            "score": round(h.score, 3), "similarity": round(h.similarity, 3) if h.similarity is not None else None}


async def stats(db: AsyncSession, *, scope: str = "all") -> dict[str, int]:
    meetings, chunks, vectors = (await db.execute(select(
        func.count(func.distinct(MeetingChunk.meeting_id)), func.count(MeetingChunk.id),
        func.sum(case((MeetingChunk.embedding.is_not(None), 1), else_=0))).where(scope_clause(MeetingChunk.scope, scope)))).one()
    return {"meetings": meetings or 0, "chunks": chunks or 0, "vectors": int(vectors or 0)}
