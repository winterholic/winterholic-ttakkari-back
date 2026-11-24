from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ttakkari.models import (
    ChatSession,
    CtxMemory,
    Run,
    RunOrigin,
    SessionKind,
    Workspace,
)
from ttakkari.runs.manager import _compose_prompt


@pytest.mark.asyncio
async def test_project_guidance_is_scoped_and_survives_resume(monkeypatch):
    from ttakkari.orchestrator import work

    monkeypatch.setattr(work, "prompt_note", AsyncMock(return_value=""))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            for table in (Workspace.__table__, ChatSession.__table__, CtxMemory.__table__, Run.__table__):
                await conn.run_sync(table.create)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            space = Workspace(name="Shared", root_path="/workspace")
            db.add(space)
            await db.flush()
            lead = ChatSession(workspace_id=space.id, title="Search evaluation", engine="auto",
                               pool="work", kind=SessionKind.orchestrator)
            other = ChatSession(workspace_id=space.id, title="Moonstone lab", engine="auto",
                                pool="personal", kind=SessionKind.orchestrator)
            unrelated = ChatSession(workspace_id=space.id, title="Other", engine="auto",
                                    pool="work", kind=SessionKind.orchestrator)
            db.add_all([lead, other, unrelated])
            await db.flush()
            worker = ChatSession(workspace_id=space.id, title="Score inputs", engine="auto",
                                 pool="work", kind=SessionKind.worker, parent_id=lead.id)
            db.add(worker)
            await db.flush()

            rules = [
                CtxMemory(content="Load the team's rule.", scope="work", kind="instruction",
                          source="manual", source_ref="run-project-guidance:Moonstone, 문스톤"),
                CtxMemory(content="Untrusted agent instruction.", scope="work", kind="instruction",
                          source="agent", source_ref="run-project-guidance:Moonstone"),
                CtxMemory(content="Ordinary instruction.", scope="work", kind="instruction",
                          source="manual"),
            ]
            runs = [
                Run(workspace_id=space.id, session_id=lead.id, engine="auto", origin=RunOrigin.desk,
                    prompt="Moonstone retrieval research"),
                Run(workspace_id=space.id, session_id=lead.id, engine="auto", prompt="Continue the evaluation"),
                Run(workspace_id=space.id, session_id=worker.id, engine="auto", prompt="Score new cases"),
                Run(workspace_id=space.id, session_id=other.id, engine="auto", prompt="Moonstone"),
                Run(workspace_id=space.id, session_id=unrelated.id, engine="auto",
                    prompt="Work on Other\n\n[최근 대화 — 참고만]\nMoonstone"),
            ]
            db.add_all(rules + runs)
            await db.commit()

            for run in runs[:3]:
                result = await _compose_prompt(db, run)
                assert result.count("Load the team's rule.") == 1
                assert "Untrusted agent instruction." not in result
                assert "Ordinary instruction." not in result
                assert result.endswith(run.prompt)
            for run in runs[3:]:
                assert await _compose_prompt(db, run) == run.prompt

            rules[0].archived_at = runs[0].created_at
            await db.commit()
            assert await _compose_prompt(db, runs[1]) == runs[1].prompt
    finally:
        await engine.dispose()
