"""테스트 세션 전용 임시 PostgreSQL 클러스터.

공유 PostgreSQL 에는 DB 를 만들지 않는다. 세션마다 /tmp/ttakkari-test-<pid>-<난수>/ 아래에 initdb 로
클러스터를 띄우고(Unix 소켓은 그 폴더 안, TCP 는 127.0.0.1 임의 포트) 끝나면 멈추고 폴더째 지운다.
프로세스가 비정상 종료돼 폴더가 남으면 다음 세션 시작 때 소유 pid 가 죽은 폴더만 정리한다.
"""

from __future__ import annotations

import atexit
import glob
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

PREFIX = "ttakkari-test-"
ROOT_DIR = Path(
    "/tmp"
)  # 소켓 경로가 macOS 의 104자 제한을 넘지 않도록 짧은 고정 경로를 쓴다
_NAME_RE = re.compile(rf"^{re.escape(PREFIX)}(\d+)-[0-9a-f]+$")
_BIN_CANDIDATES = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/local/opt/postgresql@17/bin",
)

_cluster: Cluster | None = None


def _pg_bin() -> Path:
    override = os.environ.get("TTAKKARI_TEST_PG_BIN")
    dirs = [override] if override else []
    dirs += [d for d in _BIN_CANDIDATES if Path(d, "initdb").exists()]
    found = shutil.which("initdb")
    if found:
        dirs.append(str(Path(found).parent))
    for d in dirs:
        if d and Path(d, "initdb").exists() and Path(d, "pg_ctl").exists():
            return Path(d)
    raise RuntimeError(
        "initdb/pg_ctl 를 찾지 못했다. TTAKKARI_TEST_PG_BIN 에 PostgreSQL bin 경로를 지정한다"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _postmaster_pid(folder: Path) -> int | None:
    try:
        return int((folder / "data" / "postmaster.pid").read_text().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def _stop_and_remove(folder: Path, pg_bin: Path) -> None:
    """클러스터를 멈추고 폴더를 지운다. 각 단계 실패는 다음 단계로 넘어간다."""
    data = folder / "data"
    pm = _postmaster_pid(folder)
    if (data / "postmaster.pid").exists():
        subprocess.run(
            [
                str(pg_bin / "pg_ctl"),
                "-D",
                str(data),
                "-m",
                "immediate",
                "-w",
                "-t",
                "20",
                "stop",
            ],
            capture_output=True,
            check=False,
        )
    # pg_ctl 이 실패했거나 postmaster 가 남아있는데 우리 폴더의 것이 맞을 때만 직접 끈다(pid 재사용 오인 방지).
    if pm is not None and _pid_alive(pm) and _is_postmaster_of(pm, folder):
        for _ in range(10):
            if not _pid_alive(pm):
                break
            time.sleep(0.1)
        if _pid_alive(pm):
            try:
                os.kill(pm, signal.SIGKILL)
            except OSError:
                pass
    shutil.rmtree(folder, ignore_errors=True)


def _is_postmaster_of(pid: int, folder: Path) -> bool:
    out = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return str(folder) in out


def reap_stale() -> list[str]:
    """소유 pid 가 죽은 /tmp/ttakkari-test-* 폴더를 정리하고 지운 폴더 이름을 돌려준다. 살아 있는 소유자 것은 건드리지 않는다."""
    reaped: list[str] = []
    try:
        pg_bin = _pg_bin()
    except RuntimeError:
        return reaped
    for path in sorted(glob.glob(str(ROOT_DIR / (PREFIX + "*")))):
        folder = Path(path)
        m = _NAME_RE.match(folder.name)
        if not m or not folder.is_dir() or folder.is_symlink():
            continue
        if _pid_alive(int(m.group(1))):
            continue
        _stop_and_remove(folder, pg_bin)
        reaped.append(folder.name)
    return reaped


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    def __init__(self, folder: Path, port: int, pg_bin: Path) -> None:
        self.folder = folder
        self.port = port
        self.pg_bin = pg_bin
        self.stopped = False

    @property
    def sockdir(self) -> Path:
        return self.folder / "sock"

    def create_database(self, name: str) -> str:
        """임시 클러스터 안에 DB 를 만들고 SQLAlchemy asyncpg URL 을 돌려준다. 공유 서버가 아니라 이 클러스터에만 만든다."""
        _run(
            [
                str(self.pg_bin / "createdb"),
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                name,
            ]
        )
        return f"postgresql+asyncpg://127.0.0.1:{self.port}/{name}"

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        _stop_and_remove(self.folder, self.pg_bin)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # macOS 에서 postmaster 는 유효한 로케일이 없으면 "became multithreaded during startup" 으로 죽는다.
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"},
    )


def start_cluster() -> Cluster:
    """세션당 1회 임시 클러스터를 띄운다. 같은 프로세스에서 다시 부르면 기존 것을 돌려준다."""
    global _cluster
    if _cluster is not None and not _cluster.stopped:
        return _cluster
    pg_bin = _pg_bin()
    reap_stale()
    folder = ROOT_DIR / f"{PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
    folder.mkdir(mode=0o700)
    cluster = Cluster(folder, 0, pg_bin)
    _cluster = cluster
    # 어떤 종료 경로에서도 정리한다. SIGKILL 만 못 잡고, 그건 다음 세션의 reap_stale 이 맡는다.
    atexit.register(cluster.stop)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        prev = signal.getsignal(sig)

        def _handler(signum, frame, _prev=prev):
            cluster.stop()
            if callable(_prev):
                _prev(signum, frame)
            else:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        signal.signal(sig, _handler)
    try:
        (folder / "sock").mkdir(mode=0o700)
        _run(
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
        last: subprocess.CalledProcessError | None = None
        for _ in range(
            8
        ):  # 포트 탐색과 기동 사이에 다른 세션이 같은 포트를 잡을 수 있다
            port = _free_port()
            opts = (
                f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories={folder / 'sock'} "
                "-c fsync=off -c synchronous_commit=off -c full_page_writes=off "
                "-c max_connections=200 -c shared_buffers=128MB"
            )
            try:
                _run(
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
                last = e
                time.sleep(0.2)
                continue
            cluster.port = port
            return cluster
        log = (
            (folder / "pg.log").read_text()[-800:]
            if (folder / "pg.log").exists()
            else ""
        )
        raise RuntimeError(
            f"임시 PostgreSQL 을 띄우지 못했다: {last.stderr if last else ''}\n{log}"
        )
    except BaseException:
        cluster.stop()
        raise


def use_cluster_env(cluster: Cluster) -> None:
    """이 프로세스와 자식(alembic 등)의 접속 기본값을 임시 클러스터로 돌린다."""
    os.environ["PGHOST"] = "127.0.0.1"
    os.environ["PGPORT"] = str(cluster.port)
    os.environ.pop("PGUSER", None)
    os.environ.pop("PGSERVICE", None)
    os.environ.pop("PGPASSWORD", None)
    os.environ.pop("DATABASE_URL", None)


def localhost_database_url(database: str) -> str:
    """alembic 자식 등 별도 프로세스에 넘기는 localhost URL. 클러스터가 떠 있으면 그 포트를 쓴다."""
    return (
        f"postgresql+asyncpg://localhost:{os.environ.get('PGPORT', '5432')}/{database}"
    )


_asyncpg_orig_connect = None


def patch_asyncpg_for_cluster(cluster: Cluster) -> None:
    """asyncpg 직결(host="localhost", 포트 없음)을 임시 클러스터로 돌린다.

    마이그레이션 시험들은 asyncpg.connect(database=..., host="localhost") 를 쓰는데 asyncpg 는
    PGPORT 환경 변수를 보지 않으므로 여기서 포트를 주입한다. 명시 포트·dsn·외부 호스트는 건드리지
    않는다. 시험 세션에서만 쓰고(src 는 항상 dsn·URL 로 접속해 영향 없음) 두 번 부르면 무시한다.
    """
    import asyncpg

    global _asyncpg_orig_connect
    if _asyncpg_orig_connect is not None:
        return
    orig = asyncpg.connect
    _asyncpg_orig_connect = orig

    async def _connect(*args, **kwargs):
        dsn = args[0] if args else kwargs.get("dsn")
        if (
            dsn is None
            and kwargs.get("port") is None
            and kwargs.get("host") in (None, "localhost", "127.0.0.1")
        ):
            kwargs = {**kwargs, "port": cluster.port}
        return await orig(*args, **kwargs)

    asyncpg.connect = _connect


if __name__ == "__main__":  # 수동 정리: python tests/pg_sandbox.py
    print("\n".join(reap_stale()) or "정리할 잔재 없음", file=sys.stderr)
