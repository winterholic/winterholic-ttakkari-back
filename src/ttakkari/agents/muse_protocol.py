"""Small MSP v1 NDJSON transport. No model loop, session fold or automatic replay.

Wire reference: https://meta-models.github.io/muse-code-sdk/next/
Diagnostic spike only. The product integration will use the official SDK.
Use the installed CLI's exported schema, and fail on transport corruption.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable

MAX_FRAME_BYTES = 16 * 1024 * 1024


def new_command_id() -> str:
    """UUIDv7 without requiring Python 3.14's uuid.uuid7."""
    millis = time.time_ns() // 1_000_000
    random_bits = uuid.uuid4().int
    value = (millis << 80) | (0x7 << 76) | (random_bits & ((1 << 76) - 1))
    value = (value & ~(3 << 62)) | (2 << 62)
    return str(uuid.UUID(int=value))


class MuseProtocolError(RuntimeError):
    def __init__(self, message: str, kind: str = 'transportError'):
        super().__init__(message)
        self.kind = kind


class MuseConnection:
    def __init__(self, proc, on_notification, on_stderr):
        self.proc = proc
        self.on_notification = on_notification
        self.on_stderr = on_stderr
        self.pending: dict[int, asyncio.Future] = {}
        self.serial = 0
        self.failure: BaseException | None = None
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._stderr())

    @classmethod
    async def start(cls, argv: list[str], *, env=None, cwd=None,
                    on_notification: Callable[[dict], None] | None = None,
                    on_stderr: Callable[[str], None] | None = None):
        # Keep the child in the Run's process group; RunManager owns cancellation.
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env, cwd=cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME_BYTES,
        )
        return cls(proc, on_notification or (lambda _: None), on_stderr or (lambda _: None))

    async def send(self, frame: dict) -> None:
        if self.failure:
            raise MuseProtocolError(str(self.failure))
        raw = json.dumps({'jsonrpc': '2.0', **frame}, ensure_ascii=False).encode() + b'\n'
        if len(raw) > MAX_FRAME_BYTES:
            raise MuseProtocolError('MSP frame exceeds limit')
        self.proc.stdin.write(raw)
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None, *, timeout: float = 30) -> dict:
        self.serial += 1
        rid = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.send({'id': rid, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(rid, None)

    async def command(self, method: str, params: dict, **kwargs) -> dict:
        # A failed/uncertain command is never automatically resubmitted.
        command_id = new_command_id()
        result = await self.request(method, {**params, 'commandId': command_id}, **kwargs)
        if result.get('commandId') != command_id:
            raise MuseProtocolError('MSP command acknowledgement identity mismatch')
        return result

    async def initialize(self, *, session_mcp: bool = False) -> dict:
        result = await self.request('initialize', {
            'clientInfo': {'name': 'ttakkari', 'version': '0.1.0'},
            'capabilities': {'userInputDialogs': False,
                             'requestedCapabilities': ['sessionMcp'] if session_mcp else []},
        })
        if result.get('schema', {}).get('version') != 1:
            raise MuseProtocolError('Unsupported MSP schema version')
        if session_mcp and 'sessionMcp' not in result.get('grantedCapabilities', []):
            raise MuseProtocolError('Muse host did not grant sessionMcp')
        await self.send({'method': 'initialized'})
        return result

    async def _read(self):
        try:
            while raw := await self.proc.stdout.readline():
                frame = json.loads(raw)
                if not isinstance(frame, dict) or frame.get('jsonrpc') != '2.0':
                    raise MuseProtocolError('Invalid MSP frame')
                if 'method' in frame:
                    if 'id' in frame:
                        # No interactive approvals or unsupported server RPC.
                        await self.send({'id': frame['id'], 'error': {
                            'code': -32601, 'message': 'Unattended client cannot service this request'}})
                    else:
                        self.on_notification(frame)
                    continue
                future = self.pending.get(frame.get('id'))
                if future is None or future.done():
                    continue  # Late answer to a timed-out read; no replay.
                if 'error' in frame:
                    error = frame['error']
                    future.set_exception(MuseProtocolError(
                        error.get('message', 'MSP error'), error.get('data', {}).get('kind', 'protocolError')))
                elif isinstance(frame.get('result'), dict):
                    future.set_result(frame['result'])
                else:
                    raise MuseProtocolError('MSP response has no object result')
            raise MuseProtocolError('Muse host closed stdout')
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - transport failure is propagated to every pending caller
            self.failure = error
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(MuseProtocolError(str(error)))

    async def _stderr(self):
        while raw := await self.proc.stderr.readline():
            self.on_stderr(raw.decode(errors='replace'))

    async def close(self, timeout: float = 5) -> None:
        if not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        finally:
            for task in (self.reader, self.stderr):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
