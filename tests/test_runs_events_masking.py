from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from ttakkari.db import sessionmaker
from ttakkari.models import Run, RunStatus
from ttakkari.runs.events import append


def test_append_masks_sensitive_keys_and_values():
    db = MagicMock()
    run = Run(id=uuid.uuid4(), last_seq=0)

    payload = {
        "tool": "shell",
        "command": "curl -H 'Authorization: Bearer secret-token-12345678' https://api.example.com",
        "api_key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456",
        "password": "super-secret-password-123",
        "ttk_refresh": "ttk_refresh_canary_value_9876543210123456",
        "normal_field": "safe_information",
        "details": {
            "token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
            "nested_safe": 42,
        },
    }

    ev = append(db, run, "tool_call", payload)

    assert run.last_seq == 1
    assert ev.seq == 1
    assert ev.type == "tool_call"

    # 일반 필드는 보존되어야 한다 (과도한 삭제 방지 대조군)
    assert ev.payload["tool"] == "shell"
    assert ev.payload["normal_field"] == "safe_information"
    assert ev.payload["details"]["nested_safe"] == 42

    # 민감한 값 및 키는 마스킹되어야 한다
    assert ev.payload["password"] == "[REDACTED]"
    assert ev.payload["api_key"] == "[REDACTED]"
    assert ev.payload["details"]["token"] == "[REDACTED]"
    assert "secret-token-12345678" not in ev.payload["command"]
    assert "[REDACTED]" in ev.payload["command"]
    assert "ttk_refresh_canary_value_9876543210123456" not in str(ev.payload)


@pytest.mark.asyncio
async def test_sensitive_event_canary_stays_masked_in_database_list_and_sse(client, auth, new_session):
    workspace, session = await new_session()
    bearer = "synthetic-bearer-canary-abcdefghijk"
    cookie = "synthetic-cookie-canary-abcdefghijk"
    async with sessionmaker()() as db:
        run = Run(
            id=uuid.uuid4(), session_id=uuid.UUID(session["id"]), workspace_id=uuid.UUID(workspace["id"]),
            prompt="synthetic event scrub test", engine="claude", status=RunStatus.succeeded,
        )
        db.add(run)
        await db.flush()
        append(db, run, "message", {
            "message": "Normal diagnostic text remains visible.",
            "nested": [{"authorization": f"Bearer {bearer}"}, {"headers": f"Cookie: ttk_refresh={cookie}"}],
        })
        run_id = run.id
        await db.commit()

    listed = await client.get(f"/api/runs/{run_id}/events", headers=auth)
    streamed = await client.get(f"/api/runs/{run_id}/stream", headers=auth)
    assert listed.status_code == streamed.status_code == 200
    assert bearer not in listed.text + streamed.text
    assert cookie not in listed.text + streamed.text
    assert "Normal diagnostic text remains visible." in listed.text + streamed.text
