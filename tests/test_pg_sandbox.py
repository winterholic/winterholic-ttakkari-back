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


# ---- SysV 공유메모리 세그먼트 정리 (kern.sysv.shmmni=32 고갈 방지) ----

DARWIN_HEADER = (
    "IPC status from <running system> as of Wed Oct 21 19:25:45 KST 2026\n"
    "T     ID     KEY        MODE       OWNER    GROUP  CREATOR   CGROUP NATTCH  SEGSZ  CPID  LPID   ATIME    DTIME    CTIME\n"
    "Shared Memory:\n"
)


def darwin_row(shmid, key, *, owner="winterholic", nattch=0, cpid=4242):
    return (
        f"m {shmid} 0x{key:08x} --rw------- {owner}    staff {owner}    staff "
        f"{nattch}     56  {cpid}      0 no-entry no-entry 19:22:21\n"
    )


class FakeIpc:
    """ipcs/ipcrm 실행기 대역. 호출 기록을 남기고 ipcs 출력은 목록 순서대로 돌려준다."""

    def __init__(self, ipcs_outputs, *, rm_returncode=0):
        self.outputs = list(ipcs_outputs)
        self.calls: list[list[str]] = []
        self.rm_returncode = rm_returncode

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        res = MagicMock()
        res.stderr = ""
        res.returncode = 0
        if cmd[0].endswith("ipcrm"):
            res.returncode = self.rm_returncode
            res.stdout = ""
        else:
            res.stdout = (
                self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
            )
        return res

    @property
    def removed(self):
        return [c for c in self.calls if c[0].endswith("ipcrm")]


@pytest.fixture
def darwin_ipc(monkeypatch):
    monkeypatch.setattr(pg_sandbox, "_PLATFORM", "darwin")
    monkeypatch.setattr(pg_sandbox, "_tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        pg_sandbox, "_current_user_names", lambda: {"winterholic", "501"}
    )


REF = pg_sandbox.ShmRef(pid=4242, key=0x0E787393, shmid=5111812)


def release(ipc, ref=REF, *, alive=lambda pid: False, **kw):
    kw.setdefault("timeout", 0.05)
    kw.setdefault("interval", 0.01)
    return pg_sandbox.release_shm_segment(
        ref, runner=ipc, alive=alive, sleep=lambda s: None, **kw
    )


def test_read_shm_ref_parses_postmaster_pid_line_7(tmp_path):
    folder = make_cluster_folder(tmp_path, 1, "aabbccdd")
    (folder / "data").mkdir()
    (folder / "data" / "postmaster.pid").write_text(
        "89523\n/x/data\n1792578401\n64760\n/x/sock\n127.0.0.1\n242774931   5111812\nready   \n"
    )
    assert pg_sandbox._read_shm_ref(folder) == pg_sandbox.ShmRef(
        pid=89523, key=242774931, shmid=5111812
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "89523\n/x/data\n1792578401\n64760\n/x/sock\n127.0.0.1\n",  # 7번째 줄 없음
        "89523\n/x\n1\n1\n/s\nh\nnot numbers\n",
        "89523\n/x\n1\n1\n/s\nh\n242774931\n",  # id 없음
        "abc\n/x\n1\n1\n/s\nh\n242774931 5111812\n",
        "89523\n/x\n1\n1\n/s\nh\n0 0\n",  # IPC_PRIVATE 류는 대상이 아니다
    ],
)
def test_read_shm_ref_rejects_malformed(tmp_path, content):
    folder = make_cluster_folder(tmp_path, 1, "aabbccdd")
    (folder / "data").mkdir()
    (folder / "data" / "postmaster.pid").write_text(content)
    assert pg_sandbox._read_shm_ref(folder) is None


def test_read_shm_ref_ignores_symlinked_data(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "postmaster.pid").write_text("1\n/x\n1\n1\n/s\nh\n242774931 5111812\n")
    folder = make_cluster_folder(tmp_path, 1, "aabbccdd")
    (folder / "data").symlink_to(outside)
    assert pg_sandbox._read_shm_ref(folder) is None


def test_release_removes_matching_segment(darwin_ipc):
    other = darwin_row(131073, 0x0E6C1474, cpid=75719)
    ipc = FakeIpc([DARWIN_HEADER + other + darwin_row(5111812, 0x0E787393)])
    assert release(ipc) is True
    assert ipc.removed == [["/usr/bin/ipcrm", "-m", "5111812"]]


def test_release_noop_when_segment_already_gone(darwin_ipc):
    ipc = FakeIpc([DARWIN_HEADER + darwin_row(131073, 0x0E6C1474, cpid=75719)])
    assert release(ipc) is True
    assert ipc.removed == []


def test_release_noop_without_ref(darwin_ipc):
    ipc = FakeIpc([DARWIN_HEADER])
    assert pg_sandbox.release_shm_segment(None, runner=ipc) is True
    assert ipc.calls == []


