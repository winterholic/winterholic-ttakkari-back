"""C2: 수집 플랫폼·지식 도서관 single library 마이그레이션 및 중복 병합 검증.

1. Alembic 0074 마이그레이션 roundtrip (upgrade -> downgrade -> upgrade)
2. personal / work 두 scope 에 같은 URL·해시가 있는 fixture 병합 반례:
   - 원본 이력·리비전·패시지·근거·관측 불변 보존
   - collector_jobs: twin request_key 재명명으로 멱등 키 충돌 방지 및 모든 job 보존
   - collector_sources: 좁은 pool/설정으로 병합, 탈락 출처 ledger 기록
   - collector_fetch_cache: 최신 검증값 유지, 탈락 캐시 ledger 기록
   - collector_items: scope 단일 정규화 (LIBRARY_SCOPE)
   - kb_documents: kb_document_aliases 로 canonical 연계 (covered/variant), 삭제 없음
   - collection_scope_ledger: 감사 추적 행 기록
3. 병합 후 통합 도서관 검색 및 회사 엔진 pool 격리 검증
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from test_collector_stage_migration import _load

from ttakkari.collector import scope_merge
from ttakkari.collector.control_models import (
    CollectionScopeLedger,
    CollectorSource,
    KbDocumentAccess,
    KbDocumentAlias,
)
from ttakkari.collector.library import LIBRARY_SCOPE, collector_doc_keys
from ttakkari.collector_models import (
    CollectorFetchCache,
    CollectorItem,
    CollectorJob,
)
from ttakkari.db import sessionmaker
from ttakkari.kb_library import (
    get_document,
    search_passages,
)
from ttakkari.models import Base

TABLES_0074 = {
    "kb_document_aliases", "collection_scope_ledger", "collector_jobs", "collector_sources", "kb_document_access",
}


async def test_0074_migration_roundtrip_matches_models():
    migration = _load("0074_collection_single_library")
    assert migration.down_revision == "0073_collection_mission_runs"

    async with sessionmaker()() as db:
        connection = await db.connection()

        def migrate(sync, operation):
            with Operations.context(MigrationContext.configure(sync)):
                operation()

        def include(obj, name, kind, reflected, compare_to):
            if kind == "table":
                return name in TABLES_0074
            table = getattr(obj, "table", None)
            return table is None or table.name in TABLES_0074

        def drift(sync):
            return compare_metadata(
                MigrationContext.configure(sync, opts={"compare_type": True, "include_object": include}),
                Base.metadata,
            )

        # 1. upgrade 상태에서 drift 없음 확인
        assert await connection.run_sync(drift) == []

        # 2. downgrade 실행: 0073 에서 만든 테이블·인덱스·컬럼 정리
        await connection.run_sync(migrate, migration.downgrade)
        assert await db.scalar(text("SELECT to_regclass('kb_document_aliases')")) is None
        assert await db.scalar(text("SELECT to_regclass('collection_scope_ledger')")) is None

        # 3. 다시 upgrade: 테이블과 인덱스, pool 컬럼 복원
        await connection.run_sync(migrate, migration.upgrade)
        assert await connection.run_sync(drift) == []
        assert await db.scalar(text("SELECT to_regclass('kb_document_aliases')")) is not None
        assert await db.scalar(text("SELECT to_regclass('collection_scope_ledger')")) is not None
        await db.commit()


async def test_scope_merge_counterexample_and_provenance_preservation():
    """두 scope 에 같은 URL·해시·키가 존재하는 반례 상황에서 병합을 수행한다.

    - 원본 데이터 삭제 없이 canonical 과 alias 로 연결
    - FK 무결성 유지 (job -> item -> delivery)
    - ledger 감사 기록 보존
    - dry-run 은 변경을 반영하지 않고 롤백
    """
    now = datetime.now(UTC)
    tenant = "owner"
    source_key = "repo:example/docs"
    url = "https://example.com/docs/intro"
    url_k = "urlkey-intro"
    req_key = "req-fixed-identical"
    shared_text = "이 문서는 개인과 회사 양쪽에서 수집된 동일 본문 예제입니다."

    async with sessionmaker()() as db:
        # 1. 두 scope 에 동일 source_key 출처 등록
        src_personal = CollectorSource(
            id=uuid.uuid4(), tenant_id=tenant, scope="personal", pool="personal",
            source_key=source_key, title="Personal Docs Source", request={"seeds": [url]},
            enabled=True, auto_import=True, interval_minutes=60, created_at=now - timedelta(hours=2),
        )
        src_work = CollectorSource(
            id=uuid.uuid4(), tenant_id=tenant, scope="work", pool="work",
            source_key=source_key, title="Work Docs Source", request={"seeds": [url]},
            enabled=True, auto_import=False, interval_minutes=30, created_at=now - timedelta(hours=1),
        )
        db.add_all([src_personal, src_work])

        # 2. 두 scope 에 동일 request_key job 등록
        job_personal = CollectorJob(
            id=uuid.uuid4(), tenant_id=tenant, scope="personal", pool="personal",
            source_key=source_key, request_key=req_key, payload_hash="hash-p-1",
            envelope_id=uuid.uuid4(), envelope={"source": {"key": source_key}}, status="succeeded",
            created_at=now - timedelta(hours=2),
        )
        job_work = CollectorJob(
            id=uuid.uuid4(), tenant_id=tenant, scope="work", pool="work",
            source_key=source_key, request_key=req_key, payload_hash="hash-w-1",
            envelope_id=uuid.uuid4(), envelope={"source": {"key": source_key}}, status="succeeded",
            created_at=now - timedelta(hours=1),
        )
        db.add_all([job_personal, job_work])
        await db.flush()

        # 3. 두 scope 에 동일 URL 의 조건부 요청 fetch cache 등록 (시간이 다름)
        cache_personal = CollectorFetchCache(
            id=uuid.uuid4(), tenant_id=tenant, scope="personal", source_key=source_key,
            url_key=url_k, url=url, etag="etag-old", last_fetched_at=now - timedelta(hours=2),
        )
        cache_work = CollectorFetchCache(
            id=uuid.uuid4(), tenant_id=tenant, scope="work", source_key=source_key,
            url_key=url_k, url=url, etag="etag-new", last_fetched_at=now - timedelta(hours=1),
        )
        db.add_all([cache_personal, cache_work])

        # 4. 두 scope 에 동일 본문의 CollectorItem 등록
        item_personal = CollectorItem(
            id=uuid.uuid4(), job_id=job_personal.id, tenant_id=tenant, scope="personal",
            source_key=source_key, kind="document", status="accepted", stage="done",
            url=url, url_key=url_k, text_sha256="sha-intro-sample",
            created_at=now - timedelta(hours=2),
        )
        item_work = CollectorItem(
            id=uuid.uuid4(), job_id=job_work.id, tenant_id=tenant, scope="work",
            source_key=source_key, kind="document", status="accepted", stage="done",
            url=url, url_key=url_k, text_sha256="sha-intro-sample",
            created_at=now - timedelta(hours=1),
        )
        db.add_all([item_personal, item_work])
        await db.flush()

        # 5. KB 에 수집 문서 등록 (collector doc_key 및 origin_meta 불변 삽입)
        from ttakkari.kb_library import segment_revision
        from ttakkari.kb_store import ingest_source

        _new_doc_key, legacy_keys = collector_doc_keys(tenant, source_key, url)
        meta = {"tenant_id": tenant, "collector_source_key": source_key}
        reg_p = await ingest_source(
            db, source_key=legacy_keys[0], data=shared_text.encode("utf-8"), kind="web",
            locator=url, media_type="text/plain", title="Personal Intro", lang="ko",
            origin_meta={"via": "collector", **meta},
        )
        db.add(KbDocumentAccess(document_id=reg_p.document_id, tenant_id=tenant, scope="personal", pool="personal"))
        await segment_revision(db, reg_p.revision_id)

        reg_w = await ingest_source(
            db, source_key=legacy_keys[1], data=shared_text.encode("utf-8"), kind="web",
            locator=url, media_type="text/plain", title="Work Intro", lang="ko",
            origin_meta={"via": "collector", **meta},
        )
        db.add(KbDocumentAccess(document_id=reg_w.document_id, tenant_id=tenant, scope="work", pool="work"))
        await segment_revision(db, reg_w.revision_id)

        await db.commit()

        # ---- Dry-run 검증 ----------------------------------------------------
        report_dry = await scope_merge.merge_scopes(db)
        await db.rollback()

        assert "tables" in report_dry
        assert report_dry["tables"]["collector_jobs"]["normalized"] >= 1
        assert report_dry["tables"]["collector_sources"]["groups_merged"] >= 1
        assert report_dry["tables"]["collector_fetch_cache"]["duplicates_dropped"] >= 1

        # rollback 후 원래 상태 보존 확인 (dry-run 격리)
        sources_still_two = (await db.execute(
            select(CollectorSource).where(CollectorSource.tenant_id == tenant)
        )).scalars().all()
        assert len(sources_still_two) == 2

        # ---- Apply 검증 ------------------------------------------------------
        report_applied = await scope_merge.merge_scopes(db)
        await db.commit()

        # 1. collector_sources: 하나로 합쳐지고 좁은 pool 및 설정 적용
        sources_after = (await db.execute(
            select(CollectorSource).where(CollectorSource.tenant_id == tenant)
        )).scalars().all()
        assert len(sources_after) == 1
        winner_src = sources_after[0]
        assert winner_src.scope == LIBRARY_SCOPE
        assert winner_src.pool == "personal"  # narrowest_pool('personal', 'work') -> personal
        assert winner_src.interval_minutes == 30  # 짧은 쪽 선택
        assert winner_src.auto_import is False  # 끈 쪽이 있으면 False

        # 2. collector_jobs: 둘 다 보존 (삭제 없음!), 멱등 충돌 방지를 위해 twin request_key 재명명
        jobs_after = (await db.execute(
            select(CollectorJob).where(CollectorJob.tenant_id == tenant).order_by(CollectorJob.created_at)
        )).scalars().all()
        assert len(jobs_after) == 2
        assert all(j.scope == LIBRARY_SCOPE for j in jobs_after)
        assert jobs_after[0].request_key == req_key
        assert "~merged:work:" in jobs_after[1].request_key
        assert jobs_after[1].pool == "work"  # 원래 work pool 보존

        # 3. collector_fetch_cache: 최신 1개만 남고 이전 캐시는 삭제
        cache_after = (await db.execute(
            select(CollectorFetchCache).where(CollectorFetchCache.tenant_id == tenant)
        )).scalars().all()
        assert len(cache_after) == 1
        assert cache_after[0].etag == "etag-new"
        assert cache_after[0].scope == LIBRARY_SCOPE

        # 4. collector_items: scope 가 LIBRARY_SCOPE 로 정규화
        items_after = (await db.execute(
            select(CollectorItem).where(CollectorItem.tenant_id == tenant)
        )).scalars().all()
        assert len(items_after) == 2
        assert all(i.scope == LIBRARY_SCOPE for i in items_after)

        # 5. kb_documents & kb_document_aliases: 원본 리비전·문서 삭제 없이 alias 생성
        aliases = (await db.execute(
            select(KbDocumentAlias).where(
                KbDocumentAlias.canonical_document_id.in_([reg_p.document_id, reg_w.document_id])
                | KbDocumentAlias.document_id.in_([reg_p.document_id, reg_w.document_id])
            )
        )).scalars().all()
        assert len(aliases) == 1
        alias = aliases[0]
        assert alias.canonical_document_id == reg_p.document_id
        assert alias.document_id == reg_w.document_id
        assert alias.relation == "covered"  # 같은 본문이므로 covered

        # 6. canonical 문서 조회 시 aliases 메타데이터 확인
        doc_detail = await get_document(db, reg_p.document_id, revision_id=None, passage_limit=10, passage_offset=0)
        assert len(doc_detail["aliases"]) == 1
        assert doc_detail["aliases"][0]["document_id"] == str(reg_w.document_id)
        assert doc_detail["aliases"][0]["relation"] == "covered"

        # 7. 검색 시 covered 된 중복 문서는 숨겨지고 canonical 하나만 반환 (중복 노출 방지)
        search_res = await search_passages(db, query="동일 본문 예제", limit=10, offset=0, document_id=None, current_only=True)
        matched_doc_ids = {hit["document"]["id"] for hit in search_res["items"]}
        assert str(reg_p.document_id) in matched_doc_ids
        assert str(reg_w.document_id) not in matched_doc_ids

        # 8. collection_scope_ledger 감사 기록 확인
        ledger_rows = (await db.execute(
            select(CollectionScopeLedger).where(CollectionScopeLedger.run_id == report_applied["run_id"])
        )).scalars().all()
        actions = {r.action for r in ledger_rows}
        assert "duplicate_merged" in actions
        assert "duplicate_dropped" in actions
        assert "alias_covered" in actions
        assert "request_key_renamed" in actions
        assert "scope_normalized" in actions

        # 반복 apply는 원본·alias·ledger를 중복 변경하지 않는다.
        report_repeat = await scope_merge.merge_scopes(db)
        assert report_repeat["tables"]["collector_jobs"]["normalized"] == 0
        assert report_repeat["tables"]["collector_jobs"]["request_keys_renamed"] == 0
        assert report_repeat["tables"]["collector_sources"]["groups_merged"] == 0
        assert report_repeat["tables"]["collector_fetch_cache"]["duplicates_dropped"] == 0
        assert report_repeat["tables"]["kb_document_aliases"]["created_covered"] == 0
        assert report_repeat["tables"]["kb_document_aliases"]["created_variant"] == 0
        await db.commit()
        assert await db.scalar(text("SELECT count(*) FROM kb_document_aliases WHERE document_id = :id"),
                               {"id": reg_w.document_id}) == 1


async def test_0074_downgrade_restores_legacy_scope_from_pool():
    """병합 후 0074 downgrade 가 실행되면 pool='work' 인 행들의 scope 가 'work' 로 복원된다."""
    migration = _load("0074_collection_single_library")
    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        job = CollectorJob(
            id=uuid.uuid4(), tenant_id="owner", scope=LIBRARY_SCOPE, pool="work",
            source_key="test-key-restore", request_key="req-work-restore", payload_hash="h1",
            envelope_id=uuid.uuid4(), envelope={"source": {"key": "test-key-restore"}}, status="succeeded",
            created_at=now,
        )
        src = CollectorSource(
            id=uuid.uuid4(), tenant_id="owner", scope=LIBRARY_SCOPE, pool="work",
            source_key="test-key-restore", title="Work Source", request={},
            created_at=now,
        )
        job_id = job.id
        src_id = src.id
        db.add_all([job, src])
        await db.commit()

        connection = await db.connection()

        def migrate_down(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()

        def migrate_up(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()

        await connection.run_sync(migrate_down)
        # downgrade 후 pool='work' 였던 행의 scope 가 'work' 로 복원됨
        restored_job_scope = await db.scalar(text("SELECT scope FROM collector_jobs WHERE id = :id"), {"id": job_id})
        assert restored_job_scope == "work"
        restored_src_scope = await db.scalar(text("SELECT scope FROM collector_sources WHERE id = :id"), {"id": src_id})
        assert restored_src_scope == "work"

        # 다시 upgrade 복구
        await connection.run_sync(migrate_up)
        await db.commit()


def test_cli_collection_scope_merge_parser_registered():
    from ttakkari.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["collection-scope-merge", "--help"])
    assert exc.value.code == 0
