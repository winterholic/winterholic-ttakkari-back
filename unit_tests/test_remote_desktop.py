"""Remote desktop policy and transport; no production DB or macOS settings."""
import asyncio
import time

import pytest
from pydantic import ValidationError

from ttakkari.remote_desktop import Device, PermitStore, relay


@pytest.mark.parametrize('host', ['127.0.0.1', '192.168.1.4', '10.0.0.8', '172.16.0.2', '100.100.10.2', 'fd7a:115c:a1e0::2'])
def test_private_targets(host):
    assert Device(id='mac', name='Mac', host=host).host == host


@pytest.mark.parametrize('host', ['8.8.8.8', '169.254.169.254', '0.0.0.0', 'localhost', 'example.com', '::', '::ffff:8.8.8.8', '224.0.0.1'])
def test_no_public_metadata_dns_or_unspecified_targets(host):
    with pytest.raises(ValidationError):
        Device(id='mac', name='Mac', host=host)


def test_ticket_single_use_origin_device_and_expiry():
    store = PermitStore()
    permit = store.authorize('mac')
    ticket = store.ticket(permit, 'mac', 'https://phone')
    assert store.consume(ticket, 'https://evil') is None
    assert store.consume(ticket, 'https://phone') == 'mac'
    assert store.consume(ticket, 'https://phone') is None
    assert store.ticket(permit, 'other', 'https://phone') is None
    store.grants[permit] = ('mac', time.monotonic() - 1)
    assert store.ticket(permit, 'mac', 'https://phone') is None


def test_expired_ticket_and_bounded_store():
    store = PermitStore()
    p = store.authorize('mac')
    t = store.ticket(p, 'mac', 'https://phone')
    store.tickets[t] = ('mac', 'https://phone', time.monotonic() - 1)
    assert store.consume(t, 'https://phone') is None
    for _ in range(100):
        store.authorize('mac')
    assert len(store.grants) <= 32


async def test_binary_transport_and_socket_cleanup():
    got = []
    closed = asyncio.Event()
    async def vnc(reader, writer):
        writer.write(b'RFB 003.008\n')
        await writer.drain()
        got.append(await reader.readexactly(6))
        writer.close()
        await writer.wait_closed()
        closed.set()
    server = await asyncio.start_server(vnc, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    class WS:
        async def send_bytes(self, data):
            assert data == b'RFB 003.008\n'
        async def receive(self):
            if not got:
                await asyncio.sleep(.01)
                return {'type':'websocket.receive', 'bytes':b'\x05\x01\x00\x04\x00\x08'}
            await asyncio.sleep(10)
    try:
        await asyncio.wait_for(relay(WS(), reader, writer), 2)
        assert got == [b'\x05\x01\x00\x04\x00\x08']
        await asyncio.wait_for(closed.wait(), 1)
        assert writer.is_closing()
    finally:
        server.close()
        await server.wait_closed()
