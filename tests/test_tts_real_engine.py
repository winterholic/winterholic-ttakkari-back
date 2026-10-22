"""실제 Supertonic 런타임으로 한국어 문장을 합성하는 E2E. 런타임이 없는 환경에서는 건너뛴다.

로컬 API(ASGI) 호출 → run_json_worker 자식 프로세스 → 캐시 → WAV 길이까지 실제 경로로 확인한다.
"""
from __future__ import annotations

import asyncio
import io
import wave

import pytest

from ttakkari.config import get_settings
from ttakkari.tts.supertonic import SupertonicProvider

pytestmark = pytest.mark.skipif(
    not SupertonicProvider(get_settings()).availability().ready, reason="Supertonic 런타임이 설치되지 않았다"
)

TEXT = "안녕하세요. 오늘은 2026년 10월 21일이고, 회의는 오후 3시 30분에 시작합니다."


def _wav_seconds(body: bytes) -> float:
    with wave.open(io.BytesIO(body)) as handle:
        return handle.getnframes() / handle.getframerate()


async def test_speak_generates_korean_wav_with_real_supertonic(client, auth):
    first = await client.post("/api/tts/speak", json={"text": TEXT}, headers=auth)
    assert first.status_code == 200, first.text
    assert first.headers["content-type"] == "audio/wav"
    seconds = _wav_seconds(first.content)
    assert seconds > 3.0
    assert int(first.headers["X-TTS-Chunks"]) == 2
    assert first.headers["X-TTS-Cached-Chunks"] == "0"
    assert abs(float(first.headers["X-TTS-Duration"]) - seconds) < 0.05

    again = await client.post("/api/tts/speak", json={"text": TEXT}, headers=auth)
    assert again.status_code == 200
    assert again.headers["X-TTS-Cached-Chunks"] == "2"  # 캐시 히트는 자식 프로세스를 띄우지 않는다
    assert again.content == first.content


async def test_job_generates_audio_and_token_url_serves_it(client, auth, monkeypatch):
    from ttakkari.tts import jobs

    claimed = asyncio.Event()
    run_claimed = jobs.run_claimed

    async def mark_claimed(job_id):
        claimed.set()
        await run_claimed(job_id)

    # API 응답 직후 consumer 가 먼저 claim 한 경우에도 완료를 올바르게 관찰해야 한다.
    monkeypatch.setattr(jobs, "run_claimed", mark_claimed)
    created = await client.post("/api/tts/jobs", json={"text": TEXT, "speed": 1.1}, headers=auth)
    assert created.status_code == 202
    body = created.json()
    assert body["chunks"] == 2 and body["cached_chunks"] == 0
    await asyncio.wait_for(claimed.wait(), timeout=10)

    async with asyncio.timeout(60):
        while True:
            response = await client.get(f"/api/tts/jobs/{body['job_id']}", headers=auth)
            assert response.status_code == 200, response.text
            state = response.json()
            if state["status"] in jobs.TERMINAL:
                break
            await asyncio.sleep(0.05)
    assert state["status"] == "succeeded", state
    assert state["progress"] == {"done": 2, "total": 2}
    assert state["metrics"]["rtf"] > 0
    audio = await client.get(state["audio_full_url"])  # 토큰 URL 은 Authorization 헤더 없이 열린다
    assert audio.status_code == 200
    assert _wav_seconds(audio.content) > 3.0
    assert abs(_wav_seconds(audio.content) - state["duration"]) < 0.05
