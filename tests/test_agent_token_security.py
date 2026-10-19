"""SEC2-AGT-01·AGT-02 재현 테스트. 가짜 토큰만 쓴다(실제 비밀 금지).

AGT-01: 4종 어댑터의 build_command 결과(argv = ps 노출면)에 토큰 값이 없어야 한다.
수정 전에는 Claude·Codex 가 토큰을 argv 에 그대로 넣어 실패한다.
AGT-02: 종료된 Run 에 묶인 토큰은 API 호출 시 401 이어야 한다.
수정 전에는 Run 종료 후에도 토큰이 만료 때까지 유효해 실패한다.
"""
from __future__ import annotations

import json
import stat
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest

from ttakkari.agents.antigravity import AntigravityAdapter
from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.agents.codex import CodexAdapter
from ttakkari.agents.muse import MuseAdapter
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, Workspace
from ttakkari.orchestrator.runtime import issue_token, mcp_server_spec

FAKE_TOKEN_ENV = {"TTK_ORCH_TOKEN": "FAKE-TOKEN-f8e2 TEST", "TTK_API_URL": "http://127.0.0.1:8787",
                  "TTK_ROLE": "agent"}


def _spec(root: Path, **kw) -> LaunchSpec:
    base = {"cwd": root, "prompt": "hello", "engine_session_id": None, "model": None, "effort": None,
            "outbox": root, "write_roots": [root], "system_note": "note",
            "mcp_servers": {"ttakkari": {"command": "python", "args": ["-m", "ttakkari.orchestrator.mcp_server"],
                                         "env": dict(FAKE_TOKEN_ENV)}}}
    base.update(kw)
    return LaunchSpec(**base)


def _ps(argv: list[str]) -> str:
    """ps -axww 가 보여주는 형태(공백 연결). 토큰 값이 여기 보이면 노출이다."""
    return " ".join(argv)


# ---- AGT-01: Claude ----

def test_claude_argv_hides_token_value(tmp_path):
    token = issue_token(uuid.uuid4())
    spec = _spec(tmp_path, mcp_servers={"ttakkari": {"command": "python", "args": ["-m", "mcp"],
                                                    "env": {"TTK_ORCH_TOKEN": token}}})
    adapter = ClaudeAdapter()
    try:
        argv = adapter.build_command(spec, {})
        assert token not in _ps(argv)
        idx = argv.index("--mcp-config")
        cfg = json.loads(Path(argv[idx + 1]).read_text(encoding="utf-8"))
        assert cfg["mcpServers"]["ttakkari"]["env"]["TTK_ORCH_TOKEN"] == token
    finally:
        adapter.cleanup(spec)


def test_claude_mcp_config_file_is_private_and_removed(tmp_path):
    spec = _spec(tmp_path)
    adapter = ClaudeAdapter()
    argv = adapter.build_command(spec, {})
    path = Path(argv[argv.index("--mcp-config") + 1])
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    adapter.cleanup(spec)
    assert not path.exists() and not path.parent.exists()


# ---- AGT-01: Codex ----

def test_codex_argv_hides_token_value(tmp_path):
    token = issue_token(uuid.uuid4())
    spec = _spec(tmp_path, mcp_servers={"ttakkari": {"command": "python", "args": ["-m", "mcp"],
                                                    "env": {"TTK_ORCH_TOKEN": token,
                                                            "TTK_API_URL": "http://127.0.0.1:8787",
                                                            "TTK_ROLE": "agent"}}})
    env = {"CODEX_HOME": str(tmp_path / "codex-home"), "TTAKKARI_RUN_ID": "red-test-run"}
    adapter = CodexAdapter()
    try:
        argv = adapter.build_command(spec, env)
        assert token not in _ps(argv)
        assert not any("TTK_ORCH_TOKEN" in a for a in argv)
        assert "-p" in argv
        profile = Path(env["CODEX_HOME"]) / f"{argv[argv.index('-p') + 1]}.config.toml"
        assert profile.is_file()
        assert stat.S_IMODE(profile.stat().st_mode) == 0o600
        cfg = tomllib.loads(profile.read_text(encoding="utf-8"))
        assert cfg["mcp_servers"]["ttakkari"]["env"]["TTK_ORCH_TOKEN"] == token
    finally:
        adapter.cleanup(spec)


def test_codex_profile_removed_on_cleanup(tmp_path):
    spec = _spec(tmp_path)
    env = {"CODEX_HOME": str(tmp_path / "codex-home"), "TTAKKARI_RUN_ID": "cleanup-run"}
    adapter = CodexAdapter()
    argv = adapter.build_command(spec, env)
    profile = Path(env["CODEX_HOME"]) / f"{argv[argv.index('-p') + 1]}.config.toml"
    assert profile.is_file()
    adapter.cleanup(spec)
    assert not profile.exists()


