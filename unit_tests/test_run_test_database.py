from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

from sqlalchemy.engine import make_url

from ttakkari.runs.test_database import (
    RunTestDatabase,
    patch_asyncpg_test_port,
    reap_stale,
)


def test_start_creates_a_run_scoped_cluster_and_test_database(tmp_path, monkeypatch):
    pg_bin = tmp_path / "pg"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        (pg_bin / name).touch()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("ttakkari.runs.test_database.subprocess.run", run)
    db = RunTestDatabase("run-123", tmp_path / "workspace", pg_bin=pg_bin, root=tmp_path)
    url = db.start()

    parsed = make_url(url)
    assert parsed.host == "127.0.0.1"
    assert parsed.port is not None
    assert parsed.database == "ttakkari_test"
    assert parsed.username == "ttk_test" and parsed.password
    assert db.folder.parent == tmp_path
    assert db.folder.name.startswith("ttakkari-run-test-")
    assert any(str(command[0]).endswith("/initdb") for command in calls)
    assert any(command[0].endswith("/createdb") and command[-1] == "ttakkari_test" for command in calls)


def test_close_stops_and_removes_only_its_cluster(tmp_path, monkeypatch):
    pg_bin = tmp_path / "pg"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        (pg_bin / name).touch()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("ttakkari.runs.test_database.subprocess.run", run)
    db = RunTestDatabase("run-123", tmp_path / "workspace", pg_bin=pg_bin, root=tmp_path)
    db.start()
    owned = db.folder
    bystander = tmp_path / "not-owned"
    bystander.mkdir()

    db.close()

    assert not owned.exists()
    assert bystander.is_dir()


def test_explicit_local_test_database_port_is_used_by_direct_asyncpg_calls():
    observed = {}

    async def connect(*args, **kwargs):
        observed.update(kwargs)

    asyncpg = SimpleNamespace(connect=connect)
    assert patch_asyncpg_test_port(asyncpg, "postgresql+asyncpg://127.0.0.1:55555/ttakkari_test")
    asyncio.run(asyncpg.connect(host="localhost", database="ttakkari_test"))

    assert observed["port"] == 55555


def test_each_run_gets_a_distinct_database_url_and_no_application_database_url(tmp_path, monkeypatch):
    pg_bin = tmp_path / "pg"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        (pg_bin / name).touch()

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("ttakkari.runs.test_database.subprocess.run", run)
    first = RunTestDatabase("run-one", tmp_path / "ws-one", pg_bin=pg_bin, root=tmp_path)
    second = RunTestDatabase("run-two", tmp_path / "ws-two", pg_bin=pg_bin, root=tmp_path)

    first_url = first.start()
    second_url = second.start()

    assert first.folder != second.folder
    assert first_url != second_url
    assert make_url(first_url).database == make_url(second_url).database == "ttakkari_test"
    assert first.child_env()["TTAKKARI_TEST_DB"] == first_url
    assert second.child_env()["TTAKKARI_TEST_DB"] == second_url
    assert "TTAKKARI_DATABASE_URL" not in first.child_env()
    first.close()
    second.close()


