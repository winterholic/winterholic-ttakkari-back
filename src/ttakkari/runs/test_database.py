"""Run-owned PostgreSQL clusters for backend integration tests.

These clusters never use the configured application database. Each engine attempt
gets an ephemeral database on a random loopback port and the owner tears it down
when the process exits. Marked stale clusters are reaped on the next start.
"""
from __future__ import annotations

import glob
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import make_url

log = logging.getLogger(__name__)
PREFIX = "ttakkari-run-test-"
MARKER = "run-test-cluster.marker"
ROOT = Path("/tmp")
_BIN_CANDIDATES = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/local/opt/postgresql@17/bin",
)


def patch_asyncpg_test_port(asyncpg_module, database_url: str) -> bool:
    """Route direct asyncpg(host='localhost') calls to an explicit test URL port."""
    url = make_url(database_url)
    if url.host not in {"localhost", "127.0.0.1"} or url.port is None:
        return False
    original = asyncpg_module.connect
    if getattr(original, "_ttakkari_test_port", None) == url.port:
        return True

    async def connect(*args, **kwargs):
        dsn = args[0] if args else kwargs.get("dsn")
        host = kwargs.get("host")
        if dsn is None and host in {"localhost", "127.0.0.1"} and kwargs.get("port") is None:
            kwargs["port"] = url.port
            kwargs.setdefault("user", url.username)
            kwargs.setdefault("password", url.password)
        return await original(*args, **kwargs)

    connect._ttakkari_test_port = url.port
    asyncpg_module.connect = connect
    return True


def _pg_bin(override: Path | None = None) -> Path:
    candidates = [str(override)] if override else []
    candidates += [p for p in _BIN_CANDIDATES if Path(p, "initdb").exists()]
    found = shutil.which("initdb")
    if found:
        candidates.append(str(Path(found).parent))
    for candidate in candidates:
        path = Path(candidate)
        if all((path / name).is_file() for name in ("initdb", "pg_ctl", "createdb", "psql")):
            return path
    raise RuntimeError("initdb/pg_ctl/createdb를 찾지 못했다")


