from __future__ import annotations

import json
import stat
import uuid

import pytest
from pywebpush import WebPushException
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import AuditLog, PushSubscription, Run, RunStatus
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.push import service, vapid

SUB = {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": "BPk", "auth": "au"}}


async def _subs() -> list[PushSubscription]:
    async with sessionmaker()() as db:
        return list(await db.scalars(select(PushSubscription).order_by(PushSubscription.endpoint)))


class _Resp:
    def __init__(self, code: int):
        self.status_code = code


async def test_requires_auth(client):
    assert (await client.post("/api/push/subscriptions", json=SUB)).status_code == 401
    assert (await client.request("DELETE", "/api/push/subscriptions", json={"endpoint": "x"})).status_code == 401
    assert (await client.post("/api/push/test")).status_code == 401
    assert (await client.get("/api/push/vapid-public-key")).status_code == 401


async def test_vapid_key_file_is_private_and_stable(client, auth):
    r = await client.get("/api/push/vapid-public-key", headers=auth)
    assert r.status_code == 200
    key = r.json()["public_key"]
    assert len(key) == 87  # 65바이트 비압축 점의 base64url
    path = vapid.vapid_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (await client.get("/api/push/vapid-public-key", headers=auth)).json()["public_key"] == key


async def test_subscribe_upsert_and_delete(client, auth):
    r = await client.post("/api/push/subscriptions", json={**SUB, "label": "iPhone"}, headers=auth)
    assert r.status_code == 204
    again = {**SUB, "keys": {"p256dh": "NEW", "auth": "au2"}}
    assert (await client.post("/api/push/subscriptions", json=again, headers=auth)).status_code == 204
    subs = await _subs()
    assert len(subs) == 1
    assert (subs[0].p256dh, subs[0].auth, subs[0].label) == ("NEW", "au2", "iPhone")
    r = await client.request("DELETE", "/api/push/subscriptions", json={"endpoint": SUB["endpoint"]}, headers=auth)
    assert r.status_code == 204
    assert await _subs() == []
    # 이미 없는 endpoint 삭제도 멱등
    r = await client.request("DELETE", "/api/push/subscriptions", json={"endpoint": SUB["endpoint"]}, headers=auth)
    assert r.status_code == 204


async def test_subscribe_rejects_non_https_and_missing_keys(client, auth):
    bad = await client.post("/api/push/subscriptions", json={**SUB, "endpoint": "http://x/y"}, headers=auth)
    assert bad.status_code == 422
    bad = await client.post("/api/push/subscriptions", json={"endpoint": SUB["endpoint"]}, headers=auth)
    assert bad.status_code == 422


def _run(status: RunStatus, result: str | None = None, error: str | None = None) -> Run:
    return Run(id=uuid.uuid4(), session_id=uuid.uuid4(), status=status, result_text=result, error=error)


def test_payload_failed_has_no_sensitive_content():
    run = _run(RunStatus.failed, result="x", error="Traceback /Users/winterholic/secret.py boom")
    p = service.build_payload(run, "리포트 작업")
    assert p["title"] == "작업 실패" and p["body"] == "리포트 작업"
    assert "/Users" not in json.dumps(p) and "Traceback" not in json.dumps(p)
    assert p["url"] == f"/chat/{run.session_id}" and p["tag"] == str(run.session_id)


def test_payload_body_truncated():
    p = service.build_payload(_run(RunStatus.failed), "가" * 200)
    assert len(p["body"]) == 80 and p["body"].endswith("…")


@pytest.mark.parametrize("st", [RunStatus.succeeded, RunStatus.cancelled, RunStatus.interrupted])
async def test_no_automatic_push_unless_failed(client, auth, monkeypatch, st):
    await client.post("/api/push/subscriptions", json=SUB, headers=auth)
    monkeypatch.setattr(service, "webpush", lambda *a, **k: pytest.fail(f"{st} 는 자동 알림을 보내면 안 된다"))
    await service.notify_run_finished(_run(st, result="끝났습니다"), "세션")


async def test_agent_notify_user_still_pushes(client, auth, make_dir, monkeypatch):
    await client.post("/api/push/subscriptions", json=SUB, headers=auth)
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    sess = (await client.post("/api/sessions", json={"workspace_id": ws["id"]}, headers=auth)).json()
    seen = []
    monkeypatch.setattr(service, "webpush", lambda info, data=None, **k: seen.append(json.loads(data)))
    h = {"Authorization": f"Bearer {issue_token(sess['id'])}"}
    r = await client.post("/api/agent/notify", json={"title": "승인 필요", "body": "배포해도 될까요"}, headers=h)
    assert r.status_code == 200 and r.json()["sent"] == 1
    assert seen[0]["title"] == "승인 필요" and seen[0]["url"] == f"/chat/{sess['id']}"


async def test_notify_sends_per_subscription_and_removes_gone(client, auth, monkeypatch):
    for n in ("ok", "gone", "boom"):
        body = {"endpoint": f"https://push.example/{n}", "keys": {"p256dh": "k", "auth": "a"}}
        await client.post("/api/push/subscriptions", json=body, headers=auth)
    calls: list[tuple[str, dict]] = []

    def fake(info, data=None, vapid_private_key=None, vapid_claims=None, **kw):
        calls.append((info["endpoint"], json.loads(data)))
        assert vapid_claims["sub"].startswith("mailto:")
        if info["endpoint"].endswith("/gone"):
            raise WebPushException("gone", response=_Resp(410))
        if info["endpoint"].endswith("/boom"):
            raise WebPushException("server error", response=_Resp(500))

    monkeypatch.setattr(service, "webpush", fake)
    run = _run(RunStatus.failed, error="boom")
    await service.notify_run_finished(run, "세션")

    assert sorted(c[0] for c in calls) == [f"https://push.example/{n}" for n in ("boom", "gone", "ok")]
    assert calls[0][1]["title"] == "작업 실패" and calls[0][1]["url"] == f"/chat/{run.session_id}"
    left = [s.endpoint.rsplit("/", 1)[1] for s in await _subs()]
    assert left == ["boom", "ok"]  # 410 만 삭제, 500 은 유지
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.action == "push.run_finished")))
    assert len(rows) == 1 and rows[0].detail == {"sent": 1, "removed": 1, "failed": 1}


async def test_notify_without_subscriptions_is_noop(monkeypatch):
    monkeypatch.setattr(service, "webpush", lambda *a, **k: pytest.fail("구독이 없으면 보내면 안 된다"))
    await service.notify_run_finished(_run(RunStatus.failed), "t")


async def test_test_endpoint_sends(client, auth, monkeypatch):
    await client.post("/api/push/subscriptions", json=SUB, headers=auth)
    seen = []
    monkeypatch.setattr(service, "webpush", lambda info, data=None, **k: seen.append(json.loads(data)))
    r = await client.post("/api/push/test", headers=auth)
    assert r.status_code == 200 and r.json()["sent"] == 1
    assert seen[0]["title"] == "테스트 알림"


async def test_notify_is_single_entry_point_with_defaults(client, auth, monkeypatch):
    await client.post("/api/push/subscriptions", json=SUB, headers=auth)
    seen = []
    monkeypatch.setattr(service, "webpush", lambda info, data=None, **k: seen.append(json.loads(data)))
    counts = await service.notify(service.PushKind.agent, "제목", "본문")
    assert counts["sent"] == 1
    assert seen[0] == {"title": "제목", "body": "본문", "url": "/chat", "tag": "push.agent"}
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.action == "push.agent")))
    assert len(rows) == 1
