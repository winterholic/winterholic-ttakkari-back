"""2026-10-28 owner policy: work/personal grouping does not deny host tools.

Previous blanket-403 expectations are superseded. Session authentication, fixed
operation validation, secret masking and service resource ownership remain tested.
"""

import pytest
from authz03_canary import ensure_authz03

from ttakkari.db import sessionmaker
from ttakkari.orchestrator.runtime import issue_token

pytestmark = [pytest.mark.security]


@pytest.fixture
async def env(make_dir):
    async with sessionmaker()() as db:
        return await ensure_authz03(db, make_dir)


@pytest.mark.parametrize("path,expected", [
    ("/ops-logs/issues", 200), ("/ops-logs/events", 200),
    ("/ops-logs/trend", 200), ("/ops-logs/deploys", 200),
    ("/deploy/00000000-0000-0000-0000-000000000000", 404),
    ("/notebooklm/auth/status", 200), ("/reverse", 200),
    ("/execution-capabilities", 200),
])
async def test_host_read_privileges_match_for_both_owner_groups(client, env, path, expected):
    for sid in (env.work_session_id, env.personal_session_id):
        response = await client.get("/api/agent" + path,
            headers={"Authorization": "Bearer " + issue_token(sid)})
        assert response.status_code == expected, response.text
    assert (await client.get("/api/agent" + path)).status_code == 401


async def test_work_probe_uses_own_run_validation_instead_of_pool_denial(client, env):
    response = await client.post("/api/agent/execution-capabilities/challenge",
        headers={"Authorization": "Bearer " + issue_token(env.work_session_id)})
    assert response.status_code == 409  # authenticated, but no Run identity to probe
