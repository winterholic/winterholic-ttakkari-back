"""Subscription CLI L2 transport contracts against a fake `codex` executable.

Nothing here reaches a real provider: the fake records argv/cwd/env and answers by prompt marker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete
from test_kb_v3_l2_budget_concurrency import (  # noqa: F401
    _context,
    _ledger_counts,
    pause_default_kb_worker,
)

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_pipeline import ExtractingProcessor
from ttakkari.kb_pipeline.extract_l2 import L2_SCHEMA
from ttakkari.kb_pipeline.l2_adapters import SubscriptionCliL2Adapter
from ttakkari.kb_pipeline.subscription_transport import (
    DIAG_INVALID_JSON,
    DIAG_NONZERO_EXIT,
    DIAG_SCHEMA_VIOLATION,
    DIAG_TIMEOUT,
    DIAG_USAGE_LIMIT,
    L2TransportError,
    ScopeCapacityUnavailableError,
)
from ttakkari.models import EngineAccount
from ttakkari.runs import profiles

EMPTY = {"entities": [], "assertions": [], "relations": []}
BODY_MARKER = "SECRET-PASSAGE-BODY-7731"


def test_transport_reexports_shared_error_contract_objects_and_legacy_constructor():
    import ast
    from pathlib import Path

    from ttakkari import subscription_contract
    from ttakkari.kb_pipeline import subscription_transport

    tree = ast.parse(Path(subscription_contract.__file__).read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not any(name and name.startswith(("ttakkari.kb", "ttakkari.runs", "ttakkari.orchestrator"))
                   for name in imports)

    names = (
        "DIAG_NONZERO_EXIT", "DIAG_TIMEOUT", "DIAG_USAGE_LIMIT", "DIAG_INVALID_JSON",
        "DIAG_SCHEMA_VIOLATION", "DIAG_SCOPE_CAPACITY_UNAVAILABLE",
    )
    for name in names:
        assert getattr(subscription_transport, name) is getattr(subscription_contract, name)
    assert subscription_transport.L2TransportError is subscription_contract.L2TransportError
    assert subscription_transport.ScopeCapacityUnavailableError is subscription_contract.ScopeCapacityUnavailableError

    error = subscription_transport.L2TransportError(
        DIAG_USAGE_LIMIT, "quota reached", 123, consumed=False,
        engine="codex", account="fixture-account", model="fixture-model",
    )
    assert str(error) == "[usage_limit] quota reached"
    assert (error.diagnosis, error.retry_after, error.consumed) == (DIAG_USAGE_LIMIT, 123, False)
    assert (error.engine, error.account, error.model) == ("codex", "fixture-account", "fixture-model")

    scope_error = subscription_transport.ScopeCapacityUnavailableError("no scoped profile", retry_after=456)
    assert isinstance(scope_error, subscription_contract.L2TransportError)
    assert str(scope_error) == "[scope_capacity_unavailable] no scoped profile"
    assert (scope_error.diagnosis, scope_error.retry_after, scope_error.consumed) == (
        subscription_contract.DIAG_SCOPE_CAPACITY_UNAVAILABLE, 456, False
    )


FAKE_CODEX = r'''#!{python}
import json, os, sys, time

log_dir = os.environ["FAKE_L2_LOG"]
argv = sys.argv[1:]
prompt = sys.stdin.read() if argv[-1] == "-" else argv[-1]
record = {{"argv": argv, "cwd": os.getcwd(), "codex_home": os.environ.get("CODEX_HOME"), "pid": os.getpid(), "prompt_on_stdin": argv[-1] == "-", "prompt_chars": len(prompt),
          "marker_in_argv": any("SECRET-PASSAGE-BODY-7731" in a for a in argv),
          "marker_in_stdin": "SECRET-PASSAGE-BODY-7731" in prompt,
          "paid_env": sorted(k for k in os.environ if k in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL"))}}
with open(os.path.join(log_dir, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(record) + "\n")

def reply(text):
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": text}}}}), flush=True)

if "MODE_SLEEP" in prompt:
    with open(os.path.join(log_dir, "sleep.pid"), "w") as fh:
        fh.write(str(os.getpid()))
    time.sleep(60)
elif "MODE_QUOTA" in prompt:
    print(json.dumps({{"type": "turn.failed", "error": {{"message": "Individual quota reached. Resets in 3h9m27s."}}}}), flush=True)
    sys.exit(1)
elif "MODE_BAD_JSON" in prompt:
    reply("this is not json")
elif "MODE_BAD_SCHEMA" in prompt:
    reply(json.dumps({{"entities": [], "assertions": [], "relations": [], "extra": 1}}))
else:
    time.sleep(0.05)
    reply(json.dumps({{"entities": [], "assertions": [], "relations": []}}))
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    log_dir = tmp_path / "fake-log"
    log_dir.mkdir()
    script = tmp_path / "fake-codex"
    script.write_text(FAKE_CODEX.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_L2_LOG", str(log_dir))
    monkeypatch.setattr(get_settings(), "codex_bin", str(script))
    profiles.reset_cooldowns()

    class Fake:
        dir = log_dir

        def calls(self) -> list[dict]:
            path = log_dir / "calls.jsonl"
            if not path.exists():
                return []
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    yield Fake()
    profiles.reset_cooldowns()


async def _account(tmp_path: Path, pool: str, *, mcp: bool = False) -> EngineAccount:
    config_dir = tmp_path / f"codex-home-{pool}-{uuid.uuid4().hex[:6]}"
    config_dir.mkdir()
    if mcp:
        (config_dir / "config.toml").write_text('[mcp_servers.dangerous]\ncommand = "x"\n', encoding="utf-8")
    async with sessionmaker()() as db:
        account = EngineAccount(
            name=f"l2-{pool}-{uuid.uuid4().hex[:8]}", engine="codex", pool=pool, config_dir=str(config_dir)
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
    return account


@pytest.fixture(autouse=True)
async def _clean_accounts():
    yield
    async with sessionmaker()() as db:
        await db.execute(delete(EngineAccount).where(EngineAccount.name.like("l2-%")))
        await db.commit()


def _adapter(origin_scope="personal", visibility="global", timeout=20.0) -> SubscriptionCliL2Adapter:
    return SubscriptionCliL2Adapter(
        sessionmaker(), engine="codex", timeout=timeout, concurrency=1,
        origin_scope=origin_scope, visibility=visibility,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_call_is_tool_free_isolated_and_does_not_log_the_passage(fake_cli, tmp_path, caplog):
    account = await _account(tmp_path, "personal", mcp=True)
    caplog.set_level(logging.DEBUG)

    result = await _adapter()(f"본문 {BODY_MARKER}", L2_SCHEMA)

    assert result == EMPTY
    [call] = fake_cli.calls()
    argv = call["argv"]
    assert argv[0] == "exec" and "--ephemeral" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"hooks", "shell_tool", "unified_exec", "web_search_request", "collab", "plugins"} <= disabled
    assert "mcp_servers.dangerous.enabled=false" in argv
    assert 'approval_policy="never"' in argv
    # Schema file and cwd are the same unique temp dir, and it is gone afterwards.
    cwd = Path(call["cwd"])
    assert Path(argv[argv.index("--output-schema") + 1]).parent.resolve() == cwd.resolve()
    assert Path(argv[argv.index("-C") + 1]).resolve() == cwd.resolve()
    assert cwd.name.startswith("ttakkari-kb-l2-") and not cwd.exists()
    assert call["codex_home"] == account.config_dir
    assert BODY_MARKER not in caplog.text


async def test_paid_api_credentials_are_never_inherited_by_the_cli(fake_cli, tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.setenv(key, "must-not-reach-the-subscription-cli")
    await _account(tmp_path, "personal")

    await _adapter()("env check", L2_SCHEMA)

    assert fake_cli.calls()[0]["paid_env"] == []


def test_claude_command_is_tool_free_and_carries_the_json_schema(tmp_path):
    from ttakkari.runs import desk

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(L2_SCHEMA), encoding="utf-8")
    cmd = desk._claude_cmd({}, "system", "prompt", output_schema=schema_path)

    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == L2_SCHEMA
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert "--json-schema" not in desk._claude_cmd({}, "system", "prompt")


async def test_provenance_selects_only_permitted_account_pool(fake_cli, tmp_path):
    personal = await _account(tmp_path, "personal")
    work = await _account(tmp_path, "work")

    await _adapter("work", "work_only")("work doc", L2_SCHEMA)
    assert [c["codex_home"] for c in fake_cli.calls()] == [work.config_dir]

    for scope, visibility in (("personal", "work_only"), ("unknown", "global")):
        refused = _adapter(scope, visibility)
        assert await refused.availability() == (False, "scope_capacity_unavailable")
        with pytest.raises(ScopeCapacityUnavailableError) as excinfo:
            await refused("must not leave the machine", L2_SCHEMA)
        assert excinfo.value.consumed is False
    assert len(fake_cli.calls()) == 1
    assert personal.config_dir not in [c["codex_home"] for c in fake_cli.calls()]


async def test_personal_document_never_uses_a_work_pool_account(fake_cli, tmp_path):
    """The app's personal mode lists work accounts too; L2 must still keep provenance strict."""
    work = await _account(tmp_path, "work")
    adapter = _adapter("personal", "global")

    # Only a work account exists: personal source text must not be sent anywhere.
    assert await adapter.availability() == (False, "scope_capacity_unavailable")
    with pytest.raises(ScopeCapacityUnavailableError) as excinfo:
        await adapter("personal text", L2_SCHEMA)
    assert excinfo.value.consumed is False
    assert fake_cli.calls() == []

    personal = await _account(tmp_path, "personal")
    await adapter("personal text", L2_SCHEMA)
    assert [c["codex_home"] for c in fake_cli.calls()] == [personal.config_dir]
    assert work.config_dir not in [c["codex_home"] for c in fake_cli.calls()]

    # And the reverse: a work document never reaches the personal account.
    await _adapter("work", "work_only")("work text", L2_SCHEMA)
    assert fake_cli.calls()[-1]["codex_home"] == work.config_dir


