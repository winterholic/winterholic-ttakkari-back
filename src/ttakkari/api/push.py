from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.models import PushSubscription
from ttakkari.push import vapid
from ttakkari.push.service import send_to_all
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api/push", tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=64)


class SubscriptionIn(BaseModel):
    """브라우저 PushSubscription.toJSON() 그대로. expirationTime 은 쓰지 않는다."""

    endpoint: str = Field(min_length=1, max_length=2048, pattern=r"^https://")
    keys: SubscriptionKeys
    label: str | None = Field(default=None, max_length=100)


class EndpointIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


@router.get("/vapid-public-key")
async def vapid_public_key(_: User) -> dict:
    return {"public_key": vapid.public_key()}


@router.post("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def subscribe(body: SubscriptionIn, _: User, db: Db, request: Request) -> Response:
    sub = await db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    ua = (request.headers.get("user-agent") or "")[:512] or None
    if sub is None:
        sub = PushSubscription(endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth,
                               label=body.label, user_agent=ua)
        db.add(sub)
    else:
        sub.p256dh, sub.auth, sub.user_agent = body.keys.p256dh, body.keys.auth, ua
        if body.label is not None:
            sub.label = body.label
    record(db, action="push.subscribe", detail={"label": body.label}, ip=client_ip(request), user_agent=ua)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(body: EndpointIn, _: User, db: Db, request: Request) -> Response:
    sub = await db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if sub is not None:
        await db.delete(sub)
    record(db, action="push.unsubscribe", ip=client_ip(request))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/test")
async def test_push(_: User, db: Db, request: Request) -> dict:
    record(db, action="push.test", ip=client_ip(request))
    await db.commit()
    payload = {"title": "테스트 알림", "body": "알림이 정상적으로 도착했습니다.", "url": "/chat", "tag": "ttakkari-test"}
    return await send_to_all(payload, action="push.test_sent")
