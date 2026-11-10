"""수집 지식 도서관의 오염을 스스로 찾아 가리고, 기록해 두었다가 되돌린다.

수집은 계속 쓰레기를 섞는다(스팸 사이트, 같은 본문 반복, 주제 밖 문서, 틀 제목). 사람이 보고 고치는 대신 이 점검이
주기적으로 같은 기준으로 훑는다. 원칙:
- 지우지 않는다. 문서는 kb_document_access.hidden, 제목은 이전 값 보존, 엔터티는 archived 로만 바꾼다.
- 바꾼 행마다 collection_scope_ledger 에 이전 값을 남긴다(run_id 'kbrepair:...'). revert 가 이 기록으로 되돌린다.
- 주인이 되돌린 행은 다시 건드리지 않는다('repair_reverted').
- 기본은 dry-run 이다. 적용은 apply=True 일 때만 한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text

from ttakkari.collector import catalog, triage
from ttakkari.collector.control_models import CollectionScopeLedger
from ttakkari.kb_delivery import GENERIC_TITLES, document_title
from ttakkari.kb_pipeline.entity_filter import is_template_heading

RUN_PREFIX = "kbrepair:"
LOCK_KEY = "kb-repair"
THIN_CHARS = 500
OFF_TOPIC_BELOW = 0.06  # 어휘 점수는 문서 하나를 가리는 근거가 아니다(공식 문서도 0점이 나온다). 사이트 단위 판정의 신호로만 쓴다
HOST_MIN_DOCS = 30
HOST_FLAGGED_RATIO = 0.6
HOST_KEEP_RELEVANCE = 0.25
SUSPECT_MIN_DOCS = 20
SUSPECT_RATIO = 0.5
CTA_DENSITY = 0.5  # 1000자당 영업 문구 수
BATCH = 300
_LISTING = re.compile(r"(arxiv\.org/list|/category/|/author/|/archive|[?&]page=|/page/\d+/?$|tistory\.com/tag/)")
_CTA = re.compile(r"(?i)(book a (free )?(call|consultation)|get a free quote|request a (demo|quote)|hire (a|our|the top)|schedule a (call|demo)|freelance .{0,30} for hire)")


@dataclass
class Facts:
    doc_id: str
    title: str
    created_at: datetime
    url: str
    host: str
    chars: int
    text_hash: str
    cta: float
    relevance: float | None
    own_relevance: float | None  # 이 문서를 가져온 임무 지시문 하나에 대한 관련도(사이트 의심 신호용)
    mission_goal_title: str | None
    new_title: str | None
    reasons: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


def _goal_title(goal: str | None) -> str | None:
    if not goal:
        return None
    return goal.strip().splitlines()[0].lstrip("# ").strip()[:100] or None


def facts_for(row: dict[str, Any], goals: tuple[str, ...] = ()) -> Facts:
    """한 문서의 판정 재료. 순수 계산이라 스레드에서 돌려도 된다.

    관련도는 이 도서관의 어떤 임무 지시문에든 맞는지를 본다(가장 높은 값). 한 임무에 안 맞아도 다른 임무에 맞는
    문서는 도서관에 속한다. 비교할 수 있는 지시문이 하나도 없으면 None 이라 판정하지 않는다."""
    body, url, goal = row["body"], row["url"] or "", row["goal"]
    scores = [triage.calculate_relevance(g, row["title"], body)[0] for g in (*goals, *([goal] if goal and goal not in goals else []))
              if triage._comparable_goal_words(g, body) >= 5]
    relevance = max(scores) if scores else None
    own = triage.calculate_relevance(goal, row["title"], body)[0] if goal and triage._comparable_goal_words(goal, body) >= 5 else None
    goal_title = _goal_title(goal)
    title = row["title"]
    new_title = None
    if title.casefold() in GENERIC_TITLES or (goal_title and title.strip() == goal_title):
        candidate = document_title(None, title, url, body)
        new_title = candidate if candidate and candidate != title else None
    return Facts(
        doc_id=str(row["doc_id"]), title=title, created_at=row["created_at"], url=url,
        host=(urlsplit(url).hostname or "").lower().removeprefix("www."), chars=row["char_count"],
        text_hash=hashlib.sha256(body.encode()).hexdigest(), cta=len(_CTA.findall(body)) / max(1.0, len(body) / 1000),
        relevance=relevance, own_relevance=own, mission_goal_title=goal_title, new_title=new_title,
    )


def judge(docs: list[Facts], blocked: frozenset[str] = frozenset()) -> None:
    """각 문서의 reasons(숨길 이유)와 signals(사이트 판정에만 쓰는 약한 신호)를 채운다.

    약한 신호는 한 문서를 가리지 못한다. 한 사이트 문서의 대부분(HOST_FLAGGED_RATIO)이 신호를 보일 때만 그 사이트가 스팸이다."""
    for d in docs:
        if d.host in blocked:
            d.reasons.append("blocked_host")
        if d.chars < THIN_CHARS:
            d.reasons.append("thin")
        if _LISTING.search(d.url):
            d.reasons.append("listing_page")
        # 주인이 지시문에 이름 붙인 공식 사이트(카탈로그)는 어휘 점수가 낮아도 가리지 않는다.
        if d.relevance is not None and d.relevance < OFF_TOPIC_BELOW and catalog.target_for_host(d.host) is None:
            d.signals.append("off_topic")
        if d.cta >= CTA_DENSITY:
            d.signals.append("commercial")
    by_host: dict[str, list[Facts]] = defaultdict(list)
    for d in docs:
        if d.host:
            by_host[d.host].append(d)
    for host, host_docs in by_host.items():
        if len(host_docs) < HOST_MIN_DOCS or catalog.target_for_host(host) is not None:
            continue
        flagged = [d for d in host_docs if d.signals]
        if len(flagged) / len(host_docs) >= HOST_FLAGGED_RATIO:
            for d in host_docs:
                if not d.reasons and (d.relevance is None or d.relevance < HOST_KEEP_RELEVANCE):
                    d.reasons.append("spam_host")
    by_hash: dict[str, list[Facts]] = defaultdict(list)
    for d in docs:
        by_hash[d.text_hash].append(d)
    for group in by_hash.values():
        if len(group) > 1:
            keeper = min(group, key=lambda d: (bool(d.reasons), d.created_at, d.doc_id))
            for d in group:
                if d is not keeper and "duplicate" not in d.reasons:
                    d.reasons.append("duplicate")


BLOCKED_KEY = "kb_repair_blocked_hosts"


async def blocked_hosts(db) -> frozenset[str]:
    row = (await db.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": BLOCKED_KEY})).first()
    return frozenset(row[0].get("hosts", [])) if row else frozenset()


async def set_host_blocked(db, host: str, blocked: bool) -> list[str]:
    """주인이 사이트를 막거나 푼다. 막은 사이트의 문서는 다음 점검부터 계속 가려진다(되돌리기 쉬운 가림)."""
    host = host.strip().lower().removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9.-]{3,253}", host) or "." not in host:
        raise ValueError("invalid_host")
    hosts = set(await blocked_hosts(db))
    hosts.add(host) if blocked else hosts.discard(host)
    await db.execute(text("INSERT INTO app_settings (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), now()) "
                          "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
                     {"k": BLOCKED_KEY, "v": __import__("json").dumps({"hosts": sorted(hosts)})})
    if not blocked:
        await db.execute(text("""UPDATE kb_document_access SET hidden = false WHERE hidden AND document_id IN (
            SELECT d.id FROM kb_documents d JOIN collection_scope_ledger l ON l.row_key = d.id::text AND l.action = 'repair_hide'
            WHERE l.after->'reasons' ? 'blocked_host' AND l.after->>'host' = :h)"""), {"h": host})
    return sorted(hosts)


def suspect_hosts(docs: list[Facts], blocked: frozenset[str]) -> list[dict[str, Any]]:
    """자동으로 가리지 않고 주인에게 보여 주는 의심 사이트: 문서 대부분이 가져온 임무와 무관하거나 영업 문구가 많다."""
    by_host: dict[str, list[Facts]] = defaultdict(list)
    for d in docs:
        if d.host and d.host not in blocked and catalog.target_for_host(d.host) is None:
            by_host[d.host].append(d)
    out = []
    for host, group in by_host.items():
        weak = [d for d in group if (d.own_relevance is not None and d.own_relevance < OFF_TOPIC_BELOW) or d.cta >= CTA_DENSITY]
        if len(group) >= SUSPECT_MIN_DOCS and len(weak) / len(group) >= SUSPECT_RATIO:
            out.append({"host": host, "documents": len(group), "weak_ratio": round(len(weak) / len(group), 2),
                        "sample": [d.title[:60] for d in group[:3]]})
    return sorted(out, key=lambda r: -r["documents"])[:20]


_FETCH = text("""
SELECT d.id AS doc_id, d.title, d.created_at, r.char_count, r.body, s.locator AS url, m.goal
FROM kb_documents d
JOIN kb_document_access a ON a.document_id = d.id AND NOT a.hidden
JOIN LATERAL (SELECT * FROM kb_revisions WHERE document_id = d.id ORDER BY rev_no DESC LIMIT 1) r ON TRUE
JOIN kb_sources s ON s.id = r.source_id AND s.kind = 'web' AND s.origin_meta->>'via' = 'collector'
LEFT JOIN LATERAL (
    SELECT cm.goal FROM collector_items i
    JOIN collection_mission_jobs j ON j.job_id = i.job_id
    JOIN collection_missions cm ON cm.id = j.mission_id
    WHERE i.id = (s.origin_meta->>'item_id')::uuid LIMIT 1
) m ON TRUE
WHERE d.id > :last AND NOT (d.id = ANY(:skip))
ORDER BY d.id LIMIT :n
""")


async def _goals(db) -> tuple[str, ...]:
    rows = await db.execute(text("SELECT DISTINCT goal FROM collection_missions"))
    return tuple(r[0] for r in rows if r[0])


async def _collect(db, skip: list[uuid.UUID]) -> list[Facts]:
    out: list[Facts] = []
    goals = await _goals(db)
    last = uuid.UUID(int=0)
    while True:
        rows = (await db.execute(_FETCH, {"last": last, "skip": skip, "n": BATCH})).mappings().all()
        if not rows:
            return out
        last = rows[-1]["doc_id"]
        out.extend(await asyncio.to_thread(lambda batch=rows: [facts_for(dict(r), goals) for r in batch]))


async def _reverted_keys(db) -> set[str]:
    rows = await db.execute(text("SELECT row_key FROM collection_scope_ledger WHERE action = 'repair_reverted'"))
    return {r[0] for r in rows}


async def scan(db, *, apply: bool = False) -> dict[str, Any]:
    """오염을 찾아 보고한다. apply=True 면 같은 트랜잭션에서 가리고 기록한다(commit 은 호출자)."""
    got = (await db.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": LOCK_KEY})).scalar()
    if not got:
        return {"skipped": "another_repair_running"}
    run_id = RUN_PREFIX + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    reverted = await _reverted_keys(db)
    docs = await _collect(db, [uuid.UUID(k) for k in reverted if _is_uuid(k)])
    blocked = await blocked_hosts(db)
    judge(docs, blocked)
    hide = [d for d in docs if d.reasons]
    retitle = [d for d in docs if not d.reasons and d.new_title]
    entities = await _template_entities(db, reverted)
    report: dict[str, Any] = {
        "run_id": run_id, "applied": apply, "scanned_documents": len(docs),
        "hide": {"total": len(hide), "by_reason": dict(Counter(r for d in hide for r in d.reasons[:1])),
                 "spam_hosts": sorted({d.host for d in hide if "spam_host" in d.reasons}),
                 "samples": {r: [d.title[:80] for d in hide if d.reasons[0] == r][:5] for r in {d.reasons[0] for d in hide}}},
        "suspect_hosts": suspect_hosts(docs, blocked),
        "retitle": {"total": len(retitle), "samples": [f"{d.title[:40]} -> {d.new_title[:60]}" for d in retitle[:5]]},
        "archive_entities": {"total": len(entities), "samples": [n for _, n in entities[:8]]},
    }
    if not apply:
        return report
    for d in hide:
        db.add(_ledger(run_id, "kb_document_access", d.doc_id, "repair_hide", {"hidden": False}, {"hidden": True, "reasons": d.reasons, "host": d.host}))
    for d in retitle:
        db.add(_ledger(run_id, "kb_documents", d.doc_id, "repair_title", {"title": d.title}, {"title": d.new_title[:500]}))
    for eid, name in entities:
        db.add(_ledger(run_id, "kb_entities", eid, "repair_entity_archive", {"status": "active", "name": name}, {"status": "archived"}))
    await db.flush()
    if hide:
        await db.execute(text("UPDATE kb_document_access SET hidden = true WHERE document_id = ANY(:ids) AND NOT hidden"),
                         {"ids": [uuid.UUID(d.doc_id) for d in hide]})
    for d in retitle:
        await db.execute(text("UPDATE kb_documents SET title = :t, updated_at = now() WHERE id = :i AND title = :o"),
                         {"t": d.new_title[:500], "i": uuid.UUID(d.doc_id), "o": d.title})
    if entities:
        await db.execute(text("UPDATE kb_entities SET status = 'archived', updated_at = now() WHERE id = ANY(:ids) AND status = 'active'"),
                         {"ids": [uuid.UUID(e) for e, _ in entities]})
    return report


async def _template_entities(db, reverted: set[str]) -> list[tuple[str, str]]:
    rows = await db.execute(text("SELECT id::text, canonical_name FROM kb_entities WHERE status = 'active' AND entity_kind = 'concept'"))
    return [(i, n) for i, n in rows if i not in reverted and is_template_heading(n)]


async def revert(db, run_id: str) -> dict[str, Any]:
    """한 점검이 바꾼 행을 이전 값으로 되돌리고, 다음 점검이 다시 건드리지 않게 표시한다."""
    if not run_id.startswith(RUN_PREFIX):
        raise ValueError("not_a_repair_run")
    rows = (await db.execute(text(
        "SELECT action, row_key, before, after FROM collection_scope_ledger WHERE run_id = :r"), {"r": run_id})).mappings().all()
    if not rows:
        raise LookupError("repair_run_not_found")
    done: Counter[str] = Counter()
    for row in rows:
        key = uuid.UUID(row["row_key"])
        if row["action"] == "repair_hide":
            n = (await db.execute(text("UPDATE kb_document_access SET hidden = false WHERE document_id = :i AND hidden"), {"i": key})).rowcount
        elif row["action"] == "repair_title":
            n = (await db.execute(text("UPDATE kb_documents SET title = :o, updated_at = now() WHERE id = :i AND title = :n"),
                                  {"o": row["before"]["title"], "i": key, "n": row["after"]["title"]})).rowcount
        elif row["action"] == "repair_entity_archive":
            n = (await db.execute(text("UPDATE kb_entities SET status = 'active', updated_at = now() WHERE id = :i AND status = 'archived'"), {"i": key})).rowcount
        else:
            continue
        done[row["action"]] += n
        db.add(_ledger(RUN_PREFIX + "revert:" + run_id[len(RUN_PREFIX):], "ledger", str(key), "repair_reverted", {"run_id": run_id}, {}))
    return {"run_id": run_id, "restored": dict(done)}


async def runs(db, limit: int = 20) -> list[dict[str, Any]]:
    rows = await db.execute(text("""
        SELECT run_id, min(created_at) AS at, count(*) AS rows, jsonb_object_agg(action, n) AS actions
        FROM (SELECT run_id, action, created_at, count(*) OVER (PARTITION BY run_id, action) AS n FROM collection_scope_ledger
              WHERE run_id LIKE 'kbrepair:%') t
        GROUP BY run_id ORDER BY min(created_at) DESC LIMIT :n"""), {"n": limit})
    return [{"run_id": r[0], "at": r[1].isoformat(), "rows": r[2], "actions": r[3]} for r in rows]


def _ledger(run_id: str, table: str, row_key: str, action: str, before: dict, after: dict) -> CollectionScopeLedger:
    return CollectionScopeLedger(run_id=run_id[:64], table_name=table, row_key=row_key[:128], action=action, before=before, after=after)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
