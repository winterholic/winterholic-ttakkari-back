"""로컬 TTS API 계약(설계 §7, §7.2): 인증 401, 형식 오류·미존재 404, 접근 가능한 job 은 항상 200 + status."""
from __future__ import annotations

import io
import json
import uuid
import wave

import jwt
from sqlalchemy import text
from tts_helpers import insert_job, job_row

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.tts import cache, jobs, service
from ttakkari.tts.providers import ProviderAvailability, ProviderState

SECRET_SENTENCE = "비밀문장 알파브라보 델타 하나둘셋."

pytest_plugins = ("conftest_tts",)


def wav_info(body: bytes) -> tuple[float, int]:
    with wave.open(io.BytesIO(body)) as handle:
        return handle.getnframes() / handle.getframerate(), handle.getframerate()


# ---------------------------------------------------------------- GET/DELETE 계약 (설계 §7.2)


async def test_job_endpoints_require_bearer(client, tts_env):
    jid = uuid.uuid4()
    for method in ("get", "delete"):
        r = await getattr(client, method)(f"/api/tts/jobs/{jid}")
        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"
        assert r.headers["www-authenticate"] == "Bearer"
        bad = await getattr(client, method)(f"/api/tts/jobs/{jid}", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401


async def test_malformed_and_unknown_job_id_is_404_not_422(client, auth, tts_env):
    for method in ("get", "delete"):
        for raw in ("not-a-uuid", "123", str(uuid.uuid4())):
            r = await getattr(client, method)(f"/api/tts/jobs/{raw}", headers=auth)
            assert r.status_code == 404, (method, raw, r.text)
            assert r.json()["code"] == "not_found"


async def test_failed_and_cancelled_jobs_are_http_200_with_status(client, auth, tts_env):
    me = jobs.owner_id()
    failed = await insert_job(owner=me, status="failed", error_code="worker_timeout")
    broken = await insert_job(owner=me, status="failed", error_code="worker_failed")
    cancelled = await insert_job(owner=me, status="cancelled")
    r = await client.get(f"/api/tts/jobs/{failed}", headers=auth)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["error_code"], r.json()["retryable"]) == ("failed", "worker_timeout", True)
    r = await client.get(f"/api/tts/jobs/{broken}", headers=auth)
    assert (r.status_code, r.json()["retryable"]) == (200, False)
    r = await client.get(f"/api/tts/jobs/{cancelled}", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and "error_code" not in r.json()


async def test_delete_is_idempotent_for_terminal_jobs(client, auth, tts_env):
    jid = await insert_job(owner=jobs.owner_id(), status="succeeded")
    r = await client.delete(f"/api/tts/jobs/{jid}", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    again = await client.delete(f"/api/tts/jobs/{jid}", headers=auth)
    assert again.status_code == 200 and again.json()["status"] == "succeeded"


# ---------------------------------------------------------------- 상태·음성


async def test_status_and_voices(client, auth, tts_env):
    s = (await client.get("/api/tts/status", headers=auth)).json()
    assert s["ready"] is True and s["engine"] == "fake" and s["voice_count"] == 2
    assert s["max_text_chars"] == 5000 and s["speak_max_chars"] == 500 and s["accepting"] is True
    v = (await client.get("/api/tts/voices", headers=auth)).json()
    assert [x["id"] for x in v["voices"]] == ["M1", "F1"]
    assert v["voices"][0]["lang"] == ["ko", "en"]
    assert (await client.get("/api/tts/status")).status_code == 401
    assert (await client.get("/api/tts/voices")).status_code == 401


async def test_unavailable_engine_returns_503_with_reason(client, auth, tts_env, monkeypatch):
    monkeypatch.setattr(
        type(tts_env), "availability", lambda self: ProviderAvailability(ProviderState.unconfigured, "모델 설치 필요")
    )
    r = await client.post("/api/tts/speak", json={"text": "안녕하세요."}, headers=auth)
    assert r.status_code == 503 and r.json()["code"] == "tts_unavailable" and "모델" in r.json()["message"]
    assert (await client.get("/api/tts/status", headers=auth)).json()["ready"] is False


# ---------------------------------------------------------------- /speak


async def test_speak_miss_then_hit_skips_worker(client, auth, tts_env):
    body = {"text": "안녕하세요. 오늘 회의는 3시에 있습니다.", "voice": "F1", "speed": 1.2}
    first = await client.post("/api/tts/speak", json=body, headers=auth)
    assert first.status_code == 200 and first.headers["content-type"] == "audio/wav"
    assert first.headers["X-TTS-Chunks"] == "2" and first.headers["X-TTS-Cached-Chunks"] == "0"
    seconds, rate = wav_info(first.content)
    assert rate == 8000 and abs(seconds - 0.5) < 0.01 and abs(float(first.headers["X-TTS-Duration"]) - seconds) < 0.01
    assert len(tts_env.calls) == 1 and len(tts_env.calls[0]) == 2
    second = await client.post("/api/tts/speak", json=body, headers=auth)
    assert second.status_code == 200 and second.headers["X-TTS-Cached-Chunks"] == "2"
    assert second.content == first.content
    assert len(tts_env.calls) == 1  # 캐시 히트는 워커를 부르지 않는다
    stats = (await client.get("/api/tts/cache/stats", headers=auth)).json()
    assert stats["entries"] == 2 and stats["hits"] == 2 and stats["misses"] == 2 and stats["bytes"] > 0


async def test_speak_synthesizes_only_missing_chunks(client, auth, tts_env):
    await client.post("/api/tts/speak", json={"text": "첫 문장입니다."}, headers=auth)
    tts_env.calls.clear()
    r = await client.post("/api/tts/speak", json={"text": "첫 문장입니다. 둘째 문장입니다."}, headers=auth)
    assert r.headers["X-TTS-Cached-Chunks"] == "1"
    assert tts_env.calls == [["둘째 문장입니다."]]


async def test_speak_validation(client, auth, tts_env):
    long = await client.post("/api/tts/speak", json={"text": "가" * 501}, headers=auth)
    assert long.status_code == 422 and long.json()["code"] == "text_too_long"
    for body, code in (
        ({"text": "안녕", "voice": "ZZ"}, "unknown_voice"),
        ({"text": "안녕", "speed": 9}, "invalid_speed"),
        ({"text": "!!! ... ,,"}, "empty_text"),
    ):
        r = await client.post("/api/tts/speak", json=body, headers=auth)
        assert r.status_code == 422 and r.json()["code"] == code, (body, r.text)
    assert (await client.post("/api/tts/speak", json={"text": ""}, headers=auth)).status_code == 422
    assert (await client.post("/api/tts/speak", json={"text": "안녕"})).status_code == 401
    assert tts_env.calls == []


async def test_speak_waits_for_budget_then_503_busy(client, auth, tts_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_tts_speak_wait_seconds", 0.3)
    async with service._execution_lock:  # /jobs 워커가 실행 중인 상황과 같은 예산을 쥔다
        r = await client.post("/api/tts/speak", json={"text": "기다리는 문장입니다."}, headers=auth)
    assert r.status_code == 503 and r.json()["code"] == "busy" and r.headers["retry-after"] == "5"
    assert tts_env.calls == []
    ok = await client.post("/api/tts/speak", json={"text": "기다리는 문장입니다."}, headers=auth)
    assert ok.status_code == 200


async def test_speak_cache_hit_does_not_need_budget(client, auth, tts_env, monkeypatch):
    await client.post("/api/tts/speak", json={"text": "캐시 문장입니다."}, headers=auth)
    monkeypatch.setattr(get_settings(), "local_tts_speak_wait_seconds", 0.1)
    async with service._execution_lock:
        r = await client.post("/api/tts/speak", json={"text": "캐시 문장입니다."}, headers=auth)
    assert r.status_code == 200 and r.headers["X-TTS-Cached-Chunks"] == "1"


async def test_speak_worker_failure_is_502_without_detail(client, auth, tts_env):
    tts_env.fail = RuntimeError("secret internal path /Users/x")
    r = await client.post("/api/tts/speak", json={"text": "실패하는 문장입니다."}, headers=auth)
    assert r.status_code == 502 and r.json()["code"] == "worker_failed" and "secret" not in r.text
    tts_env.fail = TimeoutError()
    r = await client.post("/api/tts/speak", json={"text": "시간 초과 문장입니다."}, headers=auth)
    assert r.status_code == 504 and r.json()["code"] == "worker_timeout"


# ---------------------------------------------------------------- /jobs


async def test_job_lifecycle_and_token_audio(client, auth, tts_env):
    r = await client.post("/api/tts/jobs", json={"text": "첫째 문장입니다. 둘째 문장입니다. 셋째 문장입니다."}, headers=auth)
    assert r.status_code == 202
    created = r.json()
    assert created["chunks"] == 3 and created["cached_chunks"] == 0 and created["status"] == "queued"
    queued = (await client.get(f"/api/tts/jobs/{created['job_id']}", headers=auth)).json()
    assert queued["status"] == "queued" and queued["progress"] == {"done": 0, "total": 3}
    assert all("audio_url" not in c for c in queued["chunks"]) and "audio_full_url" not in queued

    assert await jobs.tick() is True
    done = (await client.get(f"/api/tts/jobs/{created['job_id']}", headers=auth)).json()
    assert done["status"] == "succeeded" and done["progress"] == {"done": 3, "total": 3}
    assert [c["index"] for c in done["chunks"]] == [0, 1, 2]
    assert all(c["audio_url"].startswith("/api/tts/audio/") and c["duration"] == 0.25 for c in done["chunks"])
    assert done["synthesized_chunks"] == 3 and done["metrics"]["rtf"] == 0.4 and done["metrics"]["first_chunk_seconds"] == 0.5
    assert abs(done["duration"] - 0.75) < 1e-6

    # <audio> 는 Authorization 헤더를 못 싣는다. 토큰 URL 은 헤더 없이 열린다.
    chunk = await client.get(done["chunks"][1]["audio_url"])
    assert chunk.status_code == 200 and chunk.headers["content-type"] == "audio/wav"
    assert abs(wav_info(chunk.content)[0] - 0.25) < 0.01
    full = await client.get(done["audio_full_url"])
    assert full.status_code == 200 and abs(wav_info(full.content)[0] - 0.75) < 0.01
    assert "attachment" in (await client.get(done["audio_full_url"] + "?download=true")).headers["content-disposition"]
    ranged = await client.get(done["audio_full_url"], headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100


async def test_all_cached_job_is_succeeded_immediately_without_worker(client, auth, tts_env):
    text = "이미 만든 문장입니다."
    await client.post("/api/tts/speak", json={"text": text}, headers=auth)
    tts_env.calls.clear()
    r = await client.post("/api/tts/jobs", json={"text": text}, headers=auth)
    assert r.status_code == 202 and r.json()["status"] == "succeeded" and r.json()["cached_chunks"] == 1
    state = (await client.get(f"/api/tts/jobs/{r.json()['job_id']}", headers=auth)).json()
    assert state["status"] == "succeeded" and "audio_full_url" in state
    assert await jobs.tick() is False and tts_env.calls == []


async def test_cached_chunks_are_playable_while_job_is_queued(client, auth, tts_env):
    await client.post("/api/tts/speak", json={"text": "먼저 만든 문장입니다."}, headers=auth)
    r = await client.post("/api/tts/jobs", json={"text": "먼저 만든 문장입니다. 새로 만들 문장입니다."}, headers=auth)
    assert r.json()["cached_chunks"] == 1
    state = (await client.get(f"/api/tts/jobs/{r.json()['job_id']}", headers=auth)).json()
    assert state["status"] == "queued" and state["progress"] == {"done": 1, "total": 2}
    assert "audio_url" in state["chunks"][0] and "audio_url" not in state["chunks"][1]


async def test_job_requires_auth_and_validates_input(client, auth, tts_env):
    assert (await client.post("/api/tts/jobs", json={"text": "안녕"})).status_code == 401
    too_long = await client.post("/api/tts/jobs", json={"text": "가" * 5001}, headers=auth)
    assert too_long.status_code == 422 and too_long.json()["code"] == "text_too_long"
    assert (await client.post("/api/tts/jobs", json={"text": "안녕", "voice": "X9"}, headers=auth)).status_code == 422


async def test_audio_token_failures(client, auth, tts_env):
    assert (await client.get("/api/tts/audio/garbage")).status_code == 403
    from ttakkari.security.auth import ALGO, ISSUER

    s = get_settings()
    base = {"iss": ISSUER, "sub": "chunk:" + "0" * 32, "jti": "x"}
    expired = jwt.encode({**base, "aud": "tts-audio", "exp": 1}, s.jwt_secret, algorithm=ALGO)
    assert (await client.get(f"/api/tts/audio/{expired}")).status_code == 403
    wrong_aud = jwt.encode({**base, "aud": "download", "exp": 9999999999}, s.jwt_secret, algorithm=ALGO)
    assert (await client.get(f"/api/tts/audio/{wrong_aud}")).status_code == 403
    from ttakkari.tts import read_model

    missing = read_model.chunk_url("0" * 32)
    r = await client.get(missing)
    assert r.status_code == 404 and r.json()["code"] == "audio_expired"
    # 일반 다운로드 토큰(aud=download)으로 TTS 오디오를 열 수 없다.
    from ttakkari.security.auth import issue_download_token

    dl, _ = issue_download_token(uuid.uuid4(), "original")
    assert (await client.get(f"/api/tts/audio/{dl}")).status_code == 403


async def test_audio_expired_when_cache_is_pruned_after_success(client, auth, tts_env):
    r = await client.post("/api/tts/jobs", json={"text": "곧 지워질 문장입니다."}, headers=auth)
    await jobs.tick()
    state = (await client.get(f"/api/tts/jobs/{r.json()['job_id']}", headers=auth)).json()
    url = state["audio_full_url"]
    for path in cache.cache_root().glob("??/*.wav"):
        path.unlink()
    assert (await client.get(url)).status_code == 404
    after = (await client.get(f"/api/tts/jobs/{r.json()['job_id']}", headers=auth)).json()
    assert after["status"] == "succeeded" and "audio_full_url" not in after and after["progress"]["done"] == 0


# ---------------------------------------------------------------- 원문 미저장

async def test_text_is_never_persisted_in_db_or_responses(client, auth, tts_env):
    r = await client.post("/api/tts/jobs", json={"text": SECRET_SENTENCE}, headers=auth)
    jid = r.json()["job_id"]
    assert jobs._pending_texts[uuid.UUID(jid)] == [SECRET_SENTENCE]  # 접수 프로세스 메모리에만 있다
    await jobs.tick()
    assert uuid.UUID(jid) not in jobs._pending_texts  # 종료 뒤 해제
    async with sessionmaker()() as db:
        dump = (await db.execute(text("SELECT row_to_json(t)::text FROM tts_jobs t"))).scalars().all()
        cache_dump = (await db.execute(text("SELECT row_to_json(t)::text FROM tts_cache t"))).scalars().all()
    blob = json.dumps(dump + cache_dump, ensure_ascii=False)
    for word in ("비밀문장", "알파브라보", "델타"):
        assert word not in blob
    state = json.dumps((await client.get(f"/api/tts/jobs/{jid}", headers=auth)).json(), ensure_ascii=False)
    assert "비밀문장" not in state
    row = await job_row(uuid.UUID(jid))
    assert set(row.params) == {"engine", "model_revision", "voice", "speed", "lang", "text_length", "chunk_count",
                               "chunk_keys", "cached_chunks"}  # fmt: skip


# ---------------------------------------------------------------- 세대 비활성

async def test_job_not_accepted_when_generation_is_not_active(client, auth, tts_env, tmp_path):
    runtime = generation.GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18801")
    await runtime.register()
    generation.current = runtime  # standby: active 가 아니다
    try:
        r = await client.post("/api/tts/jobs", json={"text": "접수되면 안 되는 문장입니다."}, headers=auth)
        assert r.status_code == 503 and r.json()["code"] == "generation_not_active" and r.headers["retry-after"]
        assert jobs._pending_texts == {}
        async with sessionmaker()() as db:
            assert (await db.execute(text("SELECT count(*) FROM tts_jobs"))).scalar_one() == 0
        speak = await client.post("/api/tts/speak", json={"text": "접수되면 안 되는 문장입니다."}, headers=auth)
        assert speak.status_code == 503 and speak.json()["code"] == "generation_not_active"
        assert (await client.get("/api/tts/status", headers=auth)).json()["accepting"] is False
        assert tts_env.calls == []
    finally:
        generation.current = None
        await runtime.close()


# ---------------------------------------------------------------- 캐시 API

async def test_cache_stats_and_prune_endpoints(client, auth, tts_env, monkeypatch):
    await client.post("/api/tts/speak", json={"text": "정리될 문장입니다. 남을 문장입니다."}, headers=auth)
    assert (await client.get("/api/tts/cache/stats")).status_code == 401
    assert (await client.post("/api/tts/cache/prune")).status_code == 401
    stats = (await client.get("/api/tts/cache/stats", headers=auth)).json()
    assert stats["entries"] == 2 and stats["max_bytes"] == 2 * 1024**3
    nothing = (await client.post("/api/tts/cache/prune", headers=auth)).json()
    assert nothing == {"removed": 0, "freed_bytes": 0}
    monkeypatch.setattr(get_settings(), "local_tts_cache_max_gb", stats["bytes"] / 2 / 1024**3)
    pruned = (await client.post("/api/tts/cache/prune", headers=auth)).json()
    assert pruned["removed"] == 1 and pruned["freed_bytes"] > 0
    assert (await client.get("/api/tts/cache/stats", headers=auth)).json()["entries"] == 1
