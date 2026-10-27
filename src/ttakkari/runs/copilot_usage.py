"""Read-only Copilot account quotas, shared by the OpenCode and Copilot engines.

Uses the official SDK account.getQuota RPC, never a model session or /usage prompt.
Credentials remain inside this service; RPC errors and provider bodies are not logged.
SDK contract: github-copilot-sdk 1.0.26, account quota API (experimental).
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
from uuid import UUID

from ttakkari.config import get_settings
from ttakkari.runs.usage import Usage, Window, _ts

RPC_TIMEOUT = 20.0
LABELS = {"premium_interactions": "Copilot 월간 프리미엄", "chat": "Copilot 채팅", "completions": "Copilot 코드 완성"}


def parse_quota(body: dict, *, now: float | None = None) -> Usage:
    u = Usage(source="copilot_sdk")
    observed_at = time.time() if now is None else now
    snapshots = body.get("quotaSnapshots") if isinstance(body, dict) else None
    if not isinstance(snapshots, dict):
        u.error = "Copilot 계정 사용량 미제공"
        return u
    missing = []
    # Only project known quota types: arbitrary provider keys are not user labels.
    for kind, label in LABELS.items():
        s = snapshots.get(kind)
        if s is None:
            continue
        if not isinstance(s, dict):
            missing.append(label)
            continue
        key = f"copilot_{kind}"
        if s.get("isUnlimitedEntitlement") is True or s.get("entitlementRequests") == -1:
            u.quota_unlimited[key] = label
            continue
        remaining = s.get("remainingPercentage")
        if (isinstance(remaining, bool) or not isinstance(remaining, (float, int))
                or not math.isfinite(remaining) or not 0 <= remaining <= 100):
            missing.append(label)
            continue
        percent = 100 if remaining == 0 else min(99, round(100 - remaining))
        reset = _ts(s.get("resetDate"))
        # Some runtimes return their fetch timestamp as resetDate (#2619).
        # A past instant cannot be the next reset of a newly fetched snapshot.
        # Keep the observed quota but leave its next reset unknown; never invent
        # a monthly boundary or reinterpret an old cached observation as fresh.
        if reset is not None and reset <= observed_at:
            reset = None
        u.windows.append(Window(key, label, percent, reset, float(remaining)))
    if u.windows or u.quota_unlimited:
        u.updated_at = observed_at
    if missing or not (u.windows or u.quota_unlimited):
        u.error = "Copilot 계정 사용량 미제공" + (f" · {', '.join(missing)}" if missing else "")
    return u


def _new_client(**kwargs):
    from copilot import CopilotClient, RuntimeConnection
    return CopilotClient(connection=RuntimeConnection.for_stdio(path=get_settings().copilot_bin),
                         base_directory=kwargs["env"]["COPILOT_HOME"],
                         mode="copilot-cli" if kwargs.get("use_logged_in_user") else "empty", log_level="none", **kwargs)


async def _quota_rpc(*, env: dict[str, str], token: str | None, use_logged_in_user: bool) -> dict:
    client = _new_client(env=env, working_directory=env.get("HOME"), use_logged_in_user=use_logged_in_user)
    try:
        async with asyncio.timeout(RPC_TIMEOUT):
            await client.start()
            from copilot.generated.rpc import AccountGetQuotaRequest
            result = await client.rpc.account.get_quota(AccountGetQuotaRequest(git_hub_token=token))
            return result.to_dict()
    finally:
        # No sessions were created. Tear down our child even on startup failure or cancellation.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.force_stop(), 5.0)


def _opencode_token(account_id: UUID, config_dir: str | None) -> str:
    from ttakkari import opencode_accounts as managed
    if not config_dir:
        raise ValueError("account profile required")
    p = managed.load(config_dir)
    if p.account_id != account_id:
        raise ValueError("account identity mismatch")
    managed._safe(p.auth_json_path)
    # Only this selected account's GitHub OAuth credential; never ambient GitHub/gh auth.
    body = json.loads(p.auth_json_path.read_text(encoding="utf-8"))
    auth = body.get("github-copilot") if isinstance(body, dict) else None
    token = auth.get("refresh") if isinstance(auth, dict) and auth.get("type") == "oauth" else None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Copilot OAuth missing")
    return token


async def fetch_usage(account_id: UUID, engine: str, config_dir: str | None) -> Usage:
    try:
        with tempfile.TemporaryDirectory(prefix="ttk-copilot-quota-") as scratch:
            env = {k: v for k, v in os.environ.items() if k not in {
                "GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_HOME", "GH_CONFIG_DIR",
            } and not k.startswith("COPILOT_PROVIDER_")}
            env.update(HOME=scratch, COPILOT_HOME=f"{scratch}/copilot", GH_CONFIG_DIR=f"{scratch}/gh",
                       XDG_CONFIG_HOME=f"{scratch}/config", XDG_DATA_HOME=f"{scratch}/data",
                       XDG_STATE_HOME=f"{scratch}/state", XDG_CACHE_HOME=f"{scratch}/cache",
                       COPILOT_AUTO_UPDATE="false")
            token = None
            if engine == "opencode":
                token = await asyncio.to_thread(_opencode_token, account_id, config_dir)
            elif engine == "copilot":
                from ttakkari import copilot_accounts as managed
                if config_dir:
                    p = managed.load(config_dir)
                    if p.account_id != account_id:
                        raise ValueError("account identity mismatch")
                    env = managed.execution_env(config_dir, env, is_system=p.is_system)
                else:
                    env = managed.execution_env(None, env, is_system=True)
                # The explicitly selected host account uses the OS login/keychain
                # identity. A scratch HOME makes the CLI report not authenticated.
                # Isolated accounts and OpenCode retain their scratch HOME.
                if not config_dir or p.is_system:
                    env["HOME"] = str(Path.home())
            else:
                raise ValueError("unsupported quota engine")
            return parse_quota(await _quota_rpc(env=env, token=token, use_logged_in_user=engine == "copilot"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — SDK errors may contain credentials; sanitize at the boundary.
        # No str(exc), traceback, response body, account path or token crosses this boundary.
        reason = "시간 초과" if isinstance(exc, TimeoutError) else "SDK 설치 확인" if isinstance(exc, ImportError) else "인증 또는 공급자 연결 확인"
        return Usage(source="copilot_sdk", error=f"Copilot 사용량 조회 실패 · {reason}")
