"""KB v3 L2 Subscription CLI transport.

Executes bounded, tool-free structured extraction using existing subscription CLIs
(Codex / Claude) without additional API charges, external credit overage, or tool permissions.

Key guarantees:
1. Strict tool-free invocation: read-only ephemeral sandbox, hooks/MCP/shell disabled.
2. Provenance-based pool isolation: work_only docs are strictly restricted to 'work' pool accounts;
   they never route to personal cloud.
3. Accurate error classification: nonzero_exit, timeout, usage_limit, invalid_json, schema_violation.
4. Process lifecycle cleanup: temp directory & process fencing on cancellation/timeout.
5. Privacy preservation: raw passage body is never leaked in log messages or diagnostic outputs.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.kb_pipeline.extract_l2 import L2_SYSTEM_PROMPT
from ttakkari.runs import desk, profiles
from ttakkari.runs.profiles import Profile

log = logging.getLogger(__name__)

# Diagnosis classification constants
DIAG_NONZERO_EXIT = "nonzero_exit"
DIAG_TIMEOUT = "timeout"
DIAG_USAGE_LIMIT = "usage_limit"
DIAG_INVALID_JSON = "invalid_json"
DIAG_SCHEMA_VIOLATION = "schema_violation"
DIAG_SCOPE_CAPACITY_UNAVAILABLE = "scope_capacity_unavailable"

# Patterns indicating quota / rate-limiting from subscription CLIs
_USAGE_LIMIT_PATTERNS = (
    re.compile(r"try again at", re.IGNORECASE),
    re.compile(r"resets?\s+(?:at\s+)?\d+", re.IGNORECASE),
    re.compile(r"rate\s*limit", re.IGNORECASE),
    re.compile(r"quota\s*exceeded", re.IGNORECASE),
    re.compile(r"usage\s*limit", re.IGNORECASE),
    re.compile(r"\bquota\b", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"hit the rate limit", re.IGNORECASE),
)

ALLOWED_RELATION_TYPES = frozenset({
    "part_of",
    "manages",
    "depends_on",
    "uses",
    "implements",
    "alternative_to",
    "related_to",
})

ALLOWED_MODALITIES = frozenset({"fact", "recommendation", "requirement", "deprecated"})
ALLOWED_POLARITIES = frozenset({"positive", "negative"})
ALLOWED_ENTITY_KINDS = frozenset({
    "concept",
    "technology",
    "system",
    "api",
    "symbol",
    "standard",
    "organization",
    "person",
    "document_topic",
})
ALLOWED_ENTITY_DOMAINS = frozenset({
    "dev",
    "infra",
    "ai",
    "security",
    "business",
    "science",
    "general",
})


class L2TransportError(RuntimeError):
    """Base error for L2 transport failures with structured diagnosis."""

    def __init__(self, diagnosis: str, message: str, retry_after: float | None = None,
                 *, consumed: bool = True) -> None:
        super().__init__(f"[{diagnosis}] {message}")
        self.diagnosis = diagnosis
        self.retry_after = retry_after
        self.consumed = consumed


class ScopeCapacityUnavailableError(L2TransportError):
    """Raised when no profile is available for the required scope."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(DIAG_SCOPE_CAPACITY_UNAVAILABLE, message, retry_after, consumed=False)


def diagnose_cli_error(error_text: str | None) -> str:
    """Classify CLI failure text into explicit diagnostic categories."""
    if not error_text:
        return DIAG_NONZERO_EXIT

    text = error_text.strip()
    if "시간 초과" in text or "timeout" in text.lower():
        return DIAG_TIMEOUT

    for pat in _USAGE_LIMIT_PATTERNS:
        if pat.search(text):
            return DIAG_USAGE_LIMIT

    return DIAG_NONZERO_EXIT


