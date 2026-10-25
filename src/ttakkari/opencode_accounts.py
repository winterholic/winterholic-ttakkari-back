"""Managed OpenCode homes with GitHub Copilot authentication.

Keeps each account isolated in its own XDG data/config/state/cache directory so credentials
never mix. Never parses or exposes auth tokens; status is checked via official `opencode auth list`
or file presence/mtime.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ttakkari.config import get_settings

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
AUTH_STATES = {"setup_required", "authenticated", "auth_expired", "unknown"}

# Known strings indicating authentication failure during execution
AUTH_FAILURE_PATTERNS = (
    "unauthorized",
    "401",
    "token expired",
    "not authenticated",
    "login required",
    "bad credentials",
    "invalid credentials",
    "device code expired",
    "authentication failed",
    "sign in",
    "re-authenticate",
)


class OpencodeProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedOpencodeProfile:
    home: Path
    account_id: UUID

    @property
    def manifest_path(self) -> Path:
        return self.home / MANIFEST

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def auth_json_path(self) -> Path:
        return self.data_dir / "opencode" / "auth.json"


def _safe(path: Path) -> None:
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise OpencodeProfileError(f"Unsafe path (symlink or relative): {path}")
    if path.exists():
        st = path.stat()
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise OpencodeProfileError(f"Unsafe permissions on path: {path}")


def _write_manifest(profile: ManagedOpencodeProfile, data: dict) -> None:
    _safe(profile.home)
    tmp = profile.home / f".manifest-{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, profile.manifest_path)
    finally:
        tmp.unlink(missing_ok=True)


def allocate(base: Path, account_id: UUID) -> ManagedOpencodeProfile:
    """Create and return an isolated account home for an OpenCode engine account."""
    base = base.resolve()
    home = base / str(account_id) / "opencode"

    for directory in [home.parent, home, home / "data", home / "config", home / "state", home / "cache"]:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe(directory)

    profile = ManagedOpencodeProfile(home, account_id)
    if profile.manifest_path.exists():
        return load(home)

    _write_manifest(profile, {
        "version": 1,
        "account_id": str(account_id),
        "auth_state": "setup_required",
        "last_error": None,
        "last_success_at": None,
        "checked_at": time.time(),
        "principal": None,
        "cli_version": None,
    })
    return profile


def load(home: Path | str) -> ManagedOpencodeProfile:
    home = Path(home).resolve()
    _safe(home)
    manifest = home / MANIFEST
    _safe(manifest)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        aid = UUID(data["account_id"])
        if data.get("version") != 1:
            raise ValueError("Unsupported manifest version")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OpencodeProfileError("invalid_profile") from exc
    return ManagedOpencodeProfile(home, aid)


def status(home: Path | str) -> dict:
    """Return account operational status from manifest without exposing credentials."""
    p = load(home)
    try:
        data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpencodeProfileError("invalid_profile") from exc
    keys = {
        "auth_state",
        "last_error",
        "last_success_at",
        "checked_at",
        "principal",
        "cli_version",
    }
    res = {k: data.get(k) for k in keys}
    res["has_auth_file"] = p.auth_json_path.exists()
    return res


def is_ready(home: Path | str) -> bool:
    try:
        return status(home)["auth_state"] == "authenticated"
    except (OSError, ValueError):
        return False


def execution_env(home: Path | str, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Inject isolated XDG directories and secure TMPDIR for opencode execution."""
    p = load(home)
    env = (base_env or {}).copy()
    env["XDG_DATA_HOME"] = str(p.data_dir)
    env["XDG_CONFIG_HOME"] = str(p.config_dir)
    env["XDG_STATE_HOME"] = str(p.state_dir)
    env["XDG_CACHE_HOME"] = str(p.cache_dir)
    # Ensure TMPDIR is within writable paths
    tmp = Path("/Users/winterholic/development/.tmp")
    tmp.mkdir(mode=0o700, exist_ok=True)
    env["TMPDIR"] = str(tmp)
    return env


def login_command(home: Path | str) -> str:
    """Shell command for user to run interactively in a Mac Studio terminal to login via GitHub Copilot."""
    p = load(home)
    env = execution_env(p.home)
    bin_path = get_settings().opencode_bin
    exports = [
        f"TMPDIR={shlex.quote(env['TMPDIR'])}",
        f"XDG_DATA_HOME={shlex.quote(env['XDG_DATA_HOME'])}",
        f"XDG_CONFIG_HOME={shlex.quote(env['XDG_CONFIG_HOME'])}",
        f"XDG_STATE_HOME={shlex.quote(env['XDG_STATE_HOME'])}",
        f"XDG_CACHE_HOME={shlex.quote(env['XDG_CACHE_HOME'])}",
    ]
    return f"{' '.join(exports)} {shlex.quote(bin_path)} auth login -p github-copilot"


def check_auth(home: Path | str, *, cli_bin: str | None = None) -> dict:
    """Verify GitHub Copilot authentication status via official `opencode auth list` or auth.json presence.
    
    Never prints or stores tokens. Updates manifest with current status and timestamp.
    """
    p = load(home)
    data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
    binary = cli_bin or get_settings().opencode_bin
    env = execution_env(p.home, os.environ.copy())

    auth_state = "setup_required"
    cli_version = data.get("cli_version")
    last_error = None

    try:
        ver_proc = subprocess.run(
            [binary, "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if ver_proc.returncode == 0:
            cli_version = ver_proc.stdout.strip().splitlines()[-1][:32]

        list_proc = subprocess.run(
            [binary, "auth", "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        out = (list_proc.stdout + " " + list_proc.stderr).lower()
        if "github copilot" in out or "github-copilot" in out:
            auth_state = "authenticated"
        elif p.auth_json_path.exists() and p.auth_json_path.stat().st_size > 10:
            # Fallback if auth.json has content
            auth_state = "authenticated"
        else:
            auth_state = "setup_required"
    except FileNotFoundError:
        auth_state = "unknown"
        last_error = "cli_unavailable"
    except subprocess.TimeoutExpired:
        auth_state = data.get("auth_state", "unknown")
        last_error = "cli_timeout"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("OpenCode auth check failed: %s", exc)
        auth_state = "unknown"
        last_error = "check_error"

    data.update({
        "auth_state": auth_state,
        "checked_at": time.time(),
        "cli_version": cli_version,
        "last_error": last_error,
    })
    _write_manifest(p, data)
    return status(p.home)


def record_run(home: Path | str, *, success: bool, error: str | None = None) -> None:
    """Record runtime outcome. If authentication expired or failed, set auth_state to auth_expired."""
    p = load(home)
    data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
    now = time.time()
    if success:
        data.update({
            "last_success_at": now,
            "last_error": None,
        })
        _write_manifest(p, data)
        return

    text = (error or "").lower()
    if any(pat in text for pat in AUTH_FAILURE_PATTERNS):
        data.update({
            "auth_state": "auth_expired",
            "last_error": "auth_expired",
            "checked_at": now,
        })
    else:
        data.update({
            "last_error": error[:100] if error else "run_failed",
            "checked_at": now,
        })
    _write_manifest(p, data)
