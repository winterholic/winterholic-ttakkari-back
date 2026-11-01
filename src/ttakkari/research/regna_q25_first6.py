"""Narrow backend capability for the frozen Regna N3 Qwen2.5 first six cells.

This module deliberately does not expose the generic host_gpu API to work sessions.
Only the server-owned plan, source manifest and three pinned runtime assets are accepted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from ttakkari.host_resource import LeaseError, ResourceLeaseHandle, ResourceLeaseManager

PROFILE_ID = "regna-n3-qwen25-first6-cpu-v1"
OPERATION_KIND = "regna_q25_first6_cpu"
PLAN_SHA256 = "b7d9bccd070b6da2b7377ab53f394ab1b6d696a22488063da547daf0cad1deeb"
SOURCE_MANIFEST_SHA256 = "370ab02657803d134e2ca147f6ce78404c3c70d52b636ec4baaf8bed04cffe0c"
RUNNER_SHA256 = "dd15cb286eae686d2c41968016bd2234c20dc2e1d06e172886cffe39309a1ebc"
CELL_TIMEOUT_SECONDS = 120
OVERALL_TIMEOUT_SECONDS = 900
CLEANUP_RESERVE_SECONDS = 30
MAX_RSS_BYTES = 6 * 1024**3
LEASE_TTL_SECONDS = 1200
EXPECTED_CELLS = (
    ("ih-schedule_conflict-01", "ih-schedule_conflict-01:s1701", 1701),
    ("ih-schedule_conflict-01", "ih-schedule_conflict-01:s1702", 1702),
    ("ih-schedule_conflict-01", "ih-schedule_conflict-01:s1703", 1703),
    ("ih-schedule_conflict-02", "ih-schedule_conflict-02:s1701", 1701),
    ("ih-schedule_conflict-02", "ih-schedule_conflict-02:s1702", 1702),
    ("ih-schedule_conflict-02", "ih-schedule_conflict-02:s1703", 1703),
)
EXPECTED_ASSETS = {
    "binary": ("/private/tmp/regna-ios-model02-lfm-cpu/build/bin/llama-server",
               "387143c966e0cb9734d4d54af26af9f68072117279af2c964ad46aeca759cf57", 33472),
    "model": ("/Users/winterholic/development/model-assets/regna-practical-20261029/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf",
              "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e", 1117320736),
    "template": ("/Users/winterholic/development/hyperez/regna-ios/docs/research/practical-20261029/assets/templates/qwen25-gguf-embedded.jinja",
                 "d5495a1e5db0611132a97e46a65dbb64a642a499421228b9c8b93229097fa9a4", 2509),
}
REGNA_ROOT = Path("/Users/winterholic/development/hyperez/regna-ios")
PLAN_RELATIVE = Path("docs/research/practical-20261029/lead-review/resume-original-coverage-correction-r199-20261101/qwen25-first6-plan-bound.json")
MANIFEST_RELATIVE = Path("docs/research/practical-20261029/lead-review/resume-n3-other-families-r4-prep-20261101/n3-source-manifest-r4.json")
RUNNER_RELATIVE = Path("tools/practical_research/execution/independent_holdout_n3_runtime_r4.py")


class ProfileError(ValueError):
    pass


class DuplicateClaim(ProfileError):
    pass


class PreflightError(ProfileError):
    pass


class EvidenceError(ProfileError):
    pass


@dataclass(frozen=True)
class FrozenProfile:
    root: Path
    plan_path: Path
    manifest_path: Path
    runner_path: Path
    plan_sha256: str
    manifest_sha256: str
    cells: tuple[dict[str, Any], ...]
    runtime_assets: dict[str, dict[str, Any]]
    cpu_only: bool
    max_rss_bytes: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset(path: Path) -> dict[str, Any]:
    try:
        return {"path": str(path.resolve(strict=True)), "bytes": path.stat().st_size, "sha256": _sha256(path)}
    except OSError as exc:
        raise ProfileError(f"pinned runtime asset unavailable: {path.name}") from exc


def load_profile(root: Path | str = REGNA_ROOT) -> FrozenProfile:
    root = Path(root).resolve(strict=True)
    plan_path = root / PLAN_RELATIVE
    manifest_path = root / MANIFEST_RELATIVE
    runner_path = root / RUNNER_RELATIVE
    if _sha256(plan_path) != PLAN_SHA256:
        raise ProfileError("frozen first-six plan hash mismatch")
    manifest = _read_frozen_manifest(manifest_path)
    if not runner_path.is_file() or _sha256(runner_path) != RUNNER_SHA256:
        raise ProfileError("frozen r4 runner hash mismatch")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError("frozen plan cannot be read") from exc
    expected = [{"case_id": case, "cell_id": cell, "seed": seed} for case, cell, seed in EXPECTED_CELLS]
    cells = plan.get("cells")
    if cells != expected or plan.get("max_authorized_cells") != 6 or plan.get("cpu_only") is not True:
        raise ProfileError("frozen plan is not the exact first-six CPU profile")
    if plan.get("source_manifest_sha256") != _canonical_manifest_sha(manifest):
        raise ProfileError("plan does not bind the pinned source manifest")
    _verify_manifest_files(root, manifest)
    assets: dict[str, dict[str, Any]] = {}
    for name, (raw_path, expected_sha, expected_bytes) in EXPECTED_ASSETS.items():
        path = Path(raw_path)
        item = _asset(path)
        if item["sha256"] != expected_sha or item["bytes"] != expected_bytes:
            raise ProfileError(f"pinned runtime asset hash or size mismatch: {name}")
        assets[name] = item
    plan_assets = plan.get("runtime_assets")
    if plan_assets != assets:
        raise ProfileError("frozen plan runtime asset paths or hashes differ")
    return FrozenProfile(root, plan_path, manifest_path, runner_path, PLAN_SHA256,
                         SOURCE_MANIFEST_SHA256, tuple(cells), assets, True, MAX_RSS_BYTES)


def _canonical_manifest_sha(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _read_frozen_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileError("frozen source manifest cannot be read") from exc
    if hashlib.sha256(raw).hexdigest() != SOURCE_MANIFEST_SHA256:
        raise ProfileError("frozen source manifest hash mismatch")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProfileError("frozen source manifest cannot be parsed") from exc
    if not isinstance(manifest, dict):
        raise ProfileError("frozen source manifest has an invalid root")
    return manifest


def _verify_manifest_files(root: Path, manifest: dict[str, Any]) -> None:
    """Verify every source, oracle, scorer and runtime file before importing any frozen runner code."""
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ProfileError("frozen source manifest has no file pins")
    root = root.resolve(strict=True)
    runner_pin = files.get(RUNNER_RELATIVE.as_posix())
    if (not isinstance(runner_pin, dict) or runner_pin.get("sha256") != RUNNER_SHA256):
        raise ProfileError("frozen source manifest does not bind the r4 runner hash")
    for relative, pin in files.items():
        item = PurePosixPath(relative) if isinstance(relative, str) else None
        if (item is None or item.is_absolute() or ".." in item.parts or not item.parts
                or not isinstance(pin, dict) or type(pin.get("bytes")) is not int
                or not isinstance(pin.get("sha256"), str)):
            raise ProfileError("frozen source manifest contains an invalid file pin")
        path = (root / Path(*item.parts)).resolve(strict=True)
        if not path.is_relative_to(root) or path.stat().st_size != pin["bytes"] or _sha256(path) != pin["sha256"]:
            raise ProfileError(f"frozen source manifest file mismatch: {item.as_posix()}")


def parse_operation_input(payload: Any = None) -> None:
    if payload not in (None, {}):
        raise ProfileError("operation accepts no caller-supplied arguments")


def claim_first6(profile: FrozenProfile, claim_root: Path, *, owner_session: str, job_id: str) -> Path:
    identity = json.dumps({"plan_sha256": profile.plan_sha256,
                           "cells": [x["cell_id"] for x in profile.cells]}, sort_keys=True).encode()
    claim_id = hashlib.sha256(identity).hexdigest()
    claim_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = claim_root / f"{claim_id}.json"
    body = json.dumps({"profile": PROFILE_ID, "plan_sha256": profile.plan_sha256,
                       "owner_session": owner_session, "job_id": job_id,
                       "cells": [x["cell_id"] for x in profile.cells],
                       "claimed_at": datetime.now(UTC).isoformat()}, sort_keys=True).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DuplicateClaim("the frozen first-six cell identity was already claimed") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def issue_grant(profile: FrozenProfile, *, owner_session: str, job_id: str,
                output_root: Path, claim_root: Path, now: datetime | None = None) -> dict[str, Any]:
    if not owner_session or not job_id:
        raise ProfileError("grant owner and operation id are required")
    stamp = now or datetime.now(UTC)
    grant = {
        "schema": "regna-n3-runtime-grant-r4", "authorized_dispatch": True,
        "scope_id": "regna-independent-holdout-n3-r4", "grant_id": secrets.token_hex(16),
        "owner_id": owner_session, "engine": "ttakkari-host-cpu", "invocation_id": job_id,
        "output_root": str(output_root.resolve()), "claim_root": str(claim_root.resolve()),
        "expires_at_utc": (stamp + timedelta(seconds=OVERALL_TIMEOUT_SECONDS + CLEANUP_RESERVE_SECONDS)).isoformat(),
        "plan_sha256": profile.plan_sha256, "source_manifest_sha256": profile.manifest_sha256,
        "runtime_assets": profile.runtime_assets, "cells": list(profile.cells), "retry_count": 0,
        "max_concurrent_owned_children": 1, "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
        "overall_timeout_seconds": OVERALL_TIMEOUT_SECONDS,
        "cleanup_reserve_seconds": CLEANUP_RESERVE_SECONDS,
    }
    validate_grant(profile, grant, owner_session=owner_session, job_id=job_id,
                   output_root=output_root, claim_root=claim_root, now=stamp)
    return grant


def validate_grant(profile: FrozenProfile, grant: dict[str, Any], *, owner_session: str, job_id: str,
                   output_root: Path, claim_root: Path, now: datetime | None = None) -> None:
    """Validate the backend-minted grant before handing it to the frozen Regna runner."""
    stamp = now or datetime.now(UTC)
    try:
        expiry = datetime.fromisoformat(grant["expires_at_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProfileError("grant expiry is invalid") from exc
    expected_cells = list(profile.cells)
    if expiry.tzinfo is None or expiry <= stamp + timedelta(seconds=CLEANUP_RESERVE_SECONDS):
        raise ProfileError("grant is expired or cannot cover cleanup reserve")
    if (grant.get("schema") != "regna-n3-runtime-grant-r4" or grant.get("authorized_dispatch") is not True
            or grant.get("scope_id") != "regna-independent-holdout-n3-r4"
            or grant.get("owner_id") != owner_session or grant.get("engine") != "ttakkari-host-cpu"
            or grant.get("invocation_id") != job_id):
        raise ProfileError("grant owner, work scope or invocation binding mismatch")
    if (grant.get("plan_sha256") != profile.plan_sha256
            or grant.get("source_manifest_sha256") != profile.manifest_sha256
            or grant.get("runtime_assets") != profile.runtime_assets
            or grant.get("cells") != expected_cells):
        raise ProfileError("grant frozen plan, source, asset or cell binding mismatch")
    if (grant.get("output_root") != str(output_root.resolve())
            or grant.get("claim_root") != str(claim_root.resolve())):
        raise ProfileError("grant output or claim path differs from the server-owned path")
    if (grant.get("retry_count") != 0 or grant.get("max_concurrent_owned_children") != 1
            or grant.get("cell_timeout_seconds") != CELL_TIMEOUT_SECONDS
            or grant.get("overall_timeout_seconds") != OVERALL_TIMEOUT_SECONDS
            or grant.get("cleanup_reserve_seconds") != CLEANUP_RESERVE_SECONDS
            or not isinstance(grant.get("grant_id"), str) or not grant["grant_id"]):
        raise ProfileError("grant execution limits differ from the fixed profile")


def preflight(psutil_module: Any, *, minimum_available_bytes: int = MAX_RSS_BYTES) -> dict[str, Any]:
    """Server-side live memory and process observation; any incomplete probe rejects admission."""
    try:
        memory = psutil_module.virtual_memory()
        available = int(memory.available)
        uid = os.getuid()
        matching = []
        process_count = 0
        for process in psutil_module.process_iter(["pid", "cmdline", "create_time", "uids", "status", "exe"]):
            info = process.info
            uids = info.get("uids")
            if uids is not None and uids.real != uid:
                continue
            if info.get("status") == psutil_module.STATUS_ZOMBIE:
                continue
            command = info.get("cmdline")
            created = info.get("create_time")
            if command is None or created is None or info.get("status") is None:
                executable = info.get("exe")
                if (command is None and isinstance(executable, str) and executable
                        and _is_foreign_setuid_binary(executable)):
                    continue
                raise PreflightError("host process observation is incomplete")
            if not isinstance(command, (list, tuple)) or type(info.get("pid")) is not int:
                raise PreflightError("host process observation is incomplete")
            process_count += 1
            rendered = " ".join(command).lower()
            if "llama-server" in rendered or "ttakkari.host_gpu" in rendered:
                matching.append({"pid": info["pid"], "command": rendered[:500]})
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError(f"host process or memory observation failed: {type(exc).__name__}") from exc
    if available < minimum_available_bytes:
        raise PreflightError("available host memory is below the pinned RSS ceiling")
    if matching:
        raise PreflightError("a competing local model runtime is present")
    return {"available_memory_bytes": available, "competing_runtimes": [],
            "process_count": process_count, "observed_at": datetime.now(UTC).isoformat()}


def _is_foreign_setuid_binary(executable: str) -> bool:
    """Allow an unreadable foreign login binary; an unknown or own executable stays fail-closed."""
    if os.path.realpath(executable) == os.path.realpath(EXPECTED_ASSETS["binary"][0]):
        return False
    try:
        mode = os.stat(executable).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and bool(mode & (stat.S_ISUID | stat.S_ISGID))


def verify_raw_evidence(output_root: Path) -> dict[str, Any]:
    try:
        receipt = json.loads((output_root / "receipt.json").read_text(encoding="utf-8"))
        cell_log = json.loads((output_root / "cells.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"verified": False, "reason": f"raw receipt missing or unreadable: {type(exc).__name__}"}
    expected = [cell for _, cell, _ in EXPECTED_CELLS]
    if not isinstance(receipt, dict) or not isinstance(cell_log, dict):
        return {"verified": False, "reason": "raw receipt or cell log is not an object"}
    rows = cell_log.get("cells")
    receipt_cells = receipt.get("cells")
    cleanup = receipt.get("cleanup_verified")
    if (receipt.get("execution_status") != "complete" or receipt.get("cells_attempted") != expected
            or not isinstance(cleanup, dict) or cleanup.get("verified") is not True
            or receipt.get("raw_records_durable") is not True or not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
            or not isinstance(receipt_cells, list) or receipt_cells != rows
            or [row.get("cell_id") for row in rows] != expected):
        return {"verified": False, "reason": "receipt does not prove all pinned cells, raw records and cleanup"}
    seen_raw_paths: set[str] = set()
    for row in rows:
        if (row.get("raw_records_durable") is not True or row.get("cleanup_verified") is not True
                or row.get("usage_state") != "observed_exact_integer"):
            return {"verified": False, "reason": f"cell raw or cleanup evidence incomplete: {row.get('cell_id')}"}
        case_id, _, seed = next(item for item in EXPECTED_CELLS if item[1] == row["cell_id"])
        try:
            cell_record = json.loads((output_root / "cells" / f"{case_id}-s{seed}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"verified": False, "reason": f"cell raw record missing: {row['cell_id']} ({type(exc).__name__})"}
        if not isinstance(cell_record, dict):
            return {"verified": False, "reason": f"cell raw record is not an object: {row['cell_id']}"}
        usage = cell_record.get("usage")
        if (not isinstance(usage, dict)
                or any(type(usage.get(key)) is not int or usage[key] < 0
                       for key in ("prompt_tokens", "completion_tokens"))):
            return {"verified": False, "reason": f"cell tokenizer usage is missing or not integer: {row['cell_id']}"}
        generations = cell_record.get("generations")
        if not isinstance(generations, list) or not generations or any(not isinstance(row, dict) for row in generations):
            return {"verified": False, "reason": f"cell request/response generations are missing: {row['cell_id']}"}
        if cell_record.get("case_id") != case_id or cell_record.get("seed") != seed:
            return {"verified": False, "reason": f"cell record identity mismatch: {row['cell_id']}"}
        generation_ids = [generation.get("generation") for generation in generations]
        if generation_ids != list(range(len(generations))):
            return {"verified": False, "reason": f"cell generation order is invalid: {row['cell_id']}"}
        for generation in generations:
            ref = generation.get("raw_ref")
            if not isinstance(ref, dict) or ref.get("readback_verified") is not True:
                return {"verified": False, "reason": f"cell raw record was not durably read back: {row['cell_id']}"}
            if ref.get("request_sha256_verified") is not True or ref.get("sse_bytes_verified") is not True:
                return {"verified": False, "reason": f"cell request/SSE hashes are not verified: {row['cell_id']}"}
            relative_path = ref.get("path")
            if not isinstance(relative_path, str) or not relative_path:
                return {"verified": False, "reason": f"cell raw evidence path is invalid: {row['cell_id']}"}
            generation_id = generation["generation"]
            suffix = "-invalid-tool" if "model_tool_error" in generation else ""
            expected_path = f"raw/{case_id}-s{seed}-g{generation_id}{suffix}.json"
            if relative_path != expected_path or relative_path in seen_raw_paths:
                return {"verified": False, "reason": f"cell raw evidence is misbound or reused: {row['cell_id']}"}
            seen_raw_paths.add(relative_path)
            raw_path = (output_root / relative_path).resolve()
            if not raw_path.is_relative_to(output_root.resolve()) or not raw_path.is_file():
                return {"verified": False, "reason": f"cell raw evidence path is missing or escaped: {row['cell_id']}"}
            if (_sha256(raw_path) != ref.get("sha256")
                    or raw_path.stat().st_size != ref.get("bytes")):
                return {"verified": False, "reason": f"cell raw evidence file hash mismatch: {row['cell_id']}"}
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                receipts = raw.get("request_receipts") if isinstance(raw, dict) else None
                if (not isinstance(raw, dict) or raw.get("generation") != generation_id
                        or receipts != generation.get("request_receipts")
                        or not isinstance(receipts, list) or not _verify_request_sse(receipts)):
                    raise EvidenceError("request or SSE byte hash mismatch")
            except (OSError, ValueError, EvidenceError) as exc:
                return {"verified": False, "reason": f"cell request/response/SSE evidence invalid: {row['cell_id']} ({type(exc).__name__})"}
    return {"verified": True, "cells": len(expected), "plan_sha256": PLAN_SHA256,
            "receipt_sha256": _sha256(output_root / "receipt.json")}


def verify_owned_cleanup(output_root: Path, receipt: dict[str, Any]) -> bool:
    """Require each started model child to have its own wait4 and stop receipt before lease release."""
    checks = receipt.get("cleanup_verified")
    cleanup_rows = receipt.get("cleanup")
    cells = receipt.get("cells")
    attempted = receipt.get("cells_attempted")
    if (receipt.get("backend_rss_monitor_stopped") is not True
            or not isinstance(checks, dict) or checks.get("verified") is not True
            or not isinstance(checks.get("cells"), list) or not isinstance(cleanup_rows, list)
            or not isinstance(cells, list) or not cells or len(cells) != len(cleanup_rows)
            or not isinstance(attempted, list) or len(cells) != len(attempted)
            or len(cells) != len(checks["cells"]) or len(cells) > len(EXPECTED_CELLS)):
        return False
    expected_ids = [item[1] for item in EXPECTED_CELLS[:len(cells)]]
    if attempted != expected_ids or [cell.get("cell_id") if isinstance(cell, dict) else None for cell in cells] != expected_ids:
        return False
    for index, (check, cleanup, cell) in enumerate(zip(checks["cells"], cleanup_rows, cells, strict=True)):
        case_id, _, seed = EXPECTED_CELLS[index]
        if (not isinstance(check, dict) or check.get("verified") is not True
                or not isinstance(cleanup, dict) or cleanup.get("state") != "stopped"
                or cleanup.get("stopped_by_this_run") is not True
                or isinstance(cleanup.get("exit_code"), bool) or not isinstance(cleanup.get("exit_code"), int)
                or not isinstance(cleanup.get("pid"), int) or not isinstance(cell, dict)
                or cell.get("case_id") != case_id or cell.get("seed") != seed
                or cell.get("cleanup_verified") is not True):
            return False
        process_receipt_path = output_root / f"owned-server-{index}" / "process-receipt.json"
        try:
            process_receipt = json.loads(process_receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(process_receipt, dict) or process_receipt.get("pid") != cleanup["pid"]:
            return False
        events = process_receipt.get("events")
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            return False
        wait_receipts = [event for event in events if event.get("event") == "wait4_reaped"]
        stopped_receipts = [event for event in events if event.get("event") == "stopped"]
        if (not wait_receipts or wait_receipts[-1].get("exit_code") != cleanup["exit_code"]
                or not stopped_receipts or stopped_receipts[-1].get("stopped_by_this_run") is not True
                or stopped_receipts[-1].get("exit_code") != cleanup["exit_code"]):
            return False
    return True


def _verify_request_sse(receipts: list[dict[str, Any]]) -> bool:
    request_seen = False
    sse_seen = False
    for receipt in receipts:
        if not isinstance(receipt, dict):
            return False
        body, request_hash = receipt.get("request_body"), receipt.get("request_sha256")
        if not isinstance(body, str) or not isinstance(request_hash, str):
            return False
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != request_hash:
            return False
        chunks, response_hash = receipt.get("raw_sse_chunks_base64"), receipt.get("response_sha256")
        if (not isinstance(chunks, list) or not chunks or not isinstance(response_hash, str)
                or receipt.get("capture_truncated")):
            return False
        try:
            raw_bytes = b"".join(base64.b64decode(chunk, validate=True) for chunk in chunks)
        except (binascii.Error, ValueError):
            return False
        if not raw_bytes or hashlib.sha256(raw_bytes).hexdigest() != response_hash:
            return False
        request_seen = True
        sse_seen = True
    return request_seen and sse_seen


def lease_manager(data_dir: Path) -> ResourceLeaseManager:
    return ResourceLeaseManager(data_dir=data_dir)


def release_after_cleanup(manager: ResourceLeaseManager, lease: ResourceLeaseHandle, *,
                          output_root: Path, receipt: dict[str, Any]) -> bool:
    if not verify_owned_cleanup(output_root, receipt):
        return False
    lease.release(proof={"runtime_status": "stopped", "evidence": {
        "runtime_instance": lease.runtime_instance, "owned_child_waited": True,
        "cleanup_verified": True, "cleanup_receipt_sha256": hashlib.sha256(
            json.dumps(receipt, sort_keys=True, default=str).encode()).hexdigest(),
        "operation": OPERATION_KIND,
    }})
    return True


def release_without_child(lease: ResourceLeaseHandle) -> None:
    """Release a just-created lease when the frozen dispatcher never requested a model child."""
    lease.release(proof={"runtime_status": "stopped", "evidence": {
        "runtime_instance": lease.runtime_instance, "no_child_created": True,
        "operation": OPERATION_KIND,
    }})


def operation_roots(data_dir: Path, job_id: str) -> tuple[Path, Path, Path]:
    base = Path(data_dir).resolve() / "research" / OPERATION_KIND
    return base / "runs" / job_id, base / "claims", base / "grants" / f"{job_id}.json"


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _bounded_server_factory(profile: FrozenProfile, run_root: Path, violations: list[str]):
    """Wrap the frozen r4 owned child with a live RSS ceiling and fail-closed monitor."""
    import contextlib
    import subprocess
    import threading

    import psutil
    from tools.practical_research.execution import driver

    real_popen = subprocess.Popen
    monitored: dict[int, tuple[Any, threading.Event]] = {}

    def bounded_popen(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if not isinstance(command, (list, tuple)) or not command or Path(command[0]).resolve() != Path(EXPECTED_ASSETS["binary"][0]):
            return real_popen(*args, **kwargs)
        kwargs["start_new_session"] = False
        process = real_popen(*args, **kwargs)
        stop = threading.Event()
        try:
            expected_created = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            expected_created = None
            violations.append("owned model child exited before RSS supervision began")
        except (psutil.Error, OSError, ValueError, TypeError) as exc:
            expected_created = None
            violations.append(f"owned model child identity observation failed: {type(exc).__name__}")
            with contextlib.suppress(OSError):
                process.terminate()

        def monitor() -> None:
            try:
                if expected_created is None:
                    return
                while not stop.wait(0.1):
                    current = psutil.Process(process.pid)
                    if abs(current.create_time() - expected_created) > 1:
                        violations.append("owned model process identity changed")
                        return
                    if current.status() == psutil.STATUS_ZOMBIE:
                        return
                    rss = current.memory_info().rss
                    if rss > profile.max_rss_bytes:
                        verify_identity = psutil.Process(process.pid)
                        if abs(verify_identity.create_time() - expected_created) > 1:
                            violations.append("owned model process identity changed before RSS stop")
                            return
                        violations.append("owned model process exceeded the 6 GiB RSS ceiling")
                        process.terminate()
                        return
            except psutil.NoSuchProcess:
                return
            except (psutil.Error, OSError, ValueError) as exc:
                violations.append(f"owned model process RSS monitor failed: {type(exc).__name__}")
                with contextlib.suppress(OSError):
                    process.terminate()

        try:
            thread = threading.Thread(target=monitor, name=f"regna-rss-{process.pid}", daemon=True)
            thread.start()
        except (OSError, RuntimeError) as exc:
            violations.append(f"owned model RSS monitor failed to start: {type(exc).__name__}")
            with contextlib.suppress(OSError):
                process.terminate()
            return process
        monitored[process.pid] = (thread, stop)
        return process

    def make_server(args, run_dir: Path):
        child = driver.MeasuredOwnedServer(
            args.runtime_binary.resolve(), args.model.resolve(), args.template.resolve(), run_dir,
            args.context_size, args.startup_timeout, args.request_timeout, args.threads,
        )
        original_stop = child.stop

        def stop():
            item = monitored.get(child.proc.pid) if child.proc else None
            if item:
                item[1].set()
                item[0].join(timeout=1)
            return original_stop()

        original_start = child.start

        def start():
            adapter = original_start()
            if violations:
                child.stop()
                raise PreflightError("owned model RSS supervision failed before request execution")
            return adapter

        child.stop = stop
        child.start = start
        return child

    driver.subprocess.Popen = bounded_popen
    return make_server, monitored


def _dispatch_server_factory(factory, args, output_root: Path):
    """Bind a path-free zero-argument factory to unique server directories for the frozen dispatcher."""
    cell_number = 0

    def owned_server():
        nonlocal cell_number
        run_dir = output_root / f"owned-server-{cell_number}"
        cell_number += 1
        return factory(args, run_dir)

    return owned_server


def run_operation(job_id: str) -> int:
    """Create a fresh bound grant/lease, then invoke only the frozen r4 six-cell runner."""
    import contextlib

    import psutil

    from ttakkari.config import get_settings
    from ttakkari.operations import jobs

    job = jobs.get(job_id)
    if (job.get("kind") != OPERATION_KIND or not job.get("requested_by_session")
            or job.get("q25_scope") != "work"):
        raise ProfileError("operation owner or kind is invalid")
    owner_session = job["requested_by_session"]
    profile = load_profile()
    environment = preflight(psutil)
    data_dir = get_settings().data_dir
    output_root, claim_root, grant_path = operation_roots(data_dir, job_id)
    manager = lease_manager(data_dir)
    output_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output_root.parent.resolve(strict=True) != output_root.parent
            or output_root.parent.stat().st_mode & 0o077):
        raise ProfileError("server-owned Regna evidence directory is not private")
    lease = manager.acquire(
        owner_task=job_id, owner_attempt=owner_session, runtime_instance=job_id,
        resource_family="llm", claim={"profile": PROFILE_ID, "scope": "work_cpu_research",
                                      "plan_sha256": profile.plan_sha256,
                                      "cells": [x["cell_id"] for x in profile.cells]},
        ttl_seconds=LEASE_TTL_SECONDS,
    )
    durable_lease = False
    child_possible = False
    try:
        claim_first6(profile, claim_root, owner_session=owner_session, job_id=job_id)
        grant = issue_grant(profile, owner_session=owner_session, job_id=job_id,
                            output_root=output_root, claim_root=claim_root)
        _write_private_json(grant_path, grant)
        jobs.update(job_id, q25_lease_id=lease.lease_id, q25_generation_token=lease.generation_token,
                    q25_runtime_instance=lease.runtime_instance, q25_preflight=environment,
                    q25_grant_id=grant["grant_id"], q25_evidence_root=str(output_root))
        lease.mark_running()

        argv = build_runner_argv(profile, job_id=job_id, owner_session=owner_session,
                                 grant_path=grant_path, output_root=output_root, claim_root=claim_root)
        parser = _load_r4_runner(profile)
        args = parser._parser().parse_args(argv[2:])
        violations: list[str] = []
        factory, monitored = _bounded_server_factory(profile, output_root, violations)
        def record_child_possible(actual_args, run_dir):
            nonlocal child_possible
            child_possible = True
            return factory(actual_args, run_dir)

        owned_server = _dispatch_server_factory(record_child_possible, args, output_root)
        receipt = parser.dispatch(args, server_factory=owned_server)
        monitors_stopped = True
        for thread, stop in monitored.values():
            stop.set()
            thread.join(timeout=1)
            if thread.is_alive():
                monitors_stopped = False
                violations.append("owned model RSS monitor did not stop before dispatch completion")
        receipt["backend_rss_monitor_stopped"] = monitors_stopped
        if violations:
            receipt["execution_status"] = "failed"
            receipt["exit_code"] = 1
            receipt["failure"] = "; ".join(violations)[:800]
            parser.write_json(output_root / "receipt.json", receipt)
        cleanup = verify_owned_cleanup(output_root, receipt)
        if cleanup:
            if not release_after_cleanup(manager, lease, output_root=output_root, receipt=receipt):
                raise EvidenceError("owned-child wait/exit cleanup proof changed before lease release")
            durable_lease = True
        elif not child_possible:
            release_without_child(lease)
            durable_lease = True
        else:
            manager.request_cancel(lease.lease_id, lease.generation_token)
        evidence = verify_raw_evidence(output_root)
        summary = {"execution_status": receipt.get("execution_status"), "run_verdict": receipt.get("run_verdict"),
                   "cells_attempted": receipt.get("cells_attempted_count"), "raw_evidence": evidence,
                   "cleanup_verified": cleanup, "plan_sha256": profile.plan_sha256,
                   "grant_id": grant["grant_id"], "lease_id": lease.lease_id,
                   "evidence_path": str(output_root)}
        jobs.update(job_id, q25_summary=summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if evidence.get("verified") is True and not violations else 1
    except BaseException:
        if not child_possible and not durable_lease:
            with contextlib.suppress(Exception):
                release_without_child(lease)
        elif not durable_lease:
            with contextlib.suppress(Exception):
                manager.request_cancel(lease.lease_id, lease.generation_token)
        raise


def _load_r4_runner(profile: FrozenProfile):
    import sys
    import types

    if profile.manifest_sha256 != SOURCE_MANIFEST_SHA256:
        raise ProfileError("frozen source manifest profile hash mismatch")
    manifest = _read_frozen_manifest(profile.manifest_path)
    _verify_manifest_files(profile.root, manifest)
    if str(profile.root) not in sys.path:
        sys.path.insert(0, str(profile.root))
    source = profile.runner_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != RUNNER_SHA256:
        raise ProfileError("frozen r4 runner changed before load")
    module_name = "regna_n3_runtime_r4_frozen"
    module = types.ModuleType(module_name)
    module.__file__ = str(profile.runner_path)
    sys.modules[module_name] = module
    exec(compile(source, str(profile.runner_path), "exec"), module.__dict__)  # noqa: S102 - source SHA is verified above
    return module


def recover_lease_after_owner_exit(job: dict, data_dir: Path, *,
                                   operation_runner_confirmed_group_absent: bool) -> bool:
    """Release a dead helper's lease only after its entire owned process group is independently absent."""
    if operation_runner_confirmed_group_absent is not True:
        return False
    manager = lease_manager(data_dir)
    lease_id = job.get("q25_lease_id")
    generation = job.get("q25_generation_token")
    runtime = job.get("q25_runtime_instance")
    if not (lease_id and generation and runtime):
        current = manager.current()
        if not current or current.get("owner_task") != job.get("id"):
            return True  # the helper died before minting a lease
        lease_id = current.get("lease_id")
        generation = current.get("generation_token")
        runtime = current.get("runtime_instance")
    current = manager.current()
    if not current:
        return manager.recorded_phase(lease_id, generation) == "released"
    if (current.get("lease_id") != lease_id or current.get("generation_token") != generation
            or current.get("owner_task") != job.get("id") or current.get("runtime_instance") != runtime
            or current.get("owner_attempt") != job.get("requested_by_session")
            or current.get("resource_family") != "llm"):
        return False
    if manager.recorded_phase(lease_id, generation) == "released":
        return True
    try:
        manager.reconcile(lease_id, generation, proof={"runtime_status": "stopped", "evidence": {
            "runtime_instance": runtime, "operation_runner_confirmed_group_absent": True,
            "raw_evidence_verified": bool(verify_raw_evidence(
                Path(job.get("q25_evidence_root") or operation_roots(data_dir, job["id"])[0])).get("verified")),
        }})
    except (LeaseError, OSError, ValueError, KeyError):
        return False
    return True


