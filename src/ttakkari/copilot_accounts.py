"""Managed GitHub Copilot CLI homes and account isolation.

Supports both:
1. System default account (using existing ~/.copilot authentication on host)
2. Isolated accounts (using COPILOT_HOME=<account_dir>/copilot)

Never parses or exposes secret auth tokens; status is verified via official CLI
invocation with a bounded, read-only model probe. File presence is never authentication proof.
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


def _profile(home: Path | str | None, *, is_system: bool = False, create: bool = False) -> ManagedCopilotProfile:
    if home is not None and not is_system:
        return load(home)
    # Metadata only; the host credential store is never read or rewritten here.
    base = get_settings().home / "accounts"
    if create:
        return allocate(base, UUID(int=0), is_system=True)
    return ManagedCopilotProfile(base / str(UUID(int=0)) / "copilot", UUID(int=0), is_system=True)


def status(home: Path | str | None, *, is_system: bool = False) -> dict:
    """Read nonsecret observations for both managed and host-default accounts."""
    p = _profile(home, is_system=is_system)
    has_file = p.config_json_path.is_file()
    if p.manifest_path.exists():
        _safe(p.home)
        _safe(p.manifest_path)
        try:
            data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CopilotProfileError("invalid_profile") from exc
    else:
        data = {"auth_state": "unknown" if has_file else "setup_required"}
    if data.get("auth_state") == "authenticated" and data.get("auth_evidence") not in {"model_probe", "successful_run"}:
        data["auth_state"] = "unknown"
    keys = {"auth_state", "last_error", "last_success_at", "checked_at", "principal", "cli_version"}
    return {**{key: data.get(key) for key in keys}, "has_auth_file": has_file, "is_system": p.is_system}


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

    # Token/BYOK overrides would silently defeat the selected account. Do not inspect values.
    for key in list(env):
        if key in {"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_ALLOW_ALL"} or key.startswith("COPILOT_PROVIDER_"):
            env.pop(key, None)
    env["COPILOT_AUTO_UPDATE"] = "false"
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
    """Bounded model probe; may consume one request. No config/credential contents are inspected.

    Config listing succeeds even without credentials. Only a real successful response with
    the expected marker establishes authentication. Probe tools are limited to read-only view.
    """
    import tempfile

    binary = cli_bin or get_settings().copilot_bin
    env = execution_env(home, os.environ.copy(), is_system=is_system)
    profile = _profile(home, is_system=is_system, create=True)
    data = json.loads(profile.manifest_path.read_text(encoding="utf-8"))
    previous = status(home, is_system=is_system).get("auth_state", "setup_required")
    auth_state, last_error, cli_version = "unknown", None, None
    try:
        version = subprocess.run([binary, "--version"], env=env, capture_output=True, text=True,
                                 timeout=10, check=False, stdin=subprocess.DEVNULL)
        if version.returncode == 0 and version.stdout.strip():
            cli_version = version.stdout.strip().splitlines()[-1][:64]
        with tempfile.TemporaryDirectory(prefix="ttk-copilot-auth-", dir=env["TMPDIR"]) as cwd:
            result = subprocess.run(
                [binary, "-p", "Reply exactly TTAKKARI_AUTH_OK. Do not use tools.",
                 "--output-format", "json", "--stream", "on", "--model", "auto",
                 "--allow-all-tools", "--available-tools=view", "--disable-builtin-mcps",
                 "--no-ask-user", "--no-auto-update", "--no-custom-instructions", "-C", cwd],
                env=env, capture_output=True, text=True, timeout=40, check=False, stdin=subprocess.DEVNULL)
        from ttakkari.agents.base import ParseState
        from ttakkari.agents.copilot import CopilotAdapter
        adapter, parsed = CopilotAdapter(), ParseState()
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                adapter.parse_line(event, parsed, Path(cwd))
        adapter.finalize_process(parsed, return_code=result.returncode, stderr=result.stderr)
        if not parsed.is_error and "TTAKKARI_AUTH_OK" in (parsed.result_text or ""):
            auth_state = "authenticated"
            data["last_success_at"] = time.time()
            data["auth_evidence"] = "model_probe"
        else:
            # Keep diagnostic codes only; CLI output can contain account information.
            from ttakkari.runs.profiles import is_auth_error, is_usage_limit
            combined = result.stderr + "\n" + result.stdout
            if is_auth_error(combined):
                auth_state, last_error = "auth_expired", "auth_expired"
            elif is_usage_limit(combined):
                auth_state, last_error = previous, "usage_limited"
            else:
                auth_state = previous if previous == "auth_expired" else "unknown"
                last_error = "probe_failed" if result.returncode else "probe_incomplete"
    except FileNotFoundError:
        last_error = "cli_unavailable"
    except subprocess.TimeoutExpired:
        last_error = "cli_timeout"
    except (OSError, subprocess.SubprocessError):
        last_error = "check_error"
    data.update(auth_state=auth_state, checked_at=time.time(), last_error=last_error,
                cli_version=cli_version or data.get("cli_version"))
    _write_manifest(profile, data)
    return status(home, is_system=is_system)


def record_run(home: Path | str | None, *, success: bool, error: str | None = None, is_system: bool = False) -> None:
    """Record runtime outcome. If authentication expired or failed, mark auth_state as auth_expired."""
    p = _profile(home, is_system=is_system, create=True)
    data = json.loads(p.manifest_path.read_text(encoding="utf-8"))
    now = time.time()
    if success:
        data.update({
            "auth_state": "authenticated",
            "auth_evidence": "successful_run",
            "checked_at": now,
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
