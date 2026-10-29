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


@pytest.mark.asyncio
async def test_runtime_report_exposes_supervisor_queue_and_slot_ownership_without_secrets(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from ttakkari import deployment
    from ttakkari.config import get_settings

    assert Path(inline.__file__).resolve().is_relative_to(
        Path(__file__).resolve().parents[1] / "src"
    )
    home = tmp_path / "ttakkari-home"
    deploy_root = home / "deploy"
    deploy_root.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "home", home)
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    (deploy_root / "generations-enabled.json").write_text(
        json.dumps({"protocol": 1, "strategy": "generations"})
    )

    pinned_rev = "e817fc19471bdc48054158cada4a99466f7eaa6f"
    active_rev = "48eac0c612d7186e8c418fe607a036ea2ab4da09"
    stuck_rev = "12347b3f24f56f2a7c029e8ca3f394495dd240c8"
    old_rev = "c70bce7ba8ab888ea4989f2ab3ef887ff41f0809"
    queued_rev = "133ab4e0f20d16c092580a2bb9cd5a8bb9fce01e"

    pinned_release = deploy_root / "releases" / pinned_rev
    pinned_release.mkdir(parents=True)
    (pinned_release / ".release.json").write_text(json.dumps({"revision": pinned_rev}))
    (deploy_root / "supervisor.json").write_text(
        json.dumps({
            "revision": pinned_rev,
            "release": str(pinned_release),
            "source": "/Users/winterholic/secret-path/back",
        })
    )
    (deploy_root / "supervisor-runtime.json").write_text(
        json.dumps({
            "revision": pinned_rev,
            "release": str(pinned_release),
            "pid": inline.os.getpid(),
            "ports": [8791, 8792, 8793],
        })
    )
    deployment.JobStore(deploy_root)
    from ttakkari.generation_deployment import Slots
    Slots(deploy_root).persist_proxy(2)

    store = deployment.JobStore(deploy_root)
    abcb = store._write({
        "id": "abcb3f7d-a291-4789-9117-ec5c08220eba",
        "revision": stuck_rev,
        "skip_tests": False,
        "status": "recovery_pending",
        "created_at": "2026-10-28T21:52:32.996586+00:00",
        "error": "draining generation unreachable; work survival is unknown",
        "error_stage": "drain",
        "strategy": "generations",
        "candidate_slot": 0,
        "previous_slot": 1,
        "previous_generation": "01c6010d-6ecd-4bc9-a879-12b60dea1215",
        "previous_revision": old_rev,
        "candidate_generation": "18c94d7e-2bf0-4ba0-ae76-467154b56f5c",
        "activated_revision": stuck_rev,
    })
    # 6 succeeded jobs after abcb so abcb falls outside recent_deploys[-5:]
    for idx in range(6):
        store._write({
            "id": str(uuid.uuid4()),
            "revision": f"{idx:040x}",
            "skip_tests": False,
            "status": "succeeded",
            "created_at": f"2026-10-29T0{idx}:00:00+00:00",
            "error": None,
            "strategy": "generations",
        })
    f407 = store._write({
        "id": "f407df99-51b1-4843-8fa0-a3fe4ceebefb",
        "revision": queued_rev,
        "skip_tests": False,
        "status": "requested",
        "created_at": "2026-10-29T16:33:58.602253+00:00",
        "error": None,
        "strategy": "generations",
    })

    gen_active = uuid.UUID("bb450175-d7a4-495e-ae0f-e3202ce0a456")
    gen_draining = uuid.UUID("01c6010d-6ecd-4bc9-a879-12b60dea1215")
    gen_retired = uuid.UUID("18c94d7e-2bf0-4ba0-ae76-467154b56f5c")

    # Hold runtime lock for active and draining generations
    lock_active = deployment.generation._lock(home / "runtime" / f"{gen_active}.lock") if hasattr(deployment, "generation") else None
    from ttakkari.generation import _lock
    if lock_active is None:
        lock_active = _lock(home / "runtime" / f"{gen_active}.lock")
    lock_draining = _lock(home / "runtime" / f"{gen_draining}.lock")
    try:
        now = datetime.now(UTC)
        control_row = SimpleNamespace(active_generation=gen_active, executor_generation=None, epoch=123)
        gen_rows = [
            SimpleNamespace(
                id=gen_active, revision=active_rev, role="api", state="active",
                execution_mode="local", pid=inline.os.getpid(), heartbeat_at=now,
                api_url="http://127.0.0.1:8793",
            ),
            SimpleNamespace(
                id=gen_draining, revision=old_rev, role="api", state="draining",
                execution_mode="local", pid=inline.os.getpid(), heartbeat_at=now,
                api_url="http://127.0.0.1:8792",
            ),
            SimpleNamespace(
                id=gen_retired, revision=stuck_rev, role="api", state="offline",
                execution_mode="local", pid=999999, heartbeat_at=now,
                api_url="http://127.0.0.1:8791",
            ),
        ]

        def scalars_result(items):
            m = MagicMock()
            s = MagicMock()
            s.first.return_value = items[0] if items else None
            s.all.return_value = items
            m.scalars.return_value = s
            return m

        def scalar_result(val):
            m = MagicMock()
            m.scalar.return_value = val
            return m

        db = MagicMock()
        db.execute = AsyncMock(side_effect=[
            scalars_result([control_row]),
            scalars_result(gen_rows),
            result([(gen_draining, 2)]),
            result([(gen_draining, 2)]),
            scalar_result(1),
            result([]),
            result([]),  # collection_report
        ])

        report = await inline.runtime_report(db)
    finally:
        if lock_active:
            lock_active.close()
        if lock_draining:
            lock_draining.close()

    # Recent deploys only keeps last 5, so abcb is not in recent_deploys
    assert all(d["id"] != abcb["id"] for d in report["recent_deploys"])
    # But deploy_queue surfaces abcb as blocking & foreground, and f407 as blocked!
    queue = report["deploy_queue"]
    assert queue["cutover_blocked"] is True
    assert queue["foreground_job"]["id"] == abcb["id"]
    assert [j["id"] for j in queue["blocking_jobs"]] == [abcb["id"]]
    assert [j["id"] for j in queue["blocked_jobs"]] == [f407["id"]]

    # Supervisor diagnostics expose pinned vs runtime revision & freshness without leaking secret source paths
    sup = report["supervisor"]
    assert sup["pinned_revision"] == pinned_rev
    assert sup["runtime_revision"] == pinned_rev
    assert sup["matches_pinned_revision"] is True
    assert sup["matches_pinned_release"] is True
    assert sup["pid_alive"] is True
    assert isinstance(sup["age_s"], (int, float))
    assert "secret-path" not in json.dumps(report)

    # Slot and generation ownership diagnostics
    by_gen = {g["id"]: g for g in report["generations"]}
    assert by_gen[str(gen_active)]["slot"] == 2
    assert by_gen[str(gen_active)]["lock_held"] is True
    assert by_gen[str(gen_draining)]["slot"] == 1
    assert by_gen[str(gen_draining)]["lock_held"] is True
    assert by_gen[str(gen_draining)]["leases"] == 2
    assert by_gen[str(gen_retired)]["slot"] == 0
    assert by_gen[str(gen_retired)]["lock_held"] is False

    slots = {s["slot"]: s for s in report["slots"]}
    assert slots[2]["persisted_proxy"] is True
    assert slots[2]["active_owner"] == str(gen_active)
    assert slots[1]["live_generations"][0]["id"] == str(gen_draining)
    assert slots[1]["live_generations"][0]["leases"] == 2
    assert slots[0]["live_generations"] == []


