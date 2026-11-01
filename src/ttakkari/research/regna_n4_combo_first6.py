"""Narrow host CPU connector for the independently accepted Regna N4 r2 first batch.

The HTTP caller can select only this operation kind. Plan, inputs, model/runtime,
generation settings, batch and output location are pinned by this module.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ttakkari.host_resource import LeaseError, ResourceLeaseManager

OPERATION_KIND = "regna_n4_combo_first6_cpu"
PROFILE_ID = "regna-n4-combo-qwen3-1.7b-q4km-first6-cpu-r2"
REGNA_ROOT = Path("/Users/winterholic/development/hyperez/regna-ios")
FINAL_ROOT = REGNA_ROOT / "verification/n4-r2-independent-20261102/final-isolated"
PREP_REL = Path(
    "docs/research/practical-20261029/execution/preparation/n4-combo-runtime-r2-20261102"
)
PLAN_REL = PREP_REL / "plan.json"
PINS_REL = PREP_REL / "source-pins.json"
PLAN_SHA256 = "4b0888456b1aa079b0cb23764fa9a8cd14ad3c7c7e90381e0ed5415a36dc6a19"
PINS_SHA256 = "765c36b5e0323bdf9bd4f06f009c96e231b4bf54224e135aa433a8907ce3c82d"
N3_COMMON_ADAPTER_SHA256 = (
    "041b76b997428c04826521d2e401f4443ebf270046b5dbe8fc9fc7a65dc0b283"
)
MAX_CELLS = 6
CELL_TIMEOUT_SECONDS = 120
OVERALL_TIMEOUT_SECONDS = 900
CLEANUP_RESERVE_SECONDS = 30
LEASE_TTL_SECONDS = 1200
MAX_RSS_BYTES = 6 * 1024**3
ENGINE = "ttakkari-host-cpu"
LEDGER_ROOT = REGNA_ROOT / "docs/research/practical-20261029/execution/runs"
BASELINE_LEDGER = (
    REGNA_ROOT
    / "docs/research/practical-20261029/lead-review/first-baseline-review-inputs/baseline-attempts.jsonl"
)


class ProfileError(ValueError):
    pass


class DuplicateClaim(ProfileError):
    pass


class BaselineReuseError(ProfileError):
    def __init__(self, matches: list[dict[str, Any]], ledger_sha256: str):
        self.matches = matches
        self.ledger_sha256 = ledger_sha256
        super().__init__(
            f"exact prior baseline request found for {len(matches)} fixed N4 cell(s); refusing execution"
        )


class PreflightError(ProfileError):
    pass


class EvidenceError(ProfileError):
    pass


@dataclass(frozen=True)
class FrozenProfile:
    root: Path
    plan_path: Path
    source_pins_path: Path
    plan_sha256: str
    source_pins_bytes_sha256: str
    source_manifest_sha256: str
    plan: dict[str, Any]
    source_pins: dict[str, Any]
    batch: dict[str, Any]
    profile: dict[str, Any]
    input_identity_sha256_by_cell: dict[str, str]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_bytes(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def _read_pinned_json(
    path: Path, expected_sha: str, label: str
) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        if sha256_bytes(raw) != expected_sha:
            raise ProfileError(f"pinned {label} bytes changed")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"pinned {label} cannot be read") from exc
    if not isinstance(value, dict):
        raise ProfileError(f"pinned {label} root must be an object")
    return raw, value


def _load_dispatcher(root: Path):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module(
        "tools.practical_research.execution.n4_combo_holdout_runtime_r2"
    )


def load_profile(root: Path | str = FINAL_ROOT) -> FrozenProfile:
    root = Path(root).resolve(strict=True)
    plan_path, pins_path = root / PLAN_REL, root / PINS_REL
    plan_raw, plan = _read_pinned_json(plan_path, PLAN_SHA256, "N4 plan")
    pins_raw, pins = _read_pinned_json(pins_path, PINS_SHA256, "N4 source pins")
    if canonical_digest(pins) != plan.get("source_manifest_sha256"):
        raise ProfileError("plan does not bind the exact source-pins bytes")
    files = pins.get("files")
    if not isinstance(files, dict) or len(files) != 15:
        raise ProfileError("N4 source pin set must contain exactly 15 files")
    for relative, pin in files.items():
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise ProfileError("N4 source pin contains an unsafe path")
        unresolved = root / rel
        if unresolved.is_symlink():
            raise ProfileError("N4 source pin must not be a symlink")
        target = unresolved.resolve(strict=True)
        if not target.is_relative_to(root):
            raise ProfileError("N4 source pin escapes the final-isolated snapshot")
        if target.stat().st_size != pin.get("bytes") or sha256_file(target) != pin.get(
            "sha256"
        ):
            raise ProfileError(f"N4 frozen source pin mismatch: {relative}")
    if len(plan_raw) == 0 or sha256_bytes(plan_raw) != PLAN_SHA256:
        raise ProfileError("N4 plan changed while being parsed")
    manifest_rel = Path("tools/practical_research/independent_holdout/manifest.json")
    manifest_raw = (root / manifest_rel).read_bytes()
    manifest = json.loads(manifest_raw)
    dispatcher = _load_dispatcher(root)
    validation = dispatcher.validate_plan(
        plan, manifest=manifest, profile=dispatcher.PROFILE, pins=pins
    )
    if (
        validation.get("first_batch_cells") != MAX_CELLS
        or plan["batches"][0]["batch_id"] != "batch-01"
    ):
        raise ProfileError("N4 first batch is not exactly six cells")
    if plan.get("budget") != {
        "max_cells_per_grant": 6,
        "cell_timeout_seconds": 120,
        "overall_timeout_seconds": 900,
        "cleanup_reserve_seconds": 30,
    }:
        raise ProfileError("N4 plan budget differs from the fixed CPU operation")
    if (
        plan.get("execution_identity_binding", {}).get("generation_mode")
        != dispatcher.PROFILE["generation_mode"]
    ):
        raise ProfileError("N4 generation mode differs from pinned Qwen3 profile")
    identity = dispatcher.input_identity_sha256_by_cell(
        plan["batches"][0], profile=dispatcher.PROFILE
    )
    if len(identity) != MAX_CELLS or set(identity) != {
        x["cell_id"] for x in plan["batches"][0]["cells"]
    }:
        raise ProfileError("N4 exact input identity map is incomplete")
    return FrozenProfile(
        root,
        plan_path,
        pins_path,
        sha256_bytes(plan_raw),
        sha256_bytes(pins_raw),
        plan["source_manifest_sha256"],
        plan,
        pins,
        plan["batches"][0],
        dispatcher.PROFILE,
        identity,
    )


def parse_operation_input(payload: Any = None) -> None:
    if payload not in (None, {}):
        raise ProfileError("operation accepts no caller-supplied arguments")


def _ledger_files() -> list[Path]:
    files = [BASELINE_LEDGER] if BASELINE_LEDGER.is_file() else []
    if LEDGER_ROOT.is_dir():
        files.extend(LEDGER_ROOT.rglob("attempts.jsonl"))
        files.extend(LEDGER_ROOT.rglob("receipt.json"))
    return sorted(
        {p.resolve() for p in files if p.is_file() and not p.is_symlink()}, key=str
    )


def _ledger_digest(files: list[Path]) -> tuple[str, list[dict[str, Any]]]:
    rows = [
        {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in files
    ]
    return canonical_digest(rows), rows


def _walk_request_records(value: Any, source: Path):
    if isinstance(value, dict):
        raw = value.get("request_body")
        if isinstance(raw, str):
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProfileError(
                    f"prior-attempt request body is invalid JSON: {source}"
                ) from exc
            if isinstance(body, dict):
                yield sha256_bytes(raw.encode("utf-8")), source, value
        elif isinstance(raw, dict):
            serialized = json.dumps(
                raw, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            yield sha256_bytes(serialized), source, value
        for item in value.values():
            yield from _walk_request_records(item, source)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_request_records(item, source)


def _read_attempt_values(path: Path, content_digest):
    if path.name == "attempts.jsonl" or path.name.endswith("attempts.jsonl"):
        with path.open("rb") as stream:
            for line_number, line in enumerate(stream, start=1):
                content_digest.update(line)
                if line.strip():
                    try:
                        yield json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise ProfileError(
                            f"prior-attempt ledger contains invalid JSON at {path}:{line_number}"
                        ) from exc
    else:
        raw = path.read_bytes()
        content_digest.update(raw)
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProfileError(
                f"prior-attempt receipt contains invalid JSON: {path}"
            ) from exc


def _expected_baseline_request_hashes(profile: FrozenProfile) -> dict[str, str]:
    dispatcher = _load_dispatcher(profile.root)
    bundle = dispatcher.load_runtime_bundle()
    expected = {}
    for cell in profile.batch["cells"]:
        if cell["arm"] != "baseline":
            continue
        request = dispatcher.build_request(
            dispatcher._input_for(bundle, cell["case_id"]),
            arm=cell["arm"],
            seed=cell["seed"],
            generation_mode=profile.profile["generation_mode"],
        )
        serialized = dispatcher._expected_chat_request_bytes(
            request["messages"], request["seed"], profile=profile.profile
        )
        expected[cell["cell_id"]] = sha256_bytes(serialized)
    return expected


def review_prior_attempts(
    profile: FrozenProfile, *, ledger_files: list[Path] | None = None
) -> dict[str, Any]:
    """Compare exact serialized baseline HTTP requests against actual historical attempts."""
    files = _ledger_files() if ledger_files is None else sorted(ledger_files, key=str)
    if not files:
        raise ProfileError(
            "actual prior-attempt ledger is unavailable; refusing to authorize a new batch"
        )
    expected = _expected_baseline_request_hashes(profile)
    matches: list[dict[str, Any]] = []
    scanned = 0
    ledger_file_rows = []
    for path in files:
        before = path.stat()
        content_digest = hashlib.sha256()
        for value in _read_attempt_values(path, content_digest):
            scanned += 1
            for digest, source, record in _walk_request_records(value, path):
                for cell_id, expected_digest in expected.items():
                    if digest == expected_digest:
                        matches.append(
                            {
                                "cell_id": cell_id,
                                "request_sha256": digest,
                                "source_path": str(source),
                                "record_status": record.get("status"),
                                "evidence_record_sha256": canonical_digest(record),
                            }
                        )
        after = path.stat()
        before_identity = (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise ProfileError(
                f"prior-attempt ledger changed while being reviewed: {path}"
            )
        ledger_file_rows.append(
            {
                "path": str(path),
                "bytes": after.st_size,
                "sha256": content_digest.hexdigest(),
            }
        )
    if _ledger_digest(files)[1] != ledger_file_rows:
        raise ProfileError(
            "prior-attempt ledger changed between request review and grant"
        )
    ledger_sha = canonical_digest(ledger_file_rows)
    return {
        "ledger_sha256": ledger_sha,
        "ledger_files": ledger_file_rows,
        "reviewed": True,
        "baseline_reuse_decision": "exact_baseline_reuse_found"
        if matches
        else "no_exact_baseline_reuse",
        "exact_baseline_matches": matches,
        "records_scanned": scanned,
        "input_identity_sha256_by_cell": dict(profile.input_identity_sha256_by_cell),
    }


def _grant_body(
    profile: FrozenProfile,
    *,
    owner_session: str,
    job_id: str,
    lease_id: str,
    generation_token: str,
    output_root: Path,
    review: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    return {
        "grant_id": secrets.token_hex(16),
        "owner_id": owner_session,
        "engine": ENGINE,
        "invocation_id": job_id,
        "lease_id": lease_id,
        "lease_generation_token": generation_token,
        "expires_at_utc": (
            now + timedelta(seconds=OVERALL_TIMEOUT_SECONDS + CLEANUP_RESERVE_SECONDS)
        ).isoformat(),
        "fresh_owner_bound": True,
        "execution_authorized": True,
        "host_deployment_supported": True,
        "single_execution_owner_confirmed": True,
        "plan_sha256": canonical_digest(profile.plan),
        "plan_bytes_sha256": profile.plan_sha256,
        "source_manifest_sha256": profile.source_manifest_sha256,
        "source_pins_bytes_sha256": profile.source_pins_bytes_sha256,
        "batch_id": profile.batch["batch_id"],
        "cells": profile.batch["cells"],
        "asset_sha256": profile.profile["asset_sha256"],
        "profile_name": "Qwen3-1.7B-Q4_K_M",
        "family": "Qwen3",
        "native_context_tokens": 32768,
        "generation_mode": profile.profile["generation_mode"],
        "tool_schema_sha256": profile.plan["request_contract"]["tool_schema_sha256"],
        "input_manifest_sha256": profile.plan["manifest_sha256"],
        "seeds": [1701, 1702, 1703],
        "input_identity_sha256_by_cell": profile.input_identity_sha256_by_cell,
        "prior_attempt_review": {
            "ledger_sha256": review["ledger_sha256"],
            "reviewed": True,
            "baseline_reuse_decision": review["baseline_reuse_decision"],
            "exact_baseline_matches": review["exact_baseline_matches"],
        },
        "prior_attempt_review_evidence": review,
        "max_cells": MAX_CELLS,
        "cell_timeout_seconds": CELL_TIMEOUT_SECONDS,
        "overall_timeout_seconds": OVERALL_TIMEOUT_SECONDS,
        "cleanup_reserve_seconds": CLEANUP_RESERVE_SECONDS,
        "output_root": str(output_root.resolve()),
    }


def issue_grant(
    profile: FrozenProfile,
    *,
    owner_session: str,
    job_id: str,
    lease_id: str,
    generation_token: str,
    attempt_ledger: Path | None = None,
    output_root: Path,
    ledger_files: list[Path] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not all((owner_session, job_id, lease_id, generation_token)):
        raise ProfileError(
            "grant must bind owner session, operation, lease and lease generation"
        )
    if attempt_ledger is not None:
        ledger_files = [attempt_ledger]
    review = review_prior_attempts(profile, ledger_files=ledger_files)
    if review["exact_baseline_matches"]:
        raise BaselineReuseError(
            review["exact_baseline_matches"], review["ledger_sha256"]
        )
    grant = _grant_body(
        profile,
        owner_session=owner_session,
        job_id=job_id,
        lease_id=lease_id,
        generation_token=generation_token,
        output_root=output_root,
        review=review,
        now=now or datetime.now(UTC),
    )
    validate_grant(
        profile,
        grant,
        owner_session=owner_session,
        job_id=job_id,
        lease_id=lease_id,
        generation_token=generation_token,
        output_root=output_root,
        now=now,
    )
    return grant


def validate_grant(
    profile: FrozenProfile,
    grant: dict[str, Any],
    *,
    owner_session: str,
    job_id: str,
    lease_id: str,
    generation_token: str,
    output_root: Path,
    now: datetime | None = None,
) -> None:
    stamp = now or datetime.now(UTC)
    try:
        expiry = datetime.fromisoformat(grant["expires_at_utc"])
    except (KeyError, AttributeError, ValueError) as exc:
        raise ProfileError("grant expiry is invalid") from exc
    if expiry.tzinfo is None or expiry <= stamp + timedelta(
        seconds=CLEANUP_RESERVE_SECONDS
    ):
        raise ProfileError("grant expired or cannot cover cleanup reserve")
    identity = {
        "owner_id": owner_session,
        "invocation_id": job_id,
        "lease_id": lease_id,
        "lease_generation_token": generation_token,
        "engine": ENGINE,
        "plan_sha256": canonical_digest(profile.plan),
        "plan_bytes_sha256": profile.plan_sha256,
        "source_manifest_sha256": profile.source_manifest_sha256,
        "source_pins_bytes_sha256": profile.source_pins_bytes_sha256,
        "batch_id": "batch-01",
        "cells": profile.batch["cells"],
        "asset_sha256": profile.profile["asset_sha256"],
        "profile_name": "Qwen3-1.7B-Q4_K_M",
        "family": "Qwen3",
        "native_context_tokens": 32768,
        "generation_mode": profile.profile["generation_mode"],
        "tool_schema_sha256": profile.plan["request_contract"]["tool_schema_sha256"],
        "input_manifest_sha256": profile.plan["manifest_sha256"],
        "seeds": [1701, 1702, 1703],
        "input_identity_sha256_by_cell": profile.input_identity_sha256_by_cell,
        "max_cells": 6,
        "cell_timeout_seconds": 120,
        "overall_timeout_seconds": 900,
        "cleanup_reserve_seconds": 30,
        "output_root": str(output_root.resolve()),
    }
    for key, value in identity.items():
        if grant.get(key) != value:
            raise ProfileError(f"grant binding mismatch: {key}")
    for flag in (
        "fresh_owner_bound",
        "execution_authorized",
        "host_deployment_supported",
        "single_execution_owner_confirmed",
    ):
        if grant.get(flag) is not True:
            raise ProfileError(f"grant authorization missing: {flag}")
    review = grant.get("prior_attempt_review")
    evidence = grant.get("prior_attempt_review_evidence")
    if (
        not isinstance(review, dict)
        or not isinstance(evidence, dict)
        or review.get("reviewed") is not True
        or evidence.get("reviewed") is not True
        or review.get("ledger_sha256") != evidence.get("ledger_sha256")
        or review.get("baseline_reuse_decision") != "no_exact_baseline_reuse"
        or evidence.get("baseline_reuse_decision") != "no_exact_baseline_reuse"
        or evidence.get("exact_baseline_matches") != []
        or evidence.get("input_identity_sha256_by_cell")
        != profile.input_identity_sha256_by_cell
    ):
        raise ProfileError(
            "prior-attempt review is missing, inconsistent or found an exact baseline"
        )
    ledger_sha = review.get("ledger_sha256")
    if (
        not isinstance(ledger_sha, str)
        or len(ledger_sha) != 64
        or any(c not in "0123456789abcdef" for c in ledger_sha)
    ):
        raise ProfileError("prior-attempt ledger identity is invalid")
    grant_id = grant.get("grant_id")
    if (
        not isinstance(grant_id, str)
        or len(grant_id) != 32
        or any(c not in "0123456789abcdef" for c in grant_id)
    ):
        raise ProfileError("grant id is invalid")


def claim_grant(grant: dict[str, Any], claim_root: Path) -> Path:
    claim_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    claim_root.chmod(0o700)
    batch_identity = canonical_digest(
        {
            "plan_sha256": grant["plan_sha256"],
            "batch_id": grant["batch_id"],
            "cells": grant["cells"],
        }
    )
    claim = claim_root / f"first-six-{batch_identity}.json"
    body = {
        "grant_id": grant["grant_id"],
        "owner_id": grant["owner_id"],
        "invocation_id": grant["invocation_id"],
        "plan_sha256": grant["plan_sha256"],
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DuplicateClaim(
            "grant or first-six batch has already been claimed"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(body, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return claim


def _private_json(
    path: Path, value: dict[str, Any], *, overwrite: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() and not overwrite:
        raise ProfileError("refusing to overwrite durable operation evidence")
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                value, stream, ensure_ascii=False, sort_keys=True, indent=2, default=str
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _failure_receipt(
    path: Path,
    *,
    job_id: str,
    stage: str,
    exc: BaseException,
    child_possible: bool,
    planned_cell_ids: list[str] | None = None,
) -> dict[str, Any]:
    planned = list(planned_cell_ids or [])
    receipt = {
        "operation": OPERATION_KIND,
        "job_id": job_id,
        "execution_status": "incomplete",
        "failure_stage": stage,
        "failure": {"type": type(exc).__name__, "message": str(exc)[:1000]},
        "attempts": [],
        "planned_cell_ids": planned,
        "not_run_cell_ids": planned,
        "cleanup_verified": not child_possible,
        "cleanup_evidence": (
            {"verified": True, "reaped": True, "no_child_created": True}
            if not child_possible
            else {"verified": False, "reaped": None, "child_may_exist": True}
        ),
    }
    _private_json(path, receipt, overwrite=path.exists())
    return receipt


def operation_roots(data_dir: Path, job_id: str) -> tuple[Path, Path]:
    base = Path(data_dir).resolve() / "research" / OPERATION_KIND
    return base / "runs" / job_id, base / "claims"


def lease_manager(data_dir: Path) -> ResourceLeaseManager:
    return ResourceLeaseManager(data_dir=Path(data_dir))


def _verify_assets(profile: FrozenProfile) -> dict[str, Any]:
    metadata_path = (
        profile.root
        / "docs/research/practical-20261029/lead-review/results-intake-r282-20261031/runtime-assets.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected = profile.profile["asset_sha256"]
    checks = {}
    for key, field in (
        ("runtime", "binary"),
        ("model", "model"),
        ("template", "template"),
    ):
        path = Path(metadata[field])
        if not path.is_file() or path.is_symlink():
            raise PreflightError(f"pinned {key} asset is unavailable")
        digest = sha256_file(path)
        if digest != expected[key] or metadata[f"{field}_sha256"] != expected[key]:
            raise PreflightError(f"pinned {key} asset SHA-256 mismatch")
        checks[key] = {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": digest,
        }
    if metadata.get("threads") != 6:
        raise PreflightError("pinned CPU thread count differs")
    return {"assets": checks, "threads": 6, "context_tokens": 32768}


def _preflight():
    import psutil

    from ttakkari.research.regna_q25_first6 import preflight as n3_preflight

    result = n3_preflight(psutil, minimum_available_bytes=MAX_RSS_BYTES)
    return {
        **result,
        "resource_family": "llm",
        "cpu_only": True,
        "max_rss_bytes": MAX_RSS_BYTES,
    }


def _bounded_factory(profile: FrozenProfile, output_root: Path, violations: list[str]):
    """Keep the measured direct model child in the operation-owned group and monitor its RSS."""
    import subprocess
    import threading

    import psutil

    dispatcher = _load_dispatcher(profile.root)
    driver = dispatcher.driver
    real_popen = subprocess.Popen
    monitored: dict[int, tuple[Any, threading.Event]] = {}
    assets = json.loads(
        (
            profile.root
            / "docs/research/practical-20261029/lead-review/results-intake-r282-20261031/runtime-assets.json"
        ).read_text()
    )
    binary = Path(assets["binary"]).resolve()

    def bounded_popen(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if (
            not isinstance(command, (list, tuple))
            or not command
            or Path(command[0]).resolve() != binary
        ):
            return real_popen(*args, **kwargs)
        kwargs["start_new_session"] = False
        process = real_popen(*args, **kwargs)
        stop = threading.Event()
        try:
            created = psutil.Process(process.pid).create_time()
        except (psutil.Error, OSError, ValueError) as exc:
            created = None
            violations.append(
                f"owned child identity observation failed: {type(exc).__name__}"
            )
            with contextlib.suppress(OSError):
                process.terminate()

        def monitor():
            try:
                if created is None:
                    return
                while not stop.wait(0.1):
                    current = psutil.Process(process.pid)
                    if abs(current.create_time() - created) > 1:
                        violations.append("owned model process identity changed")
                        return
                    if current.status() == psutil.STATUS_ZOMBIE:
                        return
                    if current.memory_info().rss > MAX_RSS_BYTES:
                        violations.append(
                            "owned model process exceeded the 6 GiB RSS ceiling"
                        )
                        process.terminate()
                        return
            except psutil.NoSuchProcess:
                return
            except (psutil.Error, OSError, ValueError) as exc:
                violations.append(f"owned RSS monitor failed: {type(exc).__name__}")
                with contextlib.suppress(OSError):
                    process.terminate()

        thread = threading.Thread(
            target=monitor, name=f"regna-n4-rss-{process.pid}", daemon=True
        )
        try:
            thread.start()
        except (OSError, RuntimeError) as exc:
            violations.append(
                f"owned RSS monitor could not start: {type(exc).__name__}"
            )
            with contextlib.suppress(OSError):
                process.terminate()
            return process
        monitored[process.pid] = (thread, stop)
        return process

    driver.subprocess.Popen = bounded_popen

    def child_factory(cell):
        run_dir = output_root / cell["cell_id"] / "owned-server"
        child = dispatcher._make_child(profile.profile, run_dir)
        original_start, original_stop = child.start, child.stop

        def start():
            adapter = original_start()
            if violations:
                child.stop()
                raise PreflightError(
                    "owned model child failed CPU/RSS supervision preflight"
                )
            return dispatcher.n4_adapter.N4EvidenceAdapter(adapter)

        def stop():
            proc = getattr(child, "proc", None)
            item = monitored.get(proc.pid) if proc else None
            if item:
                item[1].set()
                item[0].join(timeout=1)
            return original_stop()

        child.start, child.stop = start, stop
        return child

    return child_factory, monitored, driver, real_popen


def verify_owned_cleanup(receipt: dict[str, Any]) -> bool:
    attempts = receipt.get("attempts") if isinstance(receipt, dict) else None
    if not isinstance(attempts, list):
        return False
    for attempt in attempts:
        cleanup = attempt.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("cleanup_verified") is not True:
            return False
        lifecycle = cleanup.get("lifecycle_check")
        if not isinstance(lifecycle, dict) or lifecycle.get("verified") is not True:
            return False
        if lifecycle.get("child_created") and lifecycle.get("reaped") is not True:
            return False
    return True


def finish_attempt(
    receipt: dict[str, Any],
    *,
    output_root: Path,
    lease_release,
    cleanup_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Persist failure/cleanup evidence before considering lease release."""
    verified = (
        cleanup_evidence.get("verified") is True
        and cleanup_evidence.get("reaped") is True
    )
    receipt = {
        **receipt,
        "cleanup_verified": verified,
        "cleanup_evidence": cleanup_evidence,
    }
    _private_json(
        output_root / "receipt.json",
        receipt,
        overwrite=(output_root / "receipt.json").exists(),
    )
    if verified:
        lease_release({"runtime_status": "stopped", "evidence": cleanup_evidence})
    return receipt


