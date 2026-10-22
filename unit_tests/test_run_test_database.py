from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

from sqlalchemy.engine import make_url

from ttakkari.runs.test_database import RunTestDatabase, patch_asyncpg_test_port


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
