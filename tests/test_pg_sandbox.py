"""tests/test_pg_sandbox.py

pg_sandbox 의 격리, reap_stale 안전성, asyncpg 패치/복원, signal handler 정리, IPv6 주입 단위 테스트.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pg_sandbox
import pytest


def make_cluster_folder(
    root: Path,
    pid: int,
    hex_suffix: str,
    *,
    is_symlink: bool = False,
    target: Path | None = None,
) -> Path:
    """_NAME_RE 규격을 엄격히 검증하며 테스트 클러스터 폴더(또는 symlink)를 생성한다."""
    folder_name = f"{pg_sandbox.PREFIX}{pid}-{hex_suffix}"
    assert pg_sandbox._NAME_RE.fullmatch(folder_name) is not None, (
        f"Test helper: invalid cluster folder name format '{folder_name}'"
    )
    folder = root / folder_name
    if is_symlink:
        assert target is not None, "target must be provided when is_symlink=True"
        folder.symlink_to(target)
    else:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(autouse=True)
def clean_sandbox_state():
    orig_cluster = pg_sandbox._cluster
    pg_sandbox._cluster = None
    if hasattr(pg_sandbox, "unpatch_asyncpg"):
        pg_sandbox.unpatch_asyncpg()
    elif pg_sandbox._asyncpg_orig_connect is not None:
        try:
            import asyncpg

            asyncpg.connect = pg_sandbox._asyncpg_orig_connect
        except ImportError:
            pass
        pg_sandbox._asyncpg_orig_connect = None

    orig_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)
    }
    yield
    pg_sandbox._cluster = orig_cluster
    if hasattr(pg_sandbox, "unpatch_asyncpg"):
        pg_sandbox.unpatch_asyncpg()
    elif pg_sandbox._asyncpg_orig_connect is not None:
        try:
            import asyncpg

            asyncpg.connect = pg_sandbox._asyncpg_orig_connect
        except ImportError:
            pass
        pg_sandbox._asyncpg_orig_connect = None

    for sig, h in orig_handlers.items():
        try:
            signal.signal(sig, h)
        except (OSError, ValueError):
            pass


def test_reap_stale_cleans_dead_owner_with_valid_marker(tmp_path, monkeypatch):
    """죽은 소유자 PID + 유효한 마커가 있는 정규 stale 폴더는 정상 정리된다."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)
    fake_pg_bin = tmp_path / "bin"
    fake_pg_bin.mkdir()
    monkeypatch.setattr(pg_sandbox, "_pg_bin", lambda: fake_pg_bin)

    dead_pid = 99999999
    folder = make_cluster_folder(tmp_path, dead_pid, "aabbccdd")
    (folder / "data").mkdir()

    if hasattr(pg_sandbox, "_write_marker"):
        pg_sandbox._write_marker(folder, dead_pid, 55555)

    reaped = pg_sandbox.reap_stale()
    assert folder.name in reaped
    assert not folder.exists()


