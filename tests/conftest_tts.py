"""TTS 테스트 공용 픽스처. 각 test_tts_*.py 가 pytest_plugins 로 등록한다(공용 conftest.py 는 다른 작업과 겹쳐 건드리지 않는다)."""
from __future__ import annotations

import uuid

import pytest_asyncio
from tts_fakes import FakeProvider

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.tts import cache, jobs, service


@pytest_asyncio.fixture(loop_scope="session")
async def tts_env(monkeypatch):
    """소비 루프를 멈추고(시험이 tick 을 직접 부른다) 가짜 엔진을 연결한다."""
    await jobs.stop()
    jobs._pending_texts.clear()
    jobs._running.clear()
    jobs._dead_seen.clear()
    cache.counters.reset()
    provider = FakeProvider(revision=f"fake-{uuid.uuid4().hex[:8]}")
    monkeypatch.setattr(service, "get_provider", lambda settings=None: provider)
    monkeypatch.setattr(get_settings(), "local_tts_enabled", False)  # kick() 이 소비 루프를 새로 띄우지 않게
    monkeypatch.setattr(jobs, "CANCEL_POLL_SECONDS", 0.05)
    yield provider
    await jobs.stop()
    generation.current = None
    jobs._pending_texts.clear()
