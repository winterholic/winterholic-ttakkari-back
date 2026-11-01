"""Narrow regressions for the independent N4 connector review findings."""

from __future__ import annotations

import json
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ttakkari.research import regna_n4_combo_first6 as n4


@pytest.fixture
def profile(monkeypatch):
    body = {
        "model": "Qwen3-1.7B-Q4_K_M.gguf",
        "messages": [{"role": "user", "content": "fixed"}],
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    value = n4.FrozenProfile(
        root=Path("/pinned/final-isolated"),
        plan_path=Path("/pinned/plan.json"),
        source_pins_path=Path("/pinned/source-pins.json"),
        plan_sha256="a" * 64,
        source_pins_bytes_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        plan={
            "manifest_sha256": "d" * 64,
            "request_contract": {"tool_schema_sha256": "e" * 64},
            "batches": [],
        },
        source_pins={"files": {}},
        batch={
            "batch_id": "batch-01",
            "cells": [
                {
                    "cell_id": "cell-0",
                    "case_id": "case-0",
                    "seed": 1701,
                    "arm": "baseline",
                }
            ],
        },
        profile={
            "model_name": "Qwen3-1.7B-Q4_K_M.gguf",
            "asset_sha256": {
                "model": "1" * 64,
                "runtime": "2" * 64,
                "template": "3" * 64,
                "tokenizer": "1" * 64,
            },
            "generation_mode": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        input_identity_sha256_by_cell={"cell-0": "f" * 64},
    )
    monkeypatch.setattr(
        n4,
        "_expected_baseline_request_hashes",
        lambda _profile: {"cell-0": n4.sha256_bytes(raw.encode())},
    )
    return value, raw


def _historical_record(
    raw: str,
    *,
    cell_id="cell-0",
    status="response_captured",
    execution_status="complete",
    model_sha=None,
    transport_error=None,
):
    model_sha = model_sha or "1" * 64
    return {
        "execution_status": execution_status,
        "generation_mode_by_family": {
            "Qwen3": {"chat_template_kwargs": {"enable_thinking": False}}
        },
        "runtime_assets": {
            "binary": {"sha256": "2" * 64},
            "model": {"sha256": model_sha},
            "template": {"sha256": "3" * 64},
        },
        "cells": [
            {
                "cell_id": cell_id,
                "case_id": "case-0",
                "seed": 1701,
                "status": status,
                "generation_mode": {"chat_template_kwargs": {"enable_thinking": False}},
                "model_outcome": "success"
                if status == "response_captured"
                else "failed",
                "raw_integrity_state": "verified"
                if status == "response_captured"
                else "missing",
                "raw_records_durable": status == "response_captured",
                "raw_response": "usable answer"
                if status == "response_captured"
                else None,
                "usage_state": "observed_exact_integer"
                if status == "response_captured"
                else "missing",
                "usage": {"prompt_tokens": 12, "completion_tokens": 3}
                if status == "response_captured"
                else None,
                "generations": [
                    {
                        "generation_mode": {
                            "chat_template_kwargs": {"enable_thinking": False}
                        },
                        "finish_reason": "stop",
                        "raw_integrity_state": "verified"
                        if status == "response_captured"
                        else "missing",
                        "raw_ref": {
                            "readback_verified": True,
                            "request_sha256_verified": True,
                            "sse_bytes_verified": True,
                        }
                        if status == "response_captured"
                        else None,
                        "usage": {"prompt_tokens": 12, "completion_tokens": 3}
                        if status == "response_captured"
                        else None,
                        "request_receipts": [
                            {
                                "path": "/v1/chat/completions",
                                "request_body": raw,
                                "error": transport_error,
                                "status": "http_200"
                                if transport_error is None
                                else "transport_error",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_only_successful_identity_matched_historical_baseline_is_reusable(
    tmp_path, profile
):
    frozen, raw = profile
    ledger = tmp_path / "receipt.json"
    failed = _historical_record(
        raw,
        status="runtime_error",
        execution_status="incomplete",
        transport_error="connection reset",
    )
    foreign = _historical_record(raw, model_sha="9" * 64)
    missing_identity = {"request_body": raw}
    valid = _historical_record(raw)
    ledger.write_text(
        json.dumps({"records": [failed, foreign, missing_identity, valid]}),
        encoding="utf-8",
    )

    result = n4.review_prior_attempts(frozen, ledger_files=[ledger])

    assert result["baseline_reuse_decision"] == "exact_baseline_reuse_found"
    assert len(result["exact_baseline_matches"]) == 1
    assert result["exact_baseline_matches"][0]["cell_id"] == "cell-0"
    assert len(result["prior_attempts"]) == 4
    assert {row["reuse_eligibility"] for row in result["prior_attempts"]} == {
        "eligible",
        "ineligible",
    }
    assert any(
        "runtime asset identity mismatch" in row["reason"]
        for row in result["prior_attempts"]
    )
    assert any(
        "cell identity missing" in (row["reason"] or "")
        for row in result["prior_attempts"]
    )
    with pytest.raises(n4.BaselineReuseError):
        n4.issue_grant(
            frozen,
            owner_session="owner",
            job_id="job",
            lease_id="lease",
            generation_token="generation",
            output_root=tmp_path / "evidence",
            ledger_files=[ledger],
        )


def test_failed_and_foreign_same_body_attempts_are_preserved_but_do_not_refuse(
    tmp_path, profile
):
    frozen, raw = profile
    ledger = tmp_path / "attempts.jsonl"
    records = [
        _historical_record(
            raw,
            status="runtime_error",
            execution_status="incomplete",
            transport_error="timeout",
        ),
        _historical_record(raw, model_sha="9" * 64),
    ]
    ledger.write_text(
        "\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8"
    )

    review = n4.review_prior_attempts(frozen, ledger_files=[ledger])
    grant = n4.issue_grant(
        frozen,
        owner_session="owner",
        job_id="job",
        lease_id="lease",
        generation_token="generation",
        output_root=tmp_path / "evidence",
        ledger_files=[ledger],
    )

    assert review["exact_baseline_matches"] == []
    assert review["baseline_reuse_decision"] == "no_exact_baseline_reuse"
    assert len(review["prior_attempts"]) == 2
    assert all(
        row["reuse_eligibility"] == "ineligible" for row in review["prior_attempts"]
    )
    assert (
        grant["prior_attempt_review_evidence"]["prior_attempts"]
        == review["prior_attempts"]
    )


@pytest.mark.parametrize("change", ["added", "removed", "replaced"])
def test_production_ledger_inventory_change_during_scan_is_rejected(
    tmp_path, profile, monkeypatch, change
):
    frozen, raw = profile
    root = tmp_path / "runs"
    root.mkdir()
    first = root / "attempts.jsonl"
    first.write_text('{"record_type":"old"}\n', encoding="utf-8")
    monkeypatch.setattr(n4, "BASELINE_LEDGER", tmp_path / "missing-baseline.jsonl")
    monkeypatch.setattr(n4, "LEDGER_ROOT", root)
    original = n4._read_attempt_values
    changed = False

    def mutate(path, digest):
        nonlocal changed
        yield from original(path, digest)
        if changed:
            return
        changed = True
        late = root / "later-run" / "receipt.json"
        if change == "added":
            late.parent.mkdir()
            late.write_text(
                json.dumps(
                    {
                        "cells": [
                            {
                                "generations": [
                                    {"request_receipts": [{"request_body": raw}]}
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        elif change == "removed":
            first.unlink()
        else:
            replacement = root / "replacement.json"
            replacement.write_bytes(first.read_bytes())
            first.unlink()
            replacement.rename(first)

    monkeypatch.setattr(n4, "_read_attempt_values", mutate)
    with pytest.raises(n4.ProfileError, match="inventory changed"):
        n4.review_prior_attempts(frozen)


def test_explicit_test_ledger_byte_mutation_is_still_rejected(
    tmp_path, profile, monkeypatch
):
    frozen, _raw = profile
    ledger = tmp_path / "attempts.jsonl"
    ledger.write_text(
        json.dumps({"request_body": {"other": "body"}}) + "\n", encoding="utf-8"
    )
    original = n4._walk_request_records
    mutated = False

    def mutate(value, source):
        nonlocal mutated
        yield from original(value, source)
        if not mutated:
            replacement = source.with_suffix(".replacement")
            replacement.write_text(
                source.read_text(encoding="utf-8") + '{"late":true}\n', encoding="utf-8"
            )
            replacement.replace(source)
            mutated = True

    monkeypatch.setattr(n4, "_walk_request_records", mutate)
    with pytest.raises(n4.ProfileError, match="changed while being reviewed"):
        n4.review_prior_attempts(frozen, ledger_files=[ledger])


@pytest.mark.parametrize("link_name", ["runs", "claims"])
def test_run_operation_rejects_symlink_boundary_without_touching_target(
    tmp_path, monkeypatch, capsys, link_name
):
    data_dir = tmp_path / "service-data"
    base = data_dir / "research" / n4.OPERATION_KIND
    base.mkdir(parents=True)
    foreign = tmp_path / "foreign-evidence"
    foreign.mkdir(mode=0o755)
    before_mode = stat.S_IMODE(foreign.stat().st_mode)
    (foreign / "existing.txt").write_text("untouched", encoding="utf-8")
    (base / link_name).symlink_to(foreign, target_is_directory=True)
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "kind": n4.OPERATION_KIND,
        "n4_scope": "work",
        "requested_by_session": "owner",
    }
    monkeypatch.setattr("ttakkari.operations.jobs.get", lambda _job: job)
    monkeypatch.setattr(
        "ttakkari.operations.jobs.update", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "ttakkari.config.get_settings", lambda: SimpleNamespace(data_dir=data_dir)
    )

    with pytest.raises(n4.ProfileError, match="symlink"):
        n4.run_operation(job_id)

    assert stat.S_IMODE(foreign.stat().st_mode) == before_mode
    assert (foreign / "existing.txt").read_text(encoding="utf-8") == "untouched"
    assert not (foreign / job_id).exists()
    assert (base / link_name).is_symlink()
