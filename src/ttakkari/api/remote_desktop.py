from __future__ import annotations

import asyncio
import contextlib

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, Credential
from ttakkari.remote_desktop import DEFAULT_DEVICE, Device, permits, relay
from ttakkari.security.auth import User, client_ip, limiter
from ttakkari.security.passwords import verify_password

router = APIRouter(prefix='/api/remote-desktop', tags=['remote-desktop'])
KEY = 'remote_desktop_devices'
# At most two live bridges per generation; existing AI Run resources stay isolated.
active: dict[str, WebSocket] = {}
stops: dict[str, asyncio.Event] = {}
pending = 0


class Authorization(BaseModel):
    password: str = Field(default='', max_length=1024, repr=False)
    grant: str = Field(default='', max_length=128, repr=False)


async def devices(db) -> list[Device]:
    row = await db.get(AppSetting, KEY)
    return [Device.model_validate(d) for d in row.value['devices']] if row else [DEFAULT_DEVICE]


async def reauthenticate(body: Authorization, request: Request, db) -> None:
    ip = client_ip(request) or 'unknown'
    wait = limiter.check(ip)
    if wait:
        raise HTTPException(429, '잠시 후 다시 시도하세요.', headers={'Retry-After': str(wait)})
    credential = await db.get(Credential, 1)
    if not credential or not await asyncio.to_thread(verify_password, credential.password_hash, body.password):
        limiter.fail(ip)
        record(db, action='remote_desktop.authorize', outcome='denied', ip=ip)
        await db.commit()
        raise HTTPException(403, '원격 제어를 허가하려면 따까리 비밀번호를 확인해 주세요.')
    limiter.success(ip)


@router.get('/devices')
async def listing(_: User, db: Db, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'devices': [d.model_dump() for d in await devices(db)]}


class RegisterIn(BaseModel):
    device: Device
    password: str = Field(max_length=1024, repr=False)


@router.put('/devices/{device_id}')
async def register(device_id: str, body: RegisterIn, request: Request, _: User, db: Db):
    if device_id != body.device.id:
        raise HTTPException(422, '기기 ID가 일치하지 않습니다.')
    await reauthenticate(Authorization(password=body.password), request, db)
    # Serialize edits to the registry. Default row is seeded on first explicit registration.
    from sqlalchemy import text
    await db.execute(text("SELECT pg_advisory_xact_lock(683891701)"))
    items = [d for d in await devices(db) if d.id != device_id]
    if len(items) >= 8:
        raise HTTPException(409, 'Mac은 최대 8대까지 등록할 수 있습니다.')
    row = await db.get(AppSetting, KEY)
    value = {'devices': [d.model_dump() for d in [*items, body.device]]}
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=KEY, value=value))
    record(db, action='remote_desktop.device', target_id=device_id, ip=client_ip(request))
    await db.commit()
    return body.device


@router.post('/devices/{device_id}/connect')
async def connect(device_id: str, body: Authorization, request: Request, response: Response, _: User, db: Db):
    origin = request.headers.get('origin', '')
    if origin not in get_settings().cors_origins:
        raise HTTPException(403, '허용되지 않은 접속 주소입니다.')
    device = next((d for d in await devices(db) if d.id == device_id), None)
    if device is None:
        raise HTTPException(404, '등록된 Mac이 없습니다.')
    grant = body.grant
    ticket = permits.ticket(grant, device_id, origin) if grant else None
    if ticket is None:
        await reauthenticate(body, request, db)
        grant = permits.authorize(device_id)
        ticket = permits.ticket(grant, device_id, origin)
    record(db, action='remote_desktop.authorize', target_id=device_id, ip=client_ip(request))
    await db.commit()
    response.headers['Cache-Control'] = 'no-store'
    return {'ticket': ticket, 'grant': grant, 'expires_in': 30, 'path': '/api/remote-desktop/socket'}


@router.post('/disconnect', status_code=204)
async def disconnect(body: Authorization, _: User):
    permits.revoke(body.grant)
    stop = stops.get(body.grant)
    if stop:
        stop.set()


@router.websocket('/socket')
async def socket(ws: WebSocket):
    global pending
    origin = ws.headers.get('origin', '')
    if origin not in get_settings().cors_origins or pending + len(active) >= 2:
        await ws.close(code=1008)
        return
    pending += 1
    reserved = True
    grant = None
    writer = None
    try:
        await ws.accept()
        # Capability travels in the first frame, never in a URL/access log.
        message = await asyncio.wait_for(ws.receive(), 5)
        raw = message.get('text')
        if not isinstance(raw, str) or len(raw) > 512:
            await ws.close(code=1008)
            return
        import json
        auth = json.loads(raw)
        if not isinstance(auth, dict) or not isinstance(auth.get('ticket'), str) or not isinstance(auth.get('grant'), str):
            await ws.close(code=1008)
            return
        device_id = permits.consume(auth.get('ticket', ''), origin)
        grant = auth.get('grant', '')
        entry = permits.grants.get(grant)
        if not device_id or not entry or entry[0] != device_id:
            await ws.close(code=1008, reason='접속 허가가 만료되었습니다. 다시 연결해 주세요.')
            return
        if grant in active:
            await ws.close(code=1008, reason='이미 연결 중입니다.')
            return
        async with sessionmaker()() as db:
            device = next((d for d in await devices(db) if d.id == device_id), None)
        if device is None:
            await ws.close(code=1008)
            return
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(device.host, device.port), 4)
        except (OSError, TimeoutError):
            await ws.close(code=1013, reason='Mac 화면 공유에 연결할 수 없습니다. 잠자기·화면 공유·네트워크를 확인해 주세요.')
            return
        active[grant] = ws
        stops[grant] = asyncio.Event()
        pending -= 1
        reserved = False
        await ws.send_text('ready')
        await relay(ws, reader, writer, stops[grant])
    except (WebSocketDisconnect, TimeoutError, ValueError, TypeError, OSError, RuntimeError):
        pass
    finally:
        if grant and active.get(grant) is ws:
            active.pop(grant, None)
            stops.pop(grant, None)
        if reserved:
            pending -= 1
        if writer:
            writer.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await ws.close()
