"""Synthetic resources only. Real HTTP + independent PostgreSQL transactions."""
import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from sqlalchemy import delete, select, text, update

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import LocalSttJob, Meeting, MeetingItem

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope='module', autouse=True)
async def source_triggers(_schema_and_app):
    spec = importlib.util.spec_from_file_location('share_migration', 'alembic/versions/0028_public_shares.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    async with sessionmaker()() as db:
        for statement in mod.TRIGGER_STATEMENTS:
            await db.execute(text(statement))
        await db.commit()
    yield
    async with sessionmaker()() as db:
        for name, table in [('meeting_delete', 'meetings'), ('meeting_scope', 'meetings'),
            ('item_delete', 'meeting_items'), ('item_parent', 'meeting_items'),
            ('job_delete', 'local_stt_jobs'), ('job_parent', 'local_stt_jobs')]:
            await db.execute(text(f'DROP TRIGGER public_share_{name} ON {table}'))
        await db.execute(text('DROP FUNCTION revoke_bound_public_shares()'))
        await db.commit()


@pytest.fixture(autouse=True)
def sharing_on(monkeypatch):
    monkeypatch.setitem(get_settings().__dict__, "public_sharing_enabled", True)
    from ttakkari.sharing import delivery
    for limiter in (delivery.failures, delivery.pages, delivery.downloads):
        limiter.reset()


async def source(*, scope="personal", local=False):
    async with sessionmaker()() as db:
        m = Meeting(title="Synthetic meeting", scope=scope, status="analyzed", transcript="Approved transcript",
                    transcript_source="paste", summary="Approved summary", participants=["PRIVATE_PERSON"],
                    analysis_context={"private": "PRIVATE_SENTINEL"}, vector_error="PRIVATE_SENTINEL")
        db.add(m)
        await db.flush()
        item = MeetingItem(meeting_id=m.id, kind="action", text="Approved action", owner="PRIVATE_PERSON",
                           evidence="Approved transcript", conflicts=["PRIVATE_SENTINEL"])
        db.add(item)
        job = None
        if local:
            job = LocalSttJob(meeting_id=m.id, status="succeeded", transcript=m.transcript,
                result_metadata={"duration": 4.0, "speaker_names": {"SPEAKER_00": "PRIVATE_PERSON"},
                                 "private": "PRIVATE_SENTINEL", "utterances": [
                    {"id": "7", "start": 0.0, "end": 4.0, "speaker_id": "SPEAKER_00", "speaker_label": "화자 1",
                     "text": "Approved transcript", "verification_status": "needs_review", "overlap": None,
                     "raw": "PRIVATE_SENTINEL", "original_text": "PRIVATE_SENTINEL", "words": ["PRIVATE_SENTINEL"]}]})
            db.add(job)
            await db.flush()
            m.transcript_source = "provider:local_whisper"
            m.provider_job_id = str(job.id)
        await db.commit()
        return m.id, item.id, job.id if job else None


def request(mid, *, sections=None, formats=None):
    return {"resource": {"type": "meeting", "id": str(mid)},
            "selection": {"sections": sections or ["summary", "actions", "transcript"]},
            "download_formats": formats or [], "confirm_work_scope": False}


async def preview(client, auth, body):
    r = await client.post("/api/shares/preview", headers=auth, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def publish(client, auth, mid=None, **kwargs):
    if mid is None:
        mid, _, _ = await source()
    body = request(mid, **kwargs)
    p = await preview(client, auth, body)
    r = await client.post("/api/shares", headers=auth, json={**body, "expected_source_revision": p["source_revision"]})
    assert r.status_code == 201, r.text
    data = r.json()
    key = parse_qs(urlsplit(data["capability_url"]).fragment)["key"][0]
    return data, {"Authorization": f"Bearer {key}"}, body


async def test_approved_projection_is_snapshot_and_owner_only(client, auth):
    mid, _, _ = await source(local=True)
    s, cap, body = await publish(client, auth, mid)
    r = await client.get(f'/api/public/shares/{s["id"]}', headers=cap)
    assert r.status_code == 200, r.text
    assert "PRIVATE_" not in r.text
    doc = r.json()["document"]
    assert doc["transcript"]["utterances"][0]["utterance_id"] == "7"
    assert doc["transcript"]["utterances"][0]["start"] == 0
    assert doc["transcript"]["utterances"][0]["overlap"] is None
    assert doc["actions"][0]["owner"] is None
    async with sessionmaker()() as db:
        await db.execute(update(Meeting).where(Meeting.id == mid).values(summary="UNAPPROVED"))
        await db.commit()
    assert "UNAPPROVED" not in (await client.get(f'/api/public/shares/{s["id"]}', headers=cap)).text
    assert (await client.post("/api/shares/preview", headers=cap, json=body)).status_code == 401
    for path in (f"/api/meetings/{mid}", "/api/pms/projects", "/api/artifacts"):
        assert (await client.get(path, headers=cap)).status_code == 401
    assert (await client.delete(f'/api/shares/{s["id"]}', headers=cap)).status_code == 401


@pytest.mark.parametrize('ready', [False, True])
async def test_preview_reports_public_readiness_without_creating_a_share(client, auth, monkeypatch, ready):
    monkeypatch.setattr(get_settings(), 'public_sharing_enabled', ready)
    mid, _, _ = await source()
    data = await preview(client, auth, request(mid))
    assert data['public_sharing_enabled'] is ready
    assert ('sharing_disabled' in data['warnings']) is (not ready)
    assert 'public_ui_not_enabled' not in data['warnings']
    assert (await client.get('/api/shares', headers=auth,
        params={'resource_type': 'meeting', 'resource_id': str(mid)})).json() == []


async def test_capabilities_and_jwt_audiences_never_mix(client, auth):
    s, cap, body = await publish(client, auth)
    b, _, _ = await publish(client, auth)
    public = f'/api/public/shares/{s["id"]}'
    unavailable = {"code": "share_unavailable", "message": "공유를 사용할 수 없습니다."}
    for headers in ({}, auth, {"Authorization": "Bearer bad"}):
        r = await client.get(public + "/download?format=unknown", headers=headers)
        assert r.status_code == 404 and r.json() == unavailable
    assert (await client.get(f'/api/public/shares/{b["id"]}', headers=cap)).json() == unavailable
    for aud in ("agent", "transfer", "download", "api"):
        token = jwt.encode({"iss": "ttakkari", "aud": aud, "sub": "agent", "exp": datetime.now(UTC)+timedelta(hours=1)},
                           get_settings().jwt_secret, algorithm="HS256")
        h = {"Authorization": f"Bearer {token}"}
        assert (await client.get(public, headers=h)).status_code == 404
        assert (await client.post("/api/shares/preview", headers=h, json=body)).status_code == 401
    assert (await client.get('/api/public/shares/not-a-uuid', headers=cap)).json() == unavailable


async def test_preview_conflict_and_concurrent_create(client, auth):
    mid, _, _ = await source()
    body = request(mid)
    p = await preview(client, auth, body)
    async with sessionmaker()() as db:
        await db.execute(update(Meeting).where(Meeting.id == mid).values(summary="Changed"))
        await db.commit()
    stale = await client.post('/api/shares', headers=auth, json={**body, "expected_source_revision": p['source_revision']})
    assert stale.status_code == 409 and stale.json()['code'] == 'source_changed'
    p = await preview(client, auth, body)
    responses = await asyncio.gather(*[client.post('/api/shares', headers=auth,
        json={**body, 'expected_source_revision': p['source_revision']}) for _ in range(2)])
    assert sorted(r.status_code for r in responses) == [201, 409]


@pytest.mark.parametrize('change', ['revoke', 'expire', 'delete_root', 'delete_item', 'scope', 'delete_job', 'reparent_item'])
async def test_binding_changes_deny_and_clear_payload(client, auth, change):
    mid, iid, jid = await source(local=True)
    s, cap, _ = await publish(client, auth, mid)
    if change == 'revoke':
        for _ in range(2):
            assert (await client.delete(f'/api/shares/{s["id"]}', headers=auth)).status_code == 204
    else:
        from ttakkari.models import PublicShare
        async with sessionmaker()() as db:
            if change == 'expire':
                await db.execute(update(PublicShare).where(PublicShare.id == uuid.UUID(s['id'])).values(expires_at=datetime.now(UTC)-timedelta(seconds=1)))
            elif change == 'delete_root':
                await db.execute(delete(Meeting).where(Meeting.id == mid))
            elif change == 'delete_item':
                await db.execute(delete(MeetingItem).where(MeetingItem.id == iid))
            elif change == 'delete_job':
                await db.execute(delete(LocalSttJob).where(LocalSttJob.id == jid))
            elif change == 'scope':
                await db.execute(update(Meeting).where(Meeting.id == mid).values(scope='work'))
                await db.execute(update(Meeting).where(Meeting.id == mid).values(scope='personal'))
            else:
                other, _, _ = await source()
                await db.execute(update(MeetingItem).where(MeetingItem.id == iid).values(meeting_id=other))
            await db.commit()
    r = await client.get(f'/api/public/shares/{s["id"]}', headers=cap)
    assert r.status_code == 404, r.text
    from ttakkari.sharing.service import cleanup
    await cleanup()
    from ttakkari.models import PublicShare
    async with sessionmaker()() as db:
        row = await db.get(PublicShare, uuid.UUID(s['id']))
        assert row.secret_hash is None and row.snapshot is None and row.source_bindings == []


async def test_strict_inputs_work_confirmation_and_disabled_default(client, auth, monkeypatch):
    mid, _, _ = await source(scope='work')
    body = request(mid)
    assert (await client.post('/api/shares/preview', headers=auth, json=body)).status_code == 403
    body['confirm_work_scope'] = True
    for extra in ({'document': {}}, {'confirm_work_scope': 1}, {'download_formats': ['pdf']}):
        assert (await client.post('/api/shares/preview', headers=auth, json={**body, **extra})).status_code == 422
    bad = {**body, 'selection': {'sections': ['summary'], 'include_evidence': True}}
    assert (await client.post('/api/shares/preview', headers=auth, json=bad)).status_code == 422
    p = await preview(client, auth, body)
    monkeypatch.setattr(get_settings(), 'public_sharing_enabled', False)
    r = await client.post('/api/shares', headers=auth, json={**body, 'expected_source_revision': p['source_revision']})
    assert r.status_code == 409 and r.json()['code'] == 'sharing_disabled'
    assert (await client.get('/api/public/shares/anything')).status_code == 404


async def test_delivery_headers_auth_before_query_and_supported_format_rules(client, auth):
    s, cap, _ = await publish(client, auth)
    path = f'/api/public/shares/{s["id"]}'
    for method, url, h, status in [('GET', path, cap, 200), ('HEAD', path, cap, 200),
        ('GET', path, {}, 404), ('GET', path+'/download?format=json&revision=1', cap, 403),
        ('HEAD', path+'/download?format=pdf', {}, 404)]:
        r = await client.request(method, url, headers=h)
        assert r.status_code == status, r.text
        for key, value in {'cache-control': 'private, no-store', 'referrer-policy': 'no-referrer',
            'x-content-type-options': 'nosniff', 'x-robots-tag': 'noindex, nofollow, noarchive'}.items():
            assert r.headers[key] == value
        assert 'authorization' in r.headers['vary'].lower()
        assert 'etag' not in r.headers and 'last-modified' not in r.headers
    r = await client.get(path, headers={**cap, 'Range': 'bytes=0-1'})
    assert r.status_code == 416
    assert (await client.get(path, headers={**cap, 'If-None-Match': '*'})).status_code == 200
    for origin, status in [('http://localhost:5173', 200), ('https://evil.test', 400)]:
        r = await client.options(path, headers={'Origin': origin, 'Access-Control-Request-Method': 'HEAD',
            'Access-Control-Request-Headers': 'Authorization'})
        assert r.status_code == status


async def test_public_downloads_use_registered_formatters_and_options_match_selected_parts(client, auth):
    mid, _, _ = await source(local=True)
    body = request(mid, sections=['summary', 'actions', 'transcript'], formats=['txt', 'md', 'json', 'csv'])
    p = await preview(client, auth, body)
    options = {item['format']: item for item in p['format_options']}
    assert all(options[fmt]['enabled'] and options[fmt]['applicable'] for fmt in ('txt', 'md', 'json', 'csv'))
    assert options['csv']['parts'] == ['utterances', 'actions']
    assert all(not options[fmt]['enabled'] for fmt in ('srt', 'vtt', 'docx', 'pdf'))
    s, cap, _ = await publish(client, auth, mid, sections=body['selection']['sections'], formats=body['download_formats'])
    root = f'/api/public/shares/{s["id"]}/download?revision=1'
    for fmt, part, content_type in [('txt', 'document', 'text/plain; charset=utf-8'),
        ('md', 'document', 'text/markdown; charset=utf-8'), ('json', 'document', 'application/json'),
        ('csv', 'actions', 'text/csv; charset=utf-8')]:
        response = await client.get(f'{root}&format={fmt}&part={part}', headers=cap)
        assert response.status_code == 200, response.text
        assert response.headers['content-type'] == content_type
        assert f'meeting-{s["id"]}-r1.{fmt}' in response.headers['content-disposition']
        assert 'PRIVATE_' not in response.text
        if fmt == 'csv':
            assert response.content.startswith(b'\xef\xbb\xbf')


async def test_summary_only_preview_and_exports_never_resolve_transcript_job(client, auth, monkeypatch):
    from ttakkari.meetings import document as meeting_document
    async def forbidden_job_lookup(*args, **kwargs):
        pytest.fail('summary-only selection must not fetch transcript jobs or utterances')
    monkeypatch.setattr(meeting_document, 'adopted_job', forbidden_job_lookup)
    mid, _, _ = await source(local=True)
    body = request(mid, sections=['summary'], formats=['json'])
    p = await preview(client, auth, body)
    assert p['document']['transcript'] is None
    assert p['document']['summary'] == 'Approved summary'
    result = await client.post('/api/exports', headers=auth, json={
        'resource': body['resource'], 'selection': body['selection'], 'format': 'json', 'part': 'document',
        'expected_source_revision': p['source_revision']})
    assert result.status_code == 200, result.text
    assert 'Approved transcript' not in result.text


async def test_owner_export_is_private_authenticated_revision_bound_and_not_feature_flagged(client, auth, monkeypatch):
    from ttakkari.config import get_settings
    from ttakkari.sharing import delivery
    mid, _, _ = await source(local=True)
    selection = {"sections": ["transcript"]}
    p = await preview(client, auth, request(mid, sections=selection["sections"]))
    body = {"resource": {"type": "meeting", "id": str(mid)}, "selection": selection,
        "format": "csv", "part": "utterances", "expected_source_revision": p["source_revision"]}
    assert (await client.post("/api/exports", json=body)).status_code == 401
    monkeypatch.setattr(get_settings(), "public_sharing_enabled", False)
    r = await client.post("/api/exports", headers=auth, json=body)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == delivery.MEDIA["csv"]
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "filename=\"meeting-export.csv\"" in r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")
    stale = await client.post("/api/exports", headers=auth, json={**body, "expected_source_revision": "sha256:" + "0" * 64})
    assert stale.status_code == 409 and stale.json()["code"] == "source_changed"
    unsupported = await client.post("/api/exports", headers=auth, json={**body, "format": "srt"})
    assert unsupported.status_code == 422 and unsupported.json()["code"] == "format_not_enabled"

@pytest.mark.parametrize('mutation,expected', [('revoke', 404), ('expire', 404), ('root', 404),
    ('item', 404), ('scope', 404), ('republish', 409)])
async def test_http_download_final_gate_after_concurrent_commit(client, auth, monkeypatch, mutation, expected):
    from ttakkari.sharing import delivery
    started, resume = asyncio.Event(), asyncio.Event()
    async def synthetic_formatter(document, part):
        assert 'PRIVATE_' not in document.model_dump_json()
        started.set()
        await resume.wait()
        return b'SYNTHETIC_GENERATED_BYTES'
    monkeypatch.setitem(delivery.FORMATTERS, 'json', synthetic_formatter)
    mid, iid, _ = await source()
    s, cap, body = await publish(client, auth, mid, formats=['json'])
    task = asyncio.create_task(client.get(f'/api/public/shares/{s["id"]}/download?format=json&revision=1', headers=cap))
    await asyncio.wait_for(started.wait(), 5)
    try:
        if mutation == 'revoke':
            assert (await client.delete(f'/api/shares/{s["id"]}', headers=auth)).status_code == 204
        elif mutation == 'republish':
            p = await preview(client, auth, body)
            r = await client.put(f'/api/shares/{s["id"]}/publication', headers=auth,
                json={**body, 'expected_source_revision': p['source_revision'], 'expected_publication_revision': 1})
            assert r.status_code == 200, r.text
        else:
            from ttakkari.models import PublicShare
            async with sessionmaker()() as db:
                if mutation == 'expire':
                    await db.execute(update(PublicShare).values(expires_at=datetime.now(UTC)-timedelta(seconds=1)))
                elif mutation == 'root':
                    await db.execute(delete(Meeting).where(Meeting.id == mid))
                elif mutation == 'item':
                    await db.execute(delete(MeetingItem).where(MeetingItem.id == iid))
                else:
                    await db.execute(update(Meeting).where(Meeting.id == mid).values(scope='work'))
                await db.commit()
    finally:
        resume.set()
    r = await asyncio.wait_for(task, 5)
    assert r.status_code == expected, r.text
    assert b'SYNTHETIC_GENERATED_BYTES' not in r.content


async def test_concurrent_republication_cas_and_revoke_cannot_resurrect(client, auth):
    s, cap, body = await publish(client, auth)
    p = await preview(client, auth, body)
    payload = {**body, 'expected_source_revision': p['source_revision'], 'expected_publication_revision': 1}
    responses = await asyncio.gather(*[client.put(f'/api/shares/{s["id"]}/publication', headers=auth, json=payload) for _ in range(2)])
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (await client.delete(f'/api/shares/{s["id"]}', headers=auth)).status_code == 204
    payload['expected_publication_revision'] = 2
    assert (await client.put(f'/api/shares/{s["id"]}/publication', headers=auth, json=payload)).status_code == 409
    assert (await client.get(f'/api/public/shares/{s["id"]}', headers=cap)).status_code == 404

async def test_quality_blocked_job_is_not_a_public_candidate(client, auth):
    mid, _, jid = await source(local=True)
    async with sessionmaker()() as db:
        job = await db.get(LocalSttJob, jid)
        job.result_metadata = {**job.result_metadata, 'quality': {'auto_analysis_allowed': False}}
        await db.commit()
    r = await client.post('/api/shares/preview', headers=auth, json=request(mid))
    assert r.status_code == 409 and r.json()['code'] == 'quality_review_required'


async def test_snapshot_corruption_fails_closed_without_sentinel(client, auth):
    from ttakkari.models import PublicShare
    s, cap, _ = await publish(client, auth)
    async with sessionmaker()() as db:
        row = await db.get(PublicShare, uuid.UUID(s['id']))
        row.snapshot = {**row.snapshot, 'private': 'PRIVATE_SENTINEL'}
        await db.commit()
    r = await client.get(f'/api/public/shares/{s["id"]}', headers=cap)
    assert r.status_code == 503 and 'PRIVATE_' not in r.text


async def test_download_part_enum_is_validated_after_capability(client, auth, monkeypatch):
    from ttakkari.sharing import delivery
    async def fmt(doc, part):
        return b'SYNTHETIC'
    monkeypatch.setitem(delivery.FORMATTERS, 'json', fmt)
    s, cap, _ = await publish(client, auth, formats=['json'])
    url = f'/api/public/shares/{s["id"]}/download?format=json&revision=1&part=garbage'
    assert (await client.get(url, headers=cap)).status_code == 422
    assert (await client.get(url)).status_code == 404


async def test_successful_generated_bytes_are_snapshot_only_and_bounded(client, auth, monkeypatch):
    from ttakkari.sharing import delivery
    async def fmt(doc, part):
        assert part == 'document'
        assert 'PRIVATE_' not in doc.model_dump_json()
        return doc.model_dump_json().encode()
    monkeypatch.setitem(delivery.FORMATTERS, 'json', fmt)
    mid, _, _ = await source(local=True)
    s, cap, _ = await publish(client, auth, mid, formats=['json'])
    async with sessionmaker()() as db:
        await db.execute(update(Meeting).where(Meeting.id == mid).values(title='NEW_UNAPPROVED'))
        await db.commit()
    url = f'/api/public/shares/{s["id"]}/download?format=json&revision=1'
    r = await client.get(url, headers=cap)
    assert r.status_code == 200, r.text
    assert 'NEW_UNAPPROVED' not in r.text and 'PRIVATE_' not in r.text
    assert r.json()['kind'] == 'meeting'
    assert r.headers['content-disposition'].startswith('attachment; filename="meeting-')
    assert (await client.head(url, headers=cap)).content == b''
    monkeypatch.setattr(delivery, 'MAX_BYTES', 2)
    assert (await client.get(url, headers=cap)).status_code == 413


async def test_generation_limits_and_bad_key_buckets_are_bounded(client, auth, monkeypatch):
    from ttakkari.sharing import delivery
    started, resume = asyncio.Queue(), asyncio.Event()
    async def fmt(doc, part):
        started.put_nowait(True)
        await resume.wait()
        return b'SYNTHETIC'
    monkeypatch.setitem(delivery.FORMATTERS, 'json', fmt)
    s, cap, _ = await publish(client, auth, formats=['json'])
    url = f'/api/public/shares/{s["id"]}/download?format=json&revision=1'
    tasks = [asyncio.create_task(client.get(url, headers=cap)) for _ in range(2)]
    await asyncio.wait_for(started.get(), 5)
    await asyncio.wait_for(started.get(), 5)
    try:
        r = await client.get(url, headers=cap)
        assert r.status_code == 503 and r.json()['code'] == 'export_busy'
        assert r.headers['retry-after']
    finally:
        resume.set()
    assert [r.status_code for r in await asyncio.gather(*tasks)] == [200, 200]
    for _ in range(7):
        assert (await client.get(url, headers=cap)).status_code == 200
    assert (await client.get(url, headers=cap)).status_code == 429
    for _ in range(30):
        assert (await client.get('/api/public/shares/random')).status_code == 404
    r = await client.get('/api/public/shares/another')
    assert r.status_code == 429 and r.headers['retry-after']
    delivery.failures.reset()
    for i in range(2048):
        delivery.failures.consume(str(i), 30)
    with pytest.raises(Exception) as exc:
        delivery.failures.consume('overflow', 30)
    assert exc.value.status_code == 429 and len(delivery.failures.buckets) == 2048


async def test_public_db_failure_is_generic_503(client, auth, monkeypatch, caplog):
    from ttakkari.sharing import service
    s, cap, _ = await publish(client, auth)
    def broken():
        raise RuntimeError('PRIVATE_SENTINEL')
    monkeypatch.setattr(service, 'sessionmaker', broken)
    r = await client.get(f'/api/public/shares/{s["id"]}', headers=cap)
    assert r.status_code == 503 and 'PRIVATE_' not in r.text
    assert 'PRIVATE_' not in caplog.text and cap['Authorization'] not in caplog.text


async def test_preview_has_no_reconcile_side_effect_and_summary_does_not_read_transcript(client, auth):
    from sqlalchemy import event

    # Engine already exists; get the bound engine rather than reinitialize global DB state.
    engine = sessionmaker().kw['bind'].sync_engine
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    mid, _, _ = await source(local=True)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        p = await preview(client, auth, request(mid, sections=['summary']))
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert p['document']['transcript'] is None
    assert not any('local_stt_jobs' in s or 'meetings.transcript' in s for s in statements)
    assert not any(s.lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for s in statements)


async def test_signed_links_are_rejected_without_echoing_secret(client, auth):
    mid, _, _ = await source()
    async with sessionmaker()() as db:
        await db.execute(update(Meeting).where(Meeting.id == mid).values(summary='https://test/api/dl/PRIVATE_SENTINEL'))
        await db.commit()
    r = await client.post('/api/shares/preview', headers=auth, json=request(mid))
    assert r.status_code == 422 and r.json()['code'] == 'private_link_in_content'
    assert 'PRIVATE_' not in r.text

async def wait_for_db_lock():
    async def blocked():
        while True:
            async with sessionmaker()() as db:
                n = await db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%FOR SHARE%'"))
            if n:
                return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(blocked(), 5)


@pytest.mark.parametrize('change', ['scope', 'expiry'])
async def test_final_gate_waits_on_real_row_locks_and_sees_committed_change(client, auth, monkeypatch, change):
    from ttakkari.models import PublicShare
    from ttakkari.sharing import delivery
    started, resume = asyncio.Event(), asyncio.Event()
    async def fmt(doc, part):
        started.set()
        await resume.wait()
        return b'NEVER_DELIVER'
    monkeypatch.setitem(delivery.FORMATTERS, 'json', fmt)
    mid, _, _ = await source()
    s, cap, _ = await publish(client, auth, mid, formats=['json'])
    task = asyncio.create_task(client.get(f'/api/public/shares/{s["id"]}/download?format=json&revision=1', headers=cap))
    await asyncio.wait_for(started.wait(), 5)
    async with sessionmaker()() as writer:
        if change == 'scope':
            await writer.execute(update(Meeting).where(Meeting.id == mid).values(scope='work'))
        else:
            await writer.execute(update(PublicShare).where(PublicShare.id == uuid.UUID(s['id']))
                .values(expires_at=datetime.now(UTC)-timedelta(seconds=1)))
        resume.set()
        await wait_for_db_lock()
        assert not task.done()
        await writer.commit()
    r = await asyncio.wait_for(task, 5)
    assert r.status_code == 404 and b'NEVER_DELIVER' not in r.content


async def test_view_final_gate_after_revoke_before_response_start(client, auth, monkeypatch):
    from ttakkari.sharing import service
    s, cap, _ = await publish(client, auth)
    original = service.final_gate
    started, resume = asyncio.Event(), asyncio.Event()
    async def pause(grant):
        started.set()
        await resume.wait()
        await original(grant)
    monkeypatch.setattr(service, 'final_gate', pause)
    task = asyncio.create_task(client.get(f'/api/public/shares/{s["id"]}', headers=cap))
    await asyncio.wait_for(started.wait(), 5)
    assert (await client.delete(f'/api/shares/{s["id"]}', headers=auth)).status_code == 204
    resume.set()
    r = await asyncio.wait_for(task, 5)
    assert r.status_code == 404 and 'Approved summary' not in r.text


async def test_people_evidence_opt_in_and_adopted_job_selection(client, auth):
    mid, _, jid = await source(local=True)
    async with sessionmaker()() as db:
        queued = LocalSttJob(meeting_id=mid, status='queued', transcript='OTHER')
        child = LocalSttJob(meeting_id=mid, parent_job_id=jid, status='succeeded', transcript='Approved transcript',
            result_metadata={'utterances': [{'private': 'PRIVATE_SENTINEL'}]})
        db.add_all([queued, child])
        await db.flush()
        await db.execute(update(Meeting).where(Meeting.id == mid).values(provider_job_id=str(queued.id)))
        await db.commit()
    body = request(mid)
    body['selection'].update(include_people=True, include_evidence=True)
    p = await preview(client, auth, body)
    doc = p['document']
    assert doc['transcript']['adopted_stt_job_id'] == str(jid)
    assert doc['participants'] == ['PRIVATE_PERSON']
    assert doc['transcript']['utterances'][0]['speaker_name'] == 'PRIVATE_PERSON'
    assert doc['actions'][0]['owner'] == 'PRIVATE_PERSON'
    assert doc['actions'][0]['evidence_start'] == 0
    assert doc['actions'][0]['evidence_utterance_id'] == '7'
    assert 'PRIVATE_SENTINEL' not in str(doc)


async def test_reissue_never_recovers_old_key_and_owner_metadata_has_no_secret(client, auth):
    mid, _, _ = await source()
    s, cap, _ = await publish(client, auth, mid)
    for url in (f'/api/shares/{s["id"]}', f'/api/shares?resource_type=meeting&resource_id={mid}'):
        r = await client.get(url, headers=auth)
        assert r.status_code == 200
        assert 'ps1_' not in r.text and 'Approved summary' not in r.text and 'source_bindings' not in r.text
    assert (await client.delete(f'/api/shares/{s["id"]}', headers=auth)).status_code == 204
    new, new_cap, _ = await publish(client, auth, mid)
    assert new['id'] != s['id'] and new_cap != cap
    assert (await client.get(f'/api/public/shares/{new["id"]}', headers=cap)).status_code == 404
    assert (await client.get(f'/api/public/shares/{s["id"]}', headers=new_cap)).status_code == 404


async def test_every_owner_management_operation_requires_owner(client, auth):
    s, cap, body = await publish(client, auth)
    for h in ({}, cap):
        for method, url, payload in [('POST', '/api/shares', body),
            ('PUT', f'/api/shares/{s["id"]}/publication', body), ('DELETE', f'/api/shares/{s["id"]}', None),
            ('GET', f'/api/shares/{s["id"]}', None)]:
            assert (await client.request(method, url, headers=h, json=payload)).status_code == 401

async def test_share_audit_records_only_allowlisted_metadata(client, auth, caplog):
    from ttakkari.models import AuditLog
    s, cap, _ = await publish(client, auth)
    assert (await client.get(f'/api/public/shares/{s["id"]}', headers=cap)).status_code == 200
    assert (await client.delete(f'/api/shares/{s["id"]}', headers=auth)).status_code == 204
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.target_type == 'public_share')))
        actions = {row.action for row in rows}
        assert {'share.create', 'share.read', 'share.revoke'} <= actions
        for row in rows:
            assert row.actor in ('owner', 'anonymous')
            assert row.ip is None and row.user_agent is None
            assert set(row.detail) <= {'format'}
            assert 'PRIVATE_' not in str(row.detail) and 'ps1_' not in str(row.detail)
    assert 'PRIVATE_' not in caplog.text and cap['Authorization'] not in caplog.text

