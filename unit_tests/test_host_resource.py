from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ttakkari.host_gpu import HostGpu, Supervisor
from ttakkari.host_gpu_guard import ProcState
from ttakkari.host_resource import (
    LeaseBusy,
    LeaseOwnershipError,
    ResourceLeaseManager,
)


def _manager(tmp_path: Path) -> ResourceLeaseManager:
    data = tmp_path / "data"
    return ResourceLeaseManager(data_dir=data)


def _acquire(manager: ResourceLeaseManager, *, engine: str = "comfy", now: float = 100.0):
    return manager.acquire(
        owner_task=f"task-{engine}",
        owner_attempt=f"attempt-{engine}-1",
        runtime_instance=f"runtime-{engine}-1",
        resource_family=engine,
        claim={"gpu": 1, "memory_bytes": 1024},
        ttl_seconds=10,
        now=now,
    )


def test_llm_and_comfy_share_one_exclusive_host_lease(tmp_path):
    manager = _manager(tmp_path)
    comfy = _acquire(manager)

    with pytest.raises(LeaseBusy):
        _acquire(_manager(tmp_path), engine="llm")

    assert comfy.resource_family == "comfy"
    assert comfy.owner_task == "task-comfy"
    assert comfy.owner_attempt == "attempt-comfy-1"
    assert comfy.runtime_instance == "runtime-comfy-1"
    assert comfy.generation_token


def test_existing_host_gpu_supervisor_acquires_from_the_shared_manager(tmp_path):
    data = tmp_path / "data"
    supervisor = Supervisor(
        {"launch_id": "llm-attempt-1", "request": {"model": "allowlisted-test"},
         "state_dir": str(data / "host-gpu"), "max_seconds": 30},
    )
    llm = supervisor._acquire()
    assert llm.resource_family == "llm"

    with pytest.raises(LeaseBusy):
        _acquire(ResourceLeaseManager(data_dir=data), engine="comfy")

    llm.release(proof={"runtime_status": "stopped", "evidence": {
        "runtime_instance": llm.runtime_instance, "server_started": False,
    }})


def test_unmigrated_live_llama_state_blocks_new_managed_lease(tmp_path):
    manager = _manager(tmp_path)
    legacy_state = manager.data_dir / "host-gpu" / "state.json"
    legacy_state.parent.mkdir(parents=True)
    legacy_state.write_text('{"launch_id":"legacy","phase":"ready"}', encoding="utf-8")

    with pytest.raises(LeaseBusy, match="legacy host_gpu"):
        _acquire(manager)


def test_foreign_cancel_and_release_are_rejected(tmp_path):
    manager = _manager(tmp_path)
    lease = _acquire(manager)
    stranger = _manager(tmp_path)

    with pytest.raises(LeaseOwnershipError):
        stranger.request_cancel(lease.lease_id, "foreign-generation")
    with pytest.raises(LeaseOwnershipError):
        stranger.release(
            lease.lease_id,
            "foreign-generation",
            proof={"runtime_status": "stopped", "evidence": {
                "runtime_instance": lease.runtime_instance, "exit_code": 0,
            }},
        )

    assert manager.current()["lease_id"] == lease.lease_id


def test_expired_lease_survives_restart_until_runtime_stop_is_proven(tmp_path):
    manager = _manager(tmp_path)
    lease = _acquire(manager, now=100.0)
    with pytest.raises(LeaseBusy):
        manager.reconcile(
            lease.lease_id,
            lease.generation_token,
            proof={"runtime_status": "stopped", "evidence": {
                "runtime_instance": lease.runtime_instance, "process_exit_code": 0,
            }},
            now=111.0,
        )
    lease._lock_fd.close()  # simulate a crashed manager; persisted lease remains
    restarted = _manager(tmp_path)

    with pytest.raises(LeaseBusy, match="reconcile"):
        _acquire(restarted, engine="llm", now=111.0)

    with pytest.raises(LeaseOwnershipError):
        restarted.reconcile(
            lease.lease_id,
            "wrong-generation",
            proof={"runtime_status": "stopped", "evidence": {
                "runtime_instance": lease.runtime_instance, "runtime": "gone",
            }},
            now=112.0,
        )

    with pytest.raises(ValueError, match="stopped"):
        restarted.reconcile(lease.lease_id, lease.generation_token, proof={"runtime_status": "unknown"})

    released = restarted.reconcile(
        lease.lease_id,
        lease.generation_token,
        proof={"runtime_status": "stopped", "evidence": {
            "runtime_instance": lease.runtime_instance, "comfy_prompt_history": "terminal-cancelled",
        }},
        now=112.0,
    )
    assert released["phase"] == "released"
    assert released["release_proof"]["runtime_status"] == "stopped"

    next_lease = _acquire(restarted, engine="llm", now=113.0)
    assert next_lease.resource_family == "llm"