@pytest.mark.asyncio
async def test_diagnostics_redact_raw_errors_and_block_synthetic_secret_path_canaries(tmp_path, monkeypatch):
    import json

    from ttakkari import deployment

    home = tmp_path / "home"
    deploy_root = home / "deploy"
    deploy_root.mkdir(parents=True)
    monkeypatch.setattr(inline.get_settings(), "home", home)
    monkeypatch.setattr(deployment, "state_dir", lambda: deploy_root)
    monkeypatch.setattr(deployment, "strategy", lambda: "generations")

    canaries = [
        "SYNTHETIC_SECRET_PATH_CANARY_98c1",
        "/Users/winterholic/.ttakkari/config.env",
        "postgresql://ttakkari:SUPER_SECRET_PW_771@127.0.0.1:5432/ttakkari",
        "sk-ant-api03-CANARYTOKEN999",
    ]
    raw_secret_error = (
        "draining generation unreachable; env=/Users/winterholic/.ttakkari/config.env "
        "db=postgresql://ttakkari:SUPER_SECRET_PW_771@127.0.0.1:5432/ttakkari "
        "token=sk-ant-api03-CANARYTOKEN999 canary=SYNTHETIC_SECRET_PATH_CANARY_98c1"
    )
    (deploy_root / "supervisor-upgrade.json").write_text(json.dumps({
        "revision": "/Users/winterholic/.ttakkari/config.env/SYNTHETIC_SECRET_PATH_CANARY_98c1",
        "status": "failed:SYNTHETIC_SECRET_PATH_CANARY_98c1",
        "error": raw_secret_error,
    }))

    store = deployment.JobStore(deploy_root)
    job = store._write({
        "id": "abcb3f7d-a291-4789-9117-ec5c08220eba",
        "revision": "1" * 40,
        "skip_tests": False,
        "status": "recovery_pending",
        "created_at": "2026-10-28T21:52:32.996586+00:00",
        "error": raw_secret_error,
        "error_stage": "drain:/Users/winterholic/.ttakkari/config.env:SYNTHETIC_SECRET_PATH_CANARY_98c1",
        "strategy": "generations",
        "candidate_slot": 0,
        "previous_slot": 1,
        "previous_generation": "01c6010d-6ecd-4bc9-a879-12b60dea1215",
        "previous_revision": "2" * 40,
        "candidate_generation": "18c94d7e-2bf0-4ba0-ae76-467154b56f5c",
        "activated_revision": "1" * 40,
    })

    q_diag = deployment.queue_diagnostics(store)
    s_diag = deployment.supervisor_diagnostics(deploy_root)
    d_diag = await deployment.diagnose_deployment(deploy_root, include_db=False)

    now = datetime.now(UTC)
    gen_id = uuid.UUID("bb450175-d7a4-495e-ae0f-e3202ce0a456")
    control_row = SimpleNamespace(active_generation=gen_id, executor_generation=None, epoch=123)
    gen_row = SimpleNamespace(
        id=gen_id, revision="3" * 40, role="api", state="active",
        execution_mode="local", pid=inline.os.getpid(), heartbeat_at=now,
        api_url="http://127.0.0.1:8793",
    )

    def scalars_result(items):
        m = MagicMock()
        s = MagicMock()
        s.first.return_value = items[0] if items else None
        s.all.return_value = items
        m.scalars.return_value = s
        return m

    def scalar_result(val):
        m = MagicMock()
        m.scalar.return_value = val
        return m

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        scalars_result([control_row]),
        scalars_result([gen_row]),
        result([]),
        result([]),
        scalar_result(0),
        result([]),
        result([]),
    ])
    rt_report = await inline.runtime_report(db)

    for payload in (q_diag, s_diag, d_diag, rt_report):
        serialized = json.dumps(payload)
        for canary in canaries:
            assert canary not in serialized
        assert "config.env" not in serialized
        assert "SUPER_SECRET_PW_771" not in serialized

    fg = q_diag["foreground_job"]
    assert fg["id"] == job["id"]
    assert "error" not in fg
    assert fg["error_code"] == "draining_generation_unreachable"
    assert fg["error_stage"] == "unknown"
    assert fg["has_error"] is True