def _run(command: list[str], *, check: bool = True,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=check,
                          env={**os.environ, **(env or {}), "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _shm_ref(folder: Path) -> tuple[int, int, int] | None:
    """Read postmaster.pid's (pid, SysV key, shmid) before stopping/removing it."""
    try:
        data = folder / "data"
        if data.is_symlink():
            return None
        pid_file = data / "postmaster.pid"
        if pid_file.is_symlink() or pid_file.resolve().parent != data.resolve():
            return None
        lines = pid_file.read_text().splitlines()
        pid = int(lines[0])
        key, shmid = (int(part, 0) for part in lines[6].split())
        return (pid, key, shmid) if pid > 0 and key > 0 and shmid >= 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _ipcs_rows() -> list[dict[str, str]] | None:
    ipcs = shutil.which("ipcs")
    if not ipcs:
        return None

    def parse(output: str) -> tuple[list[str] | None, list[dict[str, str]]]:
        header = None
        rows = []
        for raw in output.splitlines():
            parts = raw.split()
            lower = [part.lower() for part in parts]
            if header is None:
                if ("id" in lower or "shmid" in lower) and ("key" in lower or "cpid" in lower):
                    header = ["id" if part == "shmid" else part for part in lower]
                continue
            if header and header[-1] == "status" and len(parts) == len(header) - 1:
                parts.append("")
            if header and len(parts) == len(header):
                rows.append(dict(zip(header, parts)))
        return header, rows

    try:
        if sys.platform == "darwin":
            return parse(subprocess.run([ipcs, "-m", "-a"], capture_output=True, text=True,
                                        check=False).stdout)[1]
        base = parse(subprocess.run([ipcs, "-m"], capture_output=True, text=True,
                                    check=False).stdout)[1]
        pids = parse(subprocess.run([ipcs, "-m", "-p"], capture_output=True, text=True,
                                    check=False).stdout)[1]
        cpid_by_id = {row.get("id"): row.get("cpid", "") for row in pids}
        for row in base:
            row["cpid"] = cpid_by_id.get(row.get("id"), "")
        return base
    except OSError:
        return None


def _release_orphan_shm(ref: tuple[int, int, int] | None) -> bool:
    if ref is None:
        return True
    pid, key, shmid = ref
    rows = _ipcs_rows()
    if rows is None:
        log.error("ipcs unavailable; preserving stale test cluster with shm id %s", shmid)
        return False
    segment = next((row for row in rows if row.get("id") == str(shmid)), None)
    if segment is None:
        return True
    try:
        segment_key = int(segment.get("key", ""), 16)
        cpid = int(segment.get("cpid", ""))
        nattch = int(segment.get("nattch", ""))
    except ValueError:
        log.error("cannot verify SysV segment %s; preserving stale test cluster", shmid)
        return False
    allowed_owners = {str(os.getuid())}
    try:
        import pwd
        allowed_owners.add(pwd.getpwuid(os.getuid()).pw_name)
    except (ImportError, KeyError):
        pass
    if (segment_key != key or cpid != pid or nattch != 0 or segment.get("owner") not in allowed_owners
            or _pid_alive(pid)):
        log.error("SysV segment %s does not match this stopped test cluster; preserving it", shmid)
        return False
    ipcrm = shutil.which("ipcrm")
    if not ipcrm:
        log.error("ipcrm unavailable; preserving stale test cluster with shm id %s", shmid)
        return False
    result = subprocess.run([ipcrm, "-m", str(shmid)], capture_output=True, text=True, check=False)
    if result.returncode:
        log.error("ipcrm could not remove stale test cluster shm %s: %s", shmid, result.stderr)
        return False
    return True


def _valid_folder(folder: Path, root: Path = ROOT) -> tuple[int, int] | None:
    """Return (owner_pid, port) only for a canonical, marked directory we own."""
    try:
        if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root.resolve():
            return None
        if not folder.name.startswith(PREFIX):
            return None
        marker = folder / MARKER
        if marker.is_symlink() or not marker.is_file() or marker.resolve().parent != folder.resolve():
            return None
        fields = dict(line.split("=", 1) for line in marker.read_text().splitlines() if "=" in line)
        if fields.get("prefix") != PREFIX or fields.get("folder") != folder.name:
            return None
        return int(fields["owner_pid"]), int(fields["port"])
    except (OSError, ValueError, KeyError):
        return None


def _stop_owned(folder: Path, pg_bin: Path, root: Path = ROOT) -> bool:
    """Stop only a validated cluster, retaining it if its postmaster will not exit."""
    if _valid_folder(folder, root) is None:
        return False
    data = folder / "data"
    if data.is_symlink() or (data.exists() and data.resolve().parent != folder.resolve()):
        return False
    pid_file = data / "postmaster.pid"
    shm = _shm_ref(folder)
    try:
        pid = int(pid_file.read_text().splitlines()[0]) if pid_file.is_file() else None
    except (OSError, ValueError, IndexError):
        pid = None
    if pid is not None and _pid_alive(pid):
        _run([str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "-t", "20", "stop"], check=False)
        deadline = time.monotonic() + 5
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _pid_alive(pid):
            log.error("Run test PostgreSQL postmaster %s did not stop; preserving %s", pid, folder)
            return False
    if not _release_orphan_shm(shm):
        return False
    shutil.rmtree(folder)
    return True


def reap_stale(root: Path = ROOT) -> list[str]:
    """Reap dead-owner clusters after proving path, marker and postmaster ownership."""
    try:
        pg_bin = _pg_bin()
    except RuntimeError:
        return []
    reaped = []
    for name in sorted(glob.glob(str(root / f"{PREFIX}*"))):
        folder = Path(name)
        owner = _valid_folder(folder, root)
        if owner is None or _pid_alive(owner[0]):
            continue
        if _stop_owned(folder, pg_bin, root):
            reaped.append(folder.name)
    return reaped


class RunTestDatabase:
    """One ephemeral PostgreSQL cluster for one engine process attempt."""

    def __init__(self, run_id: str, workspace: Path, *, pg_bin: Path | None = None,
                 root: Path = ROOT) -> None:
        self.run_id = run_id
        self.workspace = workspace.resolve()
        self.root = root
        self.pg_bin = pg_bin
        self.folder: Path | None = None
        self.port: int | None = None
        self.url: str | None = None

    def start(self) -> str:
        if self.url is not None:
            return self.url
        binary = _pg_bin(self.pg_bin)
        reap_stale(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        folder = self.root / f"{PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
        folder.mkdir(mode=0o700)
        self.folder = folder
        admin_password = secrets.token_urlsafe(32)
        test_password = secrets.token_urlsafe(32)
        password_file = folder / "initdb-password"
        password_file.write_text(admin_password, encoding="utf-8")
        password_file.chmod(0o600)
        for _ in range(8):
            port = _free_port()
            marker = folder / MARKER
            marker.write_text(f"prefix={PREFIX}\nfolder={folder.name}\nowner_pid={os.getpid()}\n"
                              f"run_id={self.run_id}\nport={port}\n", encoding="utf-8")
            try:
                _run([str(binary / "initdb"), "-D", str(folder / "data"), "-A", "scram-sha-256",
                      "-U", "ttk_admin", "--pwfile", str(password_file), "-E", "UTF8",
                      "--locale=en_US.UTF-8", "--no-sync"])
                password_file.unlink(missing_ok=True)
                options = (f"-p {port} -c listen_addresses=127.0.0.1 "
                           f"-c unix_socket_directories={folder} -c fsync=off "
                           "-c synchronous_commit=off -c full_page_writes=off "
                           "-c max_connections=200 -c shared_buffers=128MB")
                admin_env = {"PGPASSWORD": admin_password}
                _run([str(binary / "pg_ctl"), "-D", str(folder / "data"), "-l", str(folder / "pg.log"),
                      "-w", "-t", "60", "-o", options, "start"])
                setup_sql = folder / "create-test-role.sql"
                setup_sql.write_text(f"CREATE ROLE ttk_test LOGIN SUPERUSER PASSWORD '{test_password}';\n",
                                     encoding="utf-8")
                setup_sql.chmod(0o600)
                _run([str(binary / "psql"), "-h", "127.0.0.1", "-p", str(port), "-U", "ttk_admin",
                      "-d", "postgres", "-f", str(setup_sql)], env=admin_env)
                setup_sql.unlink(missing_ok=True)
                _run([str(binary / "createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "ttk_admin",
                      "-O", "ttk_test", "ttakkari_test"], env=admin_env)
                self.port = port
                self.url = (f"postgresql+asyncpg://ttk_test:{quote(test_password, safe='')}@"
                            f"127.0.0.1:{port}/ttakkari_test")
                return self.url
            except subprocess.CalledProcessError as exc:
                # Only retry a bind collision. Other failures are reported as-is.
                if "address already in use" not in (exc.stderr or "").lower():
                    self.close()
                    raise
                _stop_owned(folder, binary, self.root)
                folder.mkdir(mode=0o700, exist_ok=True)
        self.close()
        raise RuntimeError("Run 전용 PostgreSQL 포트를 확보하지 못했다")

    def child_env(self) -> dict[str, str]:
        if self.url is None or self.port is None:
            raise RuntimeError("Run 전용 테스트 DB가 시작되지 않았다")
        return {"TTAKKARI_TEST_DB": self.url, "PGHOST": "127.0.0.1", "PGPORT": str(self.port)}

    def close(self) -> None:
        if self.folder is None:
            return
        folder = self.folder
        self.folder = None
        try:
            _stop_owned(folder, _pg_bin(self.pg_bin), self.root)
        except RuntimeError:
            log.exception("Run test PostgreSQL binaries disappeared; preserving %s", folder)

