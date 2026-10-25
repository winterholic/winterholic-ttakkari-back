"""수집 플랫폼·지식 도서관의 personal/work 구분을 단일 도서관으로 합친다(C2). 멱등이며 dry-run 은 같은 경로를 실행하고 되돌린다.

원칙
- 원본 이력·리비전·패시지·근거·관측은 지우지 않고 옮기지도 않는다(kb_* 는 불변 트리거가 막는다).
- 바뀐 행은 collection_scope_ledger 에 이전 값을 남긴다. 대량 scope 정규화는 표별 요약 한 줄이며, 되돌릴 근거는 pool 이다
  (work 세션 = 옛 work scope 이므로 alembic 0073 downgrade 가 pool 로 scope 를 복원한다).
- 접근 경계는 약해지지 않는다. 옛 work 행은 pool=work 로 보존하고, 한 출처에 두 쪽 수집이 겹치면 좁은 쪽(personal)을 택한다.
- 충돌 규칙은 각 함수 docstring 에 있다. 해소할 수 없는 충돌은 바꾸지 않고 report 의 unresolved 로 남긴다.
  새 코드는 병합 전후 어느 상태에서도 같은 결과를 읽으므로(collector.library.in_library) 미해소 행은 기능을 막지 않는다.
이전 세대(0072)가 아직 쓰는 중이면 실행하지 않는다. 그 세대가 병합 뒤에 옛 scope 행을 만들 수 있기 때문이다.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select, text

from ttakkari.collector.control_models import (
    CollectionScopeLedger,
    CollectorSource,
    KbDocumentAccess,
    KbDocumentAlias,
)
from ttakkari.collector.library import (
    LIBRARY_SCOPE,
    collector_doc_keys,
    effective_pool,
    legacy_work_upload_key,
    narrowest_pool,
)
from ttakkari.collector_models import CollectorFetchCache, CollectorSourcePolicy

LOCK_KEY = "collection-scope-merge"
REQUEST_KEY_MAX = 256


def _ledger(db, run_id: str, table: str, row_key: str, action: str, before: dict | None = None, after: dict | None = None):
    db.add(CollectionScopeLedger(run_id=run_id, table_name=table, row_key=row_key[:128], action=action,
                                 before=before or {}, after=after or {}))


async def _count(db, sql: str, **params) -> int:
    return int((await db.execute(text(sql), params)).scalar() or 0)


async def merge_scopes(db, *, run_id: str | None = None) -> dict[str, Any]:
    """트랜잭션 안에서 합친다. 호출자가 commit(적용) 또는 rollback(dry-run) 한다."""
    run_id = run_id or uuid.uuid4().hex
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": LOCK_KEY})
    report: dict[str, Any] = {"run_id": run_id, "tables": {}, "unresolved": [], "notes": {}}
    report["notes"]["cross_scope_duplicate_text_groups"] = await _count(db, """
        SELECT count(*) FROM (SELECT tenant_id, text_sha256 FROM collector_items
          WHERE text_sha256 IS NOT NULL GROUP BY tenant_id, text_sha256 HAVING count(DISTINCT scope) > 1) g""")
    await _merge_jobs(db, run_id, report)
    await _merge_outbox(db, run_id, report)
    await _merge_sources(db, run_id, report)
    await _merge_policies(db, run_id, report)
    await _merge_fetch_cache(db, run_id, report)
    await _normalize_simple(db, run_id, report, "collector_items")
    await _normalize_simple(db, run_id, report, "collector_dedupe_bands")
    await _merge_documents(db, run_id, report)
    report["notes"]["quality_models_retained"] = await _count(
        db, "SELECT count(*) FROM collector_quality_models WHERE scope <> :lib", lib=LIBRARY_SCOPE)
    report["notes"]["policy_rows_retained_with_legacy_scope"] = await _count(
        db, "SELECT count(*) FROM collector_source_policies WHERE scope <> :lib", lib=LIBRARY_SCOPE)
    return report


async def _normalize_simple(db, run_id: str, report: dict, table: str) -> None:
    """scope 외에 제약이 없는 표. 값만 단일 값으로 바꾼다."""
    moved = (await db.execute(text(f"UPDATE {table} SET scope = :lib WHERE scope <> :lib"), {"lib": LIBRARY_SCOPE})).rowcount
    report["tables"][table] = {"normalized": moved}
    if moved:
        _ledger(db, run_id, table, "*", "scope_normalized", {"scope": "<legacy>"}, {"scope": LIBRARY_SCOPE, "rows": moved})


async def _merge_jobs(db, run_id: str, report: dict) -> None:
    """같은 (tenant, source_key, request_key) job 이 두 scope 에 있으면 옛 쪽의 멱등 키에 접미사를 붙여 둘 다 보존한다.

    job 은 items·outbox·mission 이 참조하므로 합치거나 지우지 않는다. 새 키 요청은 canonical 로 재생(replay)된다.
    """
    twin = """EXISTS (SELECT 1 FROM collector_jobs p WHERE p.tenant_id = w.tenant_id AND p.source_key = w.source_key
                AND p.request_key = w.request_key AND p.id <> w.id AND (p.scope = :lib OR p.id < w.id))"""
    rows = (await db.execute(text(f"""SELECT w.id, w.scope, w.request_key FROM collector_jobs w
            WHERE w.scope <> :lib AND {twin} ORDER BY w.id"""), {"lib": LIBRARY_SCOPE})).all()
    for job_id, scope, request_key in rows:
        suffix = f"~merged:{scope}:{str(job_id).replace('-', '')[:8]}"
        renamed = request_key[:REQUEST_KEY_MAX - len(suffix)] + suffix
        await db.execute(text("UPDATE collector_jobs SET request_key = :rk WHERE id = :id"), {"rk": renamed, "id": job_id})
        _ledger(db, run_id, "collector_jobs", str(job_id), "request_key_renamed",
                {"request_key": request_key, "scope": scope}, {"request_key": renamed})
    moved = (await db.execute(text("""UPDATE collector_jobs SET pool = CASE WHEN scope = 'work' THEN 'work' ELSE pool END,
            scope = :lib WHERE scope <> :lib"""), {"lib": LIBRARY_SCOPE})).rowcount
    report["tables"]["collector_jobs"] = {"normalized": moved, "request_keys_renamed": len(rows)}
    if moved:
        _ledger(db, run_id, "collector_jobs", "*", "scope_normalized", {"scope": "<legacy>"},
                {"scope": LIBRARY_SCOPE, "rows": moved})


async def _merge_outbox(db, run_id: str, report: dict) -> None:
    """(tenant, sink, envelope_id) 가 두 scope 에 있는 outbox 는 envelope hash 에 묶여 있어 키를 바꿀 수 없다.
    그 행은 옛 값 그대로 두고 unresolved 로 보고한다(드물다)."""
    twin = """EXISTS (SELECT 1 FROM collector_outbox p WHERE p.tenant_id = o.tenant_id AND p.sink = o.sink
                AND p.envelope_id = o.envelope_id AND p.id <> o.id AND (p.scope = :lib OR p.id < o.id))"""
    blocked = (await db.execute(text(f"SELECT o.id FROM collector_outbox o WHERE o.scope <> :lib AND {twin}"),
                                {"lib": LIBRARY_SCOPE})).scalars().all()
    for outbox_id in blocked:
        report["unresolved"].append({"table": "collector_outbox", "row": str(outbox_id), "reason": "envelope_in_two_scopes"})
    moved = (await db.execute(text(f"""UPDATE collector_outbox o SET scope = :lib WHERE o.scope <> :lib AND NOT {twin}"""),
                              {"lib": LIBRARY_SCOPE})).rowcount
    report["tables"]["collector_outbox"] = {"normalized": moved, "blocked": len(blocked)}
    if moved:
        _ledger(db, run_id, "collector_outbox", "*", "scope_normalized", {"scope": "<legacy>"},
                {"scope": LIBRARY_SCOPE, "rows": moved})


async def _merge_fetch_cache(db, run_id: str, report: dict) -> None:
    """같은 URL 의 조건부 요청 검증값이 두 scope 에 있으면 더 최근에 가져온 행 하나만 남긴다.

    이 표는 증거가 아니라 HTTP 검증값 cache 다. 진 행은 ledger 에 전체 값을 남기고 지운다."""
    groups = (await db.execute(text("""SELECT tenant_id, source_key, url_key FROM collector_fetch_cache
            GROUP BY tenant_id, source_key, url_key HAVING count(*) > 1"""))).all()
    dropped = 0
    for tenant_id, source_key, url_key in groups:
        rows = list((await db.execute(text("""SELECT id FROM collector_fetch_cache WHERE tenant_id=:t AND source_key=:s
                AND url_key=:u ORDER BY last_fetched_at DESC, id DESC"""),
                {"t": tenant_id, "s": source_key, "u": url_key})).scalars())
        for loser_id in rows[1:]:
            loser = await db.get(CollectorFetchCache, loser_id)
            _ledger(db, run_id, "collector_fetch_cache", str(loser_id), "duplicate_dropped", {
                "tenant_id": loser.tenant_id, "scope": loser.scope, "source_key": loser.source_key,
                "url_key": loser.url_key, "url": loser.url, "etag": loser.etag, "last_modified": loser.last_modified,
                "last_raw_sha256": loser.last_raw_sha256, "last_fetched_at": loser.last_fetched_at.isoformat()},
                {"kept": str(rows[0])})
            await db.delete(loser)
            dropped += 1
    await db.flush()
    moved = (await db.execute(text("UPDATE collector_fetch_cache SET scope = :lib WHERE scope <> :lib"),
                              {"lib": LIBRARY_SCOPE})).rowcount
    report["tables"]["collector_fetch_cache"] = {"normalized": moved, "duplicates_dropped": dropped}
    if moved:
        _ledger(db, run_id, "collector_fetch_cache", "*", "scope_normalized", {"scope": "<legacy>"},
                {"scope": LIBRARY_SCOPE, "rows": moved})


async def _merge_sources(db, run_id: str, report: dict) -> None:
    """같은 (tenant, source_key) source 가 두 scope 에 있으면 하나로 합친다.

    남기는 행: canonical scope 행이 있으면 그것, 없으면 먼저 만든 행. 설정은 좁게 합친다(어느 한쪽이 멈췄으면 멈춤,
    자동 적재를 껐으면 끔, 주기는 짧은 쪽, 접근 pool 은 좁은 쪽). 이 표의 행은 다른 표가 FK 로 참조하지 않으며
    job·item 은 source_key 문자열로만 잇는다. 진 행은 ledger 에 전체 값을 남기고 지운다."""
    groups = (await db.execute(text("""SELECT tenant_id, source_key FROM collector_sources
            GROUP BY tenant_id, source_key HAVING count(*) > 1"""))).all()
    for tenant_id, source_key in groups:
        rows = list((await db.execute(text("""SELECT id FROM collector_sources WHERE tenant_id=:t AND source_key=:s
                ORDER BY (scope = :lib) DESC, created_at, id"""),
                {"t": tenant_id, "s": source_key, "lib": LIBRARY_SCOPE})).scalars())
        keep = await db.get(CollectorSource, rows[0])
        everyone = [keep] + [await db.get(CollectorSource, i) for i in rows[1:]]
        before = {"enabled": keep.enabled, "auto_import": keep.auto_import, "interval_minutes": keep.interval_minutes,
                  "pool": keep.pool, "scope": keep.scope}
        intervals = [r.interval_minutes for r in everyone if r.interval_minutes is not None]
        due = [r.next_run_at for r in everyone if r.next_run_at is not None]
        last_job = max((r for r in everyone if r.last_job_id), key=lambda r: r.updated_at, default=None)
        pool = narrowest_pool(*(effective_pool(r.pool, r.scope) for r in everyone))
        for loser in everyone[1:]:
            _ledger(db, run_id, "collector_sources", str(loser.id), "duplicate_merged", {
                "tenant_id": loser.tenant_id, "scope": loser.scope, "source_key": loser.source_key, "title": loser.title,
                "request": loser.request, "enabled": loser.enabled, "auto_import": loser.auto_import,
                "interval_minutes": loser.interval_minutes, "pool": loser.pool,
                "last_job_id": str(loser.last_job_id) if loser.last_job_id else None}, {"kept": str(keep.id)})
            await db.delete(loser)
        await db.flush()
        keep.enabled = all(r.enabled for r in everyone)
        keep.auto_import = all(r.auto_import for r in everyone)
        keep.interval_minutes = min(intervals) if intervals else None
        keep.next_run_at = min(due) if due else None
        keep.last_job_id = last_job.last_job_id if last_job else None
        keep.pool = pool
        keep.scope = LIBRARY_SCOPE
        keep.version = max(r.version for r in everyone) + 1
        _ledger(db, run_id, "collector_sources", str(keep.id), "duplicate_kept", before, {
            "enabled": keep.enabled, "auto_import": keep.auto_import, "interval_minutes": keep.interval_minutes,
            "pool": keep.pool, "merged_from": [str(r.id) for r in everyone[1:]]})
    await db.flush()
    moved = (await db.execute(text("""UPDATE collector_sources SET pool = CASE WHEN scope = 'work' THEN 'work' ELSE pool END,
            scope = :lib WHERE scope <> :lib"""), {"lib": LIBRARY_SCOPE})).rowcount
    report["tables"]["collector_sources"] = {"normalized": moved, "groups_merged": len(groups)}
    if moved:
        _ledger(db, run_id, "collector_sources", "*", "scope_normalized", {"scope": "<legacy>"},
                {"scope": LIBRARY_SCOPE, "rows": moved})


async def _merge_policies(db, run_id: str, report: dict) -> None:
    """정책은 append-only 증거이고 envelope 이 policy_version 라벨(source@vN+hash)로 참조하므로 옛 행의 버전·scope 를 바꾸지 않는다.

    옛 scope 에 가장 늦은 정책이 있으면 그 내용을 canonical scope 의 새 버전(전체 최대 버전 + 1)으로 한 번 더 적는다.
    두 scope 의 현재 정책 내용이 다르면 가장 늦게 기록한 쪽이 이긴다(새 코드가 병합 전부터 읽던 규칙과 같다).
    다르게 갈린 출처는 policy_conflicts 로 보고한다."""
    pairs = (await db.execute(text("""SELECT tenant_id, source_key FROM collector_source_policies
            GROUP BY tenant_id, source_key HAVING bool_or(scope <> :lib)"""), {"lib": LIBRARY_SCOPE})).all()
    appended, conflicts = 0, []
    for tenant_id, source_key in pairs:
        rows = list(await db.scalars(select(CollectorSourcePolicy).where(
            CollectorSourcePolicy.tenant_id == tenant_id, CollectorSourcePolicy.source_key == source_key)
            .order_by(CollectorSourcePolicy.created_at.desc(), CollectorSourcePolicy.version.desc())))
        latest = rows[0]
        latest_by_scope = {}
        for row in rows:
            latest_by_scope.setdefault(row.scope, row)
        if len({r.spec_hash for r in latest_by_scope.values()}) > 1:
            conflicts.append({"tenant_id": tenant_id, "source_key": source_key,
                              "latest_by_scope": {s: r.spec_hash[:12] for s, r in latest_by_scope.items()},
                              "winner_scope": latest.scope})
        canonical_latest = latest_by_scope.get(LIBRARY_SCOPE)
        if latest.scope == LIBRARY_SCOPE or (canonical_latest is not None and canonical_latest.spec_hash == latest.spec_hash):
            continue
        row = CollectorSourcePolicy(tenant_id=tenant_id, scope=LIBRARY_SCOPE, source_key=source_key,
                                    version=max(r.version for r in rows) + 1, spec=latest.spec, spec_hash=latest.spec_hash)
        db.add(row)
        _ledger(db, run_id, "collector_source_policies", f"{source_key}@v{row.version}", "policy_consolidated",
                {"from_scope": latest.scope, "from_version": latest.version, "spec_hash": latest.spec_hash},
                {"scope": LIBRARY_SCOPE, "version": row.version})
        appended += 1
    await db.flush()
    report["tables"]["collector_source_policies"] = {"consolidated_versions_appended": appended,
                                                      "policy_conflicts": conflicts}


def _identity_groups(rows) -> list[dict]:
    by_identity: dict[tuple, list] = defaultdict(list)
    for doc_id, doc_key, created_at, locator, meta in rows:
        meta = meta or {}
        tenant, source_key = meta.get("tenant_id"), meta.get("collector_source_key")
        if not (tenant and source_key and locator):
            continue
        new_key, legacy = collector_doc_keys(tenant, source_key, locator)
        if doc_key not in (new_key, *legacy):
            continue  # 키로 같은 출처임을 증명할 수 없으면 합치지 않는다.
        by_identity[(tenant, source_key, locator)].append((created_at, doc_id))
    return [{"reason": "collector_identity", "docs": sorted(v)} for v in by_identity.values() if len(v) > 1]


async def _merge_documents(db, run_id: str, report: dict) -> None:
    """같은 출처(tenant·출처·URL 또는 업로드 키)가 두 scope 에 각각 문서로 있으면 canonical 하나로 잇는다.

    canonical: 이미 alias 의 대상인 문서, 없으면 먼저 만든 문서. 리비전·패시지·근거·관측은 옮기지도 지우지도 않는다.
    covered: 중복의 모든 본문 해시가 canonical 에 있다 → 목록·검색에서만 가리고 ID·근거 조회는 그대로 열려 있다.
    variant: 본문이 다른 이력이 있다 → 둘 다 보이고 서로를 같은 출처로 표시한다.
    숨김(revocation)은 한쪽이라도 걸렸으면 양쪽에 건다. 접근 pool 은 canonical 에 좁은 쪽을 적용한다."""
    rows = (await db.execute(text("""SELECT DISTINCT ON (d.id) d.id, d.doc_key, d.created_at, s.locator, s.origin_meta
            FROM kb_documents d JOIN kb_revisions r ON r.document_id = d.id JOIN kb_sources s ON s.id = r.source_id
            WHERE d.doc_key LIKE 'collector:%' ORDER BY d.id, r.rev_no"""))).all()
    groups = _identity_groups(rows)
    plain = (await db.execute(text("""SELECT id, doc_key, created_at FROM kb_documents
            WHERE doc_key NOT LIKE 'collector:%' AND doc_key NOT LIKE 'work:%'"""))).all()
    work_docs = {k: (c, i) for i, k, c in (await db.execute(text(
        "SELECT id, doc_key, created_at FROM kb_documents WHERE doc_key LIKE 'work:%'"))).all()}
    for doc_id, doc_key, created_at in plain:
        twin = work_docs.get(legacy_work_upload_key(doc_key))
        if twin is not None:
            groups.append({"reason": "upload_key", "docs": sorted([(created_at, doc_id), twin])})
    aliases_made = {"covered": 0, "variant": 0}
    narrowed, hidden_propagated = 0, 0
    for group in groups:
        ids = [doc_id for _, doc_id in group["docs"]]
        known = {r[0]: r[1] for r in (await db.execute(text(
            "SELECT document_id, canonical_document_id FROM kb_document_aliases WHERE document_id = ANY(:ids) "
            "OR canonical_document_id = ANY(:ids)"), {"ids": ids})).all()}
        canonical_id = next((c for c in known.values() if c in ids), ids[0])
        pending = [i for i in ids if i != canonical_id and i not in known]
        if not pending:
            continue
        canon_hashes = await _text_hashes(db, canonical_id)
        accesses = {i: await db.get(KbDocumentAccess, i) for i in ids}
        for dup_id in pending:
            relation = "covered" if await _text_hashes(db, dup_id) <= canon_hashes else "variant"
            db.add(KbDocumentAlias(document_id=dup_id, canonical_document_id=canonical_id, relation=relation,
                                   reason=group["reason"], run_id=run_id))
            _ledger(db, run_id, "kb_document_aliases", str(dup_id), f"alias_{relation}", {},
                    {"canonical": str(canonical_id), "reason": group["reason"]})
            aliases_made[relation] += 1
        involved = [accesses[i] for i in [canonical_id, *pending] if accesses.get(i) is not None]
        if any(a.hidden for a in involved):
            for a in involved:
                if not a.hidden:
                    _ledger(db, run_id, "kb_document_access", str(a.document_id), "hidden_propagated", {"hidden": False}, {"hidden": True})
                    a.hidden = True
                    hidden_propagated += 1
        canon_access = accesses.get(canonical_id)
        if canon_access is not None:
            new_pool = narrowest_pool(*(effective_pool(a.pool, a.scope) for a in involved))
            if new_pool != effective_pool(canon_access.pool, canon_access.scope):
                _ledger(db, run_id, "kb_document_access", str(canonical_id), "pool_narrowed",
                        {"pool": canon_access.pool, "scope": canon_access.scope}, {"pool": new_pool})
                canon_access.pool = new_pool
                canon_access.scope = LIBRARY_SCOPE
                narrowed += 1
    await db.flush()
    moved = (await db.execute(text("""UPDATE kb_document_access SET pool = CASE WHEN scope = 'work' THEN 'work' ELSE pool END,
            scope = :lib WHERE scope <> :lib"""), {"lib": LIBRARY_SCOPE})).rowcount
    report["tables"]["kb_document_access"] = {"normalized": moved, "engine_access_narrowed": narrowed,
                                               "hidden_propagated": hidden_propagated}
    report["tables"]["kb_document_aliases"] = {"groups": len(groups), **{f"created_{k}": v for k, v in aliases_made.items()}}
    if moved:
        _ledger(db, run_id, "kb_document_access", "*", "scope_normalized", {"scope": "<legacy>"},
                {"scope": LIBRARY_SCOPE, "rows": moved})
    report["notes"]["kb_documents_without_access_row"] = await _count(db, """
        SELECT count(*) FROM kb_documents d WHERE NOT EXISTS (SELECT 1 FROM kb_document_access a WHERE a.document_id = d.id)""")


async def _text_hashes(db, document_id) -> set[str]:
    return set((await db.execute(text("SELECT text_sha256 FROM kb_revisions WHERE document_id = :d"),
                                 {"d": document_id})).scalars())