async def test_public_access_logs_never_record_query_or_raw_capability(client, auth, caplog):
    import logging
    s, cap, _ = await publish(client, auth)
    await client.get(f'/api/public/shares/{s["id"]}', headers=cap)
    caplog.set_level(logging.INFO, logger='uvicorn.access')
    logging.getLogger('uvicorn.access').info('%s - "%s %s HTTP/%s" %d', '127.0.0.1', 'GET',
        f'/api/public/shares/{s["id"]}?key=PRIVATE_SENTINEL', '1.1', 404)
    assert 'PRIVATE_SENTINEL' not in caplog.text

async def test_expiry_crossed_during_final_lock_wait_uses_wall_clock(client, auth, monkeypatch):
    from ttakkari.models import PublicShare
    from ttakkari.sharing import delivery
    started, resume = asyncio.Event(), asyncio.Event()
    async def fmt(doc, part):
        started.set()
        await resume.wait()
        return b'EXPIRED_BYTES'
    monkeypatch.setitem(delivery.FORMATTERS, 'json', fmt)
    s, cap, _ = await publish(client, auth, formats=['json'])
    ident = uuid.UUID(s['id'])
    async with sessionmaker()() as db:
        await db.execute(update(PublicShare).where(PublicShare.id == ident)
            .values(expires_at=datetime.now(UTC)+timedelta(seconds=0.3)))
        await db.commit()
    task = asyncio.create_task(client.get(f'/api/public/shares/{s["id"]}/download?format=json&revision=1', headers=cap))
    await asyncio.wait_for(started.wait(), 5)
    async with sessionmaker()() as writer:
        await writer.execute(select(PublicShare.id).where(PublicShare.id == ident).with_for_update())
        resume.set()
        await wait_for_db_lock()
        # The final authorization transaction began before expiry. PG now() is
        # frozen there; clock_timestamp() after lock wait must reject these bytes.
        await writer.execute(text('SELECT pg_sleep(0.4)'))
        await writer.commit()
    r = await asyncio.wait_for(task, 5)
    assert r.status_code == 404 and b'EXPIRED_BYTES' not in r.content


