"""Worker-originated one-level delegation: pure rules and role registration.

Workers with MCP tools may spawn their own sub-workers one level deep; grandchildren
cannot spawn again, and every caller only touches its own subtree (orchestrators see
one extra hop). API ownership is covered gate-side in tests/test_orchestrator.py.
"""
import os
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ttakkari.runs import worker_routing
from ttakkari.security.muse_hook import decision

WORKER_MANAGEMENT_TOOLS = {"spawn_worker", "message_worker", "list_workers", "get_worker", "stop_worker"}
ORCHESTRATOR_ONLY_TOOLS = {"manage_work", "account_usage", "stop_agents", "my_context", "reset_my_context",
                            "start_luna_common30_v2", "luna_common30_v2_status", "luna_common30_v2_cancel"}


class TestCanSpawn:
    def test_orchestrator_always_may_spawn(self):
        assert worker_routing.can_spawn("orchestrator", None) is True
        assert worker_routing.can_spawn("orchestrator", "orchestrator") is True

    def test_worker_may_spawn_only_under_orchestrator(self):
        assert worker_routing.can_spawn("worker", "orchestrator") is True
        assert worker_routing.can_spawn("worker", "worker") is False
        assert worker_routing.can_spawn("worker", None) is False
        assert worker_routing.can_spawn("worker", "chat") is False

    def test_chat_and_desk_never_spawn(self):
        assert worker_routing.can_spawn("chat", None) is False
        assert worker_routing.can_spawn("desk", None) is False
        assert worker_routing.can_spawn("", None) is False


class TestOwnsWorkerTarget:
    def test_direct_child_visible_to_any_spawner_kind(self):
        caller, target_parent = uuid.uuid4(), uuid.uuid4()
        assert worker_routing.owns_worker_target(caller, "worker", caller, None) is True
        assert worker_routing.owns_worker_target(caller, "orchestrator", caller, None) is True
        assert worker_routing.owns_worker_target(caller, "worker", target_parent, None) is False

    def test_grandchild_visible_only_to_orchestrator(self):
        caller, parent = uuid.uuid4(), uuid.uuid4()
        assert worker_routing.owns_worker_target(caller, "orchestrator", parent, caller) is True
        assert worker_routing.owns_worker_target(caller, "worker", parent, caller) is False

    def test_orphan_and_stranger_rejected(self):
        caller = uuid.uuid4()
        assert worker_routing.owns_worker_target(caller, "orchestrator", None, None) is False
        assert worker_routing.owns_worker_target(caller, "orchestrator", uuid.uuid4(), uuid.uuid4()) is False


def test_worker_spawn_cap_is_smaller_than_orchestrator_cap():
    from ttakkari.api.orchestrator import MAX_ACTIVE_WORKERS

    assert 0 < worker_routing.WORKER_SPAWN_MAX_ACTIVE < MAX_ACTIVE_WORKERS


def _stdio_params(role: str | None):
    env = dict(os.environ)
    if role is None:
        env.pop("TTK_ROLE", None)
    else:
        env["TTK_ROLE"] = role
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "ttakkari.orchestrator.mcp_server"], env=env)


async def _tool_map(role: str | None):
    params = _stdio_params(role)
    async with (stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client):
        await client.initialize()
        result = await client.list_tools()
        return {t.name: t for t in result.tools}


async def test_agent_role_registers_worker_management_tools():
    tools = await _tool_map("agent")
    assert WORKER_MANAGEMENT_TOOLS <= set(tools)
    assert set(tools) & ORCHESTRATOR_ONLY_TOOLS == set()
    assert tools["list_workers"].annotations.read_only_hint is True
    assert tools["get_worker"].annotations.read_only_hint is True
    for name in ("spawn_worker", "message_worker", "stop_worker"):
        annotations = tools[name].annotations
        assert not (annotations and annotations.read_only_hint)


async def test_orchestrator_role_keeps_full_tool_set():
    tools = await _tool_map(None)
    assert WORKER_MANAGEMENT_TOOLS | ORCHESTRATOR_ONLY_TOOLS <= set(tools)


def _policy(role):
    return {"write_roots": ["/tmp/ttk-allowed"], "protected": ["/tmp/ttk-home"],
            "private_root": "/tmp/ttk-private", "cwd": "/tmp/ttk-allowed",
            "mcp_servers": ["ttakkari"], "role": role}


def test_muse_hook_allows_known_roles_to_delegate():
    spawn = {"tool_name": "mcp__ttakkari__spawn_worker", "tool_input": {"title": "t"}}
    msg = {"tool_name": "mcp__ttakkari__message_worker", "tool_input": {}}
    for role in ("orchestrator", "agent"):
        assert decision(spawn, _policy(role)) is None
        assert decision(msg, _policy(role)) is None


def test_muse_hook_still_denies_unknown_role_delegation():
    spawn = {"tool_name": "mcp__ttakkari__spawn_worker", "tool_input": {"title": "t"}}
    msg = {"tool_name": "mcp__ttakkari__message_worker", "tool_input": {}}
    for role in ("", None, "desk"):
        assert decision(spawn, _policy(role)) is not None
        assert decision(msg, _policy(role)) is not None
