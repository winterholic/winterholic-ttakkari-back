"""작업 종류 허용 목록과 실행 계획. 모든 경로·대상은 설정과 manifest 에서 이 모듈이 정한다."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url

from ttakkari import backup_status
from ttakkari.config import get_settings

BACK = Path(__file__).resolve().parents[3]
SCRIPTS = BACK / "scripts"

HEAVY = ("backup_db", "data_snapshot", "restore_rehearsal", "snapshot_verify")
INLINE = ("runtime_report", "performance_baseline", "backup_status")
KINDS = (*HEAVY, *INLINE)
TIMEOUT_SECONDS = {"backup_db": 900, "data_snapshot": 1800, "restore_rehearsal": 1800, "snapshot_verify": 1800}
BACKUP_RETENTION_DAYS = 30
RESTORE_DB_PREFIX = "ttakkari_restore_rehearsal_"
VERIFY_DIR = ".verify"
_MANIFEST = {
    "db": re.compile(r"^ttakkari-\d{8}-\d{6}-\d+\.manifest\.json$"),
    "data": re.compile(r"^data-\d{8}-\d{6}-\d+\.manifest\.json$"),
}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_PATH_EXTRA = ("/opt/homebrew/opt/postgresql@17/bin", "/opt/homebrew/bin", "/usr/local/bin")


class Refused(Exception):
    """계획을 세울 수 없거나 안전 조건을 어겼다. status 는 HTTP 코드로 쓴다."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class Plan:
    kind: str
    argv: list[str]
    env: dict[str, str]
    timeout: int
    # 결과 보고용 비밀 아닌 식별자(대상 DB 이름 등)
    identity: dict[str, str] = field(default_factory=dict)
    cleanup_dir: Path | None = None


def backup_dir() -> Path:
    return backup_status.backup_dir()


def passphrase_file(*, create: bool) -> tuple[Path, str]:
    """(경로, 출처). 키 내용은 읽지 않는다. 없으면 create 일 때만 앱이 0600으로 새로 만든다."""
    configured = get_settings().backup_passphrase_file
    if configured:
        path, source = Path(configured).expanduser(), "configured"
    else:
        path, source = get_settings().home / "backup.key", "app_managed"
    if not path.exists():
        if source == "configured":
            raise Refused("설정된 백업 암호 파일이 없습니다(TTAKKARI_BACKUP_PASSPHRASE_FILE).")
        if not create:
            raise Refused("백업 암호 파일이 아직 없습니다. 먼저 backup_db 작업을 실행하세요.")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48) + "\n")
    if path.stat().st_mode & 0o077:
        raise Refused("백업 암호 파일 권한이 0600이 아닙니다.")
    if path.stat().st_size == 0:
        raise Refused("백업 암호 파일이 비어 있습니다.")
    return path, source


def local_database() -> tuple[str, str]:
    """(접속 URL, 운영 DB 이름). 복원은 로컬 PostgreSQL 에서만 허용한다."""
    url = get_settings().database_url
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    raw = parsed.query.get("host", ())
    sockets = [raw] if isinstance(raw, str) else list(raw)
    if host not in _LOOPBACK and not (not host and sockets and all(s.startswith("/") for s in sockets)):
        raise Refused("복원 리허설은 로컬 PostgreSQL 에서만 실행합니다.")
    if not parsed.database:
        raise Refused("운영 DB 이름을 확인할 수 없습니다.")
    return url, parsed.database


def _env() -> dict[str, str]:
    path = os.environ.get("PATH", "/usr/bin:/bin")
    for extra in _PATH_EXTRA:
        if extra not in path.split(":"):
            path = f"{path}:{extra}"
    return {"PATH": path, "HOME": str(Path.home()), "LC_ALL": "C.UTF-8", "BACKUP_DIR": str(backup_dir()),
            "RETENTION_DAYS": str(BACKUP_RETENTION_DAYS)}


def _need(*tools: str) -> None:
    if not (BACK / ".venv/bin/python").is_file():  # 운영 스크립트가 쓰는 프로젝트 Python
        raise Refused("프로젝트 Python 환경(.venv)이 이 릴리스에 없습니다.", 503)
    path = _env()["PATH"]
    missing = [t for t in (*tools, "openssl") if shutil.which(t, path=path) is None]
    if missing:
        raise Refused(f"필요한 도구가 없습니다: {', '.join(sorted(set(missing)))}", 503)


