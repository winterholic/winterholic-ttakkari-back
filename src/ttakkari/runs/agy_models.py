"""Per-account Antigravity model availability.

The static model catalog lists IDs observed on one account. Other accounts (older tier or CLI) can
reject an ID with "invalid model selection", so routing needs two account-scoped signals:
`agy models` run inside the account's own home, and runtime rejections. Both live in memory with a
TTL: a restart re-probes. An unknown list never excludes a model (fail open); only a known list that
lacks the ID, or a rejection the CLI actually reported, does.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ttakkari.config import get_settings

if TYPE_CHECKING:
    from ttakkari.runs.profiles import Profile

LIST_TTL = 6 * 3600
REJECT_TTL = 6 * 3600
PROBE_FAIL_TTL = 300
PROBE_TIMEOUT = 25
MAX_RECEIPT_EXCLUSIONS = 20

INVALID_MODEL_RE = re.compile(
    r"invalid model selection|is not recognized as a known model|not recognized as a known model or custom model",
    re.IGNORECASE,
)

_known: dict[uuid.UUID, tuple[float, frozenset[str]]] = {}
_probe_failed: dict[uuid.UUID, float] = {}
_rejected: dict[tuple[uuid.UUID, str], float] = {}
# (account id, model) -> receipt entry; refreshed whenever routing skips the pair.
_excluded: dict[tuple[uuid.UUID, str], dict] = {}
_locks: dict[uuid.UUID, asyncio.Lock] = {}


def is_invalid_model(*texts: str | None) -> bool:
    return any(t and INVALID_MODEL_RE.search(t) for t in texts)


def parse_models(output: str) -> frozenset[str]:
    """`agy models` prints `<id>\\t<label>` rows after a progress line."""
    return frozenset(
        line.split("\t", 1)[0].strip() for line in output.splitlines()
        if "\t" in line and line.split("\t", 1)[0].strip()
    )


def reset() -> None:
    _known.clear()
    _probe_failed.clear()
    _rejected.clear()
    _excluded.clear()
    _locks.clear()


def reject(account_id: uuid.UUID, model: str, *, account: str = "", detail: str = "") -> None:
    """Remember a model the CLI refused for this account so neither routing nor retries pick it again."""
    _rejected[(account_id, model)] = time.time() + REJECT_TTL
    _excluded[(account_id, model)] = {
        "account": account, "model": model, "reason": "cli_rejected_model", "detail": detail[:300],
    }


def _live(deadline: float | None) -> bool:
    return deadline is not None and deadline > time.time()


def unsupported_reason(profile: Profile) -> str | None:
    """Why this account cannot run profile.model, or None when it may (or cannot be told)."""
    if profile.engine != "antigravity" or not profile.model:
        return None
    key = (profile.id, profile.model)
    if _live(_rejected.get(key)):
        return "cli_rejected_model"
    known = _known.get(profile.id)
    if known and _live(known[0]) and profile.model not in known[1]:
        return "not_in_account_model_list"
    _rejected.pop(key, None)
    return None


def note_excluded(profile: Profile, reason: str) -> None:
    prior = _excluded.get((profile.id, profile.model or ""), {})
    _excluded[(profile.id, profile.model or "")] = {
        "account": profile.name, "model": profile.model, "reason": reason,
        **({"detail": prior["detail"]} if reason == prior.get("reason") and prior.get("detail") else {}),
    }


def exclusions() -> list[dict]:
    """Entries for the routing receipt: only pairs still excluded, so recovered accounts drop off."""
    out = []
    for (account_id, model), entry in list(_excluded.items()):
        known = _known.get(account_id)
        if _live(_rejected.get((account_id, model))) or (known and _live(known[0]) and model not in known[1]):
            out.append(entry)
        else:
            _excluded.pop((account_id, model), None)
    return out[:MAX_RECEIPT_EXCLUSIONS]


def _probe(profile: Profile) -> frozenset[str]:
    from ttakkari import antigravity_accounts as managed
    base = {k: v for k, v in os.environ.items() if not k.startswith("TTAKKARI_")}
    env = profile.env(base)
    cwd = managed.probe_directory(profile.config_dir) if profile.config_dir else Path("/private/tmp")
    result = subprocess.run(
        [get_settings().antigravity_bin, "models"], env=env, cwd=cwd, capture_output=True, text=True,
        timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL, check=False,
    )
    return parse_models(result.stdout) if result.returncode == 0 else frozenset()


async def ensure_known(profile: Profile) -> None:
    """Fill this account's model list once per TTL. Probe failures leave it unknown for a short while."""
    if profile.engine != "antigravity" or not get_settings().antigravity_model_probe:
        return
    known = _known.get(profile.id)
    if (known and _live(known[0])) or _live(_probe_failed.get(profile.id)):
        return
    async with _locks.setdefault(profile.id, asyncio.Lock()):
        known = _known.get(profile.id)
        if (known and _live(known[0])) or _live(_probe_failed.get(profile.id)):
            return
        try:
            models = await asyncio.to_thread(_probe, profile)
        except (OSError, ValueError, subprocess.SubprocessError):
            models = frozenset()
        if not models:
            _probe_failed[profile.id] = time.time() + PROBE_FAIL_TTL
            return
        _known[profile.id] = (time.time() + LIST_TTL, models)
        # A fresh list that includes a previously rejected model supersedes that rejection.
        for model in models:
            _rejected.pop((profile.id, model), None)
