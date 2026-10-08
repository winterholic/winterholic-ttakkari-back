from __future__ import annotations

import json
import stat
import uuid

import pytest
from pywebpush import WebPushException
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import AuditLog, PushSubscription, Run, RunStatus
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


def test_payload_titles_and_no_sensitive_content():
    run = _run(RunStatus.failed, result="x", error="Traceback /Users/winterholic/secret.py boom")
    p = service.build_payload(run, "리포트 작업")
    assert p["title"] == "작업 실패"
    assert "/Users" not in json.dumps(p) and "Traceback" not in json.dumps(p)
    assert p["url"] == f"/chat/{run.session_id}" and p["tag"] == str(run.session_id)
    for st, title in [(RunStatus.succeeded, "작업 완료"), (RunStatus.cancelled, "작업 취소됨"),
                      (RunStatus.interrupted, "작업 중단됨")]:
        assert service.build_payload(_run(st), "t")["title"] == title


def test_payload_body_first_line_truncated():
    run = _run(RunStatus.succeeded, result="\n\n" + "가" * 200 + "\n둘째 줄")
    p = service.build_payload(run, "제목")
    assert len(p["body"]) == 80 and p["body"].endswith("…") and p["body"].startswith("제목 · ")
    assert "둘째" not in p["body"]


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
    run = _run(RunStatus.succeeded, result="끝났습니다\n자세히")
    await service.notify_run_finished(run, "세션")

    assert sorted(c[0] for c in calls) == [f"https://push.example/{n}" for n in ("boom", "gone", "ok")]
    assert calls[0][1]["title"] == "작업 완료" and calls[0][1]["url"] == f"/chat/{run.session_id}"
    left = [s.endpoint.rsplit("/", 1)[1] for s in await _subs()]
    assert left == ["boom", "ok"]  # 410 만 삭제, 500 은 유지
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.action == "push.run_finished")))
    assert len(rows) == 1 and rows[0].detail == {"sent": 1, "removed": 1, "failed": 1}


async def test_notify_without_subscriptions_is_noop(monkeypatch):
    monkeypatch.setattr(service, "webpush", lambda *a, **k: pytest.fail("구독이 없으면 보내면 안 된다"))
    await service.notify_run_finished(_run(RunStatus.succeeded), "t")


async def test_test_endpoint_sends(client, auth, monkeypatch):
    await client.post("/api/push/subscriptions", json=SUB, headers=auth)
    seen = []
    monkeypatch.setattr(service, "webpush", lambda info, data=None, **k: seen.append(json.loads(data)))
    r = await client.post("/api/push/test", headers=auth)
    assert r.status_code == 200 and r.json()["sent"] == 1
    assert seen[0]["title"] == "테스트 알림"
