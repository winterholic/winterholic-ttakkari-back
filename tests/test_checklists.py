from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from test_context import _agent, _session

from ttakkari.planner.service import KST


async def test_daily_crud_and_fixed_reset(client, auth, monkeypatch):
    day = datetime.now(KST).date()
    base = "/api/checklists"
    r = await client.post(base + "/templates", headers=auth, json={"title": "물 마시기"})
    assert r.status_code == 201, r.text
    from ttakkari.planner import checklists as svc
    template = r.json()
    items = (await client.get(base, headers=auth)).json()["items"]
    assert len(items) == 1 and items[0]["template_id"] == template["id"]
    item = items[0]
    r = await client.patch(base + "/items/" + item["id"], headers=auth, json={"checked": True, "title": "물 두 잔"})
    assert r.status_code == 200 and r.json()["checked"]
    monkeypatch.setattr(svc, "today", lambda: day + timedelta(days=1))
    next_items = (await client.get(base, headers=auth)).json()["items"]
    assert len(next_items) == 1 and not next_items[0]["checked"] and next_items[0]["title"] == "물 마시기"
    r = await client.post(base + "/items", headers=auth, json={"title": "병원 예약", "day": str(day)})
    assert r.status_code == 201
    assert (await client.delete(base + "/items/" + r.json()["id"], headers=auth)).status_code == 204
    assert (await client.delete(base + "/items/" + next_items[0]["id"], headers=auth)).status_code == 204
    assert (await client.get(base, headers=auth)).json()["items"] == []  # 삭제된 고정 항목을 다시 만들지 않는다.
    assert (await client.patch(base + "/templates/" + template["id"], headers=auth, json={"title": "운동"})).status_code == 200
    assert (await client.delete(base + "/templates/" + template["id"], headers=auth)).status_code == 204
    monkeypatch.setattr(svc, "today", lambda: day + timedelta(days=2))
    assert (await client.get(base, headers=auth)).json()["items"] == []
    past = (await client.get(base, headers=auth, params={"day": str(day)})).json()["items"]
    assert len(past) == 1 and past[0]["checked"] and past[0]["title"] == "물 두 잔"


async def test_auth_validation_and_mcp(client, auth, make_dir):
    assert (await client.get("/api/checklists")).status_code == 401
    assert (await client.post("/api/checklists/items", headers=auth, json={"title": "  "})).status_code == 422
    assert (await client.get("/api/checklists", headers=auth, params={"day": "invalid"})).status_code == 422
    session = await _session(client, auth, make_dir)
    headers = _agent(session["id"])
    r = await client.post("/api/agent/checklists/items", headers=headers, json={"title": "MCP 항목"})
    assert r.status_code == 201, r.text
    item = r.json()
    assert (await client.patch("/api/agent/checklists/items/" + item["id"], headers=headers, json={"checked": True})).json()["checked"]
    assert len((await client.get("/api/agent/checklists", headers=headers)).json()["items"]) == 1
    assert (await client.delete("/api/agent/checklists/items/" + item["id"], headers=headers)).status_code == 204
    from ttakkari.orchestrator import mcp_server
    names = {t.name for t in await mcp_server.server.list_tools()}
    assert {"list_checklist", "create_checklist_item", "update_checklist_item", "delete_checklist_item", "list_checklist_templates", "create_checklist_template", "update_checklist_template", "delete_checklist_template"} <= names


async def test_reminder_slots_skip_completed_and_deduplicate(client, auth, monkeypatch):
    from ttakkari.planner import checklist_reminders as reminders
    from ttakkari.planner import checklists as svc
    sent = AsyncMock()
    monkeypatch.setattr(reminders, "notify", sent)
    day = svc.today()
    at = lambda h, m=0: datetime(day.year, day.month, day.day, h, m, tzinfo=KST)
    assert await reminders.send_due(at(7, 29)) == 0
    assert await reminders.send_due(at(7, 30)) == 1  # 아침에는 항목이 없어도 정리 알림
    assert await reminders.send_due(at(7, 31)) == 0
    assert await reminders.send_due(at(12)) == 0
    item = (await client.post("/api/checklists/items", headers=auth, json={"title": "운동"})).json()
    assert await reminders.send_due(at(12, 1)) == 0  # 처리한 시간대는 다시 알리지 않는다.
    assert await reminders.send_due(at(16)) == 1
    assert await reminders.send_due(at(16, 1)) == 0
    await client.patch("/api/checklists/items/" + item["id"], headers=auth, json={"checked": True})
    assert await reminders.send_due(at(20)) == 0
    await client.patch("/api/checklists/items/" + item["id"], headers=auth, json={"checked": False})
    assert await reminders.send_due(at(22)) == 1
    assert sent.await_count == 3
    assert sent.call_args.kwargs["url"].startswith("/checklists")