async def test_already_authorized_transfer_can_finish_after_revoke(client, auth, monkeypatch):
    from ttakkari.sharing.delivery import AuthorizedResponse
    s, cap, _ = await publish(client, auth)
    started, resume = asyncio.Event(), asyncio.Event()
    original = AuthorizedResponse.__call__
    async def pause_after_start(self, scope, receive, send):
        async def pause_send(message):
            await send(message)
            if message['type'] == 'http.response.start':
                started.set()
                await resume.wait()
        await original(self, scope, receive, pause_send)
    monkeypatch.setattr(AuthorizedResponse, '__call__', pause_after_start)
    task = asyncio.create_task(client.get(f'/api/public/shares/{s["id"]}', headers=cap))
    await asyncio.wait_for(started.wait(), 5)
    assert (await client.delete(f'/api/shares/{s["id"]}', headers=auth)).status_code == 204
    resume.set()
    assert (await asyncio.wait_for(task, 5)).status_code == 200
    assert (await client.get(f'/api/public/shares/{s["id"]}', headers=cap)).status_code == 404

async def test_future_formatter_permissions_and_inapplicable_parts(client, auth, monkeypatch):
    from ttakkari.sharing import delivery
    async def fmt(doc, part):
        pytest.fail('inapplicable or ungranted formatter must never execute')
    monkeypatch.setitem(delivery.FORMATTERS, 'json', fmt)
    monkeypatch.setitem(delivery.FORMATTERS, 'csv', fmt)
    mid, _, _ = await source()
    async with sessionmaker()() as db:
        await db.execute(update(Meeting).where(Meeting.id == mid).values(status='transcribed'))
        await db.commit()
    s, cap, _ = await publish(client, auth, mid, sections=['actions', 'transcript'], formats=['csv'])
    path = f'/api/public/shares/{s["id"]}/download?revision=1'
    for query, status, code in [('&format=json', 403, 'download_not_allowed'),
        ('&format=csv&part=actions', 409, 'analysis_not_generated'),
        ('&format=csv&part=utterances', 409, 'format_not_applicable'),
        ('&format=csv&part=decisions', 403, 'download_not_allowed')]:
        r = await client.get(path+query, headers=cap)
        assert r.status_code == status and r.json()['code'] == code, r.text

async def test_disabled_public_feature_always_404_without_db_or_rate_buckets(client, monkeypatch):
    from ttakkari.sharing import service
    monkeypatch.setattr(get_settings(), 'public_sharing_enabled', False)
    def forbidden_db():
        pytest.fail('disabled public feature must not read DB')
    monkeypatch.setattr(service, 'sessionmaker', forbidden_db)
    for _ in range(32):
        assert (await client.get('/api/public/shares/unknown')).status_code == 404
