"""Local RFB fixture for noVNC integration, not a Mac or a production auth test.

Run with PYTHONPATH=src: python unit_tests/remote_desktop_fixture.py --log /outbox/events.jsonl
Only binds loopback; the production router and binary bridge are exercised.
"""
import argparse
import asyncio
import contextlib
import json
import struct
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageDraw, ImageFont

from ttakkari.api import remote_desktop as api
from ttakkari.db import get_db
from ttakkari.models import Credential
from ttakkari.remote_desktop import Device
from ttakkari.security.auth import require_user
from ttakkari.security.passwords import hash_password

writers = set()
events = []
log_path = None


def log(kind, **values):
    event = {'kind': kind, **values}
    events.append(event)
    if log_path:
        with log_path.open('a') as f:
            f.write(json.dumps(event) + '\n')


def framebuffer(red_shift=0):
    image = Image.new('RGB', (1920, 1080), '#f1f4f7')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 28)
    large = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 42)
    draw.rectangle((0, 0, 1920, 64), fill='#152b40')
    draw.text((32, 15), 'RFB TEST FIXTURE - synthetic desktop, not the real Mac', font=font, fill='white')
    draw.rounded_rectangle((100, 120, 1820, 990), radius=16, fill='white', outline='#b0bec9', width=2)
    draw.rectangle((100, 120, 1820, 184), fill='#e5edf4')
    draw.text((140, 138), 'Browser / Notes / Files', font=font, fill='#152b40')
    draw.text((180, 260), 'Remote desktop integration check', font=large, fill='#152b40')
    draw.text((180, 340), '1920 x 1080 - no forced fit on iPhone', font=font, fill='#34576e')
    for i, title in enumerate(['Click and double-click', 'Two-finger scroll', 'Hold and drag', 'Pinch zoom / pan', 'Korean composition buffer']):
        y = 430 + i * 86
        draw.rectangle((180, y, 225, y + 44), outline='#296eb4', width=3)
        draw.text((250, y + 4), title, font=font, fill='#152b40')
    return image.tobytes('raw', 'RGBX' if red_shift == 0 else 'BGRX')


async def vnc(reader, writer):
    writers.add(writer)
    log('connection')
    try:
        writer.write(b'RFB 003.008\n'); await writer.drain()
        assert await reader.readexactly(12) == b'RFB 003.008\n'
        writer.write(b'\x01\x01'); await writer.drain()
        assert await reader.readexactly(1) == b'\x01'
        writer.write(b'\0\0\0\0'); await writer.drain()
        await reader.readexactly(1)
        name = b'Local RFB integration fixture'
        pixel = struct.pack('>BBBBHHHBBBxxx', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        writer.write(struct.pack('>HH', 1920, 1080) + pixel + struct.pack('>I', len(name)) + name)
        await writer.drain()
        first = True
        red_shift = 16
        updates = 0
        while True:
            kind = (await reader.readexactly(1))[0]
            if kind == 0:
                data = await reader.readexactly(19)
                red_shift = data[13]
            elif kind == 2:
                _, count = struct.unpack('>BH', await reader.readexactly(3))
                await reader.readexactly(count * 4)
            elif kind == 3:
                await reader.readexactly(9)
                await asyncio.sleep(.1)
                if first:
                    writer.write(b'\0\0\0\1' + struct.pack('>HHHHi', 0, 0, 1920, 1080, 0) + framebuffer(red_shift))
                    first = False
                    log('framebuffer', width=1920, height=1080)
                else:
                    updates += 1
                    tile = Image.new('RGB', (32, 32), (40, updates % 255, 180)).tobytes('raw', 'RGBX' if red_shift == 0 else 'BGRX')
                    writer.write(b'\0\0\0\1' + struct.pack('>HHHHi', 0, 80, 32, 32, 0) + tile)
                    if updates % 100 == 0:
                        log('frame_updates', count=updates)
                await writer.drain()
            elif kind == 4:
                data = await reader.readexactly(7)
                down, key = data[0], struct.unpack('>I', data[3:])[0]
                log('key', down=down, key=key)
            elif kind == 5:
                mask, x, y = struct.unpack('>BHH', await reader.readexactly(5))
                log('pointer', mask=mask, x=x, y=y)
            elif kind == 6:
                data = await reader.readexactly(7)
                length = struct.unpack('>I', data[3:])[0]
                text = await reader.readexactly(length)
                log('clipboard', text=text.decode('latin1'))
            else:
                log('unexpected', type=kind)
                break
    except (asyncio.IncompleteReadError, ConnectionError, RuntimeError):
        pass
    finally:
        log('disconnection')
        writers.discard(writer)
        writer.close()


hashed = hash_password('fixture-password')


class Db:
    async def get(self, model, key):
        return SimpleNamespace(password_hash=hashed) if model is Credential else None
    def add(self, obj):
        pass
    async def commit(self):
        pass


@asynccontextmanager
async def session():
    yield Db()


async def devices(db):
    return [Device.model_construct(id='fixture-mac', name='검증용 Mac · 실제 기기 아님', host='127.0.0.1', port=18972)]


@asynccontextmanager
async def lifespan(app):
    server = await asyncio.start_server(vnc, '127.0.0.1', 18972)
    yield
    for writer in list(writers):
        writer.close()
    server.close()
    await server.wait_closed()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['http://127.0.0.1:18970'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
app.include_router(api.router)
app.dependency_overrides[require_user] = lambda: 'owner'
app.dependency_overrides[get_db] = lambda: Db()
api.devices = devices
api.sessionmaker = lambda: session
api.get_settings = lambda: SimpleNamespace(cors_origins=['http://127.0.0.1:18970'])


@app.get('/fixture/events')
async def event_list():
    return events


@app.post('/fixture/drop')
async def drop():
    for writer in list(writers):
        writer.close()
    return {'dropped': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    log_path = args.log
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host='127.0.0.1', port=18971)
