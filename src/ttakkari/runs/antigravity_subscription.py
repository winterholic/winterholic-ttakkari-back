"""One bounded Antigravity turn using the account's existing CLI login."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress

from ttakkari import antigravity_accounts
from ttakkari.config import get_settings
from ttakkari.runs.subscription_policy import antigravity_has_capacity
from ttakkari.runs.usage import Usage

MODEL = "gemini-3.8-flash-low"


async def activate_subscription(config_dir: str | None, *, preflight: Usage) -> dict:
    if not antigravity_has_capacity(preflight):
        return {"status": "preflight_failed"}

    allowed_env = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL"}
    env = {key: value for key, value in os.environ.items() if key in allowed_env}
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    if config_dir:
        try:
            env = await asyncio.to_thread(antigravity_accounts.execution_env, config_dir, env)
        except (OSError, RuntimeError, ValueError):
            return {"status": "preflight_failed"}

    settings = {
        "useG1Credits": False,
        "toolPermission": "strict",
        "allowNonWorkspaceAccess": False,
        "permissions": {"deny": [f"{action}(*)" for action in (
            "command", "read_file", "write_file", "read_url", "execute_url", "download_url", "mcp",
        )]},
    }
    command = [
        get_settings().antigravity_bin, "--print", "Reply only OK. Do not use tools.",
        "--model", MODEL, "--disable-slash-commands", "--sandbox",
        "--settings", json.dumps(settings), "--output-format", "stream-json",
        "--print-timeout", "35s", "--log-file", os.devnull,
    ]
    with tempfile.TemporaryDirectory(prefix="ttk-agy-activate-") as cwd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return {"status": "spawn_failed"}
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 40)
        except TimeoutError:
            return {"status": "unknown", "reason": "timeout_after_spawn"}
        finally:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
    if proc.returncode == 0:
        for line in reversed(stdout.splitlines()):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            result = event.get("result")
            if not isinstance(result, dict) or result.get("status") != "SUCCESS" or not result.get("response"):
                break
            raw = result.get("usage")
            tokens = {key: raw[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens")
                      if isinstance(raw, dict) and type(raw.get(key)) is int and raw[key] >= 0}
            return {"status": "succeeded", "tokens": tokens}
    return {"status": "unknown"}