def operation_summary(job_id: str, data_dir: Path) -> dict[str, Any]:
    job = __import__("ttakkari.operations.jobs", fromlist=["get"]).get(job_id)
    summary = job.get("q25_summary")
    output_root = Path(job.get("q25_evidence_root") or operation_roots(data_dir, job_id)[0])
    raw = verify_raw_evidence(output_root)
    cleanup_verified = False
    try:
        receipt = json.loads((output_root / "receipt.json").read_text(encoding="utf-8"))
        cleanup_verified = isinstance(receipt, dict) and verify_owned_cleanup(output_root, receipt)
    except (OSError, ValueError):
        pass
    lease_id, generation = job.get("q25_lease_id"), job.get("q25_generation_token")
    if lease_id and generation:
        phase = lease_manager(data_dir).recorded_phase(lease_id, generation)
        lease_held = phase in ResourceLeaseManager.LIVE_PHASES
    else:
        lease_held = False
    if isinstance(summary, dict):
        cleanup_verified = cleanup_verified and summary.get("cleanup_verified") is True
        return {**summary, "raw_evidence": raw, "cleanup_verified": cleanup_verified,
                "lease_held": lease_held,
                "evidence_path": str(output_root)}
    return {"execution_status": "unknown", "raw_evidence": raw,
            "cleanup_verified": cleanup_verified or not lease_held, "lease_held": lease_held,
            "evidence_path": str(output_root)}


