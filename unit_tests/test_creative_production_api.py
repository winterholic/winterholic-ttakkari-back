"""제작 기록 화면용 API 시험. 인증 라우터를 ASGI로 직접 호출하며 HTTP 서버·GPU·DB를 쓰지 않는다.

엔진은 실제 FFmpeg(CPU)이고, 감독 LLM은 고정 응답 fake다. 백그라운드 실행은 `api._tasks`를 기다려 결정적으로 검사한다.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from creative_support import click_track, real_ffmpeg_pipeline
from fastapi import FastAPI

from ttakkari.api import creative as api
from ttakkari.creative.direction import CreativeDirector
from ttakkari.creative.director_llm import FixedResponseLlm
from ttakkari.security.auth import require_user

BIBLE = {
    "palette": ["#101418", "#f4f4ed", "#00f0b5"],
    "typography": {"font_family": "sans-serif"},
    "composition": {"size": [320, 180], "aspect": "16:9"},
    "camera": {"language": ["정적"], "default_move": "orbit"},
    "lut": {"name": "none", "intent": "중립"},
    "negative_rules": [],
}
TIMELINE = {
    "sections": [],
    "shots": [
        {
            "shot_id": "shot-1",
            "start_f": 0,
            "end_f": 48,
            "cut": "action",
            "reason": "a",
        },
        {"shot_id": "shot-2", "start_f": 48, "end_f": 96, "cut": "free", "reason": "b"},
    ],
}
SHOTS = {
    "shots": [
        {
            "shot_id": "shot-1",
            "intent": "도입",
            "engine": "ffmpeg",
            "profile": "test-pattern",
            "params": {},
            "seed": 1,
            "reason": "모션",
        },
        {
            "shot_id": "shot-2",
            "intent": "홀드",
            "engine": "ffmpeg",
            "profile": "test-pattern",
            "params": {},
            "seed": 2,
            "reason": "모션",
        },
    ]
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    pipeline, store, _registry = real_ffmpeg_pipeline(tmp_path)
    llm = FixedResponseLlm(
        {
            "bible": json.dumps(BIBLE),
            "timeline": json.dumps(TIMELINE),
            "shots": json.dumps(SHOTS),
        }
    )
    director = CreativeDirector(pipeline, llm)
    monkeypatch.setattr(api, "default_pipeline", lambda: pipeline)
    monkeypatch.setattr(api, "default_director", lambda: director)
    monkeypatch.setattr(api, "default_director_store", lambda: store)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    return app, pipeline, store, llm


async def _settle():
    while api._tasks:
        await asyncio.gather(*list(api._tasks))


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _directed(client, store):
    created = await client.post(
        "/api/creative/productions", json={"brief": "심야 도시"}
    )
    pid = created.json()["manifest"]["production_id"]
    audio = store.put_artifact(
        pid, "click.wav", click_track(seconds=4), media_type="audio/wav"
    )
    started = await client.post(
        f"/api/creative/productions/{pid}/direct",
        json={"audio_sha256": audio["sha256"], "size": [320, 180]},
    )
    assert started.status_code == 202 and started.json()["status"] == "started"
    await _settle()
    return pid, audio["sha256"]


async def test_engine_registry_endpoints_report_measurement_axes_and_policy_rules(env):
    app, _pipeline, _store, _llm = env
    async with await _client(app) as client:
        listing = (await client.get("/api/creative/engines")).json()
        ffmpeg = next(e for e in listing if e["engine_id"] == "ffmpeg")
        assert (
            ffmpeg["installation"]["state"] == "available"
            and ffmpeg["verification"]["level"] == "lab_executed"
        )
        assert ffmpeg["auto_eligible"] is True and ffmpeg["execution_mode"] == "real"
        held = await client.post(
            "/api/creative/engines/ffmpeg/policy",
            json={"policy": "held", "reason": "점검"},
        )
        assert held.status_code == 200 and held.json()["auto_eligible"] is False
        assert (
            await client.post("/api/creative/engines/unknown/measure")
        ).status_code == 422
        bad = await client.post(
            "/api/creative/engines/ffmpeg/policy",
            json={"policy": "auto_eligible", "reason": ""},
        )
        assert bad.status_code == 422


async def test_direct_then_record_view_shows_shots_stale_flags_and_no_approval(env):
    app, _pipeline, store, _llm = env
    async with await _client(app) as client:
        pid, _ = await _directed(client, store)
        record = (await client.get(f"/api/creative/productions/{pid}/record")).json()
        assert [s["shot_id"] for s in record["shots"]] == ["shot-1", "shot-2"]
        assert (
            record["bible"]["approval_event_id"] is None
            and record["bible"]["author"] == "creative-director"
        )
        assert {d["step"] for d in record["direction"]} >= {
            "bible",
            "timeline",
            "shots",
        }
        first = record["shots"][0]
        assert (
            first["engine"] == "ffmpeg"
            and first["selection"] == "auto"
            and first["stale"] is True
        )
        assert (
            first["current_attempt"] is None
            and first["revisions"][-1]["revision"] == first["revision"]
        )
        assert record["final"] is None and any(
            e["engine_id"] == "ffmpeg" and e["auto_eligible"] for e in record["engines"]
        )
        assert (await client.get(f"/api/creative/productions/{pid}/record")).json()[
            "production_id"
        ] == pid


async def test_render_then_feedback_rerender_updates_only_that_shot_and_serves_artifacts(
    env,
):
    app, _pipeline, store, _llm = env
    async with await _client(app) as client:
        pid, audio_sha = await _directed(client, store)
        for shot_id in ("shot-1", "shot-2"):
            response = await client.post(
                f"/api/creative/productions/{pid}/shots/{shot_id}/render", json={}
            )
            assert response.status_code == 202 and response.json()["state"] == "queued"
        await _settle()
        record = (await client.get(f"/api/creative/productions/{pid}/record")).json()
        hashes = {
            s["shot_id"]: s["current_attempt"]["output"]["sha256"]
            for s in record["shots"]
        }
        for shot in record["shots"]:
            assert (
                shot["current_attempt"]["state"] == "review_pending"
                and shot["stale"] is False
            )
            assert (
                shot["review"]["verdict"] == "pass"
                and shot["review"]["approval_event_id"] is None
            )
            assert (
                shot["keyframes"]
                and shot["current_attempt"]["receipt"]["real_execution"] is True
            )
        # 키프레임·영상 미리보기 링크는 토큰이 있어야 열리고 다른 제작의 sha는 거부된다.
        frame = record["shots"][0]["keyframes"][0]["sha256"]
        link = await client.post(
            f"/api/creative/productions/{pid}/artifacts/{frame}/link"
        )
        assert link.status_code == 200
        shown = await client.get(link.json()["url"])
        assert (
            shown.status_code == 200
            and shown.headers["content-type"] == "image/png"
            and shown.content[:4] == b"\x89PNG"
        )
        assert (
            await client.get(
                f"/api/creative/productions/{pid}/artifacts/{frame}",
                params={"token": "bad"},
            )
        ).status_code == 403
        assert (
            await client.post(
                f"/api/creative/productions/{pid}/artifacts/{'0' * 64}/link"
            )
        ).status_code == 404
        # 피드백(사용자 덮어쓰기)으로 shot-2만 다시 만든다.
        revision = record["shots"][1]["revision"]
        response = await client.post(
            f"/api/creative/productions/{pid}/shots/shot-2/feedback",
            json={
                "expected_shot_revision": revision,
                "text": "움직임이 단조로워요. 다른 패턴으로.",
                "idempotency_key": "ui-1",
                "overrides": {"params": {"pattern": "mandelbrot"}, "seed": 9},
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert (
            body["from_revision"] == revision
            and body["to_revision"] == revision + 1
            and body["decided_by"] == "user-overrides"
        )
        await _settle()
        after = (await client.get(f"/api/creative/productions/{pid}/record")).json()
        new_hashes = {
            s["shot_id"]: s["current_attempt"]["output"]["sha256"]
            for s in after["shots"]
        }
        assert (
            new_hashes["shot-1"] == hashes["shot-1"]
            and new_hashes["shot-2"] != hashes["shot-2"]
        )
        shot2 = after["shots"][1]
        assert (
            shot2["revision"] == revision + 1
            and shot2["params"] == {"pattern": "mandelbrot"}
            and shot2["selection"] == "explicit-lab"
        )
        assert [f["feedback_id"] for f in shot2["feedback"]] == [
            body["feedback_id"]
        ] and len(shot2["attempts"]) == 2
        assert (
            after["shots"][0]["feedback"] == []
            and after["shots"][0]["revision"] == record["shots"][0]["revision"]
        )
        # 같은 요청을 다시 보내도 새 revision이 생기지 않는다(멱등). 낡은 revision은 409.
        replay = await client.post(
            f"/api/creative/productions/{pid}/shots/shot-2/feedback",
            json={
                "expected_shot_revision": revision,
                "text": "움직임이 단조로워요. 다른 패턴으로.",
                "idempotency_key": "ui-1",
                "overrides": {"params": {"pattern": "mandelbrot"}, "seed": 9},
            },
        )
        assert (
            replay.status_code == 202
            and replay.json()["attempt_id"] == body["attempt_id"]
        )
        stale = await client.post(
            f"/api/creative/productions/{pid}/shots/shot-2/feedback",
            json={
                "expected_shot_revision": revision,
                "text": "또 다른 요청",
                "idempotency_key": "ui-2",
                "overrides": {"seed": 1},
            },
        )
        assert stale.status_code == 409
        invalid = await client.post(
            f"/api/creative/productions/{pid}/shots/shot-2/feedback",
            json={
                "expected_shot_revision": revision + 1,
                "text": "x",
                "idempotency_key": "ui-3",
                "overrides": {"params": {"pattern": "rm -rf"}},
            },
        )
        assert invalid.status_code == 422
        # 최종 조립: stale이 없고 결함이 없으면 202, 이후 기록에 합성과 기계 검수가 보인다.
        final = await client.post(
            f"/api/creative/productions/{pid}/final", json={"audio_sha256": audio_sha}
        )
        assert final.status_code == 202
        await _settle()
        done = (await client.get(f"/api/creative/productions/{pid}/record")).json()
        assert (
            done["final"]["matches_current_shots"] is True
            and done["final"]["review"]["verdict"] == "pass"
        )
        assert (
            done["final"]["review"]["approval"] is None
            and done["stale"]["transition_neighbors"] == []
        )


async def test_final_is_refused_with_422_while_a_shot_is_stale_or_defective(env):
    app, _pipeline, store, _llm = env
    async with await _client(app) as client:
        pid, _ = await _directed(client, store)
        refused = await client.post(f"/api/creative/productions/{pid}/final", json={})
        assert refused.status_code == 422 and "stale" in refused.json()["detail"]
        missing = await client.post(
            f"/api/creative/productions/{pid}/shots/shot-9/render", json={}
        )
        assert missing.status_code == 422
        assert (
            await client.get(
                "/api/creative/productions/00000000-0000-4000-8000-000000000000/record"
            )
        ).status_code == 404


async def test_resume_endpoint_reruns_only_unfinished_attempts_after_a_crash(env):
    app, pipeline, store, _llm = env
    async with await _client(app) as client:
        pid, _ = await _directed(client, store)
        attempt = pipeline.submit_shot(
            pid, "shot-1"
        )  # 제출만 되고 실행 전(= crash 직후 상태)
        resumed = await client.post(f"/api/creative/productions/{pid}/resume")
        assert resumed.status_code == 202 and resumed.json()["resumed_attempts"] == [
            attempt["attempt_id"]
        ]
        await _settle()
        state = store.get_production(pid)
        assert state["attempts"][0]["state"] == "review_pending" and state["reviews"]
        again = await client.post(f"/api/creative/productions/{pid}/resume")
        assert again.json()["resumed_attempts"] == []


async def test_direct_failure_is_recorded_in_the_production_not_raised(
    env, monkeypatch
):
    app, _pipeline, _store, llm = env
    llm.responses["bible"] = ['```json\n{"palette": []}\n```']
    async with await _client(app) as client:
        created = await client.post(
            "/api/creative/productions", json={"brief": "실패 시험"}
        )
        pid = created.json()["manifest"]["production_id"]
        assert (
            await client.post(
                f"/api/creative/productions/{pid}/direct", json={"duration_seconds": 4}
            )
        ).status_code == 202
        await _settle()
        record = (await client.get(f"/api/creative/productions/{pid}/record")).json()
        assert any(d["status"] == "failed" for d in record["direction"])
        assert record["bible"]["revision"] == 1  # 실패한 단계는 아무것도 쓰지 않았다
