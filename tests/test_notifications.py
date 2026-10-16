from __future__ import annotations

import pytest

from ttakkari.push import service


async def emit(kind=service.PushKind.agent, title="분석 결과", **kw):
    await service.notify(kind, title, "내용", url="/chat/session", **kw)


async def test_notification_endpoints_require_auth(client):
    assert (await client.get("/api/notifications")).status_code == 401
    assert (await client.patch("/api/notifications/1/read")).status_code == 401
    assert (await client.post("/api/notifications/read-all", json={"through_id": 1})).status_code == 401


async def test_notification_saved_without_push_subscription(client, auth):
    await emit()
    r = await client.get("/api/notifications", headers=auth)
    assert r.status_code == 200
    page = r.json()
    assert page["unread_counts"] == {"all": 1, "tasks": 0, "assistant": 1, "planner": 0, "meetings": 0, "system": 0}
    item = page["items"][0]
    assert (item["title"], item["body"], item["url"], item["category"], item["read_at"]) == (
        "분석 결과", "내용", "/chat/session", "assistant", None)
    assert page["next_before"] is None


@pytest.mark.parametrize("kind,category", [
    (service.PushKind.run_failed, "tasks"), (service.PushKind.run_waiting, "tasks"),
    (service.PushKind.agent, "assistant"), (service.PushKind.calendar_reminder, "planner"),
    (service.PushKind.checklist_reminder, "planner"), (service.PushKind.meeting, "meetings"),
    (service.PushKind.deployment, "system"), (service.PushKind.test, "system"),
])
async def test_categories(client, auth, kind, category):
    await emit(kind)
    assert (await client.get(f"/api/notifications?category={category}", headers=auth)).json()["items"][0]["category"] == category


async def test_filters_pagination_and_idempotent_read(client, auth):
    await emit(title="이전")
    await emit(service.PushKind.meeting, "회의")
    await emit(title="최근")
    page = (await client.get("/api/notifications?limit=2", headers=auth)).json()
    assert [x["title"] for x in page["items"]] == ["최근", "회의"]
    before = page["next_before"]
    # 새 알림이 들어와도 다음 페이지의 중복·누락이 없다.
    await emit(title="방금")
    older = (await client.get(f"/api/notifications?before={before}&limit=2", headers=auth)).json()
    assert [x["title"] for x in older["items"]] == ["이전"]
    nid = page["items"][0]["id"]
    first = await client.patch(f"/api/notifications/{nid}/read", headers=auth)
    again = await client.patch(f"/api/notifications/{nid}/read", headers=auth)
    assert first.status_code == 200 and first.json()["read_at"] == again.json()["read_at"]
    unread = (await client.get("/api/notifications?category=assistant&unread_only=true", headers=auth)).json()
    assert [x["title"] for x in unread["items"]] == ["방금", "이전"]
    assert unread["unread_counts"]["meetings"] == 1
    assert (await client.patch("/api/notifications/999999/read", headers=auth)).status_code == 404
    assert (await client.get("/api/notifications?category=made-up", headers=auth)).status_code == 422
    assert (await client.get("/api/notifications?limit=0", headers=auth)).status_code == 422


async def test_bulk_read_respects_category_and_snapshot(client, auth):
    await emit()
    await emit(service.PushKind.meeting)
    old = (await client.get("/api/notifications", headers=auth)).json()
    await emit(title="새 알림")
    r = await client.post("/api/notifications/read-all", json={"category": "assistant", "through_id": old["items"][0]["id"]}, headers=auth)
    assert r.status_code == 200 and r.json() == {"updated": 1}
    remaining = (await client.get("/api/notifications?unread_only=true", headers=auth)).json()
    assert [x["title"] for x in remaining["items"]] == ["새 알림", "분석 결과"]
    assert remaining["unread_counts"]["all"] == 2


async def test_delivery_failure_keeps_inbox(client, auth, monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("transport unavailable")
    monkeypatch.setattr(service, "_deliver", broken)
    with pytest.raises(RuntimeError, match="transport unavailable"):
        await emit()
    assert len((await client.get("/api/notifications", headers=auth)).json()["items"]) == 1


@pytest.mark.parametrize("url", ["https://example.com", "//example.com", "/\\example.com", "javascript:alert(1)", "/chat\n/other"])
async def test_only_internal_links_are_stored(client, auth, url):
    await service.notify(service.PushKind.agent, "제목", url=url)
    assert (await client.get("/api/notifications", headers=auth)).json()["items"][0]["url"] == "/notifications"