@pytest.mark.parametrize(
    "row",
    [
        darwin_row(5111812, 0x0E787394),  # id 는 같은데 key 가 다르다(id 재사용)
        darwin_row(5111812, 0x0E787393, cpid=9999),  # 다른 프로세스가 만든 세그먼트
        darwin_row(5111812, 0x0E787393, owner="root"),  # 다른 사용자 소유
    ],
    ids=["key-mismatch", "cpid-mismatch", "other-owner"],
)
def test_release_refuses_when_identity_mismatches(darwin_ipc, row, caplog):
    ipc = FakeIpc([DARWIN_HEADER + row])
    with caplog.at_level("WARNING", logger="pg_sandbox"):
        assert release(ipc) is False
    assert ipc.removed == []
    assert "지우지 않는다" in caplog.text


def test_release_refuses_while_postmaster_alive(darwin_ipc, caplog):
    ipc = FakeIpc([DARWIN_HEADER + darwin_row(5111812, 0x0E787393)])
    with caplog.at_level("WARNING", logger="pg_sandbox"):
        assert release(ipc, alive=lambda pid: True) is False
    assert ipc.removed == []
    assert "살아 있어" in caplog.text


def test_release_refuses_when_still_attached(darwin_ipc, caplog):
    ipc = FakeIpc([DARWIN_HEADER + darwin_row(5111812, 0x0E787393, nattch=2)])
    with caplog.at_level("WARNING", logger="pg_sandbox"):
        assert release(ipc) is False
    assert ipc.removed == []
    assert "NATTCH" in caplog.text


def test_release_polls_until_nattch_drops_to_zero(darwin_ipc):
    attached = DARWIN_HEADER + darwin_row(5111812, 0x0E787393, nattch=1)
    detached = DARWIN_HEADER + darwin_row(5111812, 0x0E787393, nattch=0)
    ipc = FakeIpc([attached, attached, detached])
    assert release(ipc, timeout=5.0) is True
    assert ipc.removed == [["/usr/bin/ipcrm", "-m", "5111812"]]


def test_release_polls_until_postmaster_dies(darwin_ipc):
    ipc = FakeIpc([DARWIN_HEADER + darwin_row(5111812, 0x0E787393)])
    states = iter([True, True, False])
    assert release(ipc, alive=lambda pid: next(states), timeout=5.0) is True
    assert len(ipc.removed) == 1


def test_release_reports_ipcrm_failure(darwin_ipc, caplog):
    ipc = FakeIpc([DARWIN_HEADER + darwin_row(5111812, 0x0E787393)], rm_returncode=1)
    with caplog.at_level("WARNING", logger="pg_sandbox"):
        assert release(ipc) is False
    assert "ipcrm" in caplog.text


def test_release_skips_when_ipcs_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(pg_sandbox, "_tool", lambda name: None)
    ipc = FakeIpc([DARWIN_HEADER])
    with caplog.at_level("WARNING", logger="pg_sandbox"):
        assert release(ipc) is False
    assert ipc.calls == []


def test_release_skips_on_unparsable_ipcs_output_without_removing(darwin_ipc):
    ipc = FakeIpc(["garbage\n"])
    assert release(ipc) is True  # 목록에 없으면 지울 것도 없다
    assert ipc.removed == []