async def test_passage_body_travels_on_stdin_not_argv(fake_cli, tmp_path):
    await _account(tmp_path, "personal")

    await _adapter()(f"본문 {BODY_MARKER}", L2_SCHEMA)

    [call] = fake_cli.calls()
    assert call["prompt_on_stdin"] is True
    assert call["marker_in_stdin"] is True
    assert call["marker_in_argv"] is False


def test_prompt_is_in_argv_only_when_stdin_is_not_requested(tmp_path):
    from ttakkari.runs import desk

    codex = desk._codex_cmd(profiles.Profile(id=uuid.uuid4(), name="x", engine="codex"), {}, "sys", "BODY", tmp_path)
    assert codex[-2:] == ["--", "BODY"]
    codex_stdin = desk._codex_cmd(
        profiles.Profile(id=uuid.uuid4(), name="x", engine="codex"), {}, "sys", None, tmp_path
    )
    assert codex_stdin[-1] == "-" and "BODY" not in codex_stdin
    assert "BODY" in desk._claude_cmd({}, "sys", "BODY")
    claude_stdin = desk._claude_cmd({}, "sys", None)
    assert claude_stdin[:2] == [get_settings().claude_bin, "-p"] and claude_stdin[2] == "--output-format"


async def test_quota_response_cools_account_down_and_blocks_recall(fake_cli, tmp_path):
    await _account(tmp_path, "personal")
    adapter = _adapter()

    with pytest.raises(L2TransportError) as first:
        await adapter("MODE_QUOTA first", L2_SCHEMA)
    assert first.value.diagnosis == DIAG_USAGE_LIMIT
    assert first.value.retry_after is not None

    assert await adapter.availability() == (False, "scope_capacity_unavailable")
    with pytest.raises(ScopeCapacityUnavailableError):
        await adapter("second must not reach the CLI", L2_SCHEMA)
    assert len(fake_cli.calls()) == 1
    assert adapter.retry_after is not None


