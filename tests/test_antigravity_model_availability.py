"""Accounts differ in which Antigravity models they can run; routing must not pick one the CLI rejects."""
import textwrap
import uuid
from types import SimpleNamespace

import pytest
from helpers import wait_run

from ttakkari.config import get_settings
from ttakkari.runs import agy_models, profiles, worker_routing

SONNET = "claude-sonnet-5-5-high"
FLASH = "gemini-3.8-flash-high"
CLI_ERROR = (
    'invalid model selection (--model "claude-sonnet-5-5-high" --effort ""): model claude-sonnet-5-5-high '
    "is not recognized as a known model or custom model in settings\n"
    "Available models: Gemini 3.8 Flash (High/Medium/Low), Claude Sonnet 4.6 (Thinking), "
    "Claude Opus 4.6 (Thinking), GPT-OSS 120B (Medium)"
)


@pytest.fixture(autouse=True)
def clean_registry():
    agy_models.reset()
    yield
    agy_models.reset()


def test_cli_rejection_text_and_model_list_are_parsed():
    assert agy_models.is_invalid_model(None, CLI_ERROR)
    assert not agy_models.is_invalid_model("quota exhausted", None)
    listing = "Fetching available models...\n" + f"{SONNET}\tClaude Sonnet 5.5 (High)\n{FLASH}\tGemini 3.8 Flash (High)\n"
    assert agy_models.parse_models(listing) == {SONNET, FLASH}


def test_unknown_list_never_excludes_but_a_known_list_and_a_rejection_do():
    aid = uuid.uuid4()
    p = profiles.Profile(aid, "agy", "antigravity", model=SONNET)
    assert agy_models.unsupported_reason(p) is None
    agy_models._known[aid] = (9e12, frozenset({FLASH}))
    assert agy_models.unsupported_reason(p) == "not_in_account_model_list"
    assert agy_models.unsupported_reason(replace_model(p, FLASH)) is None
    agy_models._known.pop(aid)
    agy_models.reject(aid, SONNET, account="agy", detail=CLI_ERROR)
    assert agy_models.unsupported_reason(p) == "cli_rejected_model"
    assert agy_models.exclusions()[0]["reason"] == "cli_rejected_model"


def replace_model(p, model):
    from dataclasses import replace
    return replace(p, model=model)


async def make_accounts(client, auth, *names):
    ids = {}
    for name in names:
        r = await client.post("/api/accounts", json={"name": name, "engine": "antigravity"}, headers=auth)
        assert r.status_code == 201, r.text
        ids[name] = uuid.UUID(r.json()["id"])
    return ids


async def test_account_without_the_model_is_excluded_and_receipt_records_why(client, auth, monkeypatch):
    from ttakkari.db import sessionmaker
    ids = await make_accounts(client, auth, "full", "legacy")
    # The second account gets a managed home that is not logged in; readiness is not under test.
    monkeypatch.setattr(profiles, "execution_ready", lambda profile: True)
    monkeypatch.setattr(get_settings(), "antigravity_model_probe", True)
    lists = {ids["full"]: frozenset({SONNET, FLASH}), ids["legacy"]: frozenset({FLASH})}
    monkeypatch.setattr(agy_models, "_probe", lambda profile: lists[profile.id])
    async with sessionmaker()() as db:
        free, _blocked = await worker_routing.availability(db, "general", "personal")
    by_model = {(p.name, p.model) for p in free if p.engine == "antigravity"}
    assert ("full", SONNET) in by_model and ("legacy", FLASH) in by_model
    assert ("legacy", SONNET) not in by_model
    selected = next(p for p in free if (p.name, p.model) == ("full", SONNET))
    receipt = worker_routing.receipt("general", selected, free, [], {}, trigger="initial")
    assert {"account": "legacy", "model": SONNET, "reason": "not_in_account_model_list"} in receipt["excluded"]


async def test_probe_failure_keeps_every_candidate(client, auth, monkeypatch):
    from ttakkari.db import sessionmaker
    await make_accounts(client, auth, "agy")
    monkeypatch.setattr(get_settings(), "antigravity_model_probe", True)
    monkeypatch.setattr(agy_models, "_probe", lambda profile: frozenset())
    async with sessionmaker()() as db:
        free, _ = await worker_routing.availability(db, "general", "personal")
    assert any(p.engine == "antigravity" and p.model == SONNET for p in free)
    assert agy_models.exclusions() == []


async def test_invalid_model_failure_falls_back_instead_of_reselecting_the_same_model(
    client, auth, make_dir, monkeypatch
):
    from ttakkari.agents import antigravity
    from ttakkari.orchestrator.runtime import issue_token

    root = make_dir()
    cli = root / "fake_agy.py"
    cli.write_text(textwrap.dedent(f'''\
        #!/usr/bin/env python3
        import json, sys
        model = sys.argv[sys.argv.index('--model') + 1]
        if model == {SONNET!r}:
            sys.stderr.write({CLI_ERROR!r})
            raise SystemExit(1)
        print(json.dumps({{"event": "init", "conversation_id": "fixture-" + model}}), flush=True)
        print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": model}}}}), flush=True)
    '''))
    cli.chmod(0o700)
    monkeypatch.setattr(antigravity, "get_settings", lambda: SimpleNamespace(antigravity_bin=str(cli), run_timeout_seconds=10))
    ids = await make_accounts(client, auth, "legacy")
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    orch = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)).json()
    bearer = {"Authorization": f"Bearer {issue_token(uuid.UUID(orch['id']))}"}

    async def spawn():
        r = await client.post("/api/orchestrator/workers", json={
            "title": "model fallback", "prompt": "fixture", "engine": "auto", "task_type": "general",
        }, headers=bearer)
        assert r.status_code == 201, r.text
        runs = (await client.get(f"/api/sessions/{r.json()['worker_id']}/runs", headers=auth)).json()
        return await wait_run(client, auth, runs[0]["id"])

    first = await spawn()
    assert first["status"] == "succeeded", first
    assert first["result_text"] == FLASH and first["model"] == FLASH
    events = (await client.get(f"/api/runs/{first['id']}/events", headers=auth)).json()
    kinds = [e["type"] for e in events]
    assert "account.model_unsupported" in kinds
    fallback = next(e for e in events if e["type"] == "engine.fallback")
    assert fallback["payload"]["reason"] == "invalid_model"
    assert fallback["payload"]["from_model"] == SONNET and fallback["payload"]["to_model"] == FLASH
    assert agy_models.unsupported_reason(profiles.Profile(ids["legacy"], "legacy", "antigravity", model=SONNET))

    second = await spawn()
    assert second["status"] == "succeeded" and second["model"] == FLASH
    routing = [e["payload"] for e in (await client.get(f"/api/runs/{second['id']}/events", headers=auth)).json()
               if e["type"] == "worker.routing"]
    assert routing[0]["selected"]["model"] == FLASH
    assert any(x["model"] == SONNET and x["reason"] == "cli_rejected_model" for x in routing[0]["excluded"])
    assert "account.model_unsupported" not in [e["type"] for e in (await client.get(
        f"/api/runs/{second['id']}/events", headers=auth)).json()]
