"""Small authenticated WebSocket-to-RFB bridge; no capture, AI or system setup."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import secrets
import time

from pydantic import BaseModel, Field, field_validator


class Device(BaseModel):
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,39}$')
    name: str = Field(min_length=1, max_length=60)
    host: str
    port: int = Field(default=5900, ge=5900, le=5999)

    @field_validator('host')
    @classmethod
    def private_address(cls, value: str) -> str:
        # Numeric addresses only: no DNS rebinding or arbitrary proxy targets.
        ip = ipaddress.ip_address(value)
        networks = ('127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12',
                    '192.168.0.0/16', '100.64.0.0/10', 'fd7a:115c:a1e0::/48')
        if not any(ip in ipaddress.ip_network(n) for n in networks):
            raise ValueError('LAN 또는 Tailscale IP 주소만 등록할 수 있습니다.')
        return str(ip)


DEFAULT_DEVICE = Device(id='mac-studio', name='Mac Studio', host='127.0.0.1')


class PermitStore:
    """Owner reauthentication grants (10 min) and one-use WS tickets (30 s).

    Memory-only by design: generation activation invalidates outstanding tickets.
    An established session has a separate two-hour ceiling.
    """
    def __init__(self):
        self.grants: dict[str, tuple[str, float]] = {}
        self.tickets: dict[str, tuple[str, str, float]] = {}

    def _prune(self):
        now = time.monotonic()
        for entries in (self.grants, self.tickets):
            for key in list(entries):
                if entries[key][-1] <= now:
                    del entries[key]
            while len(entries) >= 32:
                del entries[next(iter(entries))]

    def authorize(self, device: str) -> str:
        self._prune()
        token = secrets.token_urlsafe(32)
        self.grants[token] = (device, time.monotonic() + 600)
        return token

    def ticket(self, grant: str, device: str, origin: str) -> str | None:
        self._prune()
        entry = self.grants.get(grant)
        if entry is None or entry[0] != device:
            return None
        token = secrets.token_urlsafe(32)
        self.tickets[token] = (device, origin, time.monotonic() + 30)
        return token

    def consume(self, ticket: str, origin: str) -> str | None:
        self._prune()
        entry = self.tickets.get(ticket)
        if entry is None or entry[1] != origin:
            return None
        del self.tickets[ticket]
        return entry[0]

    def revoke(self, grant: str) -> None:
        entry = self.grants.pop(grant, None)
        if entry:
            self.tickets = {k: v for k, v in self.tickets.items() if v[0] != entry[0]}


permits = PermitStore()


async def relay(ws, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stop: asyncio.Event | None = None) -> None:
    async def to_browser():
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    async def to_mac():
        while True:
            msg = await ws.receive()
            if msg['type'] == 'websocket.disconnect':
                return
            data = msg.get('bytes')
            if data is None or len(data) > 1024 * 1024:
                return
            writer.write(data)
            await writer.drain()

    tasks = [asyncio.create_task(to_browser()), asyncio.create_task(to_mac())]
    if stop is not None:
        tasks.append(asyncio.create_task(stop.wait()))
    try:
        done, _ = await asyncio.wait(tasks, timeout=7200, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
