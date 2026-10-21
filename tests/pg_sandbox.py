"""테스트 세션 전용 임시 PostgreSQL 클러스터.

공유 PostgreSQL 에는 DB 를 만들지 않는다. 세션마다 /tmp/ttakkari-test-<pid>-<난수>/ 아래에 initdb 로
클러스터를 띄우고(Unix 소켓은 그 폴더 안, TCP 는 127.0.0.1 임의 포트) 끝나면 멈추고 폴더째 지운다.
프로세스가 비정상 종료돼 폴더가 남으면 다음 세션 시작 때 소유 pid 가 죽은 폴더만 정리한다.

postmaster 가 SIGKILL 로 죽으면 SysV 공유메모리 세그먼트가 남는다(macOS kern.sysv.shmmni=32 라 고아가
쌓이면 모든 initdb 가 shmget ENOSPC 로 실패한다). 그래서 폴더를 지우기 전에 postmaster.pid 의 shm key/id 를
읽어 두고, ipcs 로 key·CPID·소유자·NATTCH 가 모두 맞는 우리 세그먼트만 ipcrm 한다.
"""

from __future__ import annotations

import atexit
import glob
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

PREFIX = "ttakkari-test-"
ROOT_DIR = Path(
    "/tmp"
)  # 소켓 경로가 macOS 의 104자 제한을 넘지 않도록 짧은 고정 경로를 쓴다
_NAME_RE = re.compile(rf"^{re.escape(PREFIX)}(\d+)-[0-9a-f]+$")
MARKER_FILE = "cluster.marker"
_BIN_CANDIDATES = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/local/opt/postgresql@17/bin",
)

log = logging.getLogger("pg_sandbox")
_PLATFORM = sys.platform

# SIGKILL 직후 커널이 프로세스의 detach 를 마치기까지 NATTCH 가 잠깐 남을 수 있어 짧게 기다린다.
SHM_POLL_SECONDS = 3.0
SHM_POLL_INTERVAL = 0.1

_cluster: Cluster | None = None
_asyncpg_orig_connect = None
_active_cluster: Cluster | None = None


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


def _write_marker(folder: Path, pid: int, port: int) -> None:
    """우리가 소유한 유효한 클러스터 디렉터리임을 증명하는 마커 파일을 남긴다."""
    marker = folder / MARKER_FILE
    marker.write_text(f"prefix={PREFIX}\npid={pid}\nport={port}\n", encoding="utf-8")


def _is_valid_cluster_folder(folder: Path) -> bool:
    """/tmp/ttakkari-test-<pid>-<hex> 폴더가 우리가 관리하는 안전한 임시 클러스터인지 검증한다."""
    try:
        if folder.is_symlink() or not folder.is_dir():
            return False
        m = _NAME_RE.match(folder.name)
        if not m:
            return False
        # 루트 디렉터리 경계 탈출 방지
        resolved_folder = folder.resolve()
        resolved_root = ROOT_DIR.resolve()
        if resolved_folder.parent != resolved_root:
            return False
        # 마커 파일 검사 (invalid marker 보호)
        marker = folder / MARKER_FILE
        if not marker.exists() or marker.is_symlink() or not marker.is_file():
            return False
        if marker.resolve().parent != resolved_folder:
            return False
        content = marker.read_text(encoding="utf-8")
        lines = dict(line.split("=", 1) for line in content.splitlines() if "=" in line)
        if lines.get("prefix") != PREFIX:
            return False
        return int(lines.get("pid", -1)) == int(m.group(1))
    except (OSError, ValueError):
        return False


