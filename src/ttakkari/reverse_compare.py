"""Bounded CLI comparison capture with server-recomputable evidence."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 30


def _command(command: Sequence[str] | None, *, argv: list[str], stdin: str | None,
             cwd: Path, timeout: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if command is None:
        return None, {"error": "original command is unavailable"}
    if not isinstance(command, (list, tuple)) or not command or any(not isinstance(part, str) for part in command):
        return None, {"error": "command must be a non-empty argv list"}
    try:
        invoked = [*command, *argv]
        completed = subprocess.run(
            invoked, input=stdin.encode("utf-8") if stdin is not None else None,
            cwd=cwd, shell=False, capture_output=True, timeout=timeout,
            check=False, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, subprocess.TimeoutExpired):
            message = f"TimeoutExpired: timeout after {timeout}s"
            out = exc.stdout or b""
            err = exc.stderr or b""
            if isinstance(out, str): out = out.encode()
            if isinstance(err, str): err = err.encode()
            return None, {"command": invoked, "error": message, "timeout_output": {
                    "stdout": out.decode("utf-8", errors="replace"), "stderr": err.decode("utf-8", errors="replace"),
                    "stdout_bytes_b64": base64.b64encode(out).decode(), "stderr_bytes_b64": base64.b64encode(err).decode(),
                    "exit_code": None, "timed_out": True, "argv": invoked}}
        return None, {"command": list(command), "argv": list(command), "error": message}

    stdout = completed.stdout[:MAX_OUTPUT_BYTES]
    stderr = completed.stderr[:MAX_OUTPUT_BYTES]
    truncated = len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES
    result = {"stdout": stdout.decode("utf-8", errors="replace"),
              "stderr": stderr.decode("utf-8", errors="replace"),
              "stdout_bytes_b64": base64.b64encode(stdout).decode(),
              "stderr_bytes_b64": base64.b64encode(stderr).decode(), "exit_code": completed.returncode,
              "timed_out": False, "argv": invoked}
    return result, {"command": invoked, **result, "truncated": truncated}


def _safe_stem(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError(f"{label} must be a safe identifier (1-80 ASCII characters)")
    if ".." in value:
        raise ValueError(f"{label} cannot contain '..'")
    return value


def _capture_directory(root: Path) -> Path:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("root must be an existing directory")
    reverse = root / ".reverse"
    comparisons = reverse / "comparisons"
    for directory in (reverse, comparisons):
        if directory.exists() and directory.is_symlink():
            raise ValueError("comparison evidence directory cannot be a symlink")
    comparisons.mkdir(parents=True, exist_ok=True)
    resolved = comparisons.resolve(strict=True)
    if root not in resolved.parents:
        raise ValueError("comparison evidence path escaped root")
    return resolved


def run_comparison(
    root: Path,
    *,
    comparison_id: str,
    behavior: str,
    target_type: str,
    input_data: dict[str, Any],
    original_command: Sequence[str] | None,
    replica_command: Sequence[str],
    run_id: str,
    verified_at: str | None = None,
    pre_state: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run argv commands in the job root and write a hashed raw capture.

    The caller is responsible for choosing commands that are safe to execute in
    the current workspace. This helper never invokes a shell.
    """
    if target_type not in {"auto", "cli", "web", "api", "source"}:
        raise ValueError("target_type must be auto, cli, web, api, or source")
    if not isinstance(behavior, str) or not behavior.strip():
        raise ValueError("behavior is required")
    if not isinstance(input_data, dict) or (pre_state is not None and not isinstance(pre_state, dict)):
        raise ValueError("input_data and pre_state must be JSON objects")
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 300:
        raise ValueError("timeout_seconds must be between 0 and 300")
    argv, stdin = input_data.get("argv", []), input_data.get("stdin")
    if not isinstance(argv, list) or any(not isinstance(value, str) or "\0" in value for value in argv):
        raise ValueError("input.argv must be an array of strings")
    if stdin is not None and not isinstance(stdin, str):
        raise ValueError("input.stdin must be a string")
    comparison_id = _safe_stem(comparison_id, "comparison_id")
    run_id = _safe_stem(run_id, "run_id")
    root = Path(root).resolve(strict=True)
    evidence_dir = _capture_directory(root)

    cwd = root
    if pre_state and pre_state.get("cwd"):
        cwd = Path(pre_state["cwd"]).resolve(strict=True)
        if not cwd.is_dir() or (cwd != root and root not in cwd.parents):
            raise ValueError("pre_state.cwd must be inside the workspace")
    original, original_log = _command(original_command, argv=argv, stdin=stdin, cwd=cwd, timeout=timeout_seconds)
    replica, replica_log = _command(replica_command, argv=argv, stdin=stdin, cwd=cwd, timeout=timeout_seconds)
    if original_log.get("error") and "TimeoutExpired" not in original_log["error"]:
        original_log["error"] = f"original execution: {original_log['error']}"
    if replica_log.get("error") and "TimeoutExpired" not in replica_log["error"]:
        replica_log["error"] = f"replica execution: {replica_log['error']}"
    environment = environment or {"os": platform.system(), "os_release": platform.release(),
                                  "python": platform.python_version(), "cwd": str(cwd)}
    capture = {"schema_version": 2, "target_type": target_type, "input": input_data,
               "pre_state": pre_state or {}, "environment": environment,
               "original": original, "replica": replica,
               "execution_log": [{"side": "original", **original_log}, {"side": "replica", **replica_log}],
               "verified_at": verified_at or datetime.now(UTC).isoformat(), "run_id": run_id,
               "complete": not original_log.get("truncated", False) and not replica_log.get("truncated", False)
               and not original_log.get("error") and not replica_log.get("error")}
    encoded = json.dumps(capture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    evidence_path = evidence_dir / f"{comparison_id}-{run_id}.json"
    if evidence_path.is_symlink():
        raise ValueError("comparison evidence file cannot be a symlink")
    if evidence_path.exists():
        raise FileExistsError("comparison evidence already exists; use a new run_id")
    evidence_path.write_bytes(encoded)

    result, difference = "match", None
    if original is None:
        result, difference = "unverified", {"reason": original_log.get("error", "original command is unavailable")}
    elif not capture["complete"]:
        result, difference = "unverified", {"reason": "one or more commands failed to run completely"}
    else:
        if original.get("timed_out") or replica.get("timed_out"):
            result, difference = "unverified", {"reason": "one or more commands timed out"}
        for field in ("stdout_bytes_b64", "stderr_bytes_b64", "exit_code"):
            if result != "match":
                break
            if original[field] != replica[field]:
                result = "mismatch"
                display_field = {"stdout_bytes_b64": "stdout", "stderr_bytes_b64": "stderr"}.get(field, field)
                if field.endswith("_bytes_b64") and original.get(display_field) == replica.get(display_field):
                    display_field = field
                difference = {"field": display_field, "original": original.get(display_field),
                              "replica": replica.get(display_field)}
                break
    return {
        "id": comparison_id, "behavior": behavior.strip()[:1000], "target_type": target_type,
        "input": input_data, "pre_state": pre_state or {}, "original": original, "replica": replica,
        "environment": environment, "evidence_level": "direct" if capture["complete"] and original else "observed",
        "evidence": [{"kind": "execution", "path": evidence_path.relative_to(root).as_posix(),
                      "sha256": hashlib.sha256(encoded).hexdigest()}],
        "verified_at": verified_at or datetime.now(UTC).isoformat(), "run_id": run_id,
        "result": result, "difference": difference,
    }


_SECRET_HEADER = re.compile(r"authorization|cookie|token|secret|api[-_]?key|password", re.IGNORECASE)


def _store_structured(root: Path, comparison_id: str, run_id: str, capture: dict[str, Any]) -> dict[str, str]:
    blob = json.dumps(capture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(blob) > 1_048_576:
        raise ValueError("raw comparison capture exceeds 1 MiB")
    directory = _capture_directory(root)
    path = directory / f"{_safe_stem(comparison_id, 'comparison_id')}-{_safe_stem(run_id, 'run_id')}.json"
    if path.exists() or path.is_symlink():
        raise FileExistsError("comparison evidence already exists; use a new run_id")
    path.write_bytes(blob)
    return {"kind": "execution", "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(blob).hexdigest()}


def _structured_record(root: Path, *, comparison_id: str, behavior: str, target_type: str,
                       input_data: dict[str, Any], pre_state: dict[str, Any] | None,
                       original: Any, replica: Any, run_id: str, verified_at: str | None,
                       environment: dict[str, Any] | None, result: str, difference: Any,
                       capture: dict[str, Any], evidence_level: str) -> dict[str, Any]:
    return {"id": comparison_id, "behavior": behavior, "target_type": target_type, "input": input_data,
            "pre_state": pre_state or {}, "original": original, "replica": replica,
            "environment": environment or {"os": platform.platform()}, "evidence_level": evidence_level,
            "evidence": [_store_structured(root, comparison_id, run_id, capture)],
            "verified_at": verified_at or datetime.now(UTC).isoformat(), "run_id": run_id,
            "result": result, "difference": difference}


def _api_request(url: str, method: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("API response exceeds 1 MiB")
        safe_headers = {key.lower(): value for key, value in response.headers.items()
                        if not _SECRET_HEADER.search(key)}
        return {"status": response.code, "body": raw.decode("utf-8", errors="replace"),
                "body_bytes_b64": base64.b64encode(raw).decode(), "headers": safe_headers}


def _api_observe(base_url: str, method: str, headers: dict[str, str], body: bytes | None,
                 timeout: float, state_path: str | None) -> tuple[dict[str, Any] | None, str | None]:
    try:
        before = _api_request(base_url.rstrip("/") + state_path, "GET", headers, None, timeout) if state_path else None
        result = _api_request(base_url, method, headers, body, timeout)
        after = _api_request(base_url.rstrip("/") + state_path, "GET", headers, None, timeout) if state_path else None
        if state_path:
            result.update(state_before=before, state_after=after)
        return result, None
    except (OSError, TimeoutError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def run_api_comparison(workspace: Path, *, comparison_id: str, behavior: str,
                       input_data: dict[str, Any], original_url: str, replica_url: str, run_id: str,
                       method: str = "GET", headers: dict[str, str] | None = None,
                       timeout_seconds: float = 15.0, pre_state: dict[str, Any] | None = None,
                       environment: dict[str, Any] | None = None,
                       verified_at: str | None = None,
                       allowed_hosts: Sequence[str] = ("localhost", "127.0.0.1", "::1")) -> dict[str, Any]:
    root = Path(workspace).resolve(strict=True)
    _safe_stem(comparison_id, "comparison_id"); _safe_stem(run_id, "run_id")
    if not isinstance(input_data, dict) or not 0 < timeout_seconds <= 300:
        raise ValueError("input_data must be an object and timeout must be 0..300 seconds")
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ValueError("unsupported HTTP method")
    request_headers = dict(headers or {})
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in request_headers.items()):
        raise ValueError("headers must map strings to strings")
    if "json" in input_data:
        body = json.dumps(input_data["json"], ensure_ascii=False, separators=(",", ":")).encode()
        request_headers.setdefault("Content-Type", "application/json")
    elif "body_bytes_b64" in input_data:
        body = base64.b64decode(input_data["body_bytes_b64"], validate=True)
    else:
        body = None
    request_headers.setdefault("Accept", "*/*")
    state_path = input_data.get("state_path")
    if state_path is not None and (not isinstance(state_path, str) or not state_path.startswith("/")):
        raise ValueError("state_path must start with /")
    for endpoint in (original_url, replica_url):
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in set(allowed_hosts):
            raise ValueError("API host must be explicitly allowlisted")
    original, error_a = _api_observe(original_url, method, request_headers, body, timeout_seconds, state_path)
    replica, error_b = _api_observe(replica_url, method, request_headers, body, timeout_seconds, state_path)
    result, difference = "match", None
    if original is None or replica is None:
        result, difference = "unverified", {"reason": "API request failed", "original_error": error_a,
                                             "replica_error": error_b}
    else:
        for field in ("status", "body_bytes_b64", "headers", "state_before", "state_after"):
            if original.get(field) != replica.get(field):
                result, difference = "mismatch", {"field": field, "original": original.get(field),
                                                    "replica": replica.get(field)}
                break
    safe_headers = {k: "[REDACTED]" if _SECRET_HEADER.search(k) else v for k, v in request_headers.items()}
    log = {"method": method, "request_headers": safe_headers,
           "request_body_b64": base64.b64encode(body or b"").decode()}
    capture = {"schema_version": 2, "target_type": "api", "input": input_data, "pre_state": pre_state or {},
        "original": original, "replica": replica,
        "execution_log": [{**log, "side": "original", "url": original_url, "output": original, "error": error_a},
                          {**log, "side": "replica", "url": replica_url, "output": replica, "error": error_b}],
        "environment": environment or {"os": platform.platform()},
        "verified_at": verified_at or datetime.now(UTC).isoformat(), "run_id": run_id,
        "complete": original is not None and replica is not None}
    return _structured_record(root, comparison_id=comparison_id, behavior=behavior, target_type="api",
        input_data=input_data, pre_state=pre_state, original=original, replica=replica, run_id=run_id,
        verified_at=verified_at, environment=environment, result=result, difference=difference,
        capture=capture, evidence_level="direct" if result != "unverified" else "observed")


def run_web_comparison(workspace: Path, *, comparison_id: str, behavior: str,
                       input_data: dict[str, Any], original_observation: dict[str, Any] | None,
                       replica_observation: dict[str, Any] | None, run_id: str,
                       pre_state: dict[str, Any] | None = None, screenshots: dict[str, str] | None = None,
                       visual_assessment: dict[str, Any] | None = None,
                       environment: dict[str, Any] | None = None,
                       verified_at: str | None = None) -> dict[str, Any]:
    root = Path(workspace).resolve(strict=True)
    result, difference = "match", None
    if original_observation is None or replica_observation is None:
        result, difference = "unverified", {"reason": "browser observation missing"}
    else:
        for field in ("events", "dom", "state", "network"):
            if original_observation.get(field) != replica_observation.get(field):
                result, difference = "mismatch", {"field": field, "original": original_observation.get(field),
                                                    "replica": replica_observation.get(field)}
                break
    visual = "unverified"
    if isinstance(visual_assessment, dict) and visual_assessment.get("result") in {"match", "mismatch"}:
        visual = visual_assessment["result"]
    capture = {"schema_version": 2, "target_type": "web", "input": input_data, "pre_state": pre_state or {},
        "original": original_observation, "replica": replica_observation, "functional_result": result,
        "visual_result": visual, "visual_assessment": visual_assessment, "screenshots": screenshots or {},
        "execution_log": [], "environment": environment or {"browser_observation": "configured ttakkari_browser"},
        "verified_at": verified_at or datetime.now(UTC).isoformat(), "run_id": run_id,
        "complete": original_observation is not None and replica_observation is not None}
    record = _structured_record(root, comparison_id=comparison_id, behavior=behavior, target_type="web",
        input_data=input_data, pre_state=pre_state, original=original_observation, replica=replica_observation,
        run_id=run_id, verified_at=verified_at, environment=environment, result=result, difference=difference,
        capture=capture, evidence_level="observed")
    record["evidence"].append({"kind": "visual_assessment", **(visual_assessment or {"result": "unverified"}),
                               "screenshot_refs": screenshots or {}})
    return record


def verify_comparisons(root: Path, comparisons: list[dict[str, Any]], *, expected_run_id: str | None = None) -> list[dict[str, Any]]:
    """Check that each returned comparison still matches its stored raw capture."""
    from ttakkari.reverse_tasks import verify_comparisons as verify
    return verify(root, comparisons, expected_run_id=expected_run_id)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Capture original and replica CLI behavior")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text("utf-8"))
    comparison = run_comparison(Path(args.workspace), comparison_id=spec["id"], behavior=spec["behavior"],
        target_type=spec.get("target_type", "cli"), input_data=spec.get("input", {}),
        original_command=spec.get("original_command"), replica_command=spec["replica_command"],
        run_id=spec["run_id"], pre_state=spec.get("pre_state"), timeout_seconds=spec.get("timeout_seconds", 30))
    print(json.dumps(comparison, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
