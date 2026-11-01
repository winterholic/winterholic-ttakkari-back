"""Fixed-plan Qwen2.5 CPU operation guards and owned-child evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ttakkari.host_resource import LeaseOwnershipError
from ttakkari.operations import jobs, plans, runner
from ttakkari.research import regna_q25_first6 as q25

REGNA = Path("/Users/winterholic/development/hyperez/regna-ios")
PLAN_SHA = "b7d9bccd070b6da2b7377ab53f394ab1b6d696a22488063da547daf0cad1deeb"


@pytest.fixture(scope="module")
def profile():
    return q25.load_profile(REGNA)


def test_profile_binds_exact_frozen_plan_assets_and_first_six_cells(profile):
    assert profile.plan_sha256 == PLAN_SHA
    assert [cell["cell_id"] for cell in profile.cells] == [
        "ih-schedule_conflict-01:s1701",
        "ih-schedule_conflict-01:s1702",
        "ih-schedule_conflict-01:s1703",
        "ih-schedule_conflict-02:s1701",
        "ih-schedule_conflict-02:s1702",
        "ih-schedule_conflict-02:s1703",
    ]
    assert profile.cpu_only is True
    assert profile.max_rss_bytes == 6 * 1024**3


def test_profile_rejects_an_unpinned_r4_runner_hash(monkeypatch):
    monkeypatch.setattr(q25, "RUNNER_SHA256", "0" * 64)

    with pytest.raises(q25.ProfileError, match="runner hash"):
        q25.load_profile(REGNA)


def test_profile_rejects_a_caller_selected_path_or_extra_execution_input():
    with pytest.raises(q25.ProfileError):
        q25.parse_operation_input({"model_path": "/tmp/other.gguf"})


def test_operation_plan_has_only_the_fixed_server_entrypoint_and_deadline(profile, monkeypatch):
    monkeypatch.setattr(q25, "load_profile", lambda: profile)

    plan = plans.make_plan(q25.OPERATION_KIND, "fixed-job-id")

    assert plan.argv[-2:] == ["run", "fixed-job-id"]
    assert plan.argv[-3] == "ttakkari.research.regna_q25_first6"
    assert "--model" not in plan.argv and "--path" not in plan.argv
    assert plan.timeout == q25.OVERALL_TIMEOUT_SECONDS + q25.CLEANUP_RESERVE_SECONDS


def test_pinned_r4_runner_loads_and_accepts_only_backend_built_argv(profile, tmp_path):
    runner_module = q25._load_r4_runner(profile)
    argv = q25.build_runner_argv(profile, job_id="job-1", owner_session="work-session-1",
                                 grant_path=tmp_path / "grant.json", output_root=tmp_path / "out",
                                 claim_root=tmp_path / "claims")

    parsed = runner_module._parser().parse_args(argv[2:])

    assert parsed.engine == "ttakkari-host-cpu"
    assert parsed.context_size == 32768
    assert parsed.threads == 6
    assert parsed.startup_timeout == parsed.request_timeout == 120
    assert str(parsed.plan) == str(profile.plan_path)


def test_frozen_dispatch_gets_zero_argument_factory_with_unique_owned_paths(tmp_path):
    args = object()
    calls = []
    bound = q25._dispatch_server_factory(lambda actual_args, path: calls.append((actual_args, path)) or path,
                                         args, tmp_path / "run")

    first, second = bound(), bound()

    assert calls == [(args, tmp_path / "run" / "owned-server-0"),
                     (args, tmp_path / "run" / "owned-server-1")]
    assert first != second


def test_fresh_grant_is_bound_to_work_owner_and_exact_plan(tmp_path, profile):
    grant = q25.issue_grant(profile, owner_session="work-session-1", job_id="job-1",
                            output_root=tmp_path / "out", claim_root=tmp_path / "claims")

    assert grant["authorized_dispatch"] is True
    assert grant["owner_id"] == "work-session-1"
    assert grant["plan_sha256"] == PLAN_SHA
    assert grant["cells"] == list(profile.cells)
    assert grant["cell_timeout_seconds"] == 120
    assert grant["overall_timeout_seconds"] == 900
    assert grant["cleanup_reserve_seconds"] == 30
    assert grant["grant_id"] != ""


def test_exact_cell_claim_is_exclusive_and_not_reused(tmp_path, profile):
    claim_root = tmp_path / "claims"
    q25.claim_first6(profile, claim_root, owner_session="work-session-1", job_id="job-1")

    with pytest.raises(q25.DuplicateClaim):
        q25.claim_first6(profile, claim_root, owner_session="work-session-1", job_id="job-2")


@pytest.mark.parametrize("field,value", [
    ("owner_id", "another-session"),
    ("scope_id", "personal"),
    ("plan_sha256", "0" * 64),
    ("source_manifest_sha256", "1" * 64),
    ("output_root", "/tmp/caller-selected"),
])
def test_grant_rejects_wrong_owner_scope_hash_or_path(tmp_path, profile, field, value):
    output, claims = tmp_path / "out", tmp_path / "claims"
    grant = q25.issue_grant(profile, owner_session="work-session-1", job_id="job-1",
                            output_root=output, claim_root=claims)
    grant[field] = value

    with pytest.raises(q25.ProfileError):
        q25.validate_grant(profile, grant, owner_session="work-session-1", job_id="job-1",
                           output_root=output, claim_root=claims)


def test_grant_expiry_before_cleanup_reserve_is_rejected(tmp_path, profile):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    output, claims = tmp_path / "out", tmp_path / "claims"
    grant = q25.issue_grant(profile, owner_session="work-session-1", job_id="job-1",
                            output_root=output, claim_root=claims, now=now)

    with pytest.raises(q25.ProfileError, match="expired"):
        q25.validate_grant(profile, grant, owner_session="work-session-1", job_id="job-1",
                           output_root=output, claim_root=claims,
                           now=now + timedelta(seconds=q25.OVERALL_TIMEOUT_SECONDS + 1))


def test_preflight_fails_closed_on_unknown_process_or_memory_observation(monkeypatch):
    class UnknownPsutil:
        @staticmethod
        def virtual_memory():
            raise PermissionError("memory probe unavailable")

        @staticmethod
        def process_iter(_attrs):
            return iter(())

    with pytest.raises(q25.PreflightError):
        q25.preflight(UnknownPsutil)


def test_preflight_fails_closed_when_process_enumeration_is_incomplete():
    class IncompletePsutil:
        STATUS_ZOMBIE = "zombie"

        @staticmethod
        def virtual_memory():
            return SimpleNamespace(available=8 * 1024**3)

        @staticmethod
        def process_iter(_attrs):
            return iter([SimpleNamespace(info={"pid": 22, "cmdline": None, "create_time": None,
                                              "uids": None, "status": "running", "exe": "/unknown"})])

    with pytest.raises(q25.PreflightError, match="incomplete"):
        q25.preflight(IncompletePsutil)


def test_raw_evidence_missing_or_incomplete_never_reports_success(tmp_path):
    (tmp_path / "receipt.json").write_text(json.dumps({"execution_status": "complete", "cells": []}))

    result = q25.verify_raw_evidence(tmp_path)

    assert result["verified"] is False
    assert result["reason"]


def test_malformed_raw_evidence_fails_closed_without_throwing(tmp_path):
    (tmp_path / "receipt.json").write_text("[]")
    (tmp_path / "cells.json").write_text(json.dumps({"cells": [None]}))

    assert q25.verify_raw_evidence(tmp_path)["verified"] is False


def test_complete_synthetic_raw_receipt_requires_and_verifies_all_six_cells(tmp_path):
    output = tmp_path / "run"
    rows, ids = [], []
    for case_id, cell_id, seed in q25.EXPECTED_CELLS:
        request_body = '{"messages":[]}'
        sse = b"data: {\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":1}}\n\n"
        receipt = {
            "request_body": request_body,
            "request_sha256": hashlib.sha256(request_body.encode()).hexdigest(),
            "raw_sse_chunks_base64": [base64.b64encode(sse).decode()],
            "response_sha256": hashlib.sha256(sse).hexdigest(),
        }
        raw_path = output / "raw" / f"{case_id}-s{seed}-g0.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = json.dumps({"generation": 0, "request_receipts": [receipt]}).encode()
        raw_path.write_bytes(raw_bytes)
        ref = {"path": str(raw_path.relative_to(output)), "sha256": hashlib.sha256(raw_bytes).hexdigest(),
               "bytes": len(raw_bytes), "readback_verified": True,
               "request_sha256_verified": True, "sse_bytes_verified": True}
        cell_path = output / "cells" / f"{case_id}-s{seed}.json"
        cell_path.parent.mkdir(parents=True, exist_ok=True)
        cell_path.write_text(json.dumps({"case_id": case_id, "seed": seed,
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                                         "generations": [{"generation": 0, "request_receipts": [receipt],
                                                          "raw_ref": ref}]}))
        rows.append({"cell_id": cell_id, "raw_records_durable": True,
                     "cleanup_verified": True, "usage_state": "observed_exact_integer"})
        ids.append(cell_id)
    (output / "cells.json").write_text(json.dumps({"cells": rows}))
    (output / "receipt.json").write_text(json.dumps({"execution_status": "complete", "cells_attempted": ids,
                                                      "cleanup_verified": {"verified": True},
                                                      "raw_records_durable": True}))

    assert q25.verify_raw_evidence(output)["verified"] is True
    second_case, _, second_seed = q25.EXPECTED_CELLS[1]
    second_path = output / "cells" / f"{second_case}-s{second_seed}.json"
    second = json.loads(second_path.read_text())
    second["generations"][0]["raw_ref"]["path"] = "raw/ih-schedule_conflict-01-s1701-g0.json"
    second_path.write_text(json.dumps(second))
    assert q25.verify_raw_evidence(output)["verified"] is False


def test_cleanup_unknown_preserves_active_lease(tmp_path):
    manager = q25.lease_manager(tmp_path)
    lease = manager.acquire(
        owner_task="job-1", owner_attempt="session-1", runtime_instance="job-1",
        resource_family="llm", claim={"profile": q25.PROFILE_ID}, ttl_seconds=60,
    )

    receipt = {"cleanup_verified": {"verified": True, "cells": [{"verified": True}]},
               "cleanup": [{"state": "stopped", "exit_code": 0, "stopped_by_this_run": True, "pid": 81}],
               "cells_attempted": [q25.EXPECTED_CELLS[0][1]],
               "cells": [{"cell_id": q25.EXPECTED_CELLS[0][1], "case_id": q25.EXPECTED_CELLS[0][0],
                          "seed": q25.EXPECTED_CELLS[0][2], "cleanup_verified": True}]}
    released = q25.release_after_cleanup(manager, lease, output_root=tmp_path / "missing", receipt=receipt)

    assert released is False
    assert manager.current()["lease_id"] == lease.lease_id


def test_cleanup_boolean_without_wait4_receipt_is_not_a_release_proof(tmp_path):
    receipt = {"cleanup_verified": {"verified": True, "cells": [{"verified": True}]},
               "cleanup": [{"state": "stopped", "exit_code": 0, "stopped_by_this_run": True, "pid": 81}],
               "cells": [{"cleanup_verified": True}]}

    assert q25.verify_owned_cleanup(tmp_path, receipt) is False


def _synthetic_wait4_receipt(output_root, pid, exit_code):
    case_id, cell_id, seed = q25.EXPECTED_CELLS[0]
    server = output_root / "owned-server-0"
    server.mkdir(parents=True)
    (server / "process-receipt.json").write_text(json.dumps({
        "pid": pid,
        "events": [
            {"event": "wait4_reaped", "exit_code": exit_code},
            {"event": "stopped", "exit_code": exit_code, "stopped_by_this_run": True},
        ],
    }))
    return {"backend_rss_monitor_stopped": True,
            "cleanup_verified": {"verified": True, "cells": [{"verified": True}]},
            "cleanup": [{"state": "stopped", "exit_code": exit_code, "stopped_by_this_run": True, "pid": pid}],
            "cells_attempted": [cell_id],
            "cells": [{"cell_id": cell_id, "case_id": case_id, "seed": seed, "cleanup_verified": True}]}


def test_wait4_and_owned_stop_receipts_prove_cleanup_before_lease_release(tmp_path):
    run = tmp_path / "run"
    receipt = _synthetic_wait4_receipt(run, 81, 0)

    assert q25.verify_owned_cleanup(run, receipt) is True


def test_dead_supervisor_with_missing_raw_receipt_keeps_lease(tmp_path):
    manager = q25.lease_manager(tmp_path)
    lease = manager.acquire(
        owner_task="job-dead", owner_attempt="session-1", runtime_instance="job-dead",
        resource_family="llm", claim={"profile": q25.PROFILE_ID}, ttl_seconds=60,
    )
    lease._lock_fd.close()  # simulate owner process death; the durable lease record remains live
    job = {"id": "job-dead", "kind": q25.OPERATION_KIND, "q25_lease_id": lease.lease_id,
           "q25_generation_token": lease.generation_token, "q25_runtime_instance": lease.runtime_instance,
           "q25_evidence_root": str(tmp_path / "missing")}

    assert q25.recover_lease_after_owner_exit(
        job, tmp_path, operation_runner_confirmed_group_absent=False
    ) is False
    assert manager.current()["lease_id"] == lease.lease_id


def test_dead_supervisor_releases_only_after_outer_owned_group_is_confirmed_absent(tmp_path):
    manager = q25.lease_manager(tmp_path)
    lease = manager.acquire(
        owner_task="job-dead", owner_attempt="session-1", runtime_instance="job-dead",
        resource_family="llm", claim={"profile": q25.PROFILE_ID}, ttl_seconds=60,
    )
    lease._lock_fd.close()
    job = {"id": "job-dead", "requested_by_session": "session-1", "kind": q25.OPERATION_KIND,
           "q25_lease_id": lease.lease_id, "q25_generation_token": lease.generation_token,
           "q25_runtime_instance": lease.runtime_instance, "q25_evidence_root": str(tmp_path / "missing")}

    assert q25.recover_lease_after_owner_exit(
        job, tmp_path, operation_runner_confirmed_group_absent=True
    ) is True
    assert manager.current() is None
    assert q25.verify_raw_evidence(tmp_path / "missing")["verified"] is False


def test_normal_synthetic_child_is_waited_before_q25_lease_release(tmp_path, monkeypatch):
    operations_root = tmp_path / "operations"
    (operations_root / "jobs").mkdir(parents=True)
    monkeypatch.setattr(jobs, "root", lambda: operations_root)
    job = jobs.create(q25.OPERATION_KIND, "work-session-1")
    monkeypatch.setattr(runner, "_child_setup", lambda pidfile: _synthetic_child_setup(pidfile))
    lease = q25.lease_manager(tmp_path).acquire(
        owner_task=job["id"], owner_attempt="work-session-1", runtime_instance=job["id"],
        resource_family="llm", claim={"profile": q25.PROFILE_ID}, ttl_seconds=60,
    )
    plan = plans.Plan(q25.OPERATION_KIND, [sys.executable, "-c", "print('synthetic-child-ok')"],
                      {"PATH": "/usr/bin:/bin"}, 3)
    with (tmp_path / "synthetic.out").open("w+") as output:
        status, exit_code = runner._execute(plan, output, job_id=job["id"])

    assert (status, exit_code) == ("succeeded", 0)
    assert jobs.get(job["id"])["child_started"] is not None
    child_pid = jobs.get(job["id"])["child_pid"]
    output_root = tmp_path / "normal-cleanup"
    receipt = _synthetic_wait4_receipt(output_root, child_pid, exit_code)
    assert q25.release_after_cleanup(q25.lease_manager(tmp_path), lease,
                                    output_root=output_root, receipt=receipt) is True
    assert q25.lease_manager(tmp_path).current() is None


def test_midrun_cancel_waits_owned_synthetic_child_and_releases_only_after_cleanup(tmp_path, monkeypatch):
    operations_root = tmp_path / "operations"
    (operations_root / "jobs").mkdir(parents=True)
    monkeypatch.setattr(jobs, "root", lambda: operations_root)
    job = jobs.create(q25.OPERATION_KIND, "work-session-1")
    monkeypatch.setattr(runner, "_child_setup", lambda pidfile: _synthetic_child_setup(pidfile))
    lease = q25.lease_manager(tmp_path).acquire(
        owner_task=job["id"], owner_attempt="work-session-1", runtime_instance=job["id"],
        resource_family="llm", claim={"profile": q25.PROFILE_ID}, ttl_seconds=60,
    )
    plan = plans.Plan(q25.OPERATION_KIND,
                      [sys.executable, "-c", "import time; time.sleep(30)"],
                      {"PATH": "/usr/bin:/bin"}, 3)
    monkeypatch.setattr(runner, "_cancelled", False)
    result = []

    def execute():
        with (tmp_path / "cancel.out").open("w+") as output:
            result.append(runner._execute(plan, output, job_id=job["id"]))

    import threading

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 5
    while not jobs.get(job["id"]).get("child_pid") and time.monotonic() < deadline:
        time.sleep(0.02)
    monkeypatch.setattr(runner, "_cancelled", True)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result and result[0][0] == "cancelled"
    child_pid = jobs.get(job["id"])["child_pid"]
    output_root = tmp_path / "cancel-cleanup"
    receipt = _synthetic_wait4_receipt(output_root, child_pid, result[0][1])
    assert q25.release_after_cleanup(q25.lease_manager(tmp_path), lease,
                                    output_root=output_root, receipt=receipt) is True


def _synthetic_child_setup(pidfile):
    def write_pid():
        fd = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(str(os.getpid()).encode())
    return write_pid


def test_wrong_lease_owner_or_generation_cannot_release(tmp_path):
    manager = q25.lease_manager(tmp_path)
    lease = manager.acquire(
        owner_task="job-1", owner_attempt="session-1", runtime_instance="job-1",
        resource_family="llm", claim={"profile": q25.PROFILE_ID}, ttl_seconds=60,
    )

    with pytest.raises(LeaseOwnershipError):
        manager.release("another-lease", lease.generation_token,
                        proof={"runtime_status": "stopped", "evidence": {"runtime_instance": "job-1"}})


def test_timed_out_synthetic_child_is_waited_before_lease_release(tmp_path, monkeypatch):
    operations_root = tmp_path / "operations"
    (operations_root / "jobs").mkdir(parents=True)
    monkeypatch.setattr(jobs, "root", lambda: operations_root)
    job = jobs.create(q25.OPERATION_KIND, "work-session-1")
    monkeypatch.setattr(runner, "_child_setup", lambda pidfile: _synthetic_child_setup(pidfile))
    lease = q25.lease_manager(tmp_path).acquire(
        owner_task=job["id"], owner_attempt="work-session-1", runtime_instance=job["id"],
        resource_family="llm", claim={"profile": q25.PROFILE_ID}, ttl_seconds=60,
    )
    plan = plans.Plan(q25.OPERATION_KIND,
                      [sys.executable, "-c", "import time; time.sleep(30)"],
                      {"PATH": "/usr/bin:/bin"}, 0.1)
    with (tmp_path / "timeout.out").open("w+") as output:
        status, exit_code = runner._execute(plan, output, job_id=job["id"])

    assert status == "timeout"
    assert exit_code is not None
    assert jobs.get(job["id"])["child_started"] is not None
    child_pid = jobs.get(job["id"])["child_pid"]
    output_root = tmp_path / "timeout-cleanup"
    receipt = _synthetic_wait4_receipt(output_root, child_pid, exit_code)
    assert q25.release_after_cleanup(q25.lease_manager(tmp_path), lease,
                                    output_root=output_root, receipt=receipt) is True