def walltime_summary(
    attempts: list[dict[str, Any]], outer_wall_seconds: float
) -> dict[str, Any]:
    elapsed = [row.get("elapsed_seconds") for row in attempts]
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in elapsed
    ):
        total = None
    else:
        total = round(sum(elapsed), 9)
    return {
        "attempt_elapsed_seconds_sum": total,
        "outer_wall_seconds": outer_wall_seconds,
        "outer_covers_attempt_sum": total is not None
        and outer_wall_seconds + 1e-9 >= total,
    }


def _review_file(grant: dict[str, Any]) -> dict[str, Any]:
    return grant["prior_attempt_review_evidence"]


def _dispatch(
    profile: FrozenProfile,
    grant: dict[str, Any],
    output_root: Path,
    *,
    monotonic=time.monotonic,
):
    dispatcher = _load_dispatcher(profile.root)
    violations: list[str] = []
    child_factory, monitored, driver, real_popen = _bounded_factory(
        profile, output_root, violations
    )
    start = monotonic()
    old_umask = os.umask(0o077)
    try:
        receipt = dispatcher.dispatch_batch(
            profile.plan,
            profile.batch,
            profile=profile.profile,
            grant=grant,
            child_factory=child_factory,
            output_dir=output_root,
            expected_owner=grant["owner_id"],
            expected_engine=ENGINE,
            expected_invocation=grant["invocation_id"],
            monotonic=monotonic,
        )
    finally:
        os.umask(old_umask)
        driver.subprocess.Popen = real_popen
        monitors_stopped = True
        for thread, stop in monitored.values():
            stop.set()
            thread.join(timeout=1)
            monitors_stopped = monitors_stopped and not thread.is_alive()
    wall = max(0.0, monotonic() - start)
    summary = walltime_summary(receipt.get("attempts", []), wall)
    receipt["backend_walltime_summary"] = summary
    receipt["backend_rss_monitor_stopped"] = monitors_stopped
    if not monitors_stopped or violations:
        receipt["execution_status"] = "incomplete"
        receipt["failure"] = ("; ".join(violations) or "RSS monitor did not stop")[
            :1000
        ]
    dispatcher._write_json(output_root / "receipt.json", receipt, overwrite=True)
    dispatcher.validate_batch_receipt(profile.batch, receipt)
    return receipt