def _script(name: str) -> str:
    path = SCRIPTS / name
    if not path.is_file():
        raise Refused(f"운영 스크립트가 이 릴리스에 없습니다: {name}", 503)
    return str(path)


def latest_manifest(section: str) -> Path:
    """latest(-data).json 이 가리키는 manifest. 백업 디렉터리 안의 정해진 이름만 허용한다."""
    root = backup_dir().resolve()
    body = backup_status.read_status_file(root / (backup_status.DB_STATUS_FILE if section == "db"
                                                  else backup_status.DATA_STATUS_FILE))
    raw = body.get("manifest") if body else None
    if not isinstance(raw, str):
        raise Refused("사용할 수 있는 최근 백업이 없습니다. 먼저 백업 작업을 실행하세요.")
    path = Path(raw)
    if (path.resolve().parent != root or not _MANIFEST[section].match(path.name)
            or not path.is_file() or path.is_symlink()):
        raise Refused("최근 백업 manifest 가 백업 디렉터리 밖이거나 형식이 맞지 않아 거부합니다.")
    return path.resolve()


def _manifest_encrypted(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("encrypted"))
    except (OSError, ValueError) as exc:
        raise Refused("백업 manifest 를 읽을 수 없습니다.") from exc


def make_plan(kind: str, job_id: str) -> Plan:
    if kind not in HEAVY:
        raise Refused(f"허용되지 않은 작업 종류입니다: {kind}", 422)
    short = job_id.replace("-", "")[:12]
    bash = shutil.which("bash") or "/bin/bash"
    env, timeout = _env(), TIMEOUT_SECONDS[kind]
    if kind == "backup_db":
        _need("pg_dump", "psql")
        key, source = passphrase_file(create=True)
        env["TTAKKARI_DATABASE_URL"] = get_settings().database_url
        return Plan(kind, [bash, _script("backup_db.sh"), "--passphrase-file", str(key)], env, timeout,
                    {"key_source": source})
    if kind == "data_snapshot":
        _need()
        source_dir = get_settings().data_dir.resolve()
        if not source_dir.is_dir():
            raise Refused("데이터 디렉터리가 없습니다.")
        if backup_dir().resolve() == source_dir or source_dir in backup_dir().resolve().parents:
            raise Refused("백업 디렉터리가 스냅샷 원본 안에 있어 거부합니다.")
        key, source = passphrase_file(create=True)
        return Plan(kind, [bash, _script("snapshot_data.sh"), "--source", str(source_dir),
                           "--passphrase-file", str(key)], env, timeout, {"key_source": source, "scope": "data_dir"})
    if kind == "restore_rehearsal":
        _need("pg_restore", "psql", "createdb", "dropdb")
        url, prod_db = local_database()
        target = f"{RESTORE_DB_PREFIX}{short}"
        if target == prod_db or not target.startswith(RESTORE_DB_PREFIX):
            raise Refused("복원 대상이 격리 DB 규칙에 맞지 않습니다.")
        manifest = latest_manifest("db")
        env["TTAKKARI_DATABASE_URL"] = url
        argv = [bash, _script("restore_rehearsal.sh"), "--manifest", str(manifest), "--target-db", target]
        if _manifest_encrypted(manifest):
            key, _ = passphrase_file(create=False)
            argv += ["--passphrase-file", str(key)]
        return Plan(kind, argv, env, timeout, {"target_db": target, "manifest": manifest.name})
    _need()
    manifest = latest_manifest("data")
    verify_root = (backup_dir() / VERIFY_DIR).resolve()
    target_dir = verify_root / short
    argv = [bash, _script("snapshot_data.sh"), "--verify", str(manifest), "--target", str(target_dir)]
    if _manifest_encrypted(manifest):
        key, _ = passphrase_file(create=False)
        argv += ["--passphrase-file", str(key)]
    return Plan(kind, argv, env, timeout, {"manifest": manifest.name}, cleanup_dir=target_dir)


def runner_argv(job_id: str) -> list[str]:
    return [sys.executable, "-m", "ttakkari.operations.runner", job_id]
