"""Server-owned N4 batch connector fences and fake production dispatch."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ttakkari.research import regna_n4_combo_first6 as n4


@pytest.fixture
def profile(monkeypatch):
    value = n4.FrozenProfile(
        root=Path("/fixed/regna-final-isolated"),
        plan_path=Path("/fixed/plan.json"),
        source_pins_path=Path("/fixed/source-pins.json"),
        plan_sha256="a" * 64,
        source_pins_bytes_sha256="e" * 64,
        source_manifest_sha256="b" * 64,
        plan={
            "manifest_sha256": "c" * 64,
            "request_contract": {"tool_schema_sha256": "d" * 64},
            "batches": [
                {
                    "batch_id": "batch-01",
                    "cells": [{"cell_id": f"cell-{i}"} for i in range(6)],
                }
            ],
        },
        source_pins={
            "files": {f"source-{i}": {"sha256": f"{i:064x}"} for i in range(15)}
        },
        batch={
            "batch_id": "batch-01",
            "cells": [{"cell_id": f"cell-{i}"} for i in range(6)],
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
        input_identity_sha256_by_cell={f"cell-{i}": f"{i + 1:064x}" for i in range(6)},
    )
    body = {"model": "fixed", "messages": []}
    monkeypatch.setattr(
        n4,
        "_expected_baseline_request_hashes",
        lambda _profile: {
            f"cell-{i}": n4.sha256_bytes(
                json.dumps(body, separators=(",", ":")).encode()
            )
            for i in (0, 2, 4)
        },
    )
    return value


def test_issue_grant_binds_job_lease_owner_plan_assets_mode_and_prior_ledger(
    tmp_path, profile
):
    ledger = tmp_path / "attempt-ledger.jsonl"
    ledger.write_text(json.dumps({"request_sha256": "no-match"}) + "\n")

    grant = n4.issue_grant(
        profile,
        owner_session="work-session",
        job_id="job-id",
        lease_id="lease-id",
        generation_token="generation",
        attempt_ledger=ledger,
        output_root=tmp_path / "private-evidence",
    )

    assert grant["owner_id"] == "work-session"
    assert grant["invocation_id"] == "job-id"
    assert grant["lease_id"] == "lease-id"
    assert grant["plan_sha256"] == n4.canonical_digest(profile.plan)
    assert (
        grant["input_identity_sha256_by_cell"] == profile.input_identity_sha256_by_cell
    )
    assert grant["prior_attempt_review"]["reviewed"] is True
    assert (
        grant["prior_attempt_review"]["ledger_sha256"] == n4._ledger_digest([ledger])[0]
    )
    assert grant["max_cells"] == 6
    assert grant["cell_timeout_seconds"] == 120
    assert grant["overall_timeout_seconds"] == 900
    assert grant["cleanup_reserve_seconds"] == 30


def test_exact_reusable_baseline_from_actual_attempt_ledger_is_preserved_and_refused(
    tmp_path, profile
):
    ledger = tmp_path / "attempt-ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "status": "completed",
                "cell_id": "cell-0",
                "request_body": json.dumps(
                    {"model": "fixed", "messages": []}, separators=(",", ":")
                ),
                "raw_receipt": {"path": "existing/raw.json"},
            }
        )
        + "\n"
    )

    with pytest.raises(n4.BaselineReuseError) as exc:
        n4.issue_grant(
            profile,
            owner_session="work-session",
            job_id="job-id",
            lease_id="lease-id",
            generation_token="generation",
            attempt_ledger=ledger,
            output_root=tmp_path / "private-evidence",
        )

    assert exc.value.matches[0]["cell_id"] == "cell-0"
    assert exc.value.matches[0]["request_sha256"] == n4.sha256_bytes(
        json.dumps({"model": "fixed", "messages": []}, separators=(",", ":")).encode()
    )
    assert ledger.read_text().endswith("\n")


def test_incomplete_attempt_ledger_is_not_treated_as_reviewed(tmp_path, profile):
    ledger = tmp_path / "attempts.jsonl"
    ledger.write_text('{"status":"completed"}\n{"status":')

    with pytest.raises(n4.ProfileError, match="invalid JSON"):
        n4.review_prior_attempts(profile, ledger_files=[ledger])


def test_ledger_mutation_during_review_is_rejected(tmp_path, profile, monkeypatch):
    ledger = tmp_path / "attempts.jsonl"
    ledger.write_text("{}\n")
    original_walk = n4._walk_request_records
    mutated = False

    def mutate_during_scan(value, source):
        nonlocal mutated
        yield from original_walk(value, source)
        if not mutated:
            with source.open("a", encoding="utf-8") as stream:
                stream.write('{"late_record":true}\n')
            mutated = True

    monkeypatch.setattr(n4, "_walk_request_records", mutate_during_scan)
    with pytest.raises(n4.ProfileError, match="changed while being reviewed"):
        n4.review_prior_attempts(profile, ledger_files=[ledger])


def test_claim_is_single_use_and_tampered_grant_is_rejected(tmp_path, profile):
    ledger = tmp_path / "attempt-ledger.jsonl"
    ledger.write_text("{}\n")
    grant = n4.issue_grant(
        profile,
        owner_session="work-session",
        job_id="job-id",
        lease_id="lease-id",
        generation_token="generation",
        attempt_ledger=ledger,
        output_root=tmp_path / "private-evidence",
    )
    claim_root = tmp_path / "claims"
    n4.claim_grant(grant, claim_root)
    with pytest.raises(n4.DuplicateClaim):
        n4.claim_grant(grant, claim_root)

    changed = copy.deepcopy(grant)
    changed["cells"][0]["cell_id"] = "other-cell"
    with pytest.raises(n4.ProfileError):
        n4.validate_grant(
            profile,
            changed,
            owner_session="work-session",
            job_id="job-id",
            lease_id="lease-id",
            generation_token="generation",
            output_root=tmp_path / "private-evidence",
        )


def test_grant_expiry_must_cover_cleanup_window(tmp_path, profile):
    ledger = tmp_path / "attempt-ledger.jsonl"
    ledger.write_text("{}\n")
    now = datetime.now(UTC)
    grant = n4.issue_grant(
        profile,
        owner_session="work-session",
        job_id="job-id",
        lease_id="lease-id",
        generation_token="generation",
        attempt_ledger=ledger,
        output_root=tmp_path / "private-evidence",
        now=now,
    )
    with pytest.raises(n4.ProfileError, match="expired"):
        n4.validate_grant(
            profile,
            grant,
            owner_session="work-session",
            job_id="job-id",
            lease_id="lease-id",
            generation_token="generation",
            output_root=tmp_path / "private-evidence",
            now=now + timedelta(seconds=900),
        )


def test_unknown_child_cleanup_keeps_lease_and_failure_receipt(tmp_path):
    receipt = {
        "execution_status": "incomplete",
        "attempts": [],
        "not_run_cell_ids": ["cell-0"],
    }
    released = []

    result = n4.finish_attempt(
        receipt,
        output_root=tmp_path,
        lease_release=lambda proof: released.append(proof),
        cleanup_evidence={"verified": False, "reaped": None},
    )

    assert result["execution_status"] == "incomplete"
    assert result["cleanup_verified"] is False
    assert released == []
    persisted = json.loads((tmp_path / "receipt.json").read_text())
    assert persisted["cleanup_verified"] is False


def test_failure_receipt_is_durable_and_cleanup_requires_positive_direct_child_reap(
    tmp_path,
):
    path = tmp_path / "private" / "failure-receipt.json"
    receipt = n4._failure_receipt(
        path,
        job_id="job-id",
        stage="dispatch",
        exc=RuntimeError("fake"),
        child_possible=True,
        planned_cell_ids=["cell-0"],
    )

    assert json.loads(path.read_text()) == receipt
    assert receipt["not_run_cell_ids"] == ["cell-0"]
    assert receipt["cleanup_verified"] is False
    assert (
        n4.verify_owned_cleanup(
            {
                "attempts": [
                    {
                        "cleanup": {
                            "cleanup_verified": True,
                            "lifecycle_check": {
                                "verified": True,
                                "child_created": True,
                                "reaped": None,
                            },
                        }
                    }
                ]
            }
        )
        is False
    )


def test_batch_summary_compares_attempt_elapsed_sum_with_outer_wall_time():
    attempts = [{"elapsed_seconds": 1.25}, {"elapsed_seconds": 2.0}]
    result = n4.walltime_summary(attempts, 2.5)
    assert result == {
        "attempt_elapsed_seconds_sum": 3.25,
        "outer_wall_seconds": 2.5,
        "outer_covers_attempt_sum": False,
    }


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body

    def read1(self, _size):
        if self._read:
            return b""
        self._read = True
        return self.body


class _FakeOwnedPopen:
    def __init__(self, pid, command, stdout):
        self.pid, self.returncode, self.stdout = pid, None, stdout
        port = command[command.index("--port") + 1]
        stdout.write(f"server listening on http://127.0.0.1:{port}\n".encode())
        stdout.flush()

    def _handle_exitstatus(self, status):
        self.returncode = status >> 8

    def poll(self):
        return self.returncode


class _FakeRUsage:
    ru_maxrss = 4096


class _FakePsutilProcess:
    def __init__(self, _pid):
        self.pid = _pid

    def create_time(self):
        return 1000.0

    def status(self):
        return "running"

    def memory_info(self):
        return type("Memory", (), {"rss": 1})()


def _exercise_real_n4_dispatch(tmp_path, monkeypatch, *, omit_done=False):
    from urllib.parse import urlparse

    profile = n4.load_profile()
    import psutil
    from tools.practical_research.execution import driver
    from tools.practical_research.runtime import llama_http

    ledger = tmp_path / "actual-attempt-ledger.jsonl"
    ledger.write_text('{"record_type":"historical-attempt","status":"completed"}\n')
    output_root = tmp_path / "job" / "batch-01"
    grant = n4.issue_grant(
        profile,
        owner_session="work-session",
        job_id="job-1",
        lease_id="lease-1",
        generation_token="generation-1",
        attempt_ledger=ledger,
        output_root=output_root,
    )
    request_payloads, servers, signals, wait_counts, stop_requested = (
        [],
        [],
        [],
        {},
        set(),
    )
    next_pid = 58100

    def fake_popen(command, *, stdout, **_kwargs):
        nonlocal next_pid
        pid = next_pid
        next_pid += 1
        server = _FakeOwnedPopen(pid, command, stdout)
        servers.append(server)
        return server

    def fake_wait4(pid, options):
        assert options == driver.os.WNOHANG
        count = wait_counts.get(pid, 0) + 1
        wait_counts[pid] = count
        if pid not in stop_requested and count < 10:
            return 0, 0, None
        return pid, 0, _FakeRUsage()

    def fake_kill(pid, sig):
        signals.append((pid, sig))
        if sig in (driver.signal.SIGTERM, driver.signal.SIGKILL):
            stop_requested.add(pid)

    def fake_urlopen(request, timeout=0):
        if isinstance(request, str):
            if urlparse(request).path == "/health":
                return _FakeHTTPResponse(b"{}")
            raise AssertionError(f"unexpected GET {request}")
        path = urlparse(request.full_url).path
        body = json.loads(request.data.decode()) if request.data else None
        if path == "/apply-template":
            return _FakeHTTPResponse(
                json.dumps({"prompt": "synthetic rendered prompt"}).encode()
            )
        if path == "/tokenize":
            return _FakeHTTPResponse(json.dumps({"tokens": [1]}).encode())
        assert path == "/v1/chat/completions", path
        request_payloads.append(body)
        call = len(request_payloads)
        if call == 1:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "test-call",
                        "type": "function",
                        "function": {
                            "name": "state.read",
                            "arguments": json.dumps({"collection": "calendar"}),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta, finish = {"content": "Synthetic response"}, "stop"
        event = {
            "id": f"test-{call}",
            "model": "Qwen3-1.7B-Q4_K_M.gguf",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10 + call, "completion_tokens": 4},
        }
        raw = b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
        if not (omit_done and call == 1):
            raw += b"data: [DONE]\n\n"
        return _FakeHTTPResponse(raw)

    monkeypatch.setattr(driver, "choose_port", lambda: 19001 + len(servers))
    monkeypatch.setattr(driver.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(driver.os, "wait4", fake_wait4)
    monkeypatch.setattr(driver.os, "kill", fake_kill)
    monkeypatch.setattr(psutil, "Process", _FakePsutilProcess)
    monkeypatch.setattr(llama_http.urllib.request, "urlopen", fake_urlopen)

    dispatcher = n4._load_dispatcher(profile.root)
    runtime_root = tmp_path / "fake-runtime-root"
    asset_dir = runtime_root / "fake-assets"
    asset_dir.mkdir(parents=True)
    assets = {
        "binary": asset_dir / "llama-server",
        "model": asset_dir / "Qwen3-1.7B-Q4_K_M.gguf",
        "template": asset_dir / "chat-template.jinja",
    }
    for path in assets.values():
        path.write_bytes(b"non-loadable fake asset placeholder")
    metadata = (
        runtime_root
        / "docs/research/practical-20261029/lead-review/results-intake-r282-20261031/runtime-assets.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "binary": str(assets["binary"]),
                "model": str(assets["model"]),
                "template": str(assets["template"]),
                "threads": 6,
                "binary_sha256": profile.profile["asset_sha256"]["runtime"],
                "model_sha256": profile.profile["asset_sha256"]["model"],
                "template_sha256": profile.profile["asset_sha256"]["template"],
            }
        )
    )
    hash_map = {
        path.resolve(): profile.profile["asset_sha256"][key]
        for key, path in (
            ("runtime", assets["binary"]),
            ("model", assets["model"]),
            ("template", assets["template"]),
        )
    }
    original_make_child = dispatcher._make_child
    adapters_seen = []

    def make_child(profile_binding, run_dir):
        original_root = dispatcher.ROOT
        dispatcher.ROOT = runtime_root
        original_hash = dispatcher._sha256_file
        dispatcher._sha256_file = lambda path: hash_map[Path(path).resolve()]
        try:
            child = original_make_child(profile_binding, run_dir)
        finally:
            dispatcher.ROOT = original_root
            dispatcher._sha256_file = original_hash
        original_start = child.start

        def start():
            adapter = original_start()
            adapters_seen.append(adapter)
            return adapter

        child.start = start
        return child

    monkeypatch.setattr(dispatcher, "_make_child", make_child)
    receipt = n4._dispatch(profile, grant, output_root)
    persisted = json.loads((output_root / "receipt.json").read_text())
    return (
        receipt,
        persisted,
        request_payloads,
        adapters_seen,
        servers,
        signals,
        wait_counts,
    )


def test_fake_production_dispatch_uses_measured_owned_server_and_common_http_adapter(
    tmp_path, monkeypatch
):
    receipt, persisted, requests, adapters, servers, signals, wait_counts = (
        _exercise_real_n4_dispatch(tmp_path, monkeypatch)
    )

    assert receipt == persisted
    assert receipt["execution_status"] == "complete", receipt["attempts"][0].get(
        "error"
    )
    assert [row["arm"] for row in receipt["attempts"]] == ["baseline", "INT-003"] * 3
    assert len(servers) == 6 and len(adapters) == 6
    assert all(type(adapter).__name__ == "LlamaHTTPAdapter" for adapter in adapters)
    assert [
        row["attempt_elapsed_seconds"]
        if "attempt_elapsed_seconds" in row
        else row["elapsed_seconds"]
        for row in receipt["attempts"]
    ]
    assert all(row["cleanup"]["cleanup_verified"] for row in receipt["attempts"])
    assert all(
        row["cleanup"]["lifecycle_check"]["reaped"] is True
        for row in receipt["attempts"]
    )
    assert len(requests) == 7
    assert requests[0]["model"] == "Qwen3-1.7B-Q4_K_M.gguf"
    assert requests[0]["seed"] == 1701 and requests[0]["temperature"] == 0.0
    assert (
        requests[0]["messages"][0]["content"] != requests[2]["messages"][0]["content"]
    )
    assert len(signals) == 6 and len(wait_counts) == 6


def test_fake_production_dispatch_failure_keeps_raw_receipt_and_owned_reap(
    tmp_path, monkeypatch
):
    receipt, persisted, requests, adapters, servers, signals, _wait_counts = (
        _exercise_real_n4_dispatch(tmp_path, monkeypatch, omit_done=True)
    )

    assert receipt == persisted
    assert receipt["execution_status"] == "incomplete"
    assert len(receipt["attempts"]) == 1
    assert receipt["attempts"][0]["status"] == "raw_invalid", receipt["attempts"][0]
    assert receipt["attempts"][0]["raw_http_receipts"]
    assert receipt["attempts"][0]["cleanup"]["cleanup_verified"] is True
    assert receipt["not_run_cell_ids"] == receipt["planned_cell_ids"][1:]
    assert len(requests) == 1 and len(adapters) == len(servers) == len(signals) == 1