async def test_late_scan_only_latest_slot_and_next_day(client, auth, monkeypatch):
    from ttakkari.planner import checklist_reminders as reminders
    from ttakkari.planner import checklists as svc
    monkeypatch.setattr(reminders, "notify", AsyncMock())
    day = svc.today()
    await client.post("/api/checklists/items", headers=auth, json={"title": "운동"})
    late = datetime(day.year, day.month, day.day, 20, 5, tzinfo=KST)
    assert await reminders.send_due(late) == 1
    assert await reminders.send_due(late) == 0
    tomorrow = late + timedelta(days=1)
    assert await reminders.send_due(tomorrow.replace(hour=7, minute=30)) == 1


async def test_fixed_edits_preserve_existing_day_and_apply_tomorrow(client, auth, monkeypatch):
    from ttakkari.planner import checklists as svc
    day = svc.today()
    fixed = (await client.post('/api/checklists/templates', headers=auth, json={'title': '걷기'})).json()
    await client.patch('/api/checklists/templates/' + fixed['id'], headers=auth, json={'title': '달리기'})
    current = (await client.get('/api/checklists', headers=auth)).json()['items']
    assert current[0]['title'] == '걷기'
    monkeypatch.setattr(svc, 'today', lambda: day + timedelta(days=1))
    tomorrow = (await client.get('/api/checklists', headers=auth)).json()['items']
    assert tomorrow[0]['title'] == '달리기' and not tomorrow[0]['checked']


async def test_parallel_scans_claim_one_reminder(client, auth, monkeypatch):
    import asyncio

    from ttakkari.planner import checklist_reminders as reminders
    from ttakkari.planner import checklists as svc
    sent = AsyncMock()
    monkeypatch.setattr(reminders, 'notify', sent)
    day = svc.today()
    await client.post('/api/checklists/templates', headers=auth, json={'title': '걷기'})
    at = datetime(day.year, day.month, day.day, 12, tzinfo=KST)
    assert sum(await asyncio.gather(reminders.send_due(at), reminders.send_due(at))) == 1
    assert sent.await_count == 1
    assert len((await client.get('/api/checklists', headers=auth)).json()['items']) == 1


async def test_agent_fixed_crud_and_validation(client, auth, make_dir):
    from sqlalchemy import select

    from ttakkari.db import sessionmaker
    from ttakkari.models import AuditLog
    session = await _session(client, auth, make_dir)
    h = _agent(session['id'])
    base = '/api/agent/checklists'
    r = await client.post(base + '/templates', headers=h, json={'title': '  비타민  '})
    assert r.status_code == 201 and r.json()['title'] == '비타민'
    template = r.json()
    assert (await client.get(base + '/templates', headers=h)).json()['templates'] == [template]
    updated = await client.patch(base + '/templates/' + template['id'], headers=h, json={'title': '영양제'})
    assert updated.status_code == 200 and updated.json()['title'] == '영양제'
    item = (await client.get(base, headers=h)).json()['items'][0]
    for patch in ({'checked': None}, {'title': None}, {'title': '   '}, {'unexpected': True}):
        assert (await client.patch(base + '/items/' + item['id'], headers=h, json=patch)).status_code == 422
    assert (await client.delete(base + '/templates/' + template['id'], headers=h)).status_code == 204
    assert (await client.get(base + '/templates', headers=h)).json()['templates'] == []
    assert (await client.get(base, headers=h)).json()['total'] == 1  # 이미 생성한 기록 유지
    assert (await client.delete(base + '/templates/' + template['id'], headers=h)).status_code == 404
    async with sessionmaker()() as db:
        logs = list(await db.scalars(select(AuditLog).where(AuditLog.action.like('checklist.%'))))
        assert len(logs) == 3 and {a.actor for a in logs} == {'agent'}


async def test_utc_boundary_and_failed_delivery_is_logged_not_repeated(client, auth, monkeypatch, caplog):
    from datetime import UTC

    from ttakkari.planner import checklist_reminders as reminders
    from ttakkari.planner import checklists as svc
    day = svc.today()
    local = datetime(day.year, day.month, day.day, 7, 30, tzinfo=KST)
    failed = AsyncMock(side_effect=RuntimeError('push unavailable'))
    monkeypatch.setattr(reminders, 'notify', failed)
    assert await reminders.send_due(local.astimezone(UTC)) == 1
    assert await reminders.send_due(local.astimezone(UTC)) == 0
    assert failed.call_args.kwargs['url'] == f'/checklists?date={day}'
    assert 'checklist reminder delivery failed' in caplog.text
