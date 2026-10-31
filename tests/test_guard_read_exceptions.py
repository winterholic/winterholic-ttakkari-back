"""보호 영역(~/.ttakkari) 안의 읽기 전용 예외: 이전 Run 산출물·하네스 도구 출력은 읽게 하고, 쓰기·삭제는 계속 막는다."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ttakkari.security.guard import GuardConfig, evaluate

pytestmark = [pytest.mark.security]


@pytest.fixture
def world(tmp_path: Path):
    home = tmp_path / "ttk"
    old_outbox = home / "data" / "outbox" / "old-run"
    other_outbox = home / "data" / "outbox" / "stranger-run"
    own_outbox = home / "data" / "outbox" / "this-run"
    for d in (old_outbox, other_outbox, own_outbox):
        d.mkdir(parents=True)
    (old_outbox / "report.md").write_text("report")
    (other_outbox / "secret.md").write_text("not in my family")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = GuardConfig(write_roots=[workspace, own_outbox], protected=[home], read_roots=[old_outbox])
    return {"home": home, "old": old_outbox, "other": other_outbox, "own": own_outbox, "ws": workspace, "cfg": cfg}


def _ev(tool, tool_input, w):
    return evaluate(tool, tool_input, str(w["ws"]), w["cfg"])


def test_read_tool_can_read_a_family_runs_outbox(world):
    assert _ev("Read", {"file_path": str(world["old"] / "report.md")}, world) is None


def test_read_exception_does_not_cover_unrelated_outboxes_or_home(world):
    assert _ev("Read", {"file_path": str(world["other"] / "secret.md")}, world) is not None
    assert _ev("Read", {"file_path": str(world["home"] / "config.env")}, world) is not None


def test_write_tools_stay_blocked_in_read_only_roots(world):
    for tool in ("Write", "Edit", "MultiEdit"):
        assert _ev(tool, {"file_path": str(world["old"] / "report.md")}, world) is not None


@pytest.mark.parametrize("command", [
    "cat {old}/report.md",
    "head -n 5 {old}/report.md",
    "grep -rn report {old}",
    "ls -la {old}",
    "cp {old}/report.md ./copy.md",
    "cat {old}/report.md 2>&1 | head -n 20",  # 파일 기술자 복제는 쓰기가 아니다
    "cat {old}/report.md 2>/dev/null",
    "cat {old}/report.md 2> /dev/null",
    "cd {old}",
])
def test_bash_read_only_commands_may_read_the_family_outbox(world, command):
    assert _ev("Bash", {"command": command.format(old=world["old"])}, world) is None


@pytest.mark.parametrize("command", [
    "rm {old}/report.md",
    "cat x > {old}/report.md",
    "cat x >> {old}/report.md",
    "cat {old}/report.md > ./out.md",  # 작업 폴더로 복사하는 것도 리다이렉션이면 거부(cp 를 쓴다)
    "cat {old}/report.md 2> ./err.log",
    "cp ./a.md {old}/report.md",  # 목적지가 읽기 전용 영역
    "mv {old}/report.md ./x.md",
    "sed -i s/a/b/ {old}/report.md",
    "tee {old}/report.md",
    "cat {other}/secret.md",
])
def test_bash_everything_else_stays_blocked(world, command):
    assert _ev("Bash", {"command": command.format(old=world["old"], other=world["other"])}, world) is not None


def test_harness_tool_results_are_readable_via_claude_config_dir(world, monkeypatch, tmp_path):
    config_dir = world["home"] / "accounts" / "acct"
    result = config_dir / "projects" / "-proj" / str(uuid.uuid4()) / "tool-results" / "out.txt"
    result.parent.mkdir(parents=True)
    result.write_text("big output")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TTAKKARI_GUARD_PROTECTED", str(world["home"]))
    monkeypatch.setenv("TTAKKARI_GUARD_WRITE_ROOTS", str(world["ws"]))
    cfg = GuardConfig.from_env()
    assert evaluate("Read", {"file_path": str(result)}, str(world["ws"]), cfg) is None
    # 같은 계정 디렉터리의 인증·설정 파일과 쓰기는 그대로 막는다.
    assert evaluate("Read", {"file_path": str(config_dir / "auth.json")}, str(world["ws"]), cfg) is not None
    assert evaluate("Write", {"file_path": str(result)}, str(world["ws"]), cfg) is not None
    assert evaluate("Bash", {"command": f"cat {result}"}, str(world["ws"]), cfg) is None


def test_read_roots_file_is_loaded_and_missing_file_fails_closed(world, monkeypatch, tmp_path):
    listing = tmp_path / "roots.txt"
    listing.write_text(str(world["old"]) + "\n\n")
    monkeypatch.setenv("TTAKKARI_GUARD_PROTECTED", str(world["home"]))
    monkeypatch.setenv("TTAKKARI_GUARD_WRITE_ROOTS", str(world["ws"]))
    monkeypatch.setenv("TTAKKARI_GUARD_READ_ROOTS_FILE", str(listing))
    cfg = GuardConfig.from_env()
    assert evaluate("Read", {"file_path": str(world["old"] / "report.md")}, str(world["ws"]), cfg) is None
    monkeypatch.setenv("TTAKKARI_GUARD_READ_ROOTS_FILE", str(tmp_path / "missing.txt"))
    cfg = GuardConfig.from_env()
    assert evaluate("Read", {"file_path": str(world["old"] / "report.md")}, str(world["ws"]), cfg) is not None


def test_sensitive_files_inside_a_read_root_are_still_blocked(world):
    (world["old"] / ".env").write_text("KEY=1")
    assert _ev("Read", {"file_path": str(world["old"] / ".env")}, world) is not None


async def test_read_roots_list_covers_only_the_same_pool_family(client, auth, make_dir, tmp_path):
    from sqlalchemy import update
    from test_agent_stop_all import create_run, create_session

    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, Run
    from ttakkari.runs.manager import _write_guard_read_roots

    home = tmp_path / "ttkhome"
    outbox_root = home / "data" / "outbox"
    outbox_root.mkdir(parents=True)

    lead, _ = await create_session(client, auth, make_dir)
    worker, _ = await create_session(client, auth, make_dir, kind="worker", parent=lead["id"], workspace=lead["workspace_id"])
    sibling, _ = await create_session(client, auth, make_dir, kind="worker", parent=lead["id"], workspace=lead["workspace_id"])
    stranger, _ = await create_session(client, auth, make_dir)  # 다른 기획 계열
    other_pool, _ = await create_session(client, auth, make_dir, kind="worker", parent=lead["id"], workspace=lead["workspace_id"])

    async def run_with_outbox(sess, name):
        run = await create_run(sess, status="succeeded")
        path = outbox_root / name
        path.mkdir()
        async with sessionmaker()() as db:
            await db.execute(update(Run).where(Run.id == run.id).values(outbox_path=str(path)))
            await db.commit()
        return run

    await run_with_outbox(lead, "lead-run")
    await run_with_outbox(sibling, "sibling-run")
    await run_with_outbox(stranger, "stranger-run")
    await run_with_outbox(other_pool, "other-pool-run")
    mine = await create_run(worker, status="running")
    async with sessionmaker()() as db:
        await db.execute(update(ChatSession).where(ChatSession.id == uuid.UUID(other_pool["id"])).values(pool="work"))
        escape = await create_run(worker, status="succeeded")
        await db.execute(update(Run).where(Run.id == escape.id).values(outbox_path=str(tmp_path / "elsewhere")))
        await db.commit()
        run = await db.get(Run, mine.id)
        sess = await db.get(ChatSession, uuid.UUID(worker["id"]))
        target = await _write_guard_read_roots(db, run, sess, home)

    assert target is not None and target.parent == home / "guard-read-roots"
    listed = {Path(x).name for x in target.read_text().splitlines()}
    assert listed == {"lead-run", "sibling-run"}  # 같은 계열만. 낯선 기획·다른 pool·outbox 밖 경로는 빠진다


def test_muse_persisted_tool_outputs_are_readable_but_not_writable(world, monkeypatch):
    out = world["home"] / "accounts" / "acct" / "data" / "muse" / "sessions" / "2026" / "10" / "27" / "sid" / "tool-outputs" / "u1" / "call_1.txt"
    out.parent.mkdir(parents=True)
    out.write_text("big")
    other = world["home"] / "accounts" / "acct" / "data" / "muse" / "config.json"
    other.write_text("{}")
    monkeypatch.setenv("TTAKKARI_GUARD_PROTECTED", str(world["home"]))
    monkeypatch.setenv("TTAKKARI_GUARD_WRITE_ROOTS", str(world["ws"]))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cfg = GuardConfig.from_env()
    ws = str(world["ws"])
    assert evaluate("Read", {"file_path": str(out)}, ws, cfg) is None
    assert evaluate("Bash", {"command": f'cat "{out}"'}, ws, cfg) is None
    assert evaluate("Write", {"file_path": str(out)}, ws, cfg) is not None
    assert evaluate("Bash", {"command": f'rm "{out}"'}, ws, cfg) is not None
    assert evaluate("Read", {"file_path": str(other)}, ws, cfg) is not None  # 같은 계정의 다른 파일은 그대로 막는다
