from __future__ import annotations

import uuid

from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Notification, Run, RunStatus, SessionKind
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.push import service

PATH = '/api/notifications/preferences'


async def test_preferences_auth_and_strict_input(client, auth):
    assert (await client.get(PATH)).status_code == 401
    assert (await client.patch(PATH, json={'kind': 'push.weather', 'enabled': False})).status_code == 401
    for body in [
        {'kind': 'push.unknown', 'enabled': False},
        {'kind': 'push.weather', 'enabled': 'false'},
        {'kind': 'push.weather', 'enabled': False, 'extra': True},
        {'kind': 'push.test_sent', 'enabled': False},
    ]:
        assert (await client.patch(PATH, json=body, headers=auth)).status_code == 422


async def test_defaults_are_complete_and_save_individual_choices(client, auth):
    response = await client.get(PATH, headers=auth)
    assert response.status_code == 200
    items = {x['kind']: x for x in response.json()['items']}
    assert set(items) == {k.value for k in service.PushKind if k not in {service.PushKind.test, service.PushKind.notebooklm}}
    assert items['push.weather']['enabled'] is True
    assert items['push.calendar_reminder']['enabled'] is True
    assert items['push.agent']['enabled'] is True
    assert items['push.worker_agent']['enabled'] is False
    assert items['push.worker_failed']['enabled'] is False
    assert items['push.worker_waiting']['enabled'] is False
    for kind in ['push.weather', 'push.calendar_reminder']:
        changed = await client.patch(PATH, json={'kind': kind, 'enabled': False}, headers=auth)
        assert changed.status_code == 200
    saved = {x['kind']: x['enabled'] for x in (await client.get(PATH, headers=auth)).json()['items']}
    assert saved['push.weather'] is False and saved['push.calendar_reminder'] is False
    assert saved['push.agent'] is True


async def test_each_preference_controls_push_and_inbox(client, auth, monkeypatch):
    calls = []
    async def deliver(payload, **kwargs):
        calls.append(payload)
        return {'sent': 1, 'removed': 0, 'failed': 0}
    monkeypatch.setattr(service, '_deliver', deliver)
    response = await client.get(PATH, headers=auth)
    assert response.status_code == 200
    for item in response.json()['items']:
        kind = item['kind']
        assert (await client.patch(PATH, json={'kind': kind, 'enabled': False}, headers=auth)).status_code == 200
        result = await service.notify(service.PushKind(kind), kind)
        assert result.get('suppressed') == 1 and not calls
        async with sessionmaker()() as db:
            assert await db.scalar(select(Notification).where(Notification.title == kind)) is None
        assert (await client.patch(PATH, json={'kind': kind, 'enabled': True}, headers=auth)).status_code == 200
        await service.notify(service.PushKind(kind), kind)
        assert len(calls) == 1
        calls.clear()
        async with sessionmaker()() as db:
            assert await db.scalar(select(Notification).where(Notification.title == kind)) is not None


async def test_worker_and_nested_lead_notifications_are_separate(client, auth, new_session, monkeypatch):
    _, sess = await new_session()
    sid = uuid.UUID(sess['id'])
    async with sessionmaker()() as db:
        desk = ChatSession(workspace_id=uuid.UUID(sess['workspace_id']), title='접수', engine='codex', kind=SessionKind.desk)
        db.add(desk)
        await db.flush()
        lead = await db.get(ChatSession, sid)
        lead.kind, lead.parent_id = SessionKind.orchestrator, desk.id
        worker = ChatSession(workspace_id=lead.workspace_id, title='하위 작업', engine='codex', kind=SessionKind.worker, parent_id=sid)
        nested = ChatSession(workspace_id=lead.workspace_id, title='하위 기획', engine='codex', kind=SessionKind.orchestrator, parent_id=sid)
        db.add_all([worker, nested])
        await db.commit()
        children = [worker.id, nested.id]
    calls = []
    async def deliver(payload, **kwargs):
        calls.append(payload)
        return {'sent': 1, 'removed': 0, 'failed': 0}
    monkeypatch.setattr(service, '_deliver', deliver)
    for child in children:
        h = {'Authorization': f'Bearer {issue_token(str(child))}'}
        result = await client.post('/api/agent/notify', json={'title': '하위 완료', 'body': '완료'}, headers=h)
        assert result.status_code == 200 and result.json().get('suppressed') == 1
        await service.notify_run_finished(Run(id=uuid.uuid4(), session_id=child, status=RunStatus.failed), '하위 실패')
        from ttakkari.runs.manager import _notify_waiting
        await _notify_waiting(Run(id=uuid.uuid4(), session_id=child), '하위 대기', 2000000000)
    assert not calls
    h = {'Authorization': f'Bearer {issue_token(str(sid))}'}
    result = await client.post('/api/agent/notify', json={'title': '최종 결과', 'body': '완료'}, headers=h)
    assert result.json()['sent'] == 1 and len(calls) == 1
    assert (await client.get(PATH, headers=h)).status_code in {401, 403}
    assert (await client.patch(PATH, json={'kind': 'push.worker_agent', 'enabled': True}, headers=auth)).status_code == 200
    h = {'Authorization': f'Bearer {issue_token(str(children[0]))}'}
    result = await client.post('/api/agent/notify', json={'title': '선택한 하위 결과'}, headers=h)
    assert result.json()['sent'] == 1
    page = (await client.get('/api/notifications', headers=auth)).json()
    assert [(x['kind'], x['category']) for x in page['items']] == [('push.worker_agent', 'tasks'), ('push.agent', 'assistant')]


async def test_weather_optout_is_not_a_delivery_error_or_retry(client, auth, monkeypatch):
    from datetime import date

    from ttakkari.models import WeatherBriefing
    from ttakkari.weather import service as weather
    assert (await client.patch(PATH, json={'kind': 'push.weather', 'enabled': False}, headers=auth)).status_code == 200
    async with sessionmaker()() as db:
        row = WeatherBriefing(day=date(2026, 11, 6), status='ready', briefing={'summary': ['맑아요']})
        db.add(row)
        await db.commit()
        rid = row.id
    await weather._publish(rid)
    async with sessionmaker()() as db:
        row = await db.get(WeatherBriefing, rid)
        assert row.push_sent_at is None
        assert row.push_error == 'disabled_by_preference'
    calls = []
    async def publish(_):
        calls.append(True)
    monkeypatch.setattr(weather, '_publish', publish)
    await weather.advance(rid)
    assert not calls