def retry_seconds_from_error(error_text: str | None) -> int | None:
    """Read a relative reset duration or defer to the shared profile reset parser."""
    if not error_text:
        return None
    match = re.search(r"resets?\s+in\s+([^\n.]+)", error_text, re.IGNORECASE)
    if match:
        raw = match.group(1).strip()
        duration_pattern = r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?=\d|[\s,]|$)"
        parts = list(re.finditer(duration_pattern, raw, re.IGNORECASE))
        residue = re.sub(duration_pattern, "", raw, flags=re.IGNORECASE)
        if parts and not residue.strip(" ,"):
            seconds = 0
            for part in parts:
                amount = int(part.group(1))
                unit = part.group(2).lower()[0]
                seconds += amount * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
            return seconds + 30
    parsed = profiles.reset_seconds_from(error_text)
    return parsed if parsed and parsed > 0 else None


def validate_l2_extraction_schema(data: Any) -> dict[str, Any]:
    """Strictly validate that data adheres to L2_SCHEMA.

    Rejects missing keys, illegal enum values, and missing quotes.
    Raises L2TransportError(DIAG_SCHEMA_VIOLATION) on failure.
    """
    if not isinstance(data, dict):
        raise L2TransportError(DIAG_SCHEMA_VIOLATION, "output is not a JSON object")

    root_keys = {"entities", "assertions", "relations"}
    if set(data) != root_keys:
        raise L2TransportError(DIAG_SCHEMA_VIOLATION, "unexpected or missing top-level keys")
    for key in ("entities", "assertions", "relations"):
        if key not in data or not isinstance(data[key], list):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"missing or non-list key: {key}")

    # Validate entities
    for idx, ent in enumerate(data["entities"]):
        if not isinstance(ent, dict):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"entity[{idx}] is not an object")
        for req in ("name", "aliases", "kind", "domain"):
            if req not in ent:
                raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"entity[{idx}] missing '{req}'")
        if set(ent) != {"name", "aliases", "kind", "domain"}:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"entity[{idx}] has unexpected keys")
        if not isinstance(ent["name"], str) or not ent["name"].strip():
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"entity[{idx}] invalid name")
        if not isinstance(ent["aliases"], list) or any(not isinstance(v, str) for v in ent["aliases"]):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"entity[{idx}] aliases is not a list")
        if not isinstance(ent["kind"], str) or ent["kind"] not in ALLOWED_ENTITY_KINDS:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"entity[{idx}] invalid kind '{ent['kind']}'")
        if not isinstance(ent["domain"], str) or ent["domain"] not in ALLOWED_ENTITY_DOMAINS:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"entity[{idx}] invalid domain '{ent['domain']}'")

    # Validate assertions
    for idx, ass in enumerate(data["assertions"]):
        if not isinstance(ass, dict):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] is not an object")
        for req in ("subject", "predicate", "object_value", "modality", "polarity", "evidence_quote"):
            if req not in ass:
                raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] missing '{req}'")
        if set(ass) != {"subject", "predicate", "object_entity", "object_value", "modality", "polarity", "applies_version", "evidence_quote"}:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] has unexpected keys")
        if any(not isinstance(ass[k], str) or not ass[k].strip() for k in ("subject", "predicate", "object_value", "evidence_quote")):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] has invalid text fields")
        if ass["object_entity"] is not None and not isinstance(ass["object_entity"], str):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] invalid object_entity")
        if ass["applies_version"] is not None and not isinstance(ass["applies_version"], str):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] invalid applies_version")
        if not isinstance(ass["modality"], str) or ass["modality"] not in ALLOWED_MODALITIES:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] invalid modality '{ass['modality']}'")
        if not isinstance(ass["polarity"], str) or ass["polarity"] not in ALLOWED_POLARITIES:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] invalid polarity '{ass['polarity']}'")
        if not str(ass["evidence_quote"]).strip():
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"assertion[{idx}] evidence_quote is empty")

    # Validate relations
    for idx, rel in enumerate(data["relations"]):
        if not isinstance(rel, dict):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"relation[{idx}] is not an object")
        for req in ("source", "target", "relation_type", "evidence_quote"):
            if req not in rel:
                raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"relation[{idx}] missing '{req}'")
        if set(rel) != {"source", "target", "relation_type", "evidence_quote"}:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"relation[{idx}] has unexpected keys")
        if any(not isinstance(rel[k], str) or not rel[k].strip() for k in ("source", "target", "evidence_quote")):
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"relation[{idx}] has invalid text fields")
        if not isinstance(rel["relation_type"], str) or rel["relation_type"] not in ALLOWED_RELATION_TYPES:
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"relation[{idx}] invalid relation_type '{rel['relation_type']}'")
        if not str(rel["evidence_quote"]).strip():
            raise L2TransportError(DIAG_SCHEMA_VIOLATION, f"relation[{idx}] evidence_quote is empty")

    return data