def _postmaster_pid(folder: Path) -> int | None:
    try:
        data = folder / "data"
        if data.is_symlink():
            return None
        pm_file = data / "postmaster.pid"
        if not pm_file.is_file() or pm_file.is_symlink():
            return None
        return int(pm_file.read_text().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


@dataclass(frozen=True)
class ShmRef:
    """postmaster.pid 가 기록한 SysV 공유메모리 세그먼트 식별자와 그 postmaster 의 pid."""

    pid: int
    key: int
    shmid: int


def _read_shm_ref(folder: Path) -> ShmRef | None:
    """폴더를 지우기 전에 postmaster.pid 1번째 줄(pid)과 7번째 줄(shm_key shm_id)을 읽어 둔다."""
    try:
        data = folder / "data"
        if data.is_symlink():
            return None
        pm_file = data / "postmaster.pid"
        if not pm_file.is_file() or pm_file.is_symlink():
            return None
        lines = pm_file.read_text().splitlines()
        pid = int(lines[0])
        key_s, id_s = lines[6].split()
        ref = ShmRef(pid=pid, key=int(key_s), shmid=int(id_s))
    except (OSError, ValueError, IndexError):
        return None
    if ref.pid <= 0 or ref.key <= 0 or ref.shmid < 0:
        return None
    return ref


_IPCS_ALIASES = {"id": "id", "shmid": "id", "key": "key", "owner": "owner"}


def _parse_ipcs(text: str) -> list[dict[str, str]]:
    """ipcs -m 출력(macOS, Linux)을 헤더 이름 기준 행 목록으로 바꾼다. 헤더가 없으면 빈 목록."""
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        low = [p.lower() for p in parts]
        if header is None:
            if ("id" in low or "shmid" in low) and ("key" in low or "cpid" in low):
                header = [_IPCS_ALIASES.get(p, p) for p in low]
            continue
        # Linux 는 status 열이 비면 토큰이 하나 모자란다. 그 밖에 열 수가 안 맞는 줄은 버린다.
        if header[-1] == "status" and len(parts) == len(header) - 1:
            parts.append("")
        if len(parts) == len(header):
            rows.append(dict(zip(header, parts)))
    return rows


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _ipcs_segments(runner) -> list[dict[str, str]] | None:
    """공유메모리 세그먼트 목록. ipcs 를 못 쓰면 None(호출한 쪽은 지우지 않는다)."""
    ipcs = _tool("ipcs")
    if ipcs is None:
        return None
    try:
        if _PLATFORM == "darwin":
            out = runner(
                [ipcs, "-m", "-a"], capture_output=True, text=True, check=False
            )
            return _parse_ipcs(out.stdout or "")
        # Linux(util-linux): 기본 출력에 key/owner/nattch, -p 출력에 cpid 가 따로 있어 id 로 합친다.
        base = runner([ipcs, "-m"], capture_output=True, text=True, check=False)
        pids = runner([ipcs, "-m", "-p"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    cpid_by_id = {r["id"]: r.get("cpid", "") for r in _parse_ipcs(pids.stdout or "")}
    rows = _parse_ipcs(base.stdout or "")
    for r in rows:
        r["cpid"] = cpid_by_id.get(r["id"], "")
    return rows


def _current_user_names() -> set[str]:
    names = {str(os.getuid())}
    try:
        import pwd

        names.add(pwd.getpwuid(os.getuid()).pw_name)
    except (ImportError, KeyError):
        pass
    return names


def _match_segment(rows: list[dict[str, str]], ref: ShmRef) -> dict[str, str] | None:
    for r in rows:
        if r.get("id") == str(ref.shmid):
            return r
    return None


def _int_or_none(value: str | None, base: int = 10) -> int | None:
    try:
        return int(value or "", base)
    except ValueError:
        return None


def release_shm_segment(
    ref: ShmRef | None,
    *,
    runner=None,
    alive=None,
    sleep=time.sleep,
    timeout: float | None = None,
    interval: float | None = None,
) -> bool:
    """우리 postmaster 가 남긴 SysV 세그먼트만 ipcrm 한다. 지웠거나 이미 없으면 True, 그 외 False.

    호스트의 다른 세그먼트(공유 PG, aip 등)가 대상이 되지 않도록 shm id 에 더해 다음을 모두 확인한다:
    key == postmaster.pid 의 key, CPID == 그 postmaster pid, 그 postmaster 는 사망, NATTCH == 0,
    소유자가 현재 사용자. 하나라도 어긋나면 지우지 않고 경고만 남긴다.
    """
    if ref is None:
        return True
    runner = runner or subprocess.run
    alive = alive or _pid_alive
    timeout = SHM_POLL_SECONDS if timeout is None else timeout
    interval = SHM_POLL_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout
    me = _current_user_names()

    while True:
        # postmaster 가 살아 있으면 세그먼트는 아직 쓰는 중이다. SIGKILL 직후엔 짧게 기다린다.
        pm_dead = not alive(ref.pid)
        rows = _ipcs_segments(runner)
        if rows is None:
            log.warning("ipcs 를 쓸 수 없어 shm %s 정리를 건너뛴다", ref.shmid)
            return False
        seg = _match_segment(rows, ref)
        if seg is None:
            return True  # postmaster 가 정상 종료하며 이미 지웠다
        if _int_or_none(seg.get("key"), 16) != ref.key:
            log.warning(
                "shm %s 의 key 가 postmaster.pid 와 달라 지우지 않는다: %s",
                ref.shmid,
                seg,
            )
            return False
        if _int_or_none(seg.get("cpid")) != ref.pid:
            log.warning(
                "shm %s 의 CPID 가 postmaster %s 와 달라 지우지 않는다: %s",
                ref.shmid,
                ref.pid,
                seg,
            )
            return False
        if seg.get("owner") not in me:
            log.warning(
                "shm %s 의 소유자가 현재 사용자가 아니라 지우지 않는다: %s",
                ref.shmid,
                seg,
            )
            return False
        if pm_dead and _int_or_none(seg.get("nattch")) == 0:
            break
        if time.monotonic() >= deadline:
            why = (
                "postmaster 가 아직 살아 있어"
                if not pm_dead
                else "NATTCH 가 0 이 아니라"
            )
            log.warning("shm %s 를 %s 지우지 않는다: %s", ref.shmid, why, seg)
            return False
        sleep(interval)

    ipcrm = _tool("ipcrm")
    if ipcrm is None:
        log.warning("ipcrm 이 없어 shm %s 를 지우지 못했다", ref.shmid)
        return False
    try:
        res = runner(
            [ipcrm, "-m", str(ref.shmid)], capture_output=True, text=True, check=False
        )
    except OSError as e:
        log.warning("ipcrm -m %s 실행 실패: %s", ref.shmid, e)
        return False
    if getattr(res, "returncode", 1) != 0:
        log.warning("ipcrm -m %s 실패: %s", ref.shmid, getattr(res, "stderr", ""))
        return False
    return True


def _stop_and_remove(folder: Path, pg_bin: Path) -> None:
    """클러스터를 멈추고 폴더를 지운다. 각 단계 실패는 다음 단계로 넘어간다."""
    try:
        if folder.is_symlink() or folder.resolve().parent != ROOT_DIR.resolve():
            return
        if not _is_valid_cluster_folder(folder):
            return
    except OSError:
        return

    data = folder / "data"
    resolved_folder = folder.resolve()
    # 폴더(postmaster.pid)를 지우면 세그먼트 식별자를 잃으므로 멈추기 전에 읽어 둔다.
    shm_ref = _read_shm_ref(folder)

    # data 가 symlink 인 경우: 외부 디렉터리를 가리킬 위험이 있으므로 pg_ctl 호출 금지!
    # symlink 만 unlink 하고 외부 서버는 절대 건드리지 않는다.
    if data.is_symlink():
        try:
            data.unlink()
        except OSError:
            pass
    elif data.is_dir():
        # data 가 실제 디렉터리이고 folder 내부의 직계 data 인 경우만 pg_ctl 실행 허용
        try:
            is_safe_data = not data.is_symlink() and data.resolve() == (
                resolved_folder / "data"
            )
        except OSError:
            is_safe_data = False

        if is_safe_data:
            pm_file = data / "postmaster.pid"
            if pm_file.is_file() and not pm_file.is_symlink():
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
    pm = _postmaster_pid(folder)
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
    # postmaster 가 SIGKILL 로 죽으면 세그먼트가 남는다. 조건이 맞을 때만 지우고 아니면 경고만 한다.
    release_shm_segment(shm_ref)
    shutil.rmtree(folder, ignore_errors=True)


def _is_postmaster_of(pid: int, folder: Path) -> bool:
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(folder) in out
    except OSError:
        return False


def reap_stale() -> list[str]:
    """소유 pid 가 죽은 /tmp/ttakkari-test-* 폴더를 정리하고 지운 폴더 이름을 돌려준다.
    살아 있는 소유자, 마커가 유효하지 않은 폴더, 심볼릭 링크는 건드리지 않는다."""
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
        try:
            if folder.resolve().parent != ROOT_DIR.resolve():
                continue
        except OSError:
            continue
        if not _is_valid_cluster_folder(folder):
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


def _install_signal_handlers(cluster: Cluster) -> None:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if sig not in cluster._signal_handlers:
            cluster._signal_handlers[sig] = signal.getsignal(sig)
        prev = cluster._signal_handlers[sig]

        def _handler(signum, frame, _prev=prev, _c=cluster):
            _c.stop()
            if callable(_prev):
                _prev(signum, frame)
            else:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        signal.signal(sig, _handler)


def _restore_signal_handlers(cluster: Cluster) -> None:
    for sig, orig in list(cluster._signal_handlers.items()):
        try:
            signal.signal(sig, orig)
        except (OSError, ValueError):
            pass
    cluster._signal_handlers.clear()


class Cluster:
    def __init__(self, folder: Path, port: int, pg_bin: Path) -> None:
        self.folder = folder
        self.port = port
        self.pg_bin = pg_bin
        self.stopped = False
        self._signal_handlers: dict[signal.Signals, object] = {}

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
        global _cluster
        if self.stopped:
            return
        self.stopped = True
        unpatch_asyncpg()
        _restore_signal_handlers(self)
        _stop_and_remove(self.folder, self.pg_bin)
        if _cluster is self:
            _cluster = None


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
    _write_marker(folder, os.getpid(), 0)

    # 어떤 종료 경로에서도 정리한다. SIGKILL 만 못 잡고, 그건 다음 세션의 reap_stale 이 맡는다.
    atexit.register(cluster.stop)
    _install_signal_handlers(cluster)

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
            _write_marker(folder, os.getpid(), port)
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


def patch_asyncpg_for_cluster(cluster: Cluster) -> None:
    """asyncpg 직결(host="localhost", 포트 없음)을 임시 클러스터로 돌린다.

    마이그레이션 시험들은 asyncpg.connect(database=..., host="localhost") 를 쓰는데 asyncpg 는
    PGPORT 환경 변수를 보지 않으므로 여기서 포트를 주입한다. 명시 포트·dsn·외부 호스트는 건드리지
    않는다. 시험 세션에서만 쓰고(src 는 항상 dsn·URL 로 접속해 영향 없음) 중첩 호출 시 활성 클러스터를 갱신한다.
    """
    global _asyncpg_orig_connect, _active_cluster
    _active_cluster = cluster
    if _asyncpg_orig_connect is not None:
        return
    try:
        import asyncpg
    except ImportError:
        return
    orig = asyncpg.connect
    _asyncpg_orig_connect = orig

    async def _connect(*args, **kwargs):
        dsn = args[0] if args else kwargs.get("dsn")
        if (
            dsn is None
            and kwargs.get("port") is None
            and kwargs.get("host") in (None, "localhost", "127.0.0.1", "::1")
            and _active_cluster is not None
            and not _active_cluster.stopped
        ):
            kwargs = {**kwargs, "port": _active_cluster.port}
        return await orig(*args, **kwargs)

    asyncpg.connect = _connect


def unpatch_asyncpg() -> None:
    """asyncpg 패치를 원래대로 복원한다."""
    global _asyncpg_orig_connect, _active_cluster
    _active_cluster = None
    if _asyncpg_orig_connect is not None:
        try:
            import asyncpg

            asyncpg.connect = _asyncpg_orig_connect
        except ImportError:
            pass
        _asyncpg_orig_connect = None


if __name__ == "__main__":  # 수동 정리: python tests/pg_sandbox.py
    print("\n".join(reap_stale()) or "정리할 잔재 없음", file=sys.stderr)
