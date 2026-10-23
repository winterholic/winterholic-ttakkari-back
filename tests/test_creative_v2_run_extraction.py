"""Creative intent uses the existing persisted Qwen Run path; fixtures never claim a real model call."""

import json

import pytest

from ttakkari.config import get_settings


@pytest.mark.asyncio
async def test_intent_run_uses_personal_qwen_and_returns_review_only_structured_output(
    client, auth, tmp_path, monkeypatch,
):
    from helpers import wait_run
    from test_qwen_local_run import _write_worker

    from ttakkari.agents.qwen_local import QwenLocalAdapter
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession
    from ttakkari.runs import manager

    brief = "기술 설명 영상으로 요청을 받아 검증하는 과정을 만들어줘"
    response_text = json.dumps({
        "schema_version": 1,
        "selections": [{
            "entry_id": "C01.tech_explainer", "role": "primary", "strength": "hard",
            "source": "user_explicit", "confidence": 1,
            "evidence": [{"quote": "기술 설명"}],
        }],
        "constraints": [],
        "duration": {"mode": "auto"}, "aspect": None,
        "info_items": [{
            "id": "i1", "title": "요청 흐름", "narration": "요청을 받아 검증하는 과정",
            "weight": 1, "essential": True,
            "evidence": [{"quote": "요청을 받아 검증하는 과정"}],
        }],
        "music_seconds": None, "open_questions": [],
        "extraction": {"method": "user_structured", "model": "forged"},
    }, ensure_ascii=False)
    worker, _capture = _write_worker(tmp_path, run_result=json.dumps({
        "schema": 1, "run_id": "fixture", "status": "success", "exit_code": 0,
        "session_id": "qwen-creative-session", "result_text": response_text,
        "files": {"added": [], "modified": [], "deleted": []},
        "metrics": {"host_peaks": {"server_rss_max_bytes": 1024}, "elapsed_seconds": 0.1},
    }))
    settings = get_settings()
    monkeypatch.setattr(settings, "qwen_local_worker_path", worker)
    monkeypatch.setattr(QwenLocalAdapter, "readiness", lambda self: (True, "fixture_ready"))
    monkeypatch.setattr(manager, "QWEN_LOCAL_ADAPTER", QwenLocalAdapter(worker_path=worker, enabled=True))

    enabled = await client.put("/api/system/qwen-worker", json={"enabled": True}, headers=auth)
    assert enabled.status_code == 200, enabled.text
    created = await client.post(
        "/api/creative/v2/intent/runs", json={"brief": brief},
        headers={**auth, "Idempotency-Key": "creative-intent-request-1"},
    )
    assert created.status_code == 202, created.text
    run = await wait_run(client, auth, created.json()["run_id"])
    assert run["status"] == "succeeded" and run["engine"] == "qwen-local"
    reviewed = await client.get(
        f"/api/creative/v2/intent/runs/{run['id']}", headers=auth,
    )
    assert reviewed.status_code == 200, reviewed.text
    data = reviewed.json()
    assert data["intent"]["selections"][0]["source"] == "llm_inferred"
    assert data["intent"]["selections"][0]["strength"] == "soft"
    assert data["intent"]["info_items"][0]["essential"] is False
    assert data["intent"]["extraction"] == {"method": "llm", "model": run["model"]}
    assert data["intent"]["open_questions"] and data["review_required"] is True
    duplicate = await client.post(
        "/api/creative/v2/intent/runs", json={"brief": brief},
        headers={**auth, "Idempotency-Key": "creative-intent-request-1"},
    )
    assert duplicate.status_code == 202 and duplicate.json()["run_id"] == run["id"]
    conflict = await client.post(
        "/api/creative/v2/intent/runs", json={"brief": "다른 브리프"},
        headers={**auth, "Idempotency-Key": "creative-intent-request-1"},
    )
    assert conflict.status_code == 409
    async with sessionmaker()() as db:
        session = await db.get(ChatSession, run["session_id"])
        assert session.worker_task_type == "creative_intent"
        assert session.pool == "personal" and session.kind == "worker"


@pytest.mark.asyncio
async def test_intent_run_fails_closed_when_qwen_is_disabled(client, auth):
    response = await client.post(
        "/api/creative/v2/intent/runs", json={"brief": "기술 설명"}, headers=auth,
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "intent_engine_unavailable"