def test_reap_stale_protects_live_owner(tmp_path, monkeypatch):
    """현재 살아있는 프로세스(os.getpid())의 폴더는 절대 정리되지 않는다."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)
    fake_pg_bin = tmp_path / "bin"
    fake_pg_bin.mkdir()
    monkeypatch.setattr(pg_sandbox, "_pg_bin", lambda: fake_pg_bin)

    checked_pids: list[int] = []
    orig_pid_alive = pg_sandbox._pid_alive

    def spy_pid_alive(pid: int) -> bool:
        checked_pids.append(pid)
        return orig_pid_alive(pid)

    monkeypatch.setattr(pg_sandbox, "_pid_alive", spy_pid_alive)

    live_pid = os.getpid()
    folder = make_cluster_folder(tmp_path, live_pid, "11223344")
    (folder / "data").mkdir()
    if hasattr(pg_sandbox, "_write_marker"):
        pg_sandbox._write_marker(folder, live_pid, 55555)

    reaped = pg_sandbox.reap_stale()
    # 이름 필터를 통과하고 _pid_alive 분기까지 도달했음을 계측 검증
    assert live_pid in checked_pids
    assert folder.name not in reaped
    assert folder.exists()


def test_reap_stale_protects_invalid_marker(tmp_path, monkeypatch):
    """마커가 없거나 유효하지 않은 폴더는 유효한 16진 폴더명이어도 정리되지 않는다."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)
    fake_pg_bin = tmp_path / "bin"
    fake_pg_bin.mkdir()
    monkeypatch.setattr(pg_sandbox, "_pg_bin", lambda: fake_pg_bin)

    validated_folders: list[Path] = []
    orig_is_valid = pg_sandbox._is_valid_cluster_folder

    def spy_is_valid(f: Path) -> bool:
        validated_folders.append(f.resolve())
        return orig_is_valid(f)

    monkeypatch.setattr(pg_sandbox, "_is_valid_cluster_folder", spy_is_valid)

    dead_pid = 99999998
    # 유효한 16진 접미사 baad0001
    folder = make_cluster_folder(tmp_path, dead_pid, "baad0001")
    (folder / "data").mkdir()

    marker_file = folder / getattr(pg_sandbox, "MARKER_FILE", "cluster.marker")
    marker_file.write_text("prefix=wrong\npid=123\n")

    # 1. 폴더명이 _NAME_RE 조건을 완벽히 충족함을 명시 입증
    assert pg_sandbox._NAME_RE.fullmatch(folder.name) is not None
    # 2. 마커 파일 내용 손상으로 _is_valid_cluster_folder 가 False 임을 명시 입증
    assert pg_sandbox._is_valid_cluster_folder(folder) is False

    reaped = pg_sandbox.reap_stale()

    # 3. 이름 필터를 통과하여 마커 검증 분기까지 실제로 도달했음을 계측 검증
    assert folder.resolve() in validated_folders
    assert folder.name not in reaped
    assert folder.exists()


def test_reap_stale_protects_top_level_symlink(tmp_path, monkeypatch):
    """상위 디렉터리 수준 심볼릭 링크는 무시되고 대상 디렉터리가 보존된다."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)
    fake_pg_bin = tmp_path / "bin"
    fake_pg_bin.mkdir()
    monkeypatch.setattr(pg_sandbox, "_pg_bin", lambda: fake_pg_bin)

    stopped_folders: list[Path] = []
    orig_stop = pg_sandbox._stop_and_remove

    def spy_stop(f: Path, b: Path) -> None:
        stopped_folders.append(f.resolve())
        orig_stop(f, b)

    monkeypatch.setattr(pg_sandbox, "_stop_and_remove", spy_stop)

    outside = tmp_path / "outside_target"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("safe")
    dead_pid = 99999997
    # 대상에 유효한 마커가 있더라도 상위 symlink 방어로 인해 절대 정리되지 않아야 함
    pg_sandbox._write_marker(outside, dead_pid, 55555)
    # 유효한 16진 접미사 baad0002
    link_folder = make_cluster_folder(
        tmp_path, dead_pid, "baad0002", is_symlink=True, target=outside
    )

    # 1. 심볼릭 링크 자체의 이름이 _NAME_RE 에 완전 일치함을 입증
    assert pg_sandbox._NAME_RE.fullmatch(link_folder.name) is not None
    assert link_folder.is_symlink()

    reaped = pg_sandbox.reap_stale()

    # 2. _stop_and_remove 에 도달하지 않고 top-level symlink 방어 분기에서 차단되었음을 검증
    assert link_folder.resolve() not in stopped_folders
    assert link_folder.name not in reaped
    # 3. 외부 대상 디렉터리 및 파일 보존 (외부 영향 0)
    assert outside.exists()
    assert (outside / "sentinel.txt").read_text() == "safe"


def test_stop_and_remove_does_not_call_pg_ctl_on_nested_data_symlink(
    tmp_path, monkeypatch
):
    """죽은 owner stale 폴더 안 data 가 외부 디렉터리를 가리키는 symlink 인 경우 pg_ctl stop 을 시도하지 않는다."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)

    pg_ctl_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if "pg_ctl" in cmd[0]:
            pg_ctl_calls.append(cmd)
        m = MagicMock()
        m.stdout = ""
        m.stderr = ""
        m.returncode = 0
        return m

    monkeypatch.setattr(pg_sandbox.subprocess, "run", fake_run)

    external_data = tmp_path / "external_pg_data"
    external_data.mkdir()
    (external_data / "postmaster.pid").write_text("12345\n")
    sentinel = external_data / "sentinel.txt"
    sentinel.write_text("important data")

    dead_pid = 99999996
    # 유효한 16진 접미사 deadbeef
    folder = make_cluster_folder(tmp_path, dead_pid, "deadbeef")
    (folder / "data").symlink_to(external_data)
    if hasattr(pg_sandbox, "_write_marker"):
        pg_sandbox._write_marker(folder, dead_pid, 55555)

    # 1. 정규식 매칭 및 마커 유효성 모두 통과함을 입증 (이름 필터 탈락이 아님을 증명)
    assert pg_sandbox._NAME_RE.fullmatch(folder.name) is not None
    assert pg_sandbox._is_valid_cluster_folder(folder) is True

    fake_pg_bin = tmp_path / "bin"
    fake_pg_bin.mkdir()

    pg_sandbox._stop_and_remove(folder, fake_pg_bin)

    # 2. data.is_symlink() 보호 분기 도달 검증: 외부 pg_ctl 은 0회 호출
    assert len(pg_ctl_calls) == 0, (
        f"pg_ctl stop was called on nested symlink: {pg_ctl_calls}"
    )
    # 3. 외부 디렉터리 및 데이터 파일 절대 보존 (외부 영향 0)
    assert external_data.exists()
    assert (external_data / "postmaster.pid").exists()
    assert sentinel.exists()
    assert sentinel.read_text() == "important data"
    # 4. 안전하게 소유 클러스터 폴더만 삭제됨
    assert not folder.exists()