def resolve_target_pool(origin_scope: str, visibility: str) -> str | None:
    """Return an allowed cloud pool, or None when provenance must fail closed."""
    if origin_scope == "work" and visibility == "work_only":
        return "work"
    if origin_scope == "personal" and visibility == "global":
        return "personal"
    return None


async def check_scope_availability(
    db: AsyncSession,
    engine: str,
    target_pool: str,
) -> tuple[bool, str | None, float | None]:
    """Check if there is available profile capacity for the target pool."""
    free, blocked = await profiles.availability(db, session_engine=engine, pool=target_pool)
    if free:
        return True, None, None

    earliest_reset: float | None = None
    if blocked:
        earliest_reset = min(r for _, r in blocked)
        return False, DIAG_SCOPE_CAPACITY_UNAVAILABLE, earliest_reset

    return False, DIAG_SCOPE_CAPACITY_UNAVAILABLE, None


@dataclass
class SubscriptionCallConfig:
    engine: str
    model: str = ""
    effort: str = ""
    timeout_seconds: float = 90.0


async def execute_subscription_l2_call(
    prof: Profile,
    prompt: str,
    schema: dict[str, Any],
    config: SubscriptionCallConfig,
    *,
    run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Execute a single subscription CLI call with isolated ephemeral cwd and strict schema.

    Enforces load accounting in profiles._in_use during execution.
    Fences and cleans up temp resources on exit or cancellation.
    """
    lease_id = run_id or uuid.uuid4()
    profiles._in_use[lease_id] = prof.id

    cfg_override: dict[str, str] = {}
    if config.model:
        cfg_override["model"] = config.model
    if config.effort:
        cfg_override["effort"] = config.effort

    try:
        with tempfile.TemporaryDirectory(prefix="ttakkari-kb-l2-") as tmpdir:
            cwd = Path(tmpdir)
            schema_path = cwd / "schema.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")

            log.info(
                "Executing bounded subscription L2 call: engine=%s, pool=%s, timeout=%.1fs",
                prof.engine,
                prof.pool,
                config.timeout_seconds,
            )
            attempt = await desk._ask(
                prof,
                system=L2_SYSTEM_PROMPT,
                prompt=prompt,
                cfg_override=cfg_override or None,
                timeout_seconds=config.timeout_seconds,
                use_warm=False,
                output_schema=schema_path,
                cwd=cwd,
                subscription_only=True,
            )
    finally:
        profiles.release(lease_id)

    if attempt.text is None:
        diag = diagnose_cli_error(attempt.error)
        retry_after = None
        if diag == DIAG_USAGE_LIMIT:
            profiles.cool_down(prof.id, retry_seconds_from_error(attempt.error))
            retry_after = profiles.cooldown_until(prof.id)
        log.warning("Subscription L2 CLI execution failed: diagnosis=%s, elapsed_ms=%d", diag, attempt.elapsed_ms)
        # desk._ask reports a missing binary before any process starts: nothing was sent, so refund the slot.
        never_started = "실행 파일을 찾을 수 없습니다" in (attempt.error or "")
        raise L2TransportError(diag, "subscription CLI call failed", retry_after, consumed=not never_started)

    try:
        raw_obj = json.loads(attempt.text)
    except json.JSONDecodeError as exc:
        log.warning("Subscription L2 produced non-JSON output (length=%d)", len(attempt.text))
        raise L2TransportError(DIAG_INVALID_JSON, "subscription CLI returned invalid JSON") from exc

    return validate_l2_extraction_schema(raw_obj)
