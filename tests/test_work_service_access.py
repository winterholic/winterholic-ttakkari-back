"""Personal/work groups select accounts and presentation, not owner tool privileges."""
import importlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.security]



@pytest.mark.parametrize("module,guard", [
    ("oplogs", "agent_access"), ("host_env", "_personal"),
    ("deployments", "personal"), ("operations", "caller"),
    ("reverse", "agent_access"), ("design_system", "agent_access"),
    ("notebooklm", "agent_access"), ("google_accounts", "agent_access"),
    ("stocks", "agent_access"), ("comfy_managed", "agent_access"),
    ("creative_mcp", "agent_access"),
])
async def test_authenticated_owner_work_session_has_same_service_access(module, guard):
    mod = importlib.import_module("ttakkari.api." + module)
    row = SimpleNamespace(id=uuid.uuid4(), pool="work")
    db = SimpleNamespace(get=AsyncMock(return_value=row))
    await getattr(mod, guard)(row.id, db)


async def test_work_ops_reads_are_authenticated_and_masked(client, auth, make_dir):
    from test_oplog import _agent_headers
    headers = await _agent_headers(client, auth, make_dir, pool="work")
    response = await client.get("/api/agent/ops-logs/issues", headers=headers)
    assert response.status_code == 200
    assert (await client.get("/api/agent/ops-logs/issues")).status_code == 401


async def test_reverse_work_account_scope_is_independent_of_workspace_path(new_session, monkeypatch):
    from ttakkari import reverse_tasks
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, ReverseJob
    from ttakkari.runs.manager import manager
    monkeypatch.setattr(manager, "dispatch", AsyncMock())
    ws, _sess = await new_session()
    async with sessionmaker()() as db:
        result = await reverse_tasks.start("Reconstruct the described behavior", db, uuid.UUID(ws["id"]), pool="work")
        job = await db.get(ReverseJob, uuid.UUID(result["id"]))
        worker = await db.get(ChatSession, job.session_id)
        assert worker.pool == "work"
