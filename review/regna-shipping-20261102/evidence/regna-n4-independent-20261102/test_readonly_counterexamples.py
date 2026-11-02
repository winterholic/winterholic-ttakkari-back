"""Read-only N4 connector counterexamples; all writes are confined to tmp_path."""

from __future__ import annotations

import json
import stat
import asyncio
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from ttakkari.research import regna_n4_combo_first6 as n4


def _profile(monkeypatch):
    body = {"model": "Qwen3-1.7B-Q4_K_M.gguf", "messages": [{"role": "user", "content": "fixed"}]}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    profile = n4.FrozenProfile(
        root=Path("/pinned/final-isolated"),
        plan_path=Path("/pinned/plan.json"),
        source_pins_path=Path("/pinned/source-pins.json"),
        plan_sha256="a" * 64,
        source_pins_bytes_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        plan={"manifest_sha256": "d" * 64, "request_contract": {"tool_schema_sha256": "e" * 64}, "batches": []},
        source_pins={"files": {}},
        batch={"batch_id": "batch-01", "cells": [{"cell_id": "cell-0"}]},
        profile={"model_name": "Qwen3-1.7B-Q4_K_M.gguf", "asset_sha256": {"model": "1" * 64, "runtime": "2" * 64, "template": "3" * 64, "tokenizer": "1" * 64}, "generation_mode": {"chat_template_kwargs": {"enable_thinking": False}}},
        input_identity_sha256_by_cell={"cell-0": "f" * 64},
    )
    monkeypatch.setattr(n4, "_expected_baseline_request_hashes", lambda _profile: {"cell-0": n4.sha256_bytes(raw.encode())})
    return profile, body, raw


def test_actual_receipt_shape_failed_foreign_asset_raw_body_is_misclassified_as_reuse(tmp_path, monkeypatch):
    """The real receipt nests request_body under cells/generations/request_receipts."""
    profile, _body, raw = _profile(monkeypatch)
    ledger = tmp_path / "receipt.json"
    ledger.write_text(json.dumps({
        "schema": "historical-regna-receipt",
        "execution_status": "incomplete",
        "runtime_assets": {"model": "/other/model.gguf", "model_sha256": "9" * 64},
        "cells": [{"cell_id": "different-cell", "status": "runtime_error", "model_outcome": "failed", "generations": [{
            "request_receipts": [{"path": "/v1/chat/completions", "request_body": raw, "error": "transport failed"}]
        }]}],
    }), encoding="utf-8")

    result = n4.review_prior_attempts(profile, ledger_files=[ledger])

    assert result["baseline_reuse_decision"] == "exact_baseline_reuse_found"
    assert result["exact_baseline_matches"][0]["cell_id"] == "cell-0"
    assert result["exact_baseline_matches"][0]["record_status"] is None
    with pytest.raises(n4.BaselineReuseError):
        n4.issue_grant(
            profile,
            owner_session="owner",
            job_id="job",
            lease_id="lease",
            generation_token="generation",
            output_root=tmp_path / "private-evidence",
            ledger_files=[ledger],
        )


def test_bare_dict_request_body_without_output_is_also_exact_reuse(tmp_path, monkeypatch):
    profile, body, _raw = _profile(monkeypatch)
    ledger = tmp_path / "attempts.jsonl"
    ledger.write_text(json.dumps({"request_body": body}) + "\n", encoding="utf-8")

    result = n4.review_prior_attempts(profile, ledger_files=[ledger])

    assert result["exact_baseline_matches"]
    assert result["exact_baseline_matches"][0]["record_status"] is None


