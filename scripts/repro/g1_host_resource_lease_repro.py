"""G1 host resource lease 반례 재현 스크립트(자체 소유 CPU child + psutil 주입, GPU·운영 프로세스 불필요).

검수자와 작성자가 서로의 outbox 를 읽지 못해 저장소에 둔다. 사용:
    PYTHONPATH=src python scripts/repro/g1_host_resource_lease_repro.py
각 시나리오는 `OK`(수정된 동작) 또는 `FAIL`(결함 재현)을 출력하고, 하나라도 FAIL 이면 종료 코드 1 이다.
  A 조회 실패(권한 거부)인 살아 있는 서버: lease 해제·후속 취득이 일어나면 fail-open(검수 1차 반례)
  B reconcile 실패 후 영구 교착(검수 2차 교착 1)
  C lease 획득과 state 쓰기 사이에서 죽은 감독자(검수 2차 교착 2)
  D 수동 복구(force-release)가 안전 확인 후에만 해제
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import psutil

from ttakkari import host_gpu
from ttakkari import host_gpu_guard as guard
from ttakkari.host_gpu import GpuError, HostGpu
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    RESULTS.append((name, ok))
    print(f"{'OK  ' if ok else 'FAIL'} {name}")


def successor_blocked(data: Path) -> bool:
    try:
        lease = ResourceLeaseManager(data_dir=data).acquire(
            owner_task="successor", owner_attempt="s", runtime_instance="successor:s",
            resource_family="comfy", claim={})
    except LeaseBusy:
        return True
    lease._lock_fd.close()
    return False


def dead_child() -> tuple[int, float]:
    child = subprocess.Popen(["true"])
    start = guard.proc_start(child.pid)
    child.wait()
    return child.pid, start


def live_lease(data: Path, owner: tuple[int, float] | None = None, drop_owner: bool = False) -> dict:
    manager = ResourceLeaseManager(data_dir=data)
    lease = manager.acquire(owner_task="host_gpu", owner_attempt="a", runtime_instance="host_gpu:a",
                            resource_family="llm", claim={})
    lease._lock_fd.close()  # 소유자 프로세스가 죽어 flock 만 풀린 상태
    record = json.loads(manager.record_path.read_text())
    if drop_owner:
        record.pop("owner_pid")
        record.pop("owner_start")
    else:
        pid, start = owner or dead_child()
        record.update(owner_pid=pid, owner_start=start)
    manager.record_path.write_text(json.dumps(record))
    return record


def write_state(data: Path, lease: dict, **over) -> None:
    host_gpu._write_state(host_gpu.state_dir(data), {
        "launch_id": "L", "phase": "ready", "lease_id": lease["lease_id"],
        "lease_generation_token": lease["generation_token"], "runtime_instance": lease["runtime_instance"],
        "supervisor_pid": 999999, "supervisor_start": time.time() - 5, "server_pid": None, "server_start": None,
        "port": 19901, "started_at": time.time(), "deadline_epoch": time.time() + 60,
        "baseline_swap_bytes": 0, "swap_limit_bytes": 1, **over})


def gpu_for(root: Path) -> tuple[Path, HostGpu]:
    data = root / "data"
    return data, HostGpu(SimpleNamespace(data_dir=data))


def try_status(gpu: HostGpu) -> bool:
    try:
        gpu.status()
    except (GpuError, OSError):  # 구 구현은 reconcile 실패 OSError 를 그대로 올린다
        return False
    return True


def scenario_a(root: Path) -> None:
    data, gpu = gpu_for(root / "a")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--alias", guard.ALIAS,
                              "--port", "19901"], start_new_session=True)
    real = psutil.Process
    try:
        lease = live_lease(data)
        write_state(data, lease, server_pid=child.pid, server_start=guard.proc_start(child.pid))

        def denied(pid=None):
            if pid == child.pid:
                raise psutil.AccessDenied(pid)
            return real(pid)

        psutil.Process = denied
        try:
            reaped = try_status(gpu)
        finally:
            psutil.Process = real
        check("A child_still_alive", child.poll() is None)
        check("A active_lease_kept", ResourceLeaseManager(data_dir=data).current() is not None)
        check("A successor_blocked", successor_blocked(data))
        check("A reap_refused", not reaped)
    finally:
        child.kill()
        child.wait()


def scenario_b(root: Path) -> None:
    data, gpu = gpu_for(root / "b")
    pid, start = dead_child()
    lease = live_lease(data)
    write_state(data, lease, server_pid=pid, server_start=start)
    real = ResourceLeaseManager.reconcile

    def failing(*_a, **_k):
        raise OSError("injected")

    ResourceLeaseManager.reconcile = failing
    try:
        try_status(gpu)
    finally:
        ResourceLeaseManager.reconcile = real
    manager = ResourceLeaseManager(data_dir=data)
    check("B state_stays_live_after_reconcile_failure",
          host_gpu._read_state(host_gpu.state_dir(data))["phase"] == "ready")
    check("B lease_stays_live", manager.current() is not None)
    try_status(gpu)
    check("B retry_recovers_no_permanent_deadlock", manager.current() is None and not successor_blocked(data))


def scenario_c(root: Path) -> None:
    data, gpu = gpu_for(root / "c")
    live_lease(data)  # state.json 없음: 획득과 state 쓰기 사이에서 죽은 감독자
    try_status(gpu)
    check("C orphan_lease_reclaimed_when_owner_absent", ResourceLeaseManager(data_dir=data).current() is None)

    data, gpu = gpu_for(root / "c2")
    owner = subprocess.Popen(["sleep", "60"])
    try:
        live_lease(data, owner=(owner.pid, guard.proc_start(owner.pid)))
        try_status(gpu)
        check("C orphan_lease_kept_when_owner_alive", ResourceLeaseManager(data_dir=data).current() is not None)
    finally:
        owner.kill()
        owner.wait()

    data, gpu = gpu_for(root / "c3")
    live_lease(data, drop_owner=True)
    try_status(gpu)
    check("C orphan_lease_kept_when_owner_identity_missing",
          ResourceLeaseManager(data_dir=data).current() is not None)


def scenario_d(root: Path) -> None:
    data, gpu = gpu_for(root / "d")
    host_gpu._write_state(host_gpu.state_dir(data), {
        "launch_id": "legacy", "phase": "ready", "supervisor_pid": 999999, "supervisor_start": time.time() - 5,
        "port": 19902, "started_at": time.time(), "deadline_epoch": time.time() + 60})
    check("D automatic_recovery_refuses_legacy_state", not try_status(gpu))
    code = host_gpu.main(["force-release", "--data-dir", str(data), "--reason",
                          "repro: verified no alias process via psutil scan", "--confirm-no-live-process"])
    check("D force_release_clears_legacy_state", code == 0 and not successor_blocked(data))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="g1-lease-repro-") as tmp:
        root = Path(tmp)
        for scenario in (scenario_a, scenario_b, scenario_c, scenario_d):
            scenario(root)
    failed = [name for name, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