def test_zero_external_impact_on_mixed_stale_run(tmp_path, monkeypatch):
    """여러 종류(정상 stale, live, invalid marker, data symlink, top symlink)가 섞인 환경에서
    오직 정규 dead-owner 만 정리되고 외부 디렉터리 및 pg_ctl 은 안전하게 차단된다."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)

    pg_ctl_stopped_dirs: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        if "pg_ctl" in cmd[0] and "stop" in cmd:
            idx = cmd.index("-D")
            stopped_path = Path(cmd[idx + 1])
            # 호출 시점에 resolve 하여 symlink 대상 디렉터리를 포착
            resolved = str(stopped_path.resolve())
            pg_ctl_stopped_dirs.append(resolved)
        m = MagicMock()
        m.stdout = ""
        m.stderr = ""
        m.returncode = 0
        return m

    monkeypatch.setattr(pg_sandbox.subprocess, "run", fake_run)

    fake_pg_bin = tmp_path / "bin"
    fake_pg_bin.mkdir()
    monkeypatch.setattr(pg_sandbox, "_pg_bin", lambda: fake_pg_bin)

    external_db = tmp_path / "prod_db"
    external_db.mkdir()
    (external_db / "postmaster.pid").write_text("11111\n")
    (external_db / "sentinel.txt").write_text("production")

    # 1. 정규 dead-owner 클러스터 (유효 hex: dead0001)
    dead1 = make_cluster_folder(tmp_path, 99999991, "dead0001")
    (dead1 / "data").mkdir()
    (dead1 / "data" / "postmaster.pid").write_text("22222\n")
    pg_sandbox._write_marker(dead1, 99999991, 54391)

    # 2. live-owner 클러스터 (유효 hex: 110e0001)
    live1 = make_cluster_folder(tmp_path, os.getpid(), "110e0001")
    (live1 / "data").mkdir()
    (live1 / "data" / "postmaster.pid").write_text(f"{os.getpid()}\n")
    pg_sandbox._write_marker(live1, os.getpid(), 54392)

    # 3. invalid marker 클러스터 (유효 hex: baad0004)
    inv1 = make_cluster_folder(tmp_path, 99999992, "baad0004")
    (inv1 / "data").mkdir()
    (inv1 / "cluster.marker").write_text("corrupted")

    # 4. data -> external symlink 클러스터 (유효 hex: 555f0001)
    sym_data = make_cluster_folder(tmp_path, 99999993, "555f0001")
    (sym_data / "data").symlink_to(external_db)
    pg_sandbox._write_marker(sym_data, 99999993, 54393)

    # 5. top-level symlink 클러스터 (유효 hex: 109f0001)
    top_sym = make_cluster_folder(
        tmp_path, 99999994, "109f0001", is_symlink=True, target=external_db
    )

    # 모든 테스트 폴더가 _NAME_RE 규칙에 일치함을 사전 확인
    for f in (dead1, live1, inv1, sym_data, top_sym):
        assert pg_sandbox._NAME_RE.fullmatch(f.name) is not None

    # reap 실행
    reaped = pg_sandbox.reap_stale()

    # dead1 과 sym_data(안전한 unlink 후 폴더 정리)는 정리되고, live1/inv1/top_sym 은 건드리지 않음
    assert dead1.name in reaped
    assert sym_data.name in reaped
    assert live1.name not in reaped
    assert inv1.name not in reaped
    assert top_sym.name not in reaped

    # 디렉터리 상태 검증
    assert not dead1.exists()
    assert not sym_data.exists()
    assert live1.exists()
    assert inv1.exists()
    assert top_sym.exists()

    # 외부 DB 완전 무영향 검증 (외부 영향 0)
    assert external_db.exists()
    assert (external_db / "postmaster.pid").read_text() == "11111\n"
    assert (external_db / "sentinel.txt").read_text() == "production"
    assert str(external_db) not in pg_ctl_stopped_dirs
    resolved_stopped = [str(Path(p).resolve()) for p in pg_ctl_stopped_dirs]
    assert str(external_db.resolve()) not in resolved_stopped
    assert str(dead1 / "data") in pg_ctl_stopped_dirs


def test_asyncpg_patch_unpatch_and_ipv6(tmp_path, monkeypatch):
    """asyncpg 라우팅 패치에서 localhost, 127.0.0.1, ::1, None 은 포트 주입되고 명시포트/DSN/외부호스트는 유지되며 unpatch 시 복원된다."""

    class FakeAsyncPG:
        @staticmethod
        async def orig_connect(*args, **kwargs):
            return {"args": args, "kwargs": kwargs}

    FakeAsyncPG.connect = FakeAsyncPG.orig_connect
    monkeypatch.setitem(sys.modules, "asyncpg", FakeAsyncPG)

    cluster = pg_sandbox.Cluster(tmp_path, 55431, tmp_path / "bin")
    pg_sandbox.patch_asyncpg_for_cluster(cluster)

    async def run_cases():
        c1 = await FakeAsyncPG.connect(host="localhost")
        assert c1["kwargs"]["port"] == 55431

        c2 = await FakeAsyncPG.connect(host="127.0.0.1")
        assert c2["kwargs"]["port"] == 55431

        c3 = await FakeAsyncPG.connect(host="::1")
        assert c3["kwargs"]["port"] == 55431

        c4 = await FakeAsyncPG.connect()
        assert c4["kwargs"]["port"] == 55431

        c5 = await FakeAsyncPG.connect(host="localhost", port=6432)
        assert c5["kwargs"]["port"] == 6432

        c6 = await FakeAsyncPG.connect("postgresql://localhost/db")
        assert c6["kwargs"].get("port") is None

        c7 = await FakeAsyncPG.connect(host="db.example.com")
        assert c7["kwargs"].get("port") is None

    asyncio.run(run_cases())

    if hasattr(pg_sandbox, "unpatch_asyncpg"):
        pg_sandbox.unpatch_asyncpg()
    else:
        cluster.stop()

    assert FakeAsyncPG.connect == FakeAsyncPG.orig_connect


def test_cluster_stop_restores_signal_handlers_and_asyncpg(tmp_path, monkeypatch):
    """Cluster.stop() 후 asyncpg wrapper 와 SIGTERM/SIGHUP 핸들러가 복원된다."""

    class FakeAsyncPG:
        @staticmethod
        async def orig_connect(*args, **kwargs):
            return {}

    FakeAsyncPG.connect = FakeAsyncPG.orig_connect
    monkeypatch.setitem(sys.modules, "asyncpg", FakeAsyncPG)

    orig_term = signal.getsignal(signal.SIGTERM)
    orig_hup = signal.getsignal(signal.SIGHUP)

    cluster = pg_sandbox.Cluster(tmp_path, 55432, tmp_path / "bin")
    pg_sandbox.patch_asyncpg_for_cluster(cluster)
    if hasattr(pg_sandbox, "_install_signal_handlers"):
        pg_sandbox._install_signal_handlers(cluster)

    cluster.stop()

    assert FakeAsyncPG.connect == FakeAsyncPG.orig_connect
    assert signal.getsignal(signal.SIGTERM) == orig_term
    assert signal.getsignal(signal.SIGHUP) == orig_hup


def test_cluster_stop_restores_its_own_signal_handler_snapshot(tmp_path):
    """Each cluster restores its own start-time handlers, including nested clusters."""
    signals = (signal.SIGTERM, signal.SIGHUP)
    original_handlers = {sig: signal.getsignal(sig) for sig in signals}

    def parent_handler(signum, frame):
        pass

    try:
        for sig in signals:
            signal.signal(sig, signal.SIG_DFL)
        standalone = pg_sandbox.Cluster(tmp_path / "standalone", 55433, tmp_path)
        pg_sandbox._install_signal_handlers(standalone)
        standalone.stop()
        assert {sig: signal.getsignal(sig) for sig in signals} == {
            sig: signal.SIG_DFL for sig in signals
        }

        for sig in signals:
            signal.signal(sig, parent_handler)
        parent = pg_sandbox.Cluster(tmp_path / "parent", 55434, tmp_path)
        child = pg_sandbox.Cluster(tmp_path / "child", 55435, tmp_path)
        pg_sandbox._install_signal_handlers(parent)
        parent_handlers = {sig: signal.getsignal(sig) for sig in signals}
        pg_sandbox._install_signal_handlers(child)
        child.stop()
        assert {sig: signal.getsignal(sig) for sig in signals} == parent_handlers
        parent.stop()
        assert {sig: signal.getsignal(sig) for sig in signals} == {
            sig: parent_handler for sig in signals
        }
    finally:
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)


def test_startup_failure_restores_signal_handlers(tmp_path, monkeypatch):
    """start_cluster() 중 initdb/pg_ctl 실패 시에도 SIGTERM/SIGHUP 핸들러가 원래대로 복원된다."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)
    fake_pg_bin = tmp_path / "bin"
    fake_pg_bin.mkdir()
    monkeypatch.setattr(pg_sandbox, "_pg_bin", lambda: fake_pg_bin)

    orig_term = signal.getsignal(signal.SIGTERM)
    orig_hup = signal.getsignal(signal.SIGHUP)

    def fail_run(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="initdb failed")

    monkeypatch.setattr(pg_sandbox, "_run", fail_run)

    with pytest.raises(subprocess.CalledProcessError):
        pg_sandbox.start_cluster()

    assert signal.getsignal(signal.SIGTERM) == orig_term
    assert signal.getsignal(signal.SIGHUP) == orig_hup


