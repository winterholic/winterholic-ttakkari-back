"""Bounded, server-recomputed evidence for browser-observed web comparisons.

The module validates evidence bytes and recomputes separate functional and pixel
results. A matching file hash does not prove that a browser produced the capture.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from ttakkari.broker.policy import BrokerError, ensure_regular_file, resolve_in_root

SCHEMA_VERSION = 1
MAX_CAPTURE_BYTES = 1024 * 1024
MAX_SCREENSHOT_BYTES = 12 * 1024 * 1024
MAX_SCREENSHOT_PIXELS = 20_000_000
MAX_OBSERVATIONS = 2
MAX_EVENTS = 500
MAX_DOM_NODES = 3000
MAX_NETWORK = 500
MAX_STRING = 16_384
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_HEX_SHA = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = {"match", "mismatch", "unverified"}


class CaptureError(ValueError):
    """Capture is malformed, unsafe, missing, or outside the bounded contract."""


def create_web_capture(
    root: Path,
    *,
    comparison_id: str,
    behavior: str,
    input_data: dict[str, Any],
    original: dict[str, Any] | None,
    replica: dict[str, Any] | None,
    run_id: str,
    expected_viewport: dict[str, Any],
    origin_map: dict[str, str] | None = None,
    environment: dict[str, Any] | None = None,
    pre_state: dict[str, Any] | None = None,
    verified_at: str | None = None,
) -> dict[str, Any]:
    """Safely store a bounded raw capture and return a v2-compatible 14-key record.

    The images must already exist under root and carry path/SHA/byte-size metadata.
    """
    root = _root(root)
    if not _ID.fullmatch(comparison_id) or not _ID.fullmatch(run_id):
        raise CaptureError("comparison_id and run_id must be short safe identifiers")
    if not isinstance(behavior, str) or not behavior.strip() or len(behavior) > MAX_STRING:
        raise CaptureError("behavior must be a non-empty bounded string")
    if not isinstance(input_data, dict) or not _json_bounded(input_data):
        raise CaptureError("input must be a bounded JSON object")
    if not _valid_viewport(expected_viewport):
        raise CaptureError("expected_viewport is invalid")
    if origin_map is None:
        origin_map = {}
    if not isinstance(origin_map, dict) or len(origin_map) > 8:
        raise CaptureError("origin_map must have at most 8 explicit origin pairs")
    for source, target in origin_map.items():
        if _origin(source) is None or _origin(target) is None or source == target:
            raise CaptureError("origin_map values must be distinct HTTP(S) origins")

    stamp = verified_at or datetime.now(UTC).isoformat()
    _parse_time(stamp)
    raw = {
        "schema_version": SCHEMA_VERSION,
        "target_type": "web",
        "comparison_id": comparison_id,
        "behavior": behavior,
        "input": input_data,
        "pre_state": pre_state if isinstance(pre_state, dict) else {},
        "environment": environment if isinstance(environment, dict) else {},
        "expected_viewport": expected_viewport,
        "origin_map": origin_map,
        "original": original,
        "replica": replica,
        "run_id": run_id,
        "verified_at": stamp,
        "complete": original is not None and replica is not None,
    }
    if not _json_bounded(raw):
        raise CaptureError("raw observation exceeds bounded JSON contract")
    _validate_observation_shape(original, run_id=run_id, input_data=input_data,
                                expected_viewport=expected_viewport, required=False)
    _validate_observation_shape(replica, run_id=run_id, input_data=input_data,
                                expected_viewport=expected_viewport, required=False)
    _check_image_reference(root, original.get("screenshot") if isinstance(original, dict) else None,
                           expected_viewport, read_pixels=False)
    _check_image_reference(root, replica.get("screenshot") if isinstance(replica, dict) else None,
                           expected_viewport, read_pixels=False)

    rel = f".reverse/web-captures/{comparison_id}-{run_id}.json"
    path = _safe_path(root, rel, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(root, ".reverse/web-captures", must_exist=True)
    if path.exists() or path.is_symlink():
        raise CaptureError("capture file already exists")
    blob = _json_bytes(raw)
    if len(blob) > MAX_CAPTURE_BYTES:
        raise CaptureError("raw capture exceeds 1 MiB")
    tmp_rel = f".reverse/web-captures/.{comparison_id}-{run_id}-{secrets.token_hex(8)}.tmp"
    tmp = _safe_path(root, tmp_rel, must_exist=False)
    try:
        with tmp.open("xb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise CaptureError(f"cannot safely write capture: {exc}") from exc

    item = {
        "kind": "web_capture",
        "schema_version": SCHEMA_VERSION,
        "capture": {"path": rel, "sha256": _sha(blob), "size_bytes": len(blob)},
        "functional_result": "unverified",
        "visual_result": "unverified",
        "functional_differences": [],
        "visual": {"reason": "not recomputed yet"},
        "integrity": {"status": "stored", "reason": None},
        "provenance": {"status": "unverified", "reason": "capture writer cannot authenticate browser tool execution"},
    }
    return {
        "id": comparison_id,
        "behavior": behavior,
        "target_type": "web",
        "input": input_data,
        "pre_state": raw["pre_state"],
        "original": original,
        "replica": replica,
        "environment": raw["environment"],
        "evidence_level": "observed",
        "evidence": [item],
        "verified_at": stamp,
        "run_id": run_id,
        "result": "unverified",
        "difference": {"reason": "not recomputed yet"},
    }


def verify_web_capture(root: Path, capture: Any, *, expected_run_id: str | None = None) -> dict[str, Any]:
    """Load the hashed raw capture and recompute functional and visual evidence."""
    try:
        return _verify_web_capture(root, capture, expected_run_id=expected_run_id)
    except (CaptureError, BrokerError, OSError, ValueError, TypeError, KeyError) as exc:
        return _unverified_record(capture, str(exc))


def _verify_web_capture(root: Path, capture: Any, *, expected_run_id: str | None) -> dict[str, Any]:
    root = _root(root)
    if not isinstance(capture, dict):
        raise CaptureError("comparison record is not an object")
    evidence = capture.get("evidence")
    items = ([item for item in evidence if isinstance(item, dict) and item.get("kind") == "web_capture"]
             if isinstance(evidence, list) else [])
    if len(items) != 1:
        raise CaptureError("exactly one web_capture evidence item is required")
    item = dict(items[0])
    ref = item.get("capture")
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise CaptureError("raw capture reference is missing")
    path = _safe_path(root, ref["path"], must_exist=True)
    ensure_regular_file(path)
    blob = path.read_bytes()
    if len(blob) > MAX_CAPTURE_BYTES or ref.get("size_bytes") != len(blob) or ref.get("sha256") != _sha(blob):
        raise CaptureError("raw capture size or SHA-256 does not match")
    raw = json.loads(blob)
    _validate_raw_capture(raw)
    if expected_run_id is not None and raw["run_id"] != expected_run_id:
        raise CaptureError("run_id does not match expected_run_id")
    if (capture.get("id") != raw["comparison_id"] or capture.get("behavior") != raw["behavior"]
            or capture.get("target_type") != "web" or capture.get("input") != raw["input"]
            or capture.get("pre_state") != raw["pre_state"] or capture.get("environment") != raw["environment"]
            or capture.get("original") != raw["original"] or capture.get("replica") != raw["replica"]
            or capture.get("run_id") != raw["run_id"] or capture.get("verified_at") != raw["verified_at"]):
        raise CaptureError("comparison record differs from hashed raw capture")

    integrity_reasons: list[str] = []
    functional, functional_diffs = _functional_compare(root, raw, integrity_reasons)
    visual, visual_detail = _visual_compare(root, raw, integrity_reasons)
    if functional == "mismatch":
        result = "mismatch"
        difference: dict[str, Any] | None = {"field": "web_observation", "reasons": functional_diffs}
    elif functional == "unverified":
        result = "unverified"
        difference = {"reason": functional_diffs[0] if functional_diffs else "functional evidence is incomplete"}
    else:
        result = "match"
        difference = None
    item.update(
        functional_result=functional,
        visual_result=visual,
        functional_differences=functional_diffs,
        visual=visual_detail,
        integrity={"status": "verified" if not integrity_reasons else "unverified",
                   "reasons": integrity_reasons},
        provenance={"status": "unverified",
                    "reason": "hashed capture is self-asserted; no RunEvent/tool-result authentication is present"},
    )
    result_record = {key: capture.get(key) for key in (
        "id", "behavior", "target_type", "input", "pre_state", "original", "replica", "environment",
        "evidence_level", "verified_at", "run_id")}
    result_record.update(evidence=[item], result=result, difference=difference)
    result_record["evidence_level"] = "observed"
    return result_record


def _functional_compare(root: Path, raw: dict[str, Any], integrity_reasons: list[str]) -> tuple[str, list[str]]:
    original, replica = raw.get("original"), raw.get("replica")
    if original is None or replica is None:
        return "unverified", ["original or replica observation is missing"]
    reasons: list[str] = []
    for label, obs in (("original", original), ("replica", replica)):
        try:
            _validate_observation_shape(obs, run_id=raw["run_id"], input_data=raw["input"],
                                        expected_viewport=raw["expected_viewport"], required=True)
            _check_image_reference(root, obs["screenshot"], raw["expected_viewport"], read_pixels=False)
        except (CaptureError, BrokerError, OSError, ValueError, TypeError) as exc:
            reasons.append(f"{label}: {exc}")
    if reasons:
        integrity_reasons.extend(reasons)
        return "unverified", reasons
    preconditions = ("input", "viewport", "pre_state", "environment")
    for field in preconditions:
        if original[field] != replica[field]:
            reasons.append(f"{field} differs between observations")
    for obs in (original, replica):
        if obs["input"] != raw["input"]:
            reasons.append("observation input differs from comparison input")
        if obs["pre_state"] != raw["pre_state"]:
            reasons.append("observation pre_state differs from comparison pre_state")
        if obs["environment"] != raw["environment"]:
            reasons.append("observation environment differs from comparison environment")
    if reasons:
        return "unverified", reasons
    fields = ("events", "dom", "state")
    for field in fields:
        if original[field] != replica[field]:
            reasons.append(f"{field} differs")
    network_equal, network_reason = _network_equal(original["network"], replica["network"], raw["origin_map"])
    if not network_equal:
        reasons.append(network_reason or "network differs")
    if (not original["events"] and not original["dom"] and not original["state"] and not original["network"]
            and not replica["events"] and not replica["dom"] and not replica["state"] and not replica["network"]):
        return "unverified", ["both observations are empty"]
    return ("mismatch", reasons) if reasons else ("match", [])


def _network_equal(original: list[dict[str, Any]], replica: list[dict[str, Any]],
                   origin_map: dict[str, str]) -> tuple[bool, str | None]:
    if len(original) != len(replica):
        return False, "network request count differs"
    reverse_map = {target: source for source, target in origin_map.items()}
    for index, (left, right) in enumerate(zip(original, replica)):
        left_url, right_url = left["url"], right["url"]
        left_origin, right_origin = _origin(left_url), _origin(right_url)
        mapped = origin_map.get(left_origin or "") == right_origin
        if not mapped and left_origin == right_origin:
            mapped = True
        if not mapped and reverse_map.get(right_origin or "") == left_origin:
            mapped = True
        if not mapped:
            return False, f"network[{index}] origins differ without explicit origin mapping"
        if (_url_semantics(left_url) != _url_semantics(right_url)
                or any(left.get(key) != right.get(key) for key in
                       ("method", "request_body", "status", "response_body_sha256", "response_headers"))):
            return False, f"network[{index}] meaningful request/response fields differ"
    return True, None


def _visual_compare(root: Path, raw: dict[str, Any], integrity_reasons: list[str]) -> tuple[str, dict[str, Any]]:
    original, replica = raw.get("original"), raw.get("replica")
    if original is None or replica is None:
        return "unverified", {"reason": "original or replica observation is missing"}
    try:
        left_path = _check_image_reference(root, original.get("screenshot"), raw["expected_viewport"], read_pixels=True)
        right_path = _check_image_reference(root, replica.get("screenshot"), raw["expected_viewport"], read_pixels=True)
        if original["viewport"] != replica["viewport"] or original["viewport"] != raw["expected_viewport"]:
            return "unverified", {"reason": "viewport metadata differs"}
        with Image.open(left_path) as left_image, Image.open(right_path) as right_image:
            if left_image.format != "PNG" or right_image.format != "PNG":
                raise CaptureError("screenshots must be PNG")
            if left_image.size != right_image.size:
                return "unverified", {"reason": "PNG pixel dimensions differ"}
            width, height = left_image.size
            left_pixels = list(left_image.convert("RGBA").get_flattened_data())
            right_pixels = list(right_image.convert("RGBA").get_flattened_data())
        differing = sum(a != b for a, b in zip(left_pixels, right_pixels))
        return ("match" if differing == 0 else "mismatch"), {
            "width": width,
            "height": height,
            "different_pixels": differing,
            "total_pixels": len(left_pixels),
            "pixel_equality": "exact",
        }
    except (CaptureError, BrokerError, OSError, UnidentifiedImageError, ValueError, TypeError) as exc:
        integrity_reasons.append(str(exc))
        return "unverified", {"reason": str(exc)}


def _check_image_reference(root: Path, ref: Any, viewport: dict[str, Any], *, read_pixels: bool) -> Path | None:
    if ref is None:
        return None
    if not isinstance(ref, dict):
        raise CaptureError("screenshot reference must be an object")
    rel, sha = ref.get("path"), ref.get("sha256")
    if not isinstance(rel, str) or not _HEX_SHA.fullmatch(str(sha)):
        raise CaptureError("screenshot path or SHA-256 is invalid")
    if ref.get("format") != "png" or not _valid_viewport(ref.get("viewport")):
        raise CaptureError("screenshot format or viewport metadata is invalid")
    if ref["viewport"] != viewport:
        raise CaptureError("screenshot viewport differs from expected viewport")
    path = _safe_path(root, rel, must_exist=True)
    ensure_regular_file(path)
    stat = path.stat()
    if stat.st_size > MAX_SCREENSHOT_BYTES or ref.get("size_bytes") != stat.st_size:
        raise CaptureError("screenshot missing, truncated, or outside size bound")
    blob = path.read_bytes()
    if _sha(blob) != sha:
        raise CaptureError("screenshot SHA-256 does not match file bytes")
    if read_pixels:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise CaptureError("screenshots must be PNG")
            width, height = image.size
            expected_w = viewport["width"] * viewport["device_scale_factor"]
            expected_h = viewport["height"] * viewport["device_scale_factor"]
            if width != expected_w or height != expected_h or width * height > MAX_SCREENSHOT_PIXELS:
                raise CaptureError("PNG dimensions mismatch viewport or exceed pixel bound")
            image.verify()
    return path


def _validate_raw_capture(raw: Any) -> None:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION or raw.get("target_type") != "web":
        raise CaptureError("raw capture schema is invalid")
    required = {"schema_version", "target_type", "comparison_id", "behavior", "input", "pre_state",
                "environment", "expected_viewport", "origin_map", "original", "replica", "run_id",
                "verified_at", "complete"}
    if set(raw) != required or not _json_bounded(raw):
        raise CaptureError("raw capture fields or size are outside contract")
    if not _ID.fullmatch(raw["comparison_id"]) or not _ID.fullmatch(raw["run_id"]):
        raise CaptureError("raw identifiers are invalid")
    if not _valid_viewport(raw["expected_viewport"]):
        raise CaptureError("raw expected viewport is invalid")
    _parse_time(raw["verified_at"])
    if type(raw["complete"]) is not bool or raw["complete"] != (raw["original"] is not None and raw["replica"] is not None):
        raise CaptureError("raw complete flag is inconsistent")
    if raw["original"] is None and raw["replica"] is None:
        raise CaptureError("both observations are missing")


def _validate_observation_shape(obs: Any, *, run_id: str, input_data: dict[str, Any],
                                expected_viewport: dict[str, Any], required: bool) -> None:
    if obs is None and not required:
        return
    fields = {"run_id", "observed_at", "url", "input", "viewport", "pre_state", "environment",
              "events", "dom", "state", "network", "screenshot", "complete", "capture_state"}
    if not isinstance(obs, dict) or set(obs) != fields:
        raise CaptureError("observation fields do not match bounded schema")
    if obs["run_id"] != run_id:
        raise CaptureError("observation run_id differs from capture")
    _parse_time(obs["observed_at"])
    if abs((_parse_time(obs["observed_at"]) - _parse_time(datetime.now(UTC).isoformat())).total_seconds()) > 86400:
        raise CaptureError("observation timestamp is outside the one-day bound")
    if _origin(obs["url"]) is None:
        raise CaptureError("observation URL must be an HTTP(S) URL without credentials")
    if (obs["input"] != input_data or not _valid_viewport(obs["viewport"])
            or obs["viewport"] != expected_viewport or not isinstance(obs["pre_state"], dict)
            or not isinstance(obs["environment"], dict) or not _json_bounded(obs["environment"])):
        raise CaptureError("input, viewport, pre_state, or environment is invalid")
    if obs["capture_state"] not in {"complete", "truncated", "partial"} or type(obs["complete"]) is not bool:
        raise CaptureError("capture completion fields are invalid")
    if not isinstance(obs["events"], list) or len(obs["events"]) > MAX_EVENTS or not all(_json_bounded(x) for x in obs["events"]):
        raise CaptureError("events must be a bounded array")
    if not isinstance(obs["dom"], list) or len(obs["dom"]) > MAX_DOM_NODES or not all(_json_bounded(x) for x in obs["dom"]):
        raise CaptureError("dom must be a bounded array")
    if not isinstance(obs["state"], (dict, list)) or not _json_bounded(obs["state"]):
        raise CaptureError("state must be a bounded JSON object or array")
    if not isinstance(obs["network"], list) or len(obs["network"]) > MAX_NETWORK:
        raise CaptureError("network must be a bounded array")
    for request in obs["network"]:
        if not isinstance(request, dict) or set(request) != {
                "method", "url", "status", "request_body", "response_body_sha256", "response_headers"}:
            raise CaptureError("network request fields do not match bounded schema")
        if (not isinstance(request["method"], str) or request["method"] not in
                {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                or _origin(request["url"]) is None or type(request["status"]) is not int
                or not 100 <= request["status"] <= 599
                or request["request_body"] is not None and not _json_bounded(request["request_body"])
                or not isinstance(request["response_body_sha256"], str)
                or not _HEX_SHA.fullmatch(request["response_body_sha256"])
                or not isinstance(request["response_headers"], dict)
                or any(not isinstance(key, str) or not isinstance(value, str) or len(key) > 256 or len(value) > 4096
                       or re.search(r"authorization|cookie|token|secret|api[-_]?key|password|credential",
                                    key, re.IGNORECASE) for key, value in request["response_headers"].items())):
            raise CaptureError("network request method, URL, status, body, or hash is invalid")
    if obs["complete"] is not True or obs["capture_state"] != "complete":
        raise CaptureError("truncated or partial observation cannot be verified")


def _safe_path(root: Path, rel: str, *, must_exist: bool) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")):
        raise CaptureError("only non-empty relative paths are accepted")
    try:
        resolved = resolve_in_root(root, rel, must_exist=must_exist)
        if resolved.path != root and root not in resolved.path.parents:
            raise CaptureError("path escapes root")
        return resolved.path
    except BrokerError as exc:
        raise CaptureError(str(exc)) from exc


def _root(root: Path) -> Path:
    try:
        result = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CaptureError("workspace root is missing or cannot be resolved") from exc
    if not result.is_dir():
        raise CaptureError("workspace root must be a directory")
    return result


def _valid_viewport(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"width", "height", "device_scale_factor"}
            and type(value["width"]) is int and type(value["height"]) is int
            and type(value["device_scale_factor"]) is int and 1 <= value["width"] <= 7680
            and 1 <= value["height"] <= 4320 and 1 <= value["device_scale_factor"] <= 3
            and value["width"] * value["height"] * value["device_scale_factor"] ** 2 <= MAX_SCREENSHOT_PIXELS)


def _origin(url: Any) -> str | None:
    if not isinstance(url, str) or len(url) > MAX_STRING:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port
        default = 80 if parsed.scheme == "http" else 443
        return f"{parsed.scheme}://{parsed.hostname.lower()}" + (f":{port}" if port and port != default else "")
    except ValueError:
        return None


def _url_semantics(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.path or "/", parsed.query


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise CaptureError("timestamp must be a bounded ISO-8601 string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CaptureError("timestamp is not valid ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise CaptureError("timestamp must include a timezone")
    return result.astimezone(UTC)


def _json_bounded(value: Any) -> bool:
    try:
        return len(_json_bytes(value)) <= MAX_CAPTURE_BYTES
    except (TypeError, ValueError, RecursionError):
        return False


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("utf-8")


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _unverified_record(capture: Any, reason: str) -> dict[str, Any]:
    if not isinstance(capture, dict):
        return {"result": "unverified", "difference": {"reason": reason}, "evidence": []}
    record = {key: capture.get(key) for key in (
        "id", "behavior", "target_type", "input", "pre_state", "original", "replica", "environment",
        "evidence_level", "verified_at", "run_id")}
    evidence = capture.get("evidence") if isinstance(capture.get("evidence"), list) else []
    items = [dict(x) for x in evidence if isinstance(x, dict) and x.get("kind") == "web_capture"]
    if items:
        for item in items:
            item.update(functional_result="unverified", visual_result="unverified",
                        integrity={"status": "unverified", "reason": reason},
                        provenance={"status": "unverified", "reason": "no authenticated RunEvent/tool result"})
    record.update(evidence=items, result="unverified", difference={"reason": reason})
    return record
