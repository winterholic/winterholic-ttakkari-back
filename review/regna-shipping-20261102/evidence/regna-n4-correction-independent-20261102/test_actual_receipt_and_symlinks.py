"""Independent probes against the real historical N3 receipt schema and tmp paths."""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ttakkari.research import regna_n4_combo_first6 as n4


HISTORICAL_RECEIPT = Path(
    "/Users/winterholic/development/hyperez/regna-ios/docs/research/practical-20261029/"
    "execution/runs/n3-qwen3-1p7b-q4km-batch01-r4-20261101/receipt.json"
)
EXPECTED_BODY = json.dumps(
    {"model": "fixed", "messages": []}, separators=(",", ":")
)


def test_actual_raw_file_hash_sse_and_generation_usage_match_receipt_claims():
    receipt = json.loads(HISTORICAL_RECEIPT.read_text(encoding="utf-8"))
    cell = receipt["cells"][0]
    generation = cell["generations"][0]
    raw_ref = generation["raw_ref"]
    raw_path = HISTORICAL_RECEIPT.parent / raw_ref["path"]
    raw_bytes = raw_path.read_bytes()
    raw_record = json.loads(raw_bytes)
    chat_request = next(
        item
        for item in raw_record["request_receipts"]
        if item.get("path") == "/v1/chat/completions"
    )
    raw_chunks = b"".join(
        base64.b64decode(chunk, validate=True)
        for chunk in chat_request["raw_sse_chunks_base64"]
    )

    assert raw_ref["readback_verified"] is True
    assert len(raw_bytes) == raw_ref["bytes"]
    assert hashlib.sha256(raw_bytes).hexdigest() == raw_ref["sha256"]
    assert hashlib.sha256(chat_request["request_body"].encode("utf-8")).hexdigest() == chat_request[
        "request_sha256"
    ]
    assert hashlib.sha256(raw_chunks).hexdigest() == chat_request["response_sha256"]
    assert raw_ref["request_sha256_verified"] is True
    assert raw_ref["sse_bytes_verified"] is True
    assert generation["completion_tokens"] == generation["usage"]["completion_tokens"]
    assert cell["usage"]["prompt_tokens"] == cell["generations"][-1]["usage"]["prompt_tokens"]
    assert cell["usage"]["completion_tokens"] == cell["generations"][-1]["usage"]["completion_tokens"]


def _actual_receipt_case(monkeypatch, tmp_path):
    original = json.loads(HISTORICAL_RECEIPT.read_text(encoding="utf-8"))
    receipt = copy.deepcopy(original)
    cell = receipt["cells"][0]
    generation = cell["generations"][0]
    request = next(
        item
        for item in generation["request_receipts"]
        if item.get("path") == "/v1/chat/completions"
    )
    # Keep the actual receipt hierarchy and outcome evidence; substitute only the
    # request body with the bounded expected N4 request used by this code-level probe.
    request["request_body"] = EXPECTED_BODY
    cell_id = cell["cell_id"]
    assets = original["runtime_assets"]
    model_sha = assets["model"]["sha256"]
    mode = cell["generation_mode"]
    profile = n4.FrozenProfile(
        root=Path("/fixed/regna-final-isolated"),
        plan_path=Path("/fixed/plan.json"),
        source_pins_path=Path("/fixed/source-pins.json"),
        plan_sha256="a" * 64,
        source_pins_bytes_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        plan={},
        source_pins={},
        batch={"batch_id": "batch-01", "cells": [{"cell_id": cell_id}]},
        profile={
            "asset_sha256": {
                "runtime": assets["binary"]["sha256"],
                "model": model_sha,
                "template": assets["template"]["sha256"],
                "tokenizer": model_sha,
            },
            "generation_mode": mode,
        },
        input_identity_sha256_by_cell={cell_id: "d" * 64},
    )
    monkeypatch.setattr(
        n4,
        "_expected_baseline_request_hashes",
        lambda _profile: {cell_id: n4.sha256_bytes(EXPECTED_BODY.encode("utf-8"))},
    )
    ledger = tmp_path / "actual-schema-receipt.json"
    ledger.write_text(json.dumps(receipt), encoding="utf-8")
    return original, receipt, profile, ledger, cell, cell_id