def test_release_handles_linux_ipcs_format(monkeypatch):
    monkeypatch.setattr(pg_sandbox, "_PLATFORM", "linux")
    monkeypatch.setattr(pg_sandbox, "_tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(pg_sandbox, "_current_user_names", lambda: {"ubuntu"})
    base = (
        "\n------ Shared Memory Segments --------\n"
        "key        shmid      owner      perms      bytes      nattch     status      \n"
        "0x0e787393 5111812    ubuntu     600        56         0                       \n"
        "0x0e6c1474 131073     ubuntu     600        56         0\n"
    )
    pids = (
        "\n------ Shared Memory Creator/Last-op PIDs --------\n"
        "shmid      owner      cpid       lpid      \n"
        "5111812    ubuntu     4242       0         \n"
        "131073     ubuntu     75719      0         \n"
    )
    ipc = FakeIpc([base, pids])
    assert release(ipc) is True
    assert ipc.removed == [["/usr/bin/ipcrm", "-m", "5111812"]]


def test_stop_and_remove_releases_segment_before_rmtree(tmp_path, monkeypatch):
    """폴더를 지우기 전에 읽어 둔 shm 식별자로 해제를 시도한다(폴더가 사라진 뒤엔 읽을 수 없다)."""
    monkeypatch.setattr(pg_sandbox, "ROOT_DIR", tmp_path)
    dead_pid = 99999995
    folder = make_cluster_folder(tmp_path, dead_pid, "feedface")
    (folder / "data").mkdir()
    pg_sandbox._write_marker(folder, dead_pid, 55555)
    (folder / "data" / "postmaster.pid").write_text(
        f"{dead_pid}\n{folder}/data\n1\n1\n{folder}/sock\n127.0.0.1\n242774931   5111812\nready\n"
    )
    seen: list[pg_sandbox.ShmRef | None] = []

    def fake_release(ref, **kw):
        assert folder.exists()
        seen.append(ref)
        return True

    monkeypatch.setattr(pg_sandbox, "release_shm_segment", fake_release)
    monkeypatch.setattr(
        pg_sandbox.subprocess,
        "run",
        lambda *a, **k: MagicMock(stdout="", stderr="", returncode=0),
    )
    pg_sandbox._stop_and_remove(folder, tmp_path / "bin")
    assert seen == [pg_sandbox.ShmRef(pid=dead_pid, key=242774931, shmid=5111812)]
    assert not folder.exists()


def _ipcs_ids() -> set[int]:
    out = subprocess.run(
        ["ipcs", "-m"], capture_output=True, text=True, check=False
    ).stdout
    return {
        int(p[1])
        for p in (ln.split() for ln in out.splitlines())
        if len(p) > 2 and p[0] == "m" and p[1].isdigit()
    }


def _real_cluster_sigkilled(tmp_root: Path):
    """실제 initdb 클러스터를 /tmp 에 띄우고 postmaster 를 SIGKILL 한다. (folder, ref) 를 돌려준다."""
    try:
        pg_bin = pg_sandbox._pg_bin()
    except RuntimeError as e:
        pytest.skip(str(e))
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    dead_owner = (
        child.pid
    )  # 방금 끝난 프로세스의 pid 를 소유자로 삼아 stale 폴더로 만든다
    folder = (
        pg_sandbox.ROOT_DIR / f"{pg_sandbox.PREFIX}{dead_owner}-{os.urandom(4).hex()}"
    )
    folder.mkdir(mode=0o700)
    pg_sandbox._write_marker(folder, dead_owner, 0)
    try:
        (folder / "sock").mkdir(mode=0o700)
        pg_sandbox._run(
            [
                str(pg_bin / "initdb"),
                "-D",
                str(folder / "data"),
                "-A",
                "trust",
                "-E",
                "UTF8",
                "--locale=en_US.UTF-8",
                "--no-sync",
            ]
        )
        port = pg_sandbox._free_port()
        opts = (
            f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories={folder / 'sock'} "
            "-c fsync=off -c max_connections=20 -c shared_buffers=16MB"
        )
        pg_sandbox._run(
            [
                str(pg_bin / "pg_ctl"),
                "-D",
                str(folder / "data"),
                "-l",
                str(folder / "pg.log"),
                "-w",
                "-t",
                "60",
                "-o",
                opts,
                "start",
            ]
        )
    except subprocess.CalledProcessError as e:
        pg_sandbox.shutil.rmtree(folder, ignore_errors=True)
        pytest.skip(
            f"임시 클러스터를 띄우지 못했다(재시도 안 함): {e.cmd[0]} -> {(e.stderr or '')[-300:]}"
        )
    ref = pg_sandbox._read_shm_ref(folder)
    assert ref is not None
    os.kill(ref.pid, signal.SIGKILL)
    return folder, ref


def _cleanup_real(folder: Path, ref) -> None:
    """테스트가 실패해도 우리가 만든 클러스터·세그먼트·폴더가 남지 않게 한다."""
    if pg_sandbox._pid_alive(ref.pid) and pg_sandbox._is_postmaster_of(ref.pid, folder):
        os.kill(ref.pid, signal.SIGKILL)
    pg_sandbox.release_shm_segment(ref)
    pg_sandbox.shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="SysV ipcs 필요")
def test_real_cluster_sigkill_then_reap_stale_leaves_no_segment(capsys):
    before = _ipcs_ids()
    folder, ref = _real_cluster_sigkilled(pg_sandbox.ROOT_DIR)
    try:
        during = _ipcs_ids()
        print(f"ipcs -m count before={len(before)} during={len(during)}")
        assert ref.shmid in during, (
            "SIGKILL 직후 고아 세그먼트가 남아 있어야 이 시험이 의미가 있다"
        )
        reaped = pg_sandbox.reap_stale()
        assert folder.name in reaped
        assert not folder.exists()
        after = _ipcs_ids()
        print(f"ipcs -m count after={len(after)}")
        assert ref.shmid not in after
    finally:
        _cleanup_real(folder, ref)
    assert not folder.exists()
    assert ref.shmid not in _ipcs_ids()


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="SysV ipcs 필요")
def test_real_cluster_sigkill_then_stop_and_remove_leaves_no_segment():
    folder, ref = _real_cluster_sigkilled(pg_sandbox.ROOT_DIR)
    try:
        assert ref.shmid in _ipcs_ids()
        pg_sandbox._stop_and_remove(folder, pg_sandbox._pg_bin())
        assert not folder.exists()
        assert ref.shmid not in _ipcs_ids()
    finally:
        _cleanup_real(folder, ref)