# ---- AGT-01: Antigravity (기존 파일 방식 확인) ----

def test_antigravity_argv_hides_token_value(tmp_path):
    token = issue_token(uuid.uuid4())
    sid = str(uuid.uuid4())
    spec = _spec(tmp_path, session_id=sid,
                 mcp_servers={"ttakkari": {"command": "python", "args": ["-m", "mcp"],
                                           "env": {"TTK_ORCH_TOKEN": token}}})
    adapter = AntigravityAdapter()
    try:
        argv = adapter.build_command(spec, {"TTAKKARI_RUN_ID": "agy-test"})
        assert token not in _ps(argv)
        assert not any("TTK_ORCH_TOKEN" in a for a in argv)
    finally:
        adapter.cleanup(spec)


# ---- AGT-01: Muse (기존 파일 방식 확인) ----

def test_muse_argv_hides_token_value(tmp_path):
    token = issue_token(uuid.uuid4())
    spec = _spec(tmp_path, mcp_servers={"ttakkari": {"command": "python", "args": ["-m", "mcp"],
                                                    "env": {"TTK_ORCH_TOKEN": token}}})
    adapter = MuseAdapter()
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    try:
        argv = adapter.build_command(spec, env)
        assert token not in _ps(argv)
        assert not any("TTK_ORCH_TOKEN" in a for a in argv)
        spec_path = Path(argv[-1])
        assert spec_path.is_file()
        assert stat.S_IMODE(spec_path.stat().st_mode) == 0o600
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        assert data["mcp_servers"]["ttakkari"]["env"]["TTK_ORCH_TOKEN"] == token
    finally:
        adapter.cleanup(spec)


# ---- AGT-01 대조군: 파일로 전달된 토큰으로 MCP 와 같은 bearer 호출이 성공한다 ----

@pytest.mark.asyncio
async def test_delivered_token_authenticates_like_mcp_server(client, tmp_path, session_and_run):
    # /api/agent/* 는 토큰의 세션 행이 DB 에 있어야 하므로 임의 UUID 대신 실제 세션을 쓴다.
    sess, _ = session_and_run
    token = issue_token(sess.id)
    spec = _spec(tmp_path, mcp_servers={"ttakkari": {"command": "python", "args": ["-m", "mcp"],
                                                    "env": {"TTK_ORCH_TOKEN": token}}})
    adapter = ClaudeAdapter()
    try:
        argv = adapter.build_command(spec, {})
        delivered = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())[
            "mcpServers"]["ttakkari"]["env"]["TTK_ORCH_TOKEN"]
        assert delivered == token
        resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {delivered}"})
        assert resp.status_code == 200
    finally:
        adapter.cleanup(spec)


# ---- AGT-02 ----

@pytest.fixture
async def session_and_run(make_dir):
    root = make_dir()
    async with sessionmaker()() as db:
        ws = Workspace(id=uuid.uuid4(), name=root.name, root_path=str(root))
        db.add(ws)
        await db.flush()
        sess = ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title="SEC2 run token", engine="claude",
                           pool="personal", created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        db.add(sess)
        await db.flush()
        run = Run(session_id=sess.id, workspace_id=ws.id, prompt="fixture", engine="claude",
                  status="running")
        db.add(run)
        await db.commit()
        return sess, run


@pytest.mark.asyncio
async def test_run_bound_token_works_while_running(client, session_and_run):
    sess, run = session_and_run
    token = issue_token(sess.id, run.id)
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
async def test_run_bound_token_rejected_after_finish(client, session_and_run, terminal):
    sess, run = session_and_run
    token = issue_token(sess.id, run.id)
    async with sessionmaker()() as db:
        obj = await db.get(Run, run.id)
        obj.status = terminal
        await db.commit()
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_run_bound_token_rejected_for_unknown_run(client, session_and_run):
    sess, _ = session_and_run
    token = issue_token(sess.id, uuid.uuid4())
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_legacy_token_without_run_still_accepted(client, session_and_run):
    """rid 없는 기존 토큰은 인정한다(기존 테스트·세대 전환 draining 호환)."""
    sess, _ = session_and_run
    token = issue_token(sess.id)
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_mcp_server_spec_binds_run_id():
    sid, rid = uuid.uuid4(), uuid.uuid4()
    spec = mcp_server_spec(sid, "agent", run_id=rid)
    legacy = mcp_server_spec(sid, "agent")["env"]["TTK_ORCH_TOKEN"]
    assert "rid" not in jwt.decode(legacy, options={"verify_signature": False})
    assert jwt.decode(spec["env"]["TTK_ORCH_TOKEN"], options={"verify_signature": False})["rid"] == str(rid)