def test_actual_historical_success_receipt_is_eligible_for_exact_baseline_reuse(
    tmp_path, monkeypatch
):
    original, _receipt, profile, ledger, _cell, cell_id = _actual_receipt_case(
        monkeypatch, tmp_path
    )

    assert original["schema"] == "regna-n3-runtime-r4-receipt-v1"
    assert original["execution_status"] == "complete"
    assert len(original["cells"]) == 6
    result = n4.review_prior_attempts(profile, ledger_files=[ledger])

    assert result["baseline_reuse_decision"] == "exact_baseline_reuse_found"
    assert len(result["exact_baseline_matches"]) == 1
    assert result["exact_baseline_matches"][0]["cell_id"] == cell_id
    assert result["exact_baseline_matches"][0]["reuse_eligibility"] == "eligible"


@pytest.mark.parametrize(
    ("invalidity", "expected_reason"),
    [
        ("incomplete", "historical operation is not complete"),
        ("foreign_asset", "runtime asset identity mismatch"),
        ("missing_cell", "cell identity missing"),
    ],
)
def test_actual_historical_receipt_context_keeps_invalid_attempts_ineligible(
    tmp_path, monkeypatch, invalidity, expected_reason
):
    _original, _receipt, profile, ledger, cell, _cell_id = _actual_receipt_case(
        monkeypatch, tmp_path
    )
    receipt = json.loads(ledger.read_text(encoding="utf-8"))
    if invalidity == "incomplete":
        receipt["execution_status"] = "incomplete"
    elif invalidity == "foreign_asset":
        receipt["runtime_assets"]["model"]["sha256"] = "f" * 64
    elif invalidity == "missing_cell":
        del cell["cell_id"]
        receipt["cells"][0].pop("cell_id", None)
    ledger.write_text(json.dumps(receipt), encoding="utf-8")

    result = n4.review_prior_attempts(profile, ledger_files=[ledger])

    assert result["baseline_reuse_decision"] == "no_exact_baseline_reuse"
    assert result["exact_baseline_matches"] == []
    assert len(result["prior_attempts"]) == 1
    assert result["prior_attempts"][0]["reuse_eligibility"] == "ineligible"
    assert result["prior_attempts"][0]["reason"] == expected_reason


@pytest.mark.parametrize(
    "symlink_at", ["research", "kind", "runs", "claims", "evidence"]
)
def test_run_operation_rejects_symlinked_private_evidence_boundaries_unchanged(
    tmp_path, monkeypatch, symlink_at
):
    data_dir = tmp_path / "service-data"
    foreign = tmp_path / "foreign-evidence"
    foreign.mkdir(mode=0o755)
    (foreign / "existing.txt").write_text("untouched", encoding="utf-8")
    before_mode = stat.S_IMODE(foreign.stat().st_mode)
    job_id = str(uuid.uuid4())
    research = data_dir / "research"
    kind = research / n4.OPERATION_KIND
    runs = kind / "runs"
    claims = kind / "claims"
    links = {
        "research": (research, "research"),
        "kind": (kind, "kind"),
        "runs": (runs, "runs"),
        "claims": (claims, "claims"),
        "evidence": (runs / job_id, "runs"),
    }
    link, parent = links[symlink_at]
    link.parent.mkdir(parents=True, exist_ok=True)
    if symlink_at == "evidence":
        runs.mkdir(parents=True, exist_ok=True)
    link.symlink_to(foreign, target_is_directory=True)
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

    assert link.is_symlink()
    assert stat.S_IMODE(foreign.stat().st_mode) == before_mode
    assert (foreign / "existing.txt").read_text(encoding="utf-8") == "untouched"
    assert sorted(path.name for path in foreign.iterdir()) == ["existing.txt"]