def test_cluster_recreation_updates_port(tmp_path, monkeypatch):
    """cluster1 stop 후 cluster2 create 시 asyncpg 패치가 새 port 를 반영한다."""

    class FakeAsyncPG:
        @staticmethod
        async def orig_connect(*args, **kwargs):
            return {"args": args, "kwargs": kwargs}

    FakeAsyncPG.connect = FakeAsyncPG.orig_connect
    monkeypatch.setitem(sys.modules, "asyncpg", FakeAsyncPG)

    cluster1 = pg_sandbox.Cluster(tmp_path / "c1", 55431, tmp_path / "bin")
    pg_sandbox.patch_asyncpg_for_cluster(cluster1)

    res1 = asyncio.run(FakeAsyncPG.connect(host="localhost"))
    assert res1["kwargs"]["port"] == 55431

    cluster1.stop()

    cluster2 = pg_sandbox.Cluster(tmp_path / "c2", 55432, tmp_path / "bin")
    pg_sandbox.patch_asyncpg_for_cluster(cluster2)

    res2 = asyncio.run(FakeAsyncPG.connect(host="localhost"))
    assert res2["kwargs"]["port"] == 55432

    cluster2.stop()


def test_run_failure_exposes_stderr_in_exception_message():
    """initdb 가 실패해도 원인(stderr)이 예외 메시지에 남는다. 기존 CalledProcessError 처리와는 호환된다."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        pg_sandbox._run(["sh", "-c", "echo 'shmget: No space left on device' >&2; exit 3"])

    assert exc.value.returncode == 3
    assert "shmget: No space left on device" in str(exc.value)
