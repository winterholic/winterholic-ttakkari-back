from __future__ import annotations

import json

import pytest
from helpers import run_events, run_to_end

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunEvent, RunStatus
from ttakkari.textsafe import strip_nul

pytestmark = [pytest.mark.security]



def test_strip_nul_cleans_nested_values():
    src = {"a\u0000": ["x\u0000y", {"k": "v\u0000"}, 1, None, True], "n": 3}
    assert strip_nul(src) == {"a�": ["x�y", {"k": "v�"}, 1, None, True], "n": 3}
    assert strip_nul("clean") == "clean"


async def test_run_with_nul_in_tool_output_succeeds(client, auth, new_session):
    _ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "NULBYTE")
    assert run["status"] == "succeeded", run
    assert "\u0000" not in json.dumps(run)
    assert run["result_text"] == "done�ok"
    events = await run_events(client, auth, run["id"])
    assert any("�" in json.dumps(e, ensure_ascii=False) for e in events)
    assert "\\u0000" not in json.dumps(events)


async def test_orm_flush_strips_nul_from_text_and_jsonb(new_session):
    _ws, sess = await new_session()
    async with sessionmaker()() as db:
        chat = await db.get(ChatSession, sess["id"])
        run = Run(session_id=chat.id, workspace_id=chat.workspace_id, prompt="p\u0000q", engine="claude",
                  status=RunStatus.failed, error="e\u0000", usage={"deep": [{"t": "a\u0000b"}]})
        db.add(run)
        await db.flush()
        db.add(RunEvent(run_id=run.id, seq=1, type="log", payload={"text": "x\u0000y"}))
        await db.commit()
        # 이미 저장된 행을 고칠 때도 걸러진다.
        run.result_text = "r\u0000"
        run.usage = {"again": "z\u0000"}
        await db.commit()
        await db.refresh(run)
        assert (run.prompt, run.error, run.result_text) == ("p�q", "e�", "r�")
        assert run.usage == {"again": "z�"}