def test_receipt_added_after_inventory_scan_is_not_in_review(tmp_path, monkeypatch):
    profile, _body, raw = _profile(monkeypatch)
    root = tmp_path / "runs"
    root.mkdir()
    first = root / "attempts.jsonl"
    first.write_text('{"record_type":"old"}\n', encoding="utf-8")
    monkeypatch.setattr(n4, "BASELINE_LEDGER", tmp_path / "missing-baseline.jsonl")
    monkeypatch.setattr(n4, "LEDGER_ROOT", root)
    original_read = n4._read_attempt_values
    inserted = root / "later-run" / "receipt.json"
    injected = False

    def add_receipt_after_scan(path, digest):
        nonlocal injected
        yield from original_read(path, digest)
        if not injected:
            inserted.parent.mkdir()
            inserted.write_text(json.dumps({"cells": [{"generations": [{"request_receipts": [{"request_body": raw}]}]}]}), encoding="utf-8")
            injected = True

    monkeypatch.setattr(n4, "_read_attempt_values", add_receipt_after_scan)
    result = n4.review_prior_attempts(profile)

    assert injected and inserted.exists()
    assert result["baseline_reuse_decision"] == "no_exact_baseline_reuse"
    assert str(inserted) not in {row["path"] for row in result["ledger_files"]}


