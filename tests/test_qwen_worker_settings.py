"""Owner activation never changes protected environment policy or skips doctor."""
import uuid

import pytest

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, ChatSession, Run, SessionKind
from ttakkari.orchestrator.runtime import issue_token

URL = "/api/system/qwen-worker"
KEY = "qwen_worker_activation"


@pytest.fixture(autouse=True)
def preserve_settings_fields():
    settings = get_settings()
    initial = set(settings.model_fields_set)
    yield
    settings.__pydantic_fields_set__ = initial


async def test_default_off_and_owner_only(client, auth):
    assert (await client.get(URL)).status_code == 401
    assert (await client.put(URL, json={"enabled": True})).status_code == 401
    response = await client.get(URL, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": False, "config_locked": False, "ready": False,
                               "reason": "disabled"}


async def test_agent_token_cannot_enable(client, auth, make_dir):
    ws = (await client.post("/api/workspaces", json={"name": "q", "root_path": str(make_dir())}, headers=auth)).json()
    sess = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)).json()
    agent = {"Authorization": f"Bearer {issue_token(sess['id'])}"}
    assert (await client.put(URL, json={"enabled": True}, headers=agent)).status_code == 401


@pytest.mark.parametrize("configured", [False, True])
async def test_explicit_operator_policy_cannot_be_overridden(configured, client, auth, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "qwen_local_enabled", configured)
    response = await client.put(URL, json={"enabled": not configured}, headers=auth)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "qwen_config_locked"
    async with sessionmaker()() as db:
        assert await db.get(AppSetting, KEY) is None


async def test_bad_readiness_does_not_store_activation(client, auth, monkeypatch):
    from ttakkari.agents.qwen_local import QwenLocalAdapter
    monkeypatch.setattr(QwenLocalAdapter, "readiness", lambda self: (False, "readiness_contract_unverified"))
    response = await client.put(URL, json={"enabled": True}, headers=auth)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "qwen_not_ready"
    assert "readiness_contract_unverified" in response.json()["message"]
    async with sessionmaker()() as db:
        assert await db.get(AppSetting, KEY) is None


async def test_activation_reaches_dispatch_and_disable_takes_effect(client, auth, monkeypatch):
    from ttakkari.agents.qwen_local import QwenLocalAdapter
    from ttakkari.runs.manager import _available_profiles
    monkeypatch.setattr(QwenLocalAdapter, "readiness", lambda self: (self.enabled, "ready" if self.enabled else "disabled"))
    response = await client.put(URL, json={"enabled": True}, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "config_locked": False, "ready": True, "reason": "ready"}
    # A new DB session observes the setting, as a separate generation/process would.
    async with sessionmaker()() as db:
        sess = ChatSession(id=uuid.uuid4(), kind=SessionKind.worker, engine="qwen-local", pool="personal")
        run = Run(usage={})
        free, _ = await _available_profiles(db, sess, run, False)
        assert [p.engine for p in free] == ["qwen-local"]
        sess.pool = "work"
        assert await _available_profiles(db, sess, run, False) == ([], [])
    off = await client.put(URL, json={"enabled": False}, headers=auth)
    assert off.status_code == 200 and off.json()["enabled"] is False
    async with sessionmaker()() as db:
        sess.pool = "personal"
        assert await _available_profiles(db, sess, run, False) == ([], [])


async def test_activation_changes_adapter_used_for_launch(client, auth, monkeypatch):
    from ttakkari.agents.qwen_local import QwenLocalAdapter
    from ttakkari.runs import qwen_activation
    monkeypatch.setattr(QwenLocalAdapter, "readiness", lambda self: (self.enabled, "ready"))
    response = await client.put(URL, json={"enabled": True}, headers=auth)
    assert response.status_code == 200, response.text
    async with sessionmaker()() as db:
        adapter = await qwen_activation.adapter_for(db)
        assert adapter.enabled is True


async def test_strict_boolean_and_unknown_fields(client, auth):
    for body in ({"enabled": "true"}, {"enabled": True, "worker_path": "/tmp/other"}):
        response = await client.put(URL, json=body, headers=auth)
        assert response.status_code == 422, response.text


async def test_owner_activation_runs_and_resumes_without_changing_env(client, auth, make_dir, tmp_path, monkeypatch):
    from helpers import wait_run
    from test_qwen_local_run import _write_worker

    from ttakkari.agents.qwen_local import QwenLocalAdapter
    from ttakkari.runs import manager as run_manager
    worker, capture = _write_worker(tmp_path)
    monkeypatch.setattr(get_settings(), "qwen_local_worker_path", worker)
    base = QwenLocalAdapter(worker_path=worker, enabled=False)
    monkeypatch.setattr(run_manager, "QWEN_LOCAL_ADAPTER", base)
    enabled = await client.put(URL, json={"enabled": True}, headers=auth)
    assert enabled.status_code == 200, enabled.text
    ws = (await client.post("/api/workspaces", json={"name": "fixture", "root_path": str(make_dir())}, headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)).json()
    agent = {"Authorization": f"Bearer {issue_token(parent['id'])}"}
    created = await client.post("/api/orchestrator/workers", headers=agent, json={
        "title": "owner opt-in fixture", "prompt": "fixture only", "engine": "qwen-local", "task_type": "routine"})
    assert created.status_code == 201, created.text
    run = await wait_run(client, auth, created.json()["active_run_id"])
    assert run["status"] == "succeeded", run
    wid = created.json()["worker_id"]
    follow = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=agent,
                              json={"prompt": "continue fixture"})
    assert follow.status_code == 202, follow.text
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    resumed = await wait_run(client, auth, runs[-1]["id"])
    assert resumed["status"] == "succeeded", resumed
    argv = capture.read_text().splitlines()
    assert argv[argv.index("--resume") + 1] == "qwen-session"
    assert base.enabled is False and get_settings().qwen_local_enabled is False