def test_stale_reaper_removes_only_marked_clusters_with_dead_owners(tmp_path, monkeypatch):
    pg_bin = tmp_path / "pg"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        (pg_bin / name).touch()
    dead = tmp_path / "ttakkari-run-test-999999-abcdef12"
    dead.mkdir()
    (dead / "run-test-cluster.marker").write_text(
        f"prefix=ttakkari-run-test-\nfolder={dead.name}\nowner_pid=999999\nrun_id=old-run\nport=54321\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "ttakkari-run-test-888888-abcdef12"
    unrelated.mkdir()
    (unrelated / "run-test-cluster.marker").write_text("not our marker\n", encoding="utf-8")
    monkeypatch.setattr("ttakkari.runs.test_database._pg_bin", lambda: pg_bin)
    monkeypatch.setattr("ttakkari.runs.test_database._pid_alive", lambda pid: pid != 999999)

    assert reap_stale(tmp_path) == [dead.name]
    assert not dead.exists()
    assert unrelated.is_dir()


def test_stale_reaper_keeps_clusters_owned_by_a_live_generation(tmp_path, monkeypatch):
    pg_bin = tmp_path / "pg"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        (pg_bin / name).touch()
    live = tmp_path / "ttakkari-run-test-4242-abcdef12"
    live.mkdir()
    (live / "run-test-cluster.marker").write_text(
        f"prefix=ttakkari-run-test-\nfolder={live.name}\nowner_pid=4242\nrun_id=other-run\nport=54322\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ttakkari.runs.test_database._pg_bin", lambda: pg_bin)
    monkeypatch.setattr("ttakkari.runs.test_database._pid_alive", lambda pid: True)

    assert reap_stale(tmp_path) == []
    assert live.is_dir()


_IPCS = """IPC status from <running system> as of Sun Oct 25 20:58:23 KST 2026
T     ID     KEY        MODE       OWNER    GROUP  CREATOR   CGROUP NATTCH  SEGSZ  CPID  LPID   ATIME    DTIME    CTIME
Shared Memory:
m 101 0x00000001 --rw------- {me} staff {me} staff 0 56 111 0 no-entry no-entry 6:12:03
m 102 0x00000002 --rw------- {me} staff {me} staff 3 56 111 0 no-entry no-entry 6:12:03
m 103 0x00000003 --rw------- {me} staff {me} staff 0 4096 111 0 no-entry no-entry 6:12:03
m 104 0x00000004 --rw------- someone staff someone staff 0 56 111 0 no-entry no-entry 6:12:03
m 105 0x00000005 --rw------- {me} staff {me} staff 0 56 222 0 no-entry no-entry 6:12:03
"""


def test_sweep_removes_only_unattached_dead_owner_postgres_segments(monkeypatch):
    import os

    from ttakkari.runs import test_database as td

    removed = []

    def run(command, **kwargs):
        if command[1:3] == ["-m", "-a"]:
            return subprocess.CompletedProcess(command, 0, _IPCS.format(me=os.getuid()), "")
        removed.append(command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(td.subprocess, "run", run)
    monkeypatch.setattr(td.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(td.sys, "platform", "darwin")
    monkeypatch.setattr(td, "_pid_alive", lambda pid: pid == 222)

    assert td._sweep_orphan_shm() == 1
    assert removed == ["101"]


def test_start_sweeps_and_retries_once_when_initdb_hits_shm_limit(tmp_path, monkeypatch):
    from ttakkari.runs import test_database as td

    pg_bin = tmp_path / "pg"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        (pg_bin / name).touch()
    state = {"initdb": 0, "sweeps": 0}

    def run(command, **kwargs):
        if str(command[0]).endswith("/initdb"):
            state["initdb"] += 1
            assert (tmp_path / next(p.name for p in tmp_path.iterdir() if p.name.startswith(td.PREFIX))
                    / "initdb-password").exists()
            if state["initdb"] == 1:
                raise subprocess.CalledProcessError(
                    1, command, stderr="shmget(...) failed: No space left on device")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(td.subprocess, "run", run)
    monkeypatch.setattr(td, "_sweep_orphan_shm", lambda: state.__setitem__("sweeps", state["sweeps"] + 1) or 0)
    url = RunTestDatabase("run-shm", tmp_path / "workspace", pg_bin=pg_bin, root=tmp_path).start()

    assert url and state["initdb"] == 2 and state["sweeps"] == 2


def test_start_failure_message_carries_stderr(tmp_path, monkeypatch):
    import pytest

    from ttakkari.runs import test_database as td

    pg_bin = tmp_path / "pg"
    pg_bin.mkdir()
    for name in ("initdb", "pg_ctl", "createdb", "psql"):
        (pg_bin / name).touch()

    def run(command, **kwargs):
        if str(command[0]).endswith("/initdb"):
            raise subprocess.CalledProcessError(1, command, stderr="initdb: boom reason")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(td.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="boom reason"):
        RunTestDatabase("run-x", tmp_path / "w", pg_bin=pg_bin, root=tmp_path).start()
