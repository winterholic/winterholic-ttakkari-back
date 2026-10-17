from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import uuid
from pathlib import Path

from ttakkari.db import sessionmaker
from ttakkari.deploy_gate import RestartGate, _active_run_count
from ttakkari.models import Run

_HOLDER = (
    "import fcntl, sys\n"
    "f = open(sys.argv[1], 'a')\n"
    "fcntl.flock(f, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


@contextlib.contextmanager
def lock_held_by_other_process(path: Path):
    """다른 프로세스가 flock을 쥔 상황. 같은 프로세스의 중첩 획득은 재진입이라 중복 배포를 대표하지 못한다."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        yield
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_existing_restart_lock_needs_no_file_write(tmp_path, monkeypatch):
    path = tmp_path / "restart.lock"
    path.write_text("")
    original = Path.open

    def read_only_lock(self, mode="r", *args, **kwargs):
        if self == path and any(flag in mode for flag in "wa+"):
            raise PermissionError("existing deploy lock is read-only")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_only_lock)
    gate = RestartGate(path)
    first = gate._acquire()
    assert first is not None
    gate._release(first)


def test_restart_lock_blocks_acquire_from_another_process(tmp_path):
    path = tmp_path / "restart.lock"
    gate = RestartGate(path)
    with lock_held_by_other_process(path):
        assert gate._acquire() is None  # 동일 파일의 중복 배포 차단은 프로세스 간에 유지한다.
    again = gate._acquire()
    assert again is not None
    gate._release(again)


def test_restart_lock_is_reentrant_only_inside_the_holding_process(tmp_path):
    path = tmp_path / "restart.lock"
    outer = RestartGate(path)._acquire()
    assert outer is not None
    try:
        inner = RestartGate(path)._acquire()
        assert inner is not None  # 감독 루프 -> process() 중첩 획득(b4ffee9의 의도).
        RestartGate._release(inner)
        assert RestartGate._HELD[str(path)] == 1
    finally:
        RestartGate._release(outer)
    assert str(path) not in RestartGate._HELD


def test_gate_rechecks_after_wait_and_excludes_deploying_run(tmp_path):
    counts = iter([0, 1, 0, 0])
    restarts: list[str] = []

    gate = RestartGate(tmp_path / "restart.lock", poll_seconds=0, max_wait_seconds=1)
    asyncio.run(gate.restart_backend(lambda: next(counts), lambda: restarts.append("restart"), own_run_id="self"))

    assert restarts == ["restart"]


def test_gate_skips_duplicate_restart_when_lock_is_held(tmp_path):
    path = tmp_path / "restart.lock"
    gate = RestartGate(path, poll_seconds=0, max_wait_seconds=1)
    called: list[bool] = []
    with lock_held_by_other_process(path):
        result = asyncio.run(gate.restart_backend(lambda: 0, lambda: called.append(True)))

    assert result is False
    assert called == []


def test_frontend_deploy_is_not_gated(tmp_path):
    gate = RestartGate(tmp_path / "restart.lock")
    called: list[bool] = []

    gate.deploy_frontend(lambda: called.append(True))

    assert called == [True]


def test_active_runs_timeout_without_restarting(tmp_path):
    gate = RestartGate(tmp_path / "restart.lock", poll_seconds=0, max_wait_seconds=0.01)
    called: list[bool] = []

    result = asyncio.run(gate.restart_backend(lambda: 1, lambda: called.append(True)))

    assert result is False
    assert called == []


async def test_deploy_run_is_excluded_but_other_runs_still_block(client, auth, new_session):
    ws, sess = await new_session()
    own_id, other_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        db.add_all([
            Run(id=own_id, session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                prompt="deploy", engine="claude", status="running"),
            Run(id=other_id, session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                prompt="other", engine="claude", status="running"),
            Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                prompt="queued", engine="claude", status="queued"),
        ])
        await db.commit()

    assert await _active_run_count(str(own_id)) == 1
    assert await _active_run_count(None) == 2
