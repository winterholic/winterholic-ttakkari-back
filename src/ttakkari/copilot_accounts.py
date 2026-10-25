"""Managed GitHub Copilot CLI homes and account isolation.

Supports both:
1. System default account (using existing ~/.copilot authentication on host)
2. Isolated accounts (using COPILOT_HOME=<account_dir>/copilot)

Never parses or exposes secret auth tokens; status is verified via official CLI
invocation (`copilot config --global --list`) and safe file existence checks.
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

AUTH_FAILURE_PATTERNS = (
    "unauthorized",
    "401",
    "token expired",
    "access denied by policy settings",
    "not authenticated",
    "login required",
    "bad credentials",
    "invalid credentials",
    "device code expired",
    "authentication failed",
    "sign in",
    "re-authenticate",
)


class CopilotProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedCopilotProfile:
    home: Path
    account_id: UUID
    is_system: bool = False

    @property
    def manifest_path(self) -> Path:
        return self.home / MANIFEST

    @property
    def config_json_path(self) -> Path:
        if self.is_system:
            return Path.home() / ".copilot" / "config.json"
        return self.home / "config.json"


def _safe(path: Path) -> None:
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise CopilotProfileError(f"Unsafe path (symlink or relative): {path}")
    if path.exists():
        st = path.stat()
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise CopilotProfileError(f"Unsafe permissions on path: {path}")


def _write_manifest(profile: ManagedCopilotProfile, data: dict) -> None:
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


def allocate(base: Path, account_id: UUID, *, is_system: bool = False) -> ManagedCopilotProfile:
    """Create and return an isolated account home or system profile for Copilot CLI."""
    base = base.resolve()
    home = base / str(account_id) / "copilot"

    for directory in [home.parent, home]:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe(directory)

    profile = ManagedCopilotProfile(home, account_id, is_system=is_system)
    if profile.manifest_path.exists():
        return load(home)

    _write_manifest(profile, {
        "version": 1,
        "account_id": str(account_id),
        "is_system": is_system,
        "auth_state": "setup_required",
        "last_error": None,
        "last_success_at": None,
        "checked_at": time.time(),
        "principal": None,
        "cli_version": None,
    })
    return profile


def load(home: Path | str) -> ManagedCopilotProfile:
    home = Path(home).resolve()
    _safe(home)
    manifest = home / MANIFEST
    _safe(manifest)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        aid = UUID(data["account_id"])
        is_system = bool(data.get("is_system", False))
        if data.get("version") != 1:
            raise ValueError("Unsupported manifest version")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CopilotProfileError("invalid_profile") from exc
    return ManagedCopilotProfile(home, aid, is_system=is_system)


def status(home: Path | str | None, *, is_system: bool = False) -> dict:
    """Return operational status from manifest without exposing credentials."""
    if home is None or is_system:
        # Check system default credentials in ~/.copilot
        cfg = Path.home() / ".copilot" / "config.json"
        has_auth = cfg.exists() and cfg.stat().st_size > 5
        return {
            "auth_state": "authenticated" if has_auth else "setup_required",
            "last_error": None,
            "last_success_at": None,
            "checked_at": time.time(),
            "principal": None,
            "cli_version": None,
            "has_auth_file": has_auth,
            "is_system": True,
        }

    p = load(home)
    try:
        data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CopilotProfileError("invalid_profile") from exc

    keys = {
        "auth_state",
        "last_error",
        "last_success_at",
        "checked_at",
        "principal",
        "cli_version",
        "is_system",
    }
    res = {k: data.get(k) for k in keys}
    res["has_auth_file"] = p.config_json_path.exists()
    return res


def is_ready(home: Path | str | None, *, is_system: bool = False) -> bool:
    try:
        return status(home, is_system=is_system)["auth_state"] == "authenticated"
    except (OSError, ValueError):
        return False


def execution_env(home: Path | str | None, base_env: dict[str, str] | None = None, *, is_system: bool = False) -> dict[str, str]:
    """Inject COPILOT_HOME and secure TMPDIR for copilot execution."""
    env = (base_env or {}).copy()
    if home and not is_system:
        p = load(home)
        env["COPILOT_HOME"] = str(p.home)
    else:
        env["COPILOT_HOME"] = str(Path.home() / ".copilot")

    env["COPILOT_ALLOW_ALL"] = "true"
    tmp = Path("/Users/winterholic/development/.tmp")
    tmp.mkdir(mode=0o700, exist_ok=True)
    env["TMPDIR"] = str(tmp)
    return env


def login_command(home: Path | str | None, *, is_system: bool = False) -> str:
    """Shell command for user to run interactively in a Mac Studio terminal to login via device-code flow."""
    bin_path = get_settings().copilot_bin
    if not home or is_system:
        return f"{shlex.quote(bin_path)} login --device-code"

    p = load(home)
    return f"COPILOT_HOME={shlex.quote(str(p.home))} {shlex.quote(bin_path)} login --device-code"


def check_auth(home: Path | str | None, *, cli_bin: str | None = None, is_system: bool = False) -> dict:
    """Verify GitHub Copilot CLI authentication via `copilot config --global --list` and config.json presence.

    Never prints or stores tokens. Updates manifest with current status and timestamp.
    """
    binary = cli_bin or get_settings().copilot_bin
    env = execution_env(home, os.environ.copy(), is_system=is_system)

    auth_state = "setup_required"
    cli_version = None
    last_error = None

    # Check CLI version
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
    except FileNotFoundError:
        auth_state = "unknown"
        last_error = "cli_unavailable"
        if home and not is_system:
            p = load(home)
            data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
            data.update({"auth_state": auth_state, "last_error": last_error, "checked_at": time.time()})
            _write_manifest(p, data)
        return {"auth_state": auth_state, "last_error": last_error, "cli_version": None}
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("Copilot version check failed: %s", exc)

    # Check authentication status
    cfg_file = (Path.home() / ".copilot" / "config.json") if is_system or not home else (load(home).home / "config.json")
    has_cfg = cfg_file.exists() and cfg_file.stat().st_size > 5

    try:
        conf_proc = subprocess.run(
            [binary, "config", "--global", "--list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if conf_proc.returncode == 0 and has_cfg:
            auth_state = "authenticated"
        else:
            auth_state = "setup_required"
    except subprocess.TimeoutExpired:
        auth_state = "unknown"
        last_error = "cli_timeout"
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Copilot auth check failed: %s", exc)
        auth_state = "unknown"
        last_error = "check_error"

    if home and not is_system:
        p = load(home)
        data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
        data.update({
            "auth_state": auth_state,
            "checked_at": time.time(),
            "cli_version": cli_version or data.get("cli_version"),
            "last_error": last_error,
        })
        _write_manifest(p, data)
        return status(p.home)

    return {
        "auth_state": auth_state,
        "checked_at": time.time(),
        "cli_version": cli_version,
        "last_error": last_error,
        "has_auth_file": has_cfg,
        "is_system": True,
    }


def record_run(home: Path | str | None, *, success: bool, error: str | None = None, is_system: bool = False) -> None:
    """Record runtime outcome. If authentication expired or failed, mark auth_state as auth_expired."""
    if not home or is_system:
        return
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