@pytest.mark.parametrize(
    ("marker", "diagnosis"),
    [("MODE_BAD_JSON", DIAG_INVALID_JSON), ("MODE_BAD_SCHEMA", DIAG_SCHEMA_VIOLATION)],
)
async def test_unusable_output_is_classified_and_charged(fake_cli, tmp_path, marker, diagnosis):
    await _account(tmp_path, "personal")
    with pytest.raises(L2TransportError) as excinfo:
        await _adapter()(marker, L2_SCHEMA)
    assert excinfo.value.diagnosis == diagnosis
    assert excinfo.value.consumed is True


async def test_timeout_kills_the_process_and_removes_the_temp_dir(fake_cli, tmp_path):
    profiles._in_use.clear()  # 앞선 시험이 남긴 전역 사용 표에 오염되지 않게, 이 시험이 만든 누수만 검사한다
    await _account(tmp_path, "personal")
    with pytest.raises(L2TransportError) as excinfo:
        await _adapter(timeout=1.0)("MODE_SLEEP", L2_SCHEMA)

    assert excinfo.value.diagnosis == DIAG_TIMEOUT
    pid = int((fake_cli.dir / "sleep.pid").read_text())
    await asyncio.sleep(0.2)
    assert not _pid_alive(pid)
    assert not Path(fake_cli.calls()[0]["cwd"]).exists()
    assert profiles._in_use == {}


