"""Collector Platform: Ingestion Envelope and Budget Policy contracts, RFC 8785 JCS, and scope binding."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


# Error hierarchy. The siblings are deliberately independent: a 403 scope mismatch must never be
# caught by an `except CollectorContractError` (422) handler, and vice versa.
class CollectorError(Exception):
    """Base of every defined collector failure. `http_status`/`error_code` are the API mapping S2-02 adopts."""

    http_status = 500
    error_code = "collector_error"


class CollectorContractError(CollectorError, ValueError):
    """Raised when an ingestion envelope or policy violates schema or semantic contract (HTTP 422)."""

    http_status = 422
    error_code = "invalid_envelope"


class ScopeBindingMismatchError(CollectorError):
    """Raised when envelope access scope/tenant/principal does not match authenticated context (HTTP 403)."""

    http_status = 403
    error_code = "scope_binding_mismatch"


class CollectorConflictError(CollectorError):
    """Raised when a submission collides with an existing registration (HTTP 409)."""

    http_status = 409
    error_code = "idempotency_conflict"


class EnvelopeIdConflictError(CollectorConflictError):
    """The envelope_id is already registered in this tenant/scope under another request key (HTTP 409)."""

    error_code = "envelope_id_conflict"


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    scope: str  # "personal", "work", "shared" (derived from session.pool)
    principal_id: str
    principal_ids: tuple[str, ...] = ()
    is_admin: bool = False
    kind: str = "session"

    def __post_init__(self):
        if not self.principal_ids:
            object.__setattr__(self, "principal_ids", (self.principal_id,))


# Regex patterns & enums
HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EXTENSION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$")
ALLOWED_SCOPES = {"personal", "work", "shared"}
ALLOWED_SOURCES = {"web", "api", "feed", "file", "video", "fixture"}
ALLOWED_SENSITIVITIES = {"normal", "sensitive", "restricted"}
ALLOWED_PERMISSIONS = {"allowed", "unknown", "denied"}
ALLOWED_PURPOSES = {"store", "train", "export", "knowledge"}
ALLOWED_ROBOTS = {"allowed", "disallowed", "unavailable", "not_applicable"}
ALLOWED_FACT_STATUS = {"unknown", "supported", "refuted", "conflicting", "not_applicable"}
ALLOWED_CANONICAL_STATUS = {"unreviewed", "verified", "disputed"}
ALLOWED_DECISION_STATES = {"accept", "hold", "blocked", "error"}
ALLOWED_AXIS_STATUS = {"unmeasured", "measured", "error"}
ALLOWED_AXIS_SCALES = {"raw_logit", "ordinal", "rule", "similarity", "none"}
QUALITY_AXES = ("education", "utility", "spam", "novelty", "factuality", "relevance")


MAX_EXACT_INT = 2**53
MAX_JSON_DEPTH = 64


def _jcs_string(v: str) -> str:
    """A lone surrogate has no UTF-8 form, so it cannot be hashed; reject it as a contract error."""
    try:
        v.encode("utf-8")
    except UnicodeEncodeError as e:
        raise CollectorContractError("string contains a lone surrogate and cannot be encoded as UTF-8") from e
    return json.dumps(v, ensure_ascii=False)


# RFC 8785 JSON Canonicalization Scheme (JCS)
def _jcs_number(v: float) -> str:
    """RFC 8785 3.2.2.3: ECMAScript Number.prototype.toString on the shortest round-trip digits."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CollectorContractError(f"Expected number, got {type(v).__name__}")
    if isinstance(v, int):
        if abs(v) > MAX_EXACT_INT:
            raise CollectorContractError(f"integer {v} is outside the IEEE-754 exact range (|n| <= 2**53)")
        return str(v)
    if not math.isfinite(v):
        raise CollectorContractError("non-finite number is not representable in JSON")
    if v == 0:
        return "0"
    sign = "-" if v < 0 else ""
    t = Decimal(repr(abs(v))).normalize().as_tuple()
    d = "".join(map(str, t.digits))
    k = len(d)
    n = t.exponent + k
    if k <= n <= 21:
        out = d + "0" * (n - k)
    elif 0 < n <= 21:
        out = d[:n] + "." + d[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + d
    else:
        e = n - 1
        out = d[0] + ("." + d[1:] if k > 1 else "") + "e" + ("+" if e > 0 else "-") + str(abs(e))
    return sign + out


def jcs(o: Any) -> str:
    """RFC 8785 JSON Canonicalization Scheme (JCS). UTF-16 code unit ordering, ECMAScript numbers, UTF-8."""
    return _jcs(o, 0)


def _jcs(o: Any, depth: int) -> str:
    if depth > MAX_JSON_DEPTH:
        raise CollectorContractError(f"JSON nesting deeper than {MAX_JSON_DEPTH} levels")
    if o is None or isinstance(o, bool):
        return json.dumps(o)
    if isinstance(o, (int, float)):
        return _jcs_number(o)
    if isinstance(o, str):
        return _jcs_string(o)
    if isinstance(o, list):
        return "[" + ",".join(_jcs(v, depth + 1) for v in o) + "]"
    if isinstance(o, dict):
        if not all(isinstance(k, str) for k in o):
            raise CollectorContractError("object keys must be strings")
        quoted = {k: _jcs_string(k) for k in o}
        keys = sorted(o, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(quoted[k] + ":" + _jcs(o[k], depth + 1) for k in keys) + "}"
    raise CollectorContractError(f"Object of type {type(o).__name__} is not JSON serializable")


JCS_VECTORS = [
    (0.0, "0"),
    (-0.0, "0"),
    (1.0, "1"),
    (1e-7, "1e-7"),
    (1e21, "1e+21"),
    (1e20, "100000000000000000000"),
    (0.000001, "0.000001"),
    (333333333.3333333, "333333333.3333333"),
    (-1.5e-10, "-1.5e-10"),
    (4.5, "4.5"),
    (2e-3, "0.002"),
]


def parse_iso_datetime(t: str) -> datetime:
    """Parse an ISO 8601 string, strictly requiring timezone information."""
    if not isinstance(t, str):
        raise CollectorContractError(f"Expected datetime string, got {type(t).__name__}")
    try:
        normalized_str = t.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized_str)
    except Exception as e:
        raise CollectorContractError(f"Invalid date-time format '{t}': {e}") from e

    if dt.tzinfo is None:
        raise CollectorContractError(f"Timezone offset missing in datetime '{t}'")
    return dt


def _reject_nul(o: Any) -> None:
    """PostgreSQL text/JSONB cannot store U+0000; it is valid JSON, so reject it before the INSERT fails."""
    if isinstance(o, str):
        if "\x00" in o:
            raise CollectorContractError("string contains U+0000, which the collector store cannot hold")
    elif isinstance(o, list):
        for v in o:
            _reject_nul(v)
    elif isinstance(o, dict):
        for k, v in o.items():
            _reject_nul(k)
            _reject_nul(v)


def _not_in(value: Any, allowed: set[str]) -> bool:
    return not (isinstance(value, str) and value in allowed)


def _check_keys(d: dict[str, Any], required: set[str], optional: set[str], context: str) -> None:
    if not isinstance(d, dict):
        raise CollectorContractError(f"Expected object for {context}, got {type(d).__name__}")
    keys = set(d.keys())
    missing = required - keys
    if missing:
        raise CollectorContractError(f"Missing required properties {sorted(missing)} in {context}")
    unexpected = keys - (required | optional)
    if unexpected:
        raise CollectorContractError(
            f"Additional properties are not allowed ({sorted(unexpected)} unexpected) in {context}"
        )


def validate_envelope(x: dict[str, Any]) -> dict[str, Any]:
    """Strict JSON schema and semantic validator for Ingestion Envelope v1.0.0."""
    if not isinstance(x, dict):
        raise CollectorContractError(f"Envelope must be a JSON object, got {type(x).__name__}")
    # Canonicalizing up front rejects lone surrogates, integers beyond 2**53, NaN/Infinity and runaway nesting
    # as contract errors before any validator below calls .encode() or compares values.
    jcs(x)
    _reject_nul(x)

    required_root = {
        "schema_version",
        "envelope_id",
        "producer",
        "recorded_at",
        "source",
        "access",
        "rights",
        "raw",
        "normalized",
        "quality",
        "fact_check_status",
        "dedupe",
        "decision",
        "errors",
        "lineage",
        "extensions",
    }
    _check_keys(x, required_root, set(), "root envelope")

    if x["schema_version"] != "1.0.0":
        raise CollectorContractError(f"'1.0.0' was expected for schema_version, got '{x['schema_version']}'")

    try:
        uuid.UUID(str(x["envelope_id"]))
    except Exception as e:
        raise CollectorContractError(f"envelope_id must be a valid UUID: {e}") from e

    _check_keys(x["producer"], {"id", "version"}, set(), "producer")
    if not (isinstance(x["producer"]["id"], str) and x["producer"]["id"]):
        raise CollectorContractError("producer.id must be non-empty string")
    if not (isinstance(x["producer"]["version"], str) and x["producer"]["version"]):
        raise CollectorContractError("producer.version must be non-empty string")

    recorded_at = parse_iso_datetime(x["recorded_at"])

    _check_keys(
        x["source"],
        {"key", "kind", "url", "collected_at", "published_at", "valid_from", "valid_to"},
        set(),
        "source",
    )
    if not (isinstance(x["source"]["key"], str) and x["source"]["key"]):
        raise CollectorContractError("source.key must be non-empty string")
    if _not_in(x["source"]["kind"], ALLOWED_SOURCES):
        raise CollectorContractError(f"source.kind '{x['source']['kind']}' not in {sorted(ALLOWED_SOURCES)}")
    if x["source"]["url"] is not None and not isinstance(x["source"]["url"], str):
        raise CollectorContractError("source.url must be string or null")

    collected_at = parse_iso_datetime(x["source"]["collected_at"])
    if x["source"]["published_at"] is not None:
        parse_iso_datetime(x["source"]["published_at"])

    v_from_str = x["source"]["valid_from"]
    v_to_str = x["source"]["valid_to"]
    v_from = parse_iso_datetime(v_from_str) if v_from_str is not None else None
    v_to = parse_iso_datetime(v_to_str) if v_to_str is not None else None

    if v_from and v_to and v_from >= v_to:
        raise CollectorContractError(f"source.valid_to ({v_to_str}) must be later than source.valid_from ({v_from_str})")
    if collected_at > recorded_at:
        raise CollectorContractError("recorded_at cannot be earlier than source.collected_at")

    _check_keys(x["access"], {"tenant_id", "scope", "principal_ids", "sensitivity"}, set(), "access")
    if not (isinstance(x["access"]["tenant_id"], str) and x["access"]["tenant_id"]):
        raise CollectorContractError("access.tenant_id must be non-empty string")
    if _not_in(x["access"]["scope"], ALLOWED_SCOPES):
        raise CollectorContractError(f"'{x['access']['scope']}' is not one of {sorted(ALLOWED_SCOPES)}")
    if not isinstance(x["access"]["principal_ids"], list) or len(x["access"]["principal_ids"]) == 0:
        raise CollectorContractError("access.principal_ids must be non-empty array")
    for pid in x["access"]["principal_ids"]:
        if not (isinstance(pid, str) and pid):
            raise CollectorContractError("access.principal_ids items must be non-empty string")
    if len(set(x["access"]["principal_ids"])) != len(x["access"]["principal_ids"]):
        raise CollectorContractError("access.principal_ids items must be unique")
    if _not_in(x["access"]["sensitivity"], ALLOWED_SENSITIVITIES):
        raise CollectorContractError(f"access.sensitivity '{x['access']['sensitivity']}' not in {sorted(ALLOWED_SENSITIVITIES)}")

    _check_keys(
        x["rights"],
        {
            "license_id",
            "policy_version",
            "policy_url",
            "checked_at",
            "permission",
            "purposes",
            "robots",
            "basis",
            "retain_until",
        },
        set(),
        "rights",
    )
    if x["rights"]["license_id"] is not None and not isinstance(x["rights"]["license_id"], str):
        raise CollectorContractError("rights.license_id must be string or null")
    if not (isinstance(x["rights"]["policy_version"], str) and x["rights"]["policy_version"]):
        raise CollectorContractError("rights.policy_version must be non-empty string")
    if x["rights"]["policy_url"] is not None and not isinstance(x["rights"]["policy_url"], str):
        raise CollectorContractError("rights.policy_url must be string or null")
    parse_iso_datetime(x["rights"]["checked_at"])
    if _not_in(x["rights"]["permission"], ALLOWED_PERMISSIONS):
        raise CollectorContractError(f"rights.permission '{x['rights']['permission']}' not in {sorted(ALLOWED_PERMISSIONS)}")
    if not isinstance(x["rights"]["purposes"], list):
        raise CollectorContractError("rights.purposes must be array")
    for purp in x["rights"]["purposes"]:
        if _not_in(purp, ALLOWED_PURPOSES):
            raise CollectorContractError(f"rights.purpose '{purp}' not in {sorted(ALLOWED_PURPOSES)}")
    if len(set(x["rights"]["purposes"])) != len(x["rights"]["purposes"]):
        raise CollectorContractError("rights.purposes items must be unique")
    if _not_in(x["rights"]["robots"], ALLOWED_ROBOTS):
        raise CollectorContractError(f"rights.robots '{x['rights']['robots']}' not in {sorted(ALLOWED_ROBOTS)}")
    if not (isinstance(x["rights"]["basis"], str) and x["rights"]["basis"]):
        raise CollectorContractError("rights.basis must be non-empty string")
    if x["rights"]["retain_until"] is not None:
        parse_iso_datetime(x["rights"]["retain_until"])

    _check_keys(x["raw"], {"sha256", "byte_size", "media_type", "blob_handle"}, set(), "raw")
    if not (isinstance(x["raw"]["sha256"], str) and HEX_64_PATTERN.match(x["raw"]["sha256"])):
        raise CollectorContractError(f"raw.sha256 '{x['raw']['sha256']}' must be 64-character lowercase hex")
    if not (isinstance(x["raw"]["byte_size"], int) and x["raw"]["byte_size"] >= 0):
        raise CollectorContractError("raw.byte_size must be non-negative integer")
    if not (isinstance(x["raw"]["media_type"], str) and x["raw"]["media_type"]):
        raise CollectorContractError("raw.media_type must be non-empty string")
    if not (isinstance(x["raw"]["blob_handle"], str) and x["raw"]["blob_handle"]):
        raise CollectorContractError("raw.blob_handle must be non-empty string")

    if x["normalized"] is not None:
        _check_keys(
            x["normalized"],
            {
                "text",
                "sha256",
                "language",
                "parser_version",
                "normalizer_version",
                "chunker_version",
                "raw_alignment_handle",
                "truncated",
            },
            set(),
            "normalized",
        )
        if not isinstance(x["normalized"]["text"], str):
            raise CollectorContractError("normalized.text must be string")
        if not (isinstance(x["normalized"]["sha256"], str) and HEX_64_PATTERN.match(x["normalized"]["sha256"])):
            raise CollectorContractError(f"normalized.sha256 '{x['normalized']['sha256']}' must be 64-char lowercase hex")
        if not (isinstance(x["normalized"]["language"], str) and x["normalized"]["language"]):
            raise CollectorContractError("normalized.language must be non-empty string")
        if not (isinstance(x["normalized"]["parser_version"], str) and x["normalized"]["parser_version"]):
            raise CollectorContractError("normalized.parser_version must be non-empty string")
        if not (isinstance(x["normalized"]["normalizer_version"], str) and x["normalized"]["normalizer_version"]):
            raise CollectorContractError("normalized.normalizer_version must be non-empty string")
        if not (isinstance(x["normalized"]["chunker_version"], str) and x["normalized"]["chunker_version"]):
            raise CollectorContractError("normalized.chunker_version must be non-empty string")
        if x["normalized"]["raw_alignment_handle"] is not None and not isinstance(
            x["normalized"]["raw_alignment_handle"], str
        ):
            raise CollectorContractError("normalized.raw_alignment_handle must be string or null")
        if not isinstance(x["normalized"]["truncated"], bool):
            raise CollectorContractError("normalized.truncated must be boolean")

        computed_text_hash = hashlib.sha256(x["normalized"]["text"].encode("utf-8")).hexdigest()
        if computed_text_hash != x["normalized"]["sha256"]:
            raise CollectorContractError("normalized.sha256 text mismatch")

    _check_keys(x["quality"], set(QUALITY_AXES), set(), "quality")
    axis_req = {
        "status",
        "value",
        "scale",
        "model_id",
        "model_revision",
        "preprocess_version",
        "calibration_id",
        "calibration_gold_hash",
        "calibrated_probability",
        "query_id",
        "evidence",
    }
    for q_name in QUALITY_AXES:
        a = x["quality"][q_name]
        _check_keys(a, axis_req, set(), f"quality.{q_name}")
        if _not_in(a["status"], ALLOWED_AXIS_STATUS):
            raise CollectorContractError(f"quality.{q_name}.status '{a['status']}' not in {sorted(ALLOWED_AXIS_STATUS)}")
        if _not_in(a["scale"], ALLOWED_AXIS_SCALES):
            raise CollectorContractError(f"quality.{q_name}.scale '{a['scale']}' not in {sorted(ALLOWED_AXIS_SCALES)}")
        if not (isinstance(a["preprocess_version"], str) and a["preprocess_version"]):
            raise CollectorContractError(f"quality.{q_name}.preprocess_version must be non-empty string")
        if not isinstance(a["evidence"], list):
            raise CollectorContractError(f"quality.{q_name}.evidence must be array")

        if a["status"] == "measured":
            if not isinstance(a["value"], (int, float)):
                raise CollectorContractError(f"quality.{q_name}.value must be number when measured")
            if a["scale"] == "none":
                raise CollectorContractError(f"quality.{q_name} measured scale/query missing")
            if not (isinstance(a["model_id"], str) and a["model_id"]):
                raise CollectorContractError(f"quality.{q_name}.model_id required when measured")
            if not (isinstance(a["model_revision"], str) and a["model_revision"]):
                raise CollectorContractError(f"quality.{q_name}.model_revision required when measured")
            if q_name == "relevance" and not a["query_id"]:
                raise CollectorContractError("quality.relevance measured scale/query missing")
        elif a["status"] == "unmeasured":
            if a["value"] is not None:
                raise CollectorContractError(f"quality.{q_name}.value must be null when unmeasured")
            if a["calibrated_probability"] is not None:
                raise CollectorContractError(f"quality.{q_name}.calibrated_probability must be null when unmeasured")

        if a["calibrated_probability"] is not None:
            if a["status"] != "measured":
                raise CollectorContractError(f"quality.{q_name} probability without measurement")
            if not isinstance(a["calibrated_probability"], (int, float)) or not (0 <= a["calibrated_probability"] <= 1):
                raise CollectorContractError(f"quality.{q_name}.calibrated_probability must be in range [0, 1]")
            if not (isinstance(a["calibration_id"], str) and a["calibration_id"]):
                raise CollectorContractError(f"quality.{q_name}.calibration_id must be non-empty string when probability is calibrated")
            if not (isinstance(a["calibration_gold_hash"], str) and HEX_64_PATTERN.match(a["calibration_gold_hash"])):
                raise CollectorContractError(f"quality.{q_name}.calibration_gold_hash must be 64-char hex when probability is calibrated")

    if _not_in(x["fact_check_status"], ALLOWED_FACT_STATUS):
        raise CollectorContractError(f"fact_check_status '{x['fact_check_status']}' not in {sorted(ALLOWED_FACT_STATUS)}")
    if x["fact_check_status"] in ("supported", "refuted", "conflicting") and not x["quality"]["factuality"]["evidence"]:
        raise CollectorContractError("factuality evidence missing")

    _check_keys(
        x["dedupe"],
        {"exact_of", "cluster_id", "canonical_candidate", "canonical_status", "algorithm_version"},
        set(),
        "dedupe",
    )
    if _not_in(x["dedupe"]["canonical_status"], ALLOWED_CANONICAL_STATUS):
        raise CollectorContractError(f"dedupe.canonical_status '{x['dedupe']['canonical_status']}' invalid")
    if not (isinstance(x["dedupe"]["algorithm_version"], str) and x["dedupe"]["algorithm_version"]):
        raise CollectorContractError("dedupe.algorithm_version must be non-empty string")

    _check_keys(x["decision"], {"state", "reason_codes", "policy_version"}, set(), "decision")
    if _not_in(x["decision"]["state"], ALLOWED_DECISION_STATES):
        raise CollectorContractError(f"decision.state '{x['decision']['state']}' invalid")
    if not isinstance(x["decision"]["reason_codes"], list) or len(x["decision"]["reason_codes"]) == 0:
        raise CollectorContractError("decision.reason_codes must be non-empty array")
    if not (isinstance(x["decision"]["policy_version"], str) and x["decision"]["policy_version"]):
        raise CollectorContractError("decision.policy_version must be non-empty string")

    if x["decision"]["state"] == "accept":
        if x["normalized"] is None:
            raise CollectorContractError("normalized object required when decision is accept")
        if x["rights"]["permission"] != "allowed":
            raise CollectorContractError("'allowed' was expected for rights.permission when decision is accept")
        if "store" not in x["rights"]["purposes"]:
            raise CollectorContractError("'store' purpose required when decision is accept")
        if x["rights"]["robots"] in ("disallowed", "unavailable"):
            raise CollectorContractError("robots blocks acceptance")

    if not isinstance(x["errors"], list):
        raise CollectorContractError("errors must be an array")
    if x["decision"]["state"] == "error" and len(x["errors"]) == 0:
        raise CollectorContractError("[] should be non-empty in errors when decision is error")
    for err in x["errors"]:
        _check_keys(err, {"stage", "code", "retryable", "not_before", "detail"}, set(), "errors item")
        if not (isinstance(err["stage"], str) and err["stage"]):
            raise CollectorContractError("error.stage must be non-empty string")
        if not (isinstance(err["code"], str) and err["code"]):
            raise CollectorContractError("error.code must be non-empty string")
        if not isinstance(err["retryable"], bool):
            raise CollectorContractError("error.retryable must be boolean")
        if err["not_before"] is not None:
            parse_iso_datetime(err["not_before"])
        if not (isinstance(err["detail"], str) and err["detail"]):
            raise CollectorContractError("error.detail must be non-empty string")

    if not isinstance(x["lineage"], list):
        raise CollectorContractError("lineage must be array")

    if not isinstance(x["extensions"], dict):
        raise CollectorContractError("extensions must be an object")
    for ext_key in x["extensions"]:
        if not EXTENSION_KEY_PATTERN.match(ext_key):
            raise CollectorContractError(f"Extension property name '{ext_key}' does not match namespaced pattern")

    if "fixture:raw_text" in x["extensions"]:
        if not isinstance(x["extensions"]["fixture:raw_text"], str):
            raise CollectorContractError("extensions.fixture:raw_text must be string")
        b =x["extensions"]["fixture:raw_text"].encode("utf-8")
        if hashlib.sha256(b).hexdigest() != x["raw"]["sha256"] or len(b) != x["raw"]["byte_size"]:
            raise CollectorContractError("raw.sha256/byte_size mismatch with fixture:raw_text")

    return x


def validate_budget_policy(x: dict[str, Any]) -> dict[str, Any]:
    """Strict JSON schema and semantic validator for Budget Policy v1.0.0."""
    if not isinstance(x, dict):
        raise CollectorContractError("Budget policy must be a JSON object")

    required = {
        "schema_version",
        "timezone",
        "enabled",
        "day",
        "job",
        "request",
        "sampling",
        "unknown_usage_policy",
        "unbounded_engine_policy",
        "cache_scope",
        "retries_charge_budget",
    }
    _check_keys(x, required, set(), "budget policy")

    if x["schema_version"] != "1.0.0":
        raise CollectorContractError(f"Expected schema_version '1.0.0', got '{x['schema_version']}'")
    if x["timezone"] != "Asia/Seoul":
        raise CollectorContractError(f"Expected timezone 'Asia/Seoul', got '{x['timezone']}'")
    if not isinstance(x["enabled"], bool):
        raise CollectorContractError("enabled must be boolean")

    _check_keys(x["day"], {"tokens", "calls", "usd"}, set(), "budget day")
    _check_keys(x["job"], {"tokens", "calls", "usd"}, set(), "budget job")
    _check_keys(x["request"], {"input_tokens", "output_and_reasoning_tokens"}, set(), "budget request")
    _check_keys(x["sampling"], {"daily_absolute_documents", "strategy"}, set(), "budget sampling")

    for k in ("tokens", "calls", "usd"):
        if x["job"][k] > x["day"][k]:
            raise CollectorContractError(f"job.{k} exceeds day.{k}")

    req_tokens = x["request"]["input_tokens"] + x["request"]["output_and_reasoning_tokens"]
    if req_tokens > x["job"]["tokens"]:
        raise CollectorContractError("request exceeds job.tokens")

    if x["sampling"]["strategy"] != "stratified_reservoir":
        raise CollectorContractError("sampling.strategy must be 'stratified_reservoir'")
    if x["unknown_usage_policy"] != "keep_full_reservation":
        raise CollectorContractError("unknown_usage_policy must be 'keep_full_reservation'")
    if x["unbounded_engine_policy"] != "disabled":
        raise CollectorContractError("unbounded_engine_policy must be 'disabled'")
    if x["cache_scope"] != "tenant_scope_hash_model_prompt_policy_query":
        raise CollectorContractError("cache_scope must be 'tenant_scope_hash_model_prompt_policy_query'")
    if x["retries_charge_budget"] is not True:
        raise CollectorContractError("retries_charge_budget must be true")

    return x


def compute_payload_hash(envelope: dict[str, Any], excluded_paths: tuple[str, ...] = ("/recorded_at",)) -> str:
    """Compute deterministic canonical sha256 hash using RFC 8785 JSON Canonicalization Scheme (JCS).

    Excludes volatile transmission metadata (/recorded_at) while preserving all
    identity and semantic fields (envelope_id, source, access, rights, raw, normalized,
    quality, decision, extensions, etc.).
    """
    canonical = copy.deepcopy(envelope)
    for path in excluded_paths:
        keys = path.strip("/").split("/")
        cur = canonical
        for k in keys[:-1]:
            if isinstance(cur, dict):
                cur = cur.get(k)
        if isinstance(cur, dict):
            cur.pop(keys[-1], None)

    serialized = jcs(canonical)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def derive_scope(auth: AuthContext | dict[str, Any]) -> dict[str, Any]:
    """Derive trusted access scope from authenticated principal (session.pool / agent token)."""
    if isinstance(auth, AuthContext):
        return {
            "tenant_id": auth.tenant_id,
            "scope": auth.scope,
            "principal_ids": set(auth.principal_ids),
        }
    p_ids = {auth["principal_id"]} if "principal_id" in auth else set(auth.get("principal_ids", []))
    return {
        "tenant_id": auth["tenant_id"],
        "scope": auth.get("session_pool") or auth["scope"],
        "principal_ids": p_ids,
    }


def verify_auth_binding(auth: AuthContext | dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """Verify that envelope.access strictly matches derived scope from authenticated context.

    Rules:
    - envelope.access.tenant_id == derived.tenant_id
    - envelope.access.scope == derived.scope
    - set(envelope.access.principal_ids) == derived.principal_ids
    On mismatch: raises ScopeBindingMismatchError (HTTP 403).
    """
    access = envelope.get("access")
    if not isinstance(access, dict):
        raise ScopeBindingMismatchError("reject:scope_binding_mismatch: access must be a JSON object")

    derived = derive_scope(auth)

    if access.get("tenant_id") != derived["tenant_id"]:
        raise ScopeBindingMismatchError(
            f"reject:scope_binding_mismatch: tenant_id '{access.get('tenant_id')}' != '{derived['tenant_id']}'"
        )
    if access.get("scope") != derived["scope"]:
        raise ScopeBindingMismatchError(
            f"reject:scope_binding_mismatch: scope '{access.get('scope')}' != '{derived['scope']}'"
        )
    if set(access.get("principal_ids") or []) != derived["principal_ids"]:
        raise ScopeBindingMismatchError(
            f"reject:scope_binding_mismatch: principal_ids {access.get('principal_ids')} != {derived['principal_ids']}"
        )

    return derived