def main(argv: list[str] | None = None) -> int:
    import sys

    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 2 or values[0] != "run":
        return 2
    try:
        return run_operation(values[1])
    except Exception as exc:  # noqa: BLE001 - fixed worker boundary must serialize every failure as non-success
        print(json.dumps({"execution_status": "rejected", "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 2


def build_runner_argv(profile: FrozenProfile, *, job_id: str, owner_session: str,
                      grant_path: Path, output_root: Path, claim_root: Path) -> list[str]:
    """Fixed argv builder; the HTTP/API caller cannot influence any executable or path."""
    import sys

    return [sys.executable, str(profile.runner_path), "--plan", str(profile.plan_path),
            "--source-manifest", str(profile.manifest_path), "--grant", str(grant_path),
            "--claim-root", str(claim_root), "--output", str(output_root),
            "--runtime-binary", EXPECTED_ASSETS["binary"][0], "--model", EXPECTED_ASSETS["model"][0],
            "--template", EXPECTED_ASSETS["template"][0], "--owner-id", owner_session,
            "--engine", "ttakkari-host-cpu", "--invocation-id", job_id,
            "--context-size", "32768", "--threads", "6", "--startup-timeout", "120", "--request-timeout", "120"]


if __name__ == "__main__":
    raise SystemExit(main())