def run_operation(job_id: str) -> int:
    from ttakkari.config import get_settings
    from ttakkari.operations import jobs

    job = jobs.get(job_id)
    if (
        job.get("kind") != OPERATION_KIND
        or job.get("n4_scope") != "work"
        or not job.get("requested_by_session")
    ):
        raise ProfileError("N4 operation requires its starting work-session owner")
    owner = job["requested_by_session"]
    data_dir = get_settings().data_dir
    output_root, claim_root = operation_roots(data_dir, job_id)
    output_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_root.parent.chmod(0o700)
    if output_root.exists():
        raise ProfileError("server-owned N4 evidence directory already exists")
    output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    output_root.chmod(0o700)
    jobs.update(job_id, n4_evidence_root=str(output_root))
    try:
        profile = load_profile()
        preflight = _preflight()
        asset_receipt = _verify_assets(profile)
    except BaseException as exc:
        _failure_receipt(
            output_root / "failure-receipt.json",
            job_id=job_id,
            stage="profile_preflight_or_assets",
            exc=exc,
            child_possible=False,
            planned_cell_ids=[cell["cell_id"] for cell in profile.batch["cells"]]
            if "profile" in locals()
            else None,
        )
        raise
    batch_output_root = output_root / "batch-01"
    manager = lease_manager(data_dir)
    try:
        lease = manager.acquire(
            owner_task=job_id,
            owner_attempt=owner,
            runtime_instance=job_id,
            resource_family="llm",
            claim={
                "profile": PROFILE_ID,
                "operation": OPERATION_KIND,
                "plan_sha256": canonical_digest(profile.plan),
                "cells": [c["cell_id"] for c in profile.batch["cells"]],
            },
            ttl_seconds=LEASE_TTL_SECONDS,
        )
    except BaseException as exc:
        _failure_receipt(
            output_root / "failure-receipt.json",
            job_id=job_id,
            stage="lease_acquisition",
            exc=exc,
            child_possible=False,
            planned_cell_ids=[cell["cell_id"] for cell in profile.batch["cells"]],
        )
        raise
    released = False
    child_possible = False
    try:
        try:
            grant = issue_grant(
                profile,
                owner_session=owner,
                job_id=job_id,
                lease_id=lease.lease_id,
                generation_token=lease.generation_token,
                output_root=batch_output_root,
            )
        except BaselineReuseError as exc:
            _private_json(
                output_root / "prior-attempt-reuse-refusal.json",
                {
                    "decision": "refused_exact_baseline_reuse",
                    "ledger_sha256": exc.ledger_sha256,
                    "matches": exc.matches,
                    "plan_sha256": canonical_digest(profile.plan),
                    "input_identity_sha256_by_cell": profile.input_identity_sha256_by_cell,
                },
            )
            jobs.update(
                job_id,
                n4_evidence_root=str(output_root),
                n4_baseline_reuse_refused=True,
            )
            raise
        validate_grant(
            profile,
            grant,
            owner_session=owner,
            job_id=job_id,
            lease_id=lease.lease_id,
            generation_token=lease.generation_token,
            output_root=batch_output_root,
        )
        claim_grant(grant, claim_root)
        _private_json(output_root / "grant.json", grant)
        _private_json(output_root / "prior-attempt-review.json", _review_file(grant))
        _private_json(
            output_root / "preflight.json", {"host": preflight, **asset_receipt}
        )
        _private_json(
            output_root / "batch-claim.json",
            {
                "job_id": job_id,
                "owner_session": owner,
                "lease_id": lease.lease_id,
                "generation_token": lease.generation_token,
                "batch_id": "batch-01",
                "cells": [c["cell_id"] for c in profile.batch["cells"]],
            },
        )
        jobs.update(
            job_id,
            n4_lease_id=lease.lease_id,
            n4_generation_token=lease.generation_token,
            n4_runtime_instance=lease.runtime_instance,
            n4_evidence_root=str(output_root),
            n4_grant_id=grant["grant_id"],
            n4_plan_sha256=grant["plan_sha256"],
        )
        lease.mark_running()
        child_possible = True
        receipt = _dispatch(profile, grant, batch_output_root)
        cleanup = verify_owned_cleanup(receipt)
        if cleanup:
            lease.release(
                proof={
                    "runtime_status": "stopped",
                    "evidence": {
                        "runtime_instance": lease.runtime_instance,
                        "operation": OPERATION_KIND,
                        "receipt_sha256": sha256_file(
                            batch_output_root / "receipt.json"
                        ),
                        "all_owned_children_reaped": True,
                    },
                }
            )
            released = True
        else:
            lease.request_cancel()
        summary = {
            "execution_status": receipt.get("execution_status"),
            "batch_id": "batch-01",
            "attempted_cells": len(receipt.get("attempts", [])),
            "not_run_cell_ids": receipt.get("not_run_cell_ids"),
            "cleanup_verified": cleanup,
            "plan_sha256": grant["plan_sha256"],
            "grant_id": grant["grant_id"],
            "lease_id": lease.lease_id,
            "evidence_path": str(output_root),
            "walltime": receipt.get("backend_walltime_summary"),
        }
        _private_json(output_root / "operation-summary.json", summary)
        jobs.update(job_id, n4_summary=summary, summary=summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if receipt.get("execution_status") == "complete" and cleanup else 1
    except BaseException as exc:
        if not (batch_output_root / "receipt.json").exists():
            _failure_receipt(
                output_root / "failure-receipt.json",
                job_id=job_id,
                stage="prior_review_dispatch_or_cleanup",
                exc=exc,
                child_possible=child_possible,
                planned_cell_ids=[cell["cell_id"] for cell in profile.batch["cells"]],
            )
        if not child_possible and not released:
            with contextlib.suppress(Exception):
                lease.release(
                    proof={
                        "runtime_status": "stopped",
                        "evidence": {
                            "runtime_instance": lease.runtime_instance,
                            "no_child_created": True,
                            "operation": OPERATION_KIND,
                        },
                    }
                )
        elif not released:
            with contextlib.suppress(Exception):
                lease.request_cancel()
        raise


def operation_summary(job_id: str, data_dir: Path) -> dict[str, Any]:
    from ttakkari.operations.jobs import get

    job = get(job_id)
    evidence_root = Path(
        job.get("n4_evidence_root") or operation_roots(data_dir, job_id)[0]
    )
    receipt_path = evidence_root / "batch-01" / "receipt.json"
    reuse_refusal = None
    try:
        reuse_refusal = json.loads(
            (evidence_root / "prior-attempt-reuse-refusal.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        pass
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        cleanup = verify_owned_cleanup(receipt)
        wall = receipt.get("backend_walltime_summary")
    except (OSError, ValueError):
        try:
            receipt = json.loads(
                (evidence_root / "failure-receipt.json").read_text(encoding="utf-8")
            )
            cleanup, wall = receipt.get("cleanup_verified") is True, None
        except (OSError, ValueError):
            receipt, cleanup, wall = {}, False, None
    lease_id, generation = job.get("n4_lease_id"), job.get("n4_generation_token")
    phase = (
        lease_manager(data_dir).recorded_phase(lease_id, generation)
        if lease_id and generation
        else None
    )
    raw_verified = False
    if receipt.get("execution_status") == "complete":
        try:
            profile = load_profile()
            _load_dispatcher(profile.root).validate_batch_receipt(
                profile.batch, receipt
            )
            raw_verified = True
        except (KeyError, OSError, TypeError, ValueError):
            raw_verified = False
    return {
        **(job.get("n4_summary") or {}),
        "execution_status": receipt.get(
            "execution_status", "refused" if reuse_refusal else "unknown"
        ),
        "attempted_cells": len(receipt.get("attempts", [])),
        "cleanup_verified": cleanup,
        "lease_held": phase in ResourceLeaseManager.LIVE_PHASES,
        "walltime": wall,
        "raw_evidence_verified": raw_verified,
        "baseline_reuse_refusal": (
            {
                "ledger_sha256": reuse_refusal.get("ledger_sha256"),
                "matches": reuse_refusal.get("matches"),
            }
            if reuse_refusal
            else None
        ),
        "plan_sha256": job.get("n4_plan_sha256"),
        "evidence_path": str(evidence_root),
    }


def recover_lease_after_owner_exit(
    job: dict, data_dir: Path, *, operation_runner_confirmed_group_absent: bool
) -> bool:
    if operation_runner_confirmed_group_absent is not True:
        return False
    manager = lease_manager(data_dir)
    lease_id, generation, runtime = (
        job.get("n4_lease_id"),
        job.get("n4_generation_token"),
        job.get("n4_runtime_instance"),
    )
    if not (lease_id and generation and runtime):
        current = manager.current()
        if not current or current.get("owner_task") != job.get("id"):
            return True
        lease_id, generation, runtime = (
            current["lease_id"],
            current["generation_token"],
            current["runtime_instance"],
        )
    current = manager.current()
    if not current:
        return manager.recorded_phase(lease_id, generation) == "released"
    if (
        current.get("lease_id") != lease_id
        or current.get("generation_token") != generation
        or current.get("owner_task") != job.get("id")
        or current.get("runtime_instance") != runtime
        or current.get("owner_attempt") != job.get("requested_by_session")
        or current.get("resource_family") != "llm"
    ):
        return False
    if manager.recorded_phase(lease_id, generation) == "released":
        return True
    root = Path(job.get("n4_evidence_root") or operation_roots(data_dir, job["id"])[0])
    try:
        receipt = json.loads(
            (root / "batch-01" / "receipt.json").read_text(encoding="utf-8")
        )
        no_child = not any(
            a.get("child_started") or a.get("cleanup", {}).get("receipt")
            for a in receipt.get("attempts", [])
        )
        if not no_child and not verify_owned_cleanup(receipt):
            return False
    except (OSError, ValueError, TypeError):
        return False
    try:
        manager.reconcile(
            lease_id,
            generation,
            proof={
                "runtime_status": "stopped",
                "evidence": {
                    "runtime_instance": runtime,
                    "operation_runner_confirmed_group_absent": True,
                    "cleanup_receipt_verified": True,
                    "no_child_created": no_child,
                },
            },
        )
    except (LeaseError, OSError, ValueError, KeyError):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 2 or values[0] != "run":
        return 2
    try:
        return run_operation(values[1])
    except Exception as exc:  # noqa: BLE001 -- persist any worker-boundary failure as an unsuccessful receipt
        print(
            json.dumps(
                {
                    "execution_status": "rejected",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