def test_heartbeat_extends_expiry_and_stale_generation_cannot_release(tmp_path):
    manager = _manager(tmp_path)
    lease = _acquire(manager, now=100.0)
    refreshed = manager.heartbeat(lease.lease_id, lease.generation_token, ttl_seconds=20, now=105.0)

    assert refreshed["expires_at"] == 125.0
    with pytest.raises(LeaseOwnershipError):
        manager.release(
            lease.lease_id,
            "stale-generation",
            proof={"runtime_status": "stopped", "evidence": {
                "runtime_instance": lease.runtime_instance, "exit_code": 0,
            }},
        )
    assert manager.current()["phase"] == "starting"


def test_owner_marks_runtime_running_without_changing_its_generation(tmp_path):
    manager = _manager(tmp_path)
    lease = _acquire(manager, now=100.0)
    running = lease.mark_running(now=101.0)

    assert running["phase"] == "running"
    assert running["generation_token"] == lease.generation_token
    assert manager.current()["phase"] == "running"


def test_normal_release_requires_observed_runtime_stop(tmp_path):
    manager = _manager(tmp_path)
    lease = _acquire(manager)

    with pytest.raises(ValueError, match="evidence"):
        manager.release(lease.lease_id, lease.generation_token, proof={"runtime_status": "stopped", "evidence": {
            "runtime_instance": lease.runtime_instance,
        }})
    with pytest.raises(ValueError, match="runtime instance"):
        manager.release(lease.lease_id, lease.generation_token, proof={"runtime_status": "stopped", "evidence": {
            "runtime_instance": "foreign-runtime", "process_exit_code": 0,
        }})

    lease.request_cancel(now=101.0)
    assert manager.current()["phase"] == "cancel_requested"
    with pytest.raises(LeaseBusy):
        _acquire(_manager(tmp_path), engine="llm", now=102.0)

    released = lease.release(
        proof={"runtime_status": "stopped", "evidence": {
            "runtime_instance": lease.runtime_instance, "process_exit_code": 0,
        }}, now=102.0,
    )
    assert released["phase"] == "released"
    assert released["release_proof"]["evidence"]["process_exit_code"] == 0
    assert manager.current() is None


def test_host_gpu_restart_reconciles_only_its_recorded_runtime(tmp_path, monkeypatch):
    data = tmp_path / "data"
    manager = ResourceLeaseManager(data_dir=data)
    lease = manager.acquire(
        owner_task="host_gpu", owner_attempt="old-launch", runtime_instance="host-gpu:old-launch",
        resource_family="llm", claim={"gpu_layers": 99}, ttl_seconds=10, now=100,
    )
    state = {
        "launch_id": "old-launch", "phase": "ready", "lease_id": lease.lease_id,
        "lease_generation_token": lease.generation_token, "runtime_instance": lease.runtime_instance,
        "server_pid": 4312, "server_start": 98.5, "port": 19631,
    }
    lease._lock_fd.close()  # the supervisor process died; no runtime stop claim is implied
    killed: list[tuple[int, float, int]] = []
    monkeypatch.setattr("ttakkari.host_gpu.guard.kill_our_server",
                        lambda pid, start, port: killed.append((pid, start, port)))
    observations = iter([ProcState.PRESENT, ProcState.ABSENT])  # 신호 전에는 살아 있고, 신호 뒤 부재가 관찰된다
    seen: list[tuple[int, float]] = []
    monkeypatch.setattr("ttakkari.host_gpu.guard.process_state",
                        lambda pid, start: seen.append((pid, start)) or next(observations))
    gpu = HostGpu(SimpleNamespace(data_dir=data))

    reconciled = gpu._reap_lost(state)

    assert killed == [(4312, 98.5, 19631)]
    assert seen == [(4312, 98.5), (4312, 98.5)]
    assert reconciled["stop_reason"] == "supervisor_lost"
    assert manager.current() is None


@pytest.mark.parametrize("failure", ["submit_response_lost", "download_failed"])
def test_unreconciled_execution_failure_keeps_resource_leased(tmp_path, failure):
    manager = _manager(tmp_path)
    lease = _acquire(manager)
    # Failure to observe or collect output is not evidence that the runtime stopped.
    with pytest.raises(LeaseBusy):
        _acquire(_manager(tmp_path), engine="three")
    assert manager.current()["lease_id"] == lease.lease_id
    assert manager.current()["generation_token"] == lease.generation_token
