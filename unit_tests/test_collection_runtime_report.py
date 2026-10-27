"""Bounded diagnostics must expose failure codes without reading document payloads."""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from ttakkari.operations import inline


def result(rows):
    obj = MagicMock()
    obj.all.return_value = rows
    return obj


@pytest.mark.asyncio
async def test_collection_report_exposes_zero_result_failure_without_raw_error_or_payload():
    mid = uuid.uuid4()
    now = datetime.now(UTC)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        result([SimpleNamespace(id=mid, title='디자인 시스템·UI/UX 지식 백과사전 구축', status='partial',
                                created_at=now, updated_at=now, run_seq=1, stop='query_budget', searches_used='3',
                                gap_0='search_failed', gap_1=None, gap_2=None, gap_3=None, gap_4=None)]),
        result([SimpleNamespace(mission_id=mid, run_seq=1, status='partial', stage='mission_prepare',
                                error_code='search_provider_challenge', jobs=1, attempts=3, next_retry=None)]),
        result([SimpleNamespace(mission_id=mid, acquired=0, accepted=0, failed=0, blocked=0, imported=0)]),
    ])
    report = await inline.collection_report(db)
    assert report['readonly'] is True
    row = report['missions'][0]
    assert row['id'] == str(mid)
    assert row['counts']['acquired'] == row['counts']['imported'] == 0
    assert row['jobs'][0]['error_code'] == 'search_provider_challenge'
    assert row['jobs'][0]['attempts'] == 3
    assert row['research'] == {'stop': 'query_budget', 'searches_used': '3'}
    assert row['gap_codes'] == ['search_failed']
    statements = [str(call.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
                  for call in db.execute.call_args_list]
    assert all(sql.lstrip().startswith('SELECT') for sql in statements)
    assert 'LIMIT 20' in statements[0]
    assert all("tenant_id = 'owner'" in sql for sql in statements)
    assert 'collector_jobs.envelope' not in ' '.join(statements)
    assert 'collector_items.envelope' not in ' '.join(statements)
    assert "last_error ->> 'code'" in statements[1]
    db.commit.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_collection_report_with_no_missions_does_not_scan_job_or_item_tables():
    db = MagicMock()
    db.execute = AsyncMock(return_value=result([]))
    report = await inline.collection_report(db)
    assert report == {'readonly': True, 'limit': 20, 'missions': []}
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_report_real_queries_count_distinct_receipts_and_keep_current_run_errors():
    import json

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    mid, other = uuid.uuid4(), uuid.uuid4()
    old, current, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item = uuid.uuid4()
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    try:
        async with engine.begin() as conn:
            for ddl in (
                ('CREATE TABLE collection_missions (id UUID PRIMARY KEY, tenant_id TEXT, plan JSON, status TEXT, '
                'created_at DATETIME, updated_at DATETIME, current_run_seq INTEGER, gaps JSON)'),
                'CREATE TABLE collection_mission_jobs (mission_id UUID, job_id UUID, run_seq INTEGER)',
                ('CREATE TABLE collector_jobs (id UUID PRIMARY KEY, tenant_id TEXT, status TEXT, stage TEXT, '
                'last_error JSON, attempts INTEGER, not_before DATETIME)'),
                'CREATE TABLE collector_items (id UUID PRIMARY KEY, job_id UUID, tenant_id TEXT, raw_sha256 TEXT, status TEXT)',
                'CREATE TABLE collector_deliveries (item_id UUID, outcome TEXT)',
            ):
                await conn.execute(text(ddl))
            for ident, tenant in ((mid, 'owner'), (other, 'other')):
                await conn.execute(text('INSERT INTO collection_missions VALUES (:id, :tenant, :plan, :status, '
                                        ':now, :now, 2, :gaps)'), {'id': ident.hex, 'tenant': tenant,
                    'plan': json.dumps({'title': 'Private document body stays out', 'context': {'text': 'SECRET'},
                                     'research': {'stop': 'search_budget', 'searches_used': 16}}),
                    'status': 'partial', 'now': '2026-10-27 02:00:00',
                    'gaps': json.dumps([{'reason': 'search_failed', 'message': 'SECRET'}])})
            for ident, seq, tenant, err in ((old, 1, 'owner', 'stale_failure'),
                                           (current, 2, 'owner', 'search_provider_challenge'),
                                           (foreign, 2, 'other', 'foreign_failure')):
                await conn.execute(text('INSERT INTO collection_mission_jobs VALUES (:mid, :job, :seq)'),
                                   {'mid': mid.hex, 'job': ident.hex, 'seq': seq})
                await conn.execute(text("INSERT INTO collector_jobs VALUES (:id, :tenant, 'partial', "
                                        "'mission_prepare', :err, 3, NULL)"),
                                   {'id': ident.hex, 'tenant': tenant, 'err': json.dumps({'code': err, 'message': 'SECRET'})})
            await conn.execute(text("INSERT INTO collector_items VALUES (:id, :job, 'owner', 'hash', 'accepted')"),
                               {'id': item.hex, 'job': old.hex})
            for outcome in ('imported', 'imported', 'failed'):
                await conn.execute(text('INSERT INTO collector_deliveries VALUES (:id, :outcome)'),
                                   {'id': item.hex, 'outcome': outcome})
        async with async_sessionmaker(engine)() as db:
            report = await inline.collection_report(db)
        assert len(report['missions']) == 1
        row = report['missions'][0]
        assert row['gap_codes'] == ['search_failed']
        assert row['counts'] == {'acquired': 1, 'accepted': 1, 'failed': 0, 'blocked': 0, 'imported': 1}
        assert row['jobs'] == [{'run_seq': 2, 'status': 'partial', 'stage': 'mission_prepare',
                                'error_code': 'search_provider_challenge', 'jobs': 1, 'attempts': 3, 'next_retry': None}]
        assert 'SECRET' not in json.dumps(report)
        assert 'foreign_failure' not in json.dumps(report)
        assert 'stale_failure' not in json.dumps(report)
    finally:
        await engine.dispose()