def test_run_operation_follows_symlinked_runs_parent_and_writes_evidence_outside_data_dir(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "service-data"
    base = data_dir / "research" / n4.OPERATION_KIND
    base.mkdir(parents=True)
    foreign = tmp_path / "foreign-evidence"
    foreign.mkdir(mode=0o755)
    (base / "runs").symlink_to(foreign, target_is_directory=True)
    profile = SimpleNamespace(plan={"fixed": True}, batch={"cells": [{"cell_id": "cell-0"}]})
    lease = SimpleNamespace(
        lease_id="lease-fake", generation_token="generation-fake", runtime_instance="runtime-fake",
        mark_running=lambda: None, request_cancel=lambda: None,
        release=lambda **_kwargs: None,
    )
    released, cancelled = [], []
    lease.release = lambda **kwargs: released.append(kwargs)
    lease.request_cancel = lambda: cancelled.append(True)

    monkeypatch.setattr(n4, "load_profile", lambda: profile)
    monkeypatch.setattr(n4, "_preflight", lambda: {"fake": True})
    monkeypatch.setattr(n4, "_verify_assets", lambda _profile: {"fake": True})
    monkeypatch.setattr(n4, "lease_manager", lambda _data_dir: SimpleNamespace(acquire=lambda **_kwargs: lease))
    grant = {"grant_id": "g" * 32, "plan_sha256": "pinned-plan", "prior_attempt_review_evidence": {"reviewed": True}}
    monkeypatch.setattr(n4, "issue_grant", lambda *_args, **_kwargs: grant)
    monkeypatch.setattr(n4, "validate_grant", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(n4, "claim_grant", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(n4, "_review_file", lambda _grant: {"reviewed": True})
    monkeypatch.setattr(n4, "verify_owned_cleanup", lambda _receipt: True)

    def fake_dispatch(_profile, _grant, output_root):
        output_root.mkdir(parents=True)
        receipt = {
            "execution_status": "complete", "attempts": [{"cleanup": {"cleanup_verified": True, "lifecycle_check": {"verified": True, "reaped": True}}}],
            "not_run_cell_ids": [], "backend_walltime_summary": {"attempt_elapsed_seconds_sum": 0.1, "outer_wall_seconds": 0.2},
        }
        (output_root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    monkeypatch.setattr(n4, "_dispatch", fake_dispatch)
    jobs = SimpleNamespace(
        get=lambda _job: {"id": "job-fixed", "kind": n4.OPERATION_KIND, "n4_scope": "work", "requested_by_session": "owner"},
        update=lambda _job, **_kwargs: None,
    )
    monkeypatch.setattr("ttakkari.operations.jobs.get", jobs.get)
    monkeypatch.setattr("ttakkari.operations.jobs.update", jobs.update)
    monkeypatch.setattr("ttakkari.config.get_settings", lambda: SimpleNamespace(data_dir=data_dir))

    assert n4.run_operation("job-fixed") == 0
    capsys.readouterr()

    written = foreign / "job-fixed"
    assert (written / "operation-summary.json").is_file()
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o700
    assert (base / "runs").is_symlink()
    assert len(released) == 1 and cancelled == []


def test_run_operation_retains_fake_lease_when_cleanup_is_unknown(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "service-data"
    profile = SimpleNamespace(plan={"fixed": True}, batch={"cells": [{"cell_id": "cell-0"}]})
    released, cancelled = [], []
    lease = SimpleNamespace(
        lease_id="lease-fake", generation_token="generation-fake", runtime_instance="runtime-fake",
        mark_running=lambda: None, request_cancel=lambda: cancelled.append(True),
        release=lambda **kwargs: released.append(kwargs),
    )
    monkeypatch.setattr(n4, "load_profile", lambda: profile)
    monkeypatch.setattr(n4, "_preflight", lambda: {"fake": True})
    monkeypatch.setattr(n4, "_verify_assets", lambda _profile: {"fake": True})
    monkeypatch.setattr(n4, "lease_manager", lambda _data_dir: SimpleNamespace(acquire=lambda **_kwargs: lease))
    grant = {"grant_id": "g" * 32, "plan_sha256": "pinned-plan", "prior_attempt_review_evidence": {"reviewed": True}}
    monkeypatch.setattr(n4, "issue_grant", lambda *_args, **_kwargs: grant)
    monkeypatch.setattr(n4, "validate_grant", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(n4, "claim_grant", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(n4, "_review_file", lambda _grant: {"reviewed": True})
    monkeypatch.setattr(n4, "verify_owned_cleanup", lambda _receipt: False)

    def fake_dispatch(_profile, _grant, output_root):
        output_root.mkdir(parents=True)
        receipt = {
            "execution_status": "incomplete", "attempts": [{"cleanup": {"cleanup_verified": False, "lifecycle_check": {"verified": False, "reaped": None}}}],
            "not_run_cell_ids": ["cell-0"], "backend_walltime_summary": {"attempt_elapsed_seconds_sum": 0.1, "outer_wall_seconds": 0.2},
        }
        (output_root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    monkeypatch.setattr(n4, "_dispatch", fake_dispatch)
    jobs = SimpleNamespace(
        get=lambda _job: {"id": "job-fixed", "kind": n4.OPERATION_KIND, "n4_scope": "work", "requested_by_session": "owner"},
        update=lambda _job, **_kwargs: None,
    )
    monkeypatch.setattr("ttakkari.operations.jobs.get", jobs.get)
    monkeypatch.setattr("ttakkari.operations.jobs.update", jobs.update)
    monkeypatch.setattr("ttakkari.config.get_settings", lambda: SimpleNamespace(data_dir=data_dir))

    assert n4.run_operation("job-fixed") == 1
    capsys.readouterr()
    assert released == [] and cancelled == [True]
    assert (data_dir / "research" / n4.OPERATION_KIND / "runs" / "job-fixed" / "batch-01" / "receipt.json").is_file()


def test_runner_exit_recovery_reconciles_only_matching_lease_with_positive_receipt(tmp_path, monkeypatch):
    data_dir = tmp_path / "service-data"
    evidence = data_dir / "research" / n4.OPERATION_KIND / "runs" / "job-fixed"
    batch = evidence / "batch-01"
    batch.mkdir(parents=True)
    (batch / "receipt.json").write_text(json.dumps({
        "attempts": [{"child_started": True, "cleanup": {"cleanup_verified": True, "lifecycle_check": {"verified": True, "reaped": True, "child_created": True}}}]
    }), encoding="utf-8")
    current = {"lease_id": "lease", "generation_token": "generation", "owner_task": "job-fixed", "runtime_instance": "job-fixed", "owner_attempt": "owner", "resource_family": "llm"}
    reconciled = []
    manager = SimpleNamespace(current=lambda: current, recorded_phase=lambda *_args: "running", reconcile=lambda *args, **kwargs: reconciled.append((args, kwargs)))
    monkeypatch.setattr(n4, "lease_manager", lambda _data_dir: manager)
    job = {"id": "job-fixed", "requested_by_session": "owner", "n4_lease_id": "lease", "n4_generation_token": "generation", "n4_runtime_instance": "job-fixed", "n4_evidence_root": str(evidence)}

    assert n4.recover_lease_after_owner_exit(job, data_dir, operation_runner_confirmed_group_absent=True) is True
    assert len(reconciled) == 1
    assert n4.recover_lease_after_owner_exit(job, data_dir, operation_runner_confirmed_group_absent=False) is False


def test_runner_exit_recovery_keeps_lease_when_dispatch_failure_has_no_batch_receipt(tmp_path, monkeypatch):
    data_dir = tmp_path / "service-data"
    evidence = data_dir / "research" / n4.OPERATION_KIND / "runs" / "job-fixed"
    evidence.mkdir(parents=True)
    n4._failure_receipt(
        evidence / "failure-receipt.json",
        job_id="job-fixed",
        stage="dispatch",
        exc=RuntimeError("synthetic failure before durable batch receipt"),
        child_possible=True,
        planned_cell_ids=["cell-0"],
    )
    current = {"lease_id": "lease", "generation_token": "generation", "owner_task": "job-fixed", "runtime_instance": "job-fixed", "owner_attempt": "owner", "resource_family": "llm"}
    reconciled = []
    manager = SimpleNamespace(current=lambda: current, recorded_phase=lambda *_args: "running", reconcile=lambda *args, **kwargs: reconciled.append((args, kwargs)))
    monkeypatch.setattr(n4, "lease_manager", lambda _data_dir: manager)
    job = {"id": "job-fixed", "requested_by_session": "owner", "n4_lease_id": "lease", "n4_generation_token": "generation", "n4_runtime_instance": "job-fixed", "n4_evidence_root": str(evidence)}

    assert n4.recover_lease_after_owner_exit(job, data_dir, operation_runner_confirmed_group_absent=True) is False
    assert reconciled == []


def test_ledger_scan_fails_closed_on_malformed_and_excludes_symlink_receipt(tmp_path, monkeypatch):
    profile, _body, _raw = _profile(monkeypatch)
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"request_body":"not relevant"}', encoding="utf-8")
    (root / "linked-receipt.json").symlink_to(outside)
    monkeypatch.setattr(n4, "BASELINE_LEDGER", tmp_path / "missing-baseline.jsonl")
    monkeypatch.setattr(n4, "LEDGER_ROOT", root)

    assert n4._ledger_files() == []
    malformed = root / "attempts.jsonl"
    malformed.write_text('{"status":"ok"}\n{"request_body":', encoding="utf-8")
    with pytest.raises(n4.ProfileError, match="invalid JSON"):
        n4.review_prior_attempts(profile)


def test_same_work_owner_can_read_and_cancel_n4_operation(monkeypatch):
    from ttakkari.api import operations as api

    owner = uuid.uuid4()
    job_id = str(uuid.uuid4())
    job = {"id": job_id, "kind": n4.OPERATION_KIND, "requested_by_session": str(owner), "status": "running"}
    session = SimpleNamespace(pool="work")
    db = SimpleNamespace(commit=lambda: asyncio.sleep(0))
    monkeypatch.setattr(api, "caller", lambda _sid, _db: asyncio.sleep(0, result=session))
    monkeypatch.setattr(api.jobs, "get", lambda _job_id: job)
    monkeypatch.setattr(api.jobs, "reconciled", lambda _job_id: job)
    monkeypatch.setattr(api.jobs, "cancel", lambda _job_id: job | {"status": "cancelled", "finish_audited": True})
    monkeypatch.setattr(api.jobs, "view", lambda value: value)
    monkeypatch.setattr(api, "record", lambda *_args, **_kwargs: None)

    status = asyncio.run(api.operation_status(job_id, owner, db))
    cancelled = asyncio.run(api.cancel_operation(job_id, owner, db))

    assert status["id"] == cancelled["id"] == job_id
    assert cancelled["status"] == "cancelled"