async def test_cancel_kills_the_process_and_removes_the_temp_dir(fake_cli, tmp_path):
    profiles._in_use.clear()  # 앞선 시험이 남긴 전역 사용 표에 오염되지 않게, 이 시험이 만든 누수만 검사한다
    await _account(tmp_path, "personal")
    task = asyncio.create_task(_adapter(timeout=30.0)("MODE_SLEEP", L2_SCHEMA))
    for _ in range(100):
        if (fake_cli.dir / "sleep.pid").exists():
            break
        await asyncio.sleep(0.05)
    pid = int((fake_cli.dir / "sleep.pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.2)
    assert not _pid_alive(pid)
    assert not Path(fake_cli.calls()[0]["cwd"]).exists()
    assert profiles._in_use == {}


async def test_missing_executable_is_not_charged(fake_cli, tmp_path, monkeypatch):
    await _account(tmp_path, "personal")
    monkeypatch.setattr(get_settings(), "codex_bin", str(tmp_path / "no-such-codex"))
    with pytest.raises(L2TransportError) as excinfo:
        await _adapter()("anything", L2_SCHEMA)
    assert excinfo.value.diagnosis == DIAG_NONZERO_EXIT
    assert excinfo.value.consumed is False


async def test_two_concurrent_documents_reach_the_cli_once_at_budget_one(fake_cli, tmp_path, monkeypatch):
    """Real ExtractingProcessor + subscription adapter + fake CLI, both jobs past the legacy read."""
    settings = get_settings()
    monkeypatch.setattr(settings, "kb_l2_adapter", "subscription_cli")
    monkeypatch.setattr(settings, "kb_l2_daily_budget", 1)
    monkeypatch.setattr(settings, "kb_l2_max_calls_per_doc", 1)
    await _account(tmp_path, "personal")
    first = await _context("구독 CLI 예산 경합 문서 A. 고유 내용 L.")
    second = await _context("구독 CLI 예산 경합 문서 B. 고유 내용 M.")
    processor = ExtractingProcessor()
    barrier = asyncio.Barrier(2)
    read_calls_today = processor._l2_calls_today

    async def synchronized_read(ctx):
        value = await read_calls_today(ctx)
        await barrier.wait()
        return value

    monkeypatch.setattr(processor, "_l2_calls_today", synchronized_read)

    results = await asyncio.gather(processor(first), processor(second))

    assert len(fake_cli.calls()) == 1
    assert sum(r.detail["l2"]["calls_made"] for r in results) == 1
    assert await _ledger_counts() == {"consumed": 1}
