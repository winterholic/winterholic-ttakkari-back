"""Official Codex subscription RPC and isolated activation command construction.

The read client deliberately cannot create threads or turns. It delegates credential
storage/refresh to the installed CLI; neither tokens nor raw server errors escape it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Self

from ttakkari.runs.usage import ResetCredits, Usage, Window, label_of


class RpcError(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"Codex subscription {kind}")

    @classmethod
    def from_response(cls, error: dict) -> RpcError:
        # Inspect in memory; server error messages can contain credentials/PII.
        text = str(error.get("message", "")).lower()
        if any(x in text for x in ("401", "unauthorized", "auth", "sign in", "login")):
            return cls("auth_error")
        if any(x in text for x in ("429", "rate limit", "usage limit", "quota")):
            return cls("rate_limit")
        if error.get("code") == -32601:
            return cls("unsupported_cli")
        return cls("transport_error")


def child_env(config_dir: str | None, base: dict[str, str] | None = None) -> dict[str, str]:
    # Allowlist prevents inherited provider keys, proxy endpoints, session IDs and
    # harness credentials from affecting the subscription-only child.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    env = {k: v for k, v in (base if base is not None else os.environ).items() if k in allowed}
    if config_dir:
        env["CODEX_HOME"] = str(Path(config_dir).expanduser())
    return env


def activation_command(binary: str, model: str) -> list[str]:
    cmd = [binary, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "--skip-git-repo-check", "-s", "read-only", "-m", model,
           "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
           "-c", 'approval_policy="never"', "-c", 'model_reasoning_effort="low"',
           "-c", "project_doc_max_bytes=0", "-c", "tools.update_plan.enabled=false",
           "-c", 'developer_instructions="Reply only OK. Do not use tools."']
    for feature in ("hooks", "shell_tool", "unified_exec", "web_search_request", "standalone_web_search",
                    "request_permissions_tool", "sleep_tool", "view_image", "collab", "multi_agent_v2",
                    "apps", "enable_mcp_apps", "tool_suggest", "plugins", "code_mode", "deferred_executor",
                    "apply_patch_freeform"):
        cmd.extend(("--disable", feature))
    return cmd + ["Reply only OK. Do not use tools."]


def parse_limits(body: dict[str, Any], *, now: float | None = None) -> Usage:
    buckets = body.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(bucket, dict):
        bucket = body.get("rateLimits") or {}
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        bucket = {}
    windows = []
    for part in ("primary", "secondary"):
        w = bucket.get(part)
        if not isinstance(w, dict):
            continue
        percent, minutes = w.get("usedPercent"), w.get("windowDurationMins")
        if (isinstance(percent, bool) or not isinstance(percent, (int, float))
                or not math.isfinite(percent) or not 0 <= percent <= 100):
            continue
        # Do not guess based on primary/secondary position or unknown durations.
        key = {300: "five_hour", 10080: "seven_day"}.get(minutes) if type(minutes) is int else None
        if key is None:
            continue
        reset = w.get("resetsAt")
        reset = reset if type(reset) is int and reset > 0 else None
        # Preserve a tiny non-zero observation for skip/limit decisions.
        windows.append(Window(key, label_of(key), math.ceil(percent), reset, 100 - percent))
    allowed = body.get("ordinaryUsageAllowed")
    credits = body.get("rateLimitResetCredits")
    credits = credits if isinstance(credits, dict) else {}
    count = credits.get("availableCount")
    return Usage(windows=sorted(windows, key=lambda w: w.key), source="api", plan=bucket.get("planType"),
                 updated_at=time.time() if now is None else now, login="ok",
                 reset_credits=ResetCredits(count, None) if type(count) is int and count >= 0 else None,
                 ordinary_usage_allowed=allowed if type(allowed) is bool else None)


class RpcClient:
    """One bounded stdio connection; no shared app-server daemon or session mutation."""

    def __init__(self, command: list[str], *, env: dict[str, str], cwd: Path, timeout: float = 20) -> None:
        self.command, self.env, self.cwd, self.timeout = command, env, cwd, timeout
        self.proc: asyncio.subprocess.Process | None = None
        self.seq = 0
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = b""

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while chunk := await self.proc.stderr.read(4096):
            self._stderr_tail = (self._stderr_tail + chunk)[-8192:]

    async def __aenter__(self) -> Self:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1024 * 1024)
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            await self.call("initialize", {"clientInfo": {
                "name": "ttakkari_subscription_monitor", "title": "Ttakkari", "version": "1.0"}})
            await self._send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def _send(self, message: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def call(self, method: str, params: dict | None = None) -> dict:
        if method not in {"initialize", "account/read", "account/rateLimits/read"}:
            raise ValueError("read-only Codex RPC method required")
        self.seq += 1
        request_id = self.seq
        async with asyncio.timeout(self.timeout):
            await self._send({"id": request_id, "method": method, "params": params or {}})
            assert self.proc and self.proc.stdout
            while line := await self.proc.stdout.readline():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise RpcError("invalid_response") from None
                if not isinstance(message, dict):
                    raise RpcError("invalid_response")
                if message.get("id") != request_id:
                    # Refuse any server-initiated request; account reads need none.
                    if "method" in message and "id" in message:
                        raise RpcError("unexpected_server_request")
                    continue
                if "error" in message:
                    raise RpcError.from_response(message["error"])
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RpcError("invalid_response")
                return result
        await self.proc.wait()
        if self._stderr_task:
            await self._stderr_task
        tail = self._stderr_tail.decode(errors="replace").lower()
        kind = "process_exited"
        if any(s in tail for s in ("operation not permitted", "permission denied", "read-only file system")):
            kind = "sandbox_denied"
        elif any(s in tail for s in ("unexpected argument", "unrecognized", "unknown variant", "failed to load configuration")):
            kind = "configuration_error"
        raise RpcError(f"{kind} (exit {self.proc.returncode})")

    async def __aexit__(self, *exc) -> None:
        if self.proc is None:
            return
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            await self.proc.wait()
        if self._stderr_task:
            await self._stderr_task


async def read_usage(config_dir: str | None, *, binary: str | None = None, refresh_token: bool = False) -> Usage:
    """Read subscription identity/limits without starting a model turn.

    CLI-owned account/read may refresh credentials. A missing/unsupported login is
    explicit; historical logs are never treated as a current reset confirmation.
    """
    from ttakkari.config import get_settings

    command = [binary or get_settings().codex_bin, "app-server", "--listen", "stdio://",
               "-c", 'model_provider="openai"']
    try:
        with tempfile.TemporaryDirectory(prefix="ttk-codex-read-") as root:
            async with RpcClient(command, env=child_env(config_dir), cwd=Path(root)) as rpc:
                identity = await rpc.call("account/read", {"refreshToken": refresh_token})
                account = identity.get("account") or {}
                if not isinstance(account, dict):
                    raise RpcError("invalid_response")
                if account.get("type") != "chatgpt":
                    return Usage(error="Codex subscription auth_error: ChatGPT 구독 로그인이 필요해요", login="missing")
                body = await rpc.call("account/rateLimits/read", {"excludeResetCreditDetails": True})
                u = parse_limits(body)
                u.plan = u.plan or account.get("planType")
                if not u.windows:
                    u.error = "Codex subscription quota_unavailable: 사용량 창 미관측"
                return u
    except RpcError as exc:
        return Usage(error=str(exc), login="missing" if exc.kind == "auth_error" else None)
    except (OSError, TimeoutError, ValueError) as exc:
        # Exception text/child stderr can contain tokens or user configuration.
        return Usage(error=f"Codex subscription transport_error ({type(exc).__name__})")


async def activate_subscription(config_dir: str | None, *, preflight: Usage | None = None) -> dict:
    """One bounded, isolated CLI turn. Only pre-spawn failures may be retried.

    Timeout/transport errors after spawn are uncertain and must never replay.
    The caller persists its cycle marker before entering this function.
    """
    from ttakkari.config import get_settings

    u = preflight if preflight is not None else await read_usage(config_dir)
    if u.login != "ok" or u.error:
        return {"status": "auth_error" if u.login == "missing" else "preflight_failed"}
    if u.ordinary_usage_allowed is not True:
        return {"status": "included_usage_unavailable"}
    if any(w.percent >= 100 for w in u.windows):
        return {"status": "rate_limit"}
    proc = None
    # Empty cwd, no inherited config/MCP/hooks/rules/session, no API credentials.
    with tempfile.TemporaryDirectory(prefix="ttk-codex-activate-") as root:
        try:
            for attempt in range(2):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *activation_command(get_settings().codex_bin, "gpt-6-luna"),
                        cwd=Path(root), env=child_env(config_dir), stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    break
                except OSError:
                    if attempt:
                        return {"status": "spawn_failed", "attempts": 2}
                    await asyncio.sleep(1)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 60)
            completed, error, tokens = False, None, {}
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "turn.completed":
                    completed = True
                    raw = event.get("usage") or {}
                    tokens = {k: v for k, v in raw.items() if k in (
                        "input_tokens", "cached_input_tokens", "output_tokens") and type(v) is int and v >= 0}
                elif event.get("type") in ("turn.failed", "error"):
                    raw = event.get("error") or event
                    error = RpcError.from_response(raw if isinstance(raw, dict) else {"message": str(raw)}).kind
            if proc.returncode == 0 and completed and error is None:
                return {"status": "succeeded", "tokens": tokens}
            kind = error or RpcError.from_response({"message": stderr.decode(errors="replace")}).kind
            return {"status": kind if kind in ("auth_error", "rate_limit") else "unknown"}
        except TimeoutError:
            return {"status": "unknown", "reason": "timeout_after_spawn"}
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
