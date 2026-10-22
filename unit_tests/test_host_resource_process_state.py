"""프로세스 상태 3분류(존재/확인된 부재/unknown)와 lease 보존 회귀 검사.

권한 거부·조회 실패는 부재 증거가 아니다. 실제 자체 소유 CPU child 를 쓰고, 조회 실패는 psutil 주입으로 흉내 낸다
(알려진 ps/sysctl/Metal sandbox 차단은 재시도하지 않고 fake 로만 검사한다).
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from ttakkari import host_gpu
from ttakkari import host_gpu_guard as guard
from ttakkari.host_gpu import GpuError, HostGpu
from ttakkari.host_gpu_guard import ALIAS, ProcState
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager

SLEEP_CODE = "import time; time.sleep(120)"


def _alias_child(port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", SLEEP_CODE, "--alias", ALIAS, "--port", str(port)],
                            start_new_session=True)


def _inject_lookup_failure(monkeypatch, pid: int, error: BaseException) -> None:
    real = psutil.Process

    def lookup(target=None):
        if target == pid:
            raise error
        return real(target)

    monkeypatch.setattr(psutil, "Process", lookup)


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def children():
    spawned: list[subprocess.Popen] = []
    yield spawned
    for child in spawned:
        if child.poll() is None:
            child.kill()
        child.wait()


# ---- process_state 자체

def test_live_child_with_matching_start_is_present(children):
    child = _alias_child(19701)
    children.append(child)
    assert guard.process_state(child.pid, guard.proc_start(child.pid)) is ProcState.PRESENT


def test_reaped_pid_is_absent_even_when_start_evidence_is_missing(children):
    child = subprocess.Popen(["true"])
    start = guard.proc_start(child.pid)
    child.wait()
    assert guard.process_state(child.pid, start) is ProcState.ABSENT
    assert guard.process_state(child.pid, None) is ProcState.ABSENT


def test_terminated_but_unreaped_child_is_absent(children):
    child = _alias_child(19702)
    children.append(child)
    start = guard.proc_start(child.pid)
    child.kill()
    assert _wait(lambda: psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE)
    assert guard.process_state(child.pid, start) is ProcState.ABSENT


def test_reused_pid_with_a_different_start_time_is_absent_for_the_recorded_process(children):
    other = _alias_child(19703)
    children.append(other)
    recorded_start = guard.proc_start(other.pid) - 3600  # 상태 파일의 프로세스는 한 시간 전에 시작했다
    assert guard.process_state(other.pid, recorded_start) is ProcState.ABSENT


def test_live_process_without_start_evidence_is_unknown_not_absent(children):
    child = _alias_child(19704)
    children.append(child)
    assert guard.process_state(child.pid, None) is ProcState.UNKNOWN
    assert guard.process_state(child.pid, "not-a-number") is ProcState.UNKNOWN


@pytest.mark.parametrize("pid", [None, 0, -1, True, "123"])
def test_missing_pid_evidence_is_unknown(pid):
    assert guard.process_state(pid, 1.0) is ProcState.UNKNOWN


@pytest.mark.parametrize("error", [
    psutil.AccessDenied(4242), OSError("lookup blocked"), psutil.TimeoutExpired(1.0, 4242), psutil.Error("boom"),
])
def test_lookup_failures_are_unknown(monkeypatch, error):
    _inject_lookup_failure(monkeypatch, 4242, error)
    assert guard.process_state(4242, 100.0) is ProcState.UNKNOWN


@pytest.mark.parametrize("error", [psutil.NoSuchProcess(4242), psutil.ZombieProcess(4242)])
def test_no_such_process_is_confirmed_absent(monkeypatch, error):
    _inject_lookup_failure(monkeypatch, 4242, error)
    assert guard.process_state(4242, 100.0) is ProcState.ABSENT


def test_status_lookup_denied_after_create_time_is_unknown(monkeypatch, children):
    child = _alias_child(19705)
    children.append(child)
    start = guard.proc_start(child.pid)
    real = psutil.Process

    class Denied:
        def __init__(self, pid):
            self._real = real(pid)

        def create_time(self):
            return self._real.create_time()

        def status(self):
            raise psutil.AccessDenied(self._real.pid)

    monkeypatch.setattr(psutil, "Process", Denied)
    assert guard.process_state(child.pid, start) is ProcState.UNKNOWN


def test_terminate_group_does_not_claim_a_stop_it_cannot_observe(monkeypatch, children):
    child = _alias_child(19706)
    children.append(child)
    _inject_lookup_failure(monkeypatch, child.pid, psutil.AccessDenied(child.pid))
    assert guard.terminate_group(child.pid, grace=0.2, kill_wait=0.3) is False


def test_terminate_group_confirms_a_real_stop(children):
    child = _alias_child(19707)
    children.append(child)
    assert guard.terminate_group(child.pid, grace=5, kill_wait=3) is True
    assert child.poll() is not None or psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE


# ---- HostGpu._reap_lost: lease 는 부재가 확인될 때만 풀린다

def _lost_runtime(tmp_path: Path, *, server_pid, server_start, port: int):
    data = tmp_path / "data"
    manager = ResourceLeaseManager(data_dir=data)
    lease = manager.acquire(owner_task="host_gpu", owner_attempt="lost", runtime_instance="host-gpu:lost",
                            resource_family="llm", claim={"gpu_layers": 0}, ttl_seconds=10)
    state = {"launch_id": "lost", "phase": "ready", "lease_id": lease.lease_id,
             "lease_generation_token": lease.generation_token, "runtime_instance": lease.runtime_instance,
             "supervisor_pid": 999999, "supervisor_start": 1.0, "server_pid": server_pid,
             "server_start": server_start, "port": port, "started_at": time.time(),
             "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0, "swap_limit_bytes": 1}
    lease._lock_fd.close()  # 감독자 프로세스가 죽었다. 이것만으로는 런타임이 멈췄다는 증거가 아니다.
    directory = host_gpu.state_dir(data)
    host_gpu._write_state(directory, state)
    return SimpleNamespace(data=data, manager=manager, state=state, dir=directory,
                           gpu=HostGpu(SimpleNamespace(data_dir=data)))


def _assert_lease_and_state_preserved(runtime) -> None:
    assert runtime.manager.current()["lease_id"] == runtime.state["lease_id"]
    assert host_gpu._read_state(runtime.dir)["phase"] == "ready"
    with pytest.raises(LeaseBusy):
        ResourceLeaseManager(data_dir=runtime.data).acquire(
            owner_task="successor", owner_attempt="a", runtime_instance="successor-runtime",
            resource_family="comfy", claim={})
    with pytest.raises(GpuError):
        runtime.gpu.status()  # 같은 오판이 status/start 쪽 reconcile 경로에 남지 않는다


@pytest.mark.parametrize("error", [psutil.AccessDenied(1), OSError("ps blocked"), psutil.Error("boom")])
def test_lookup_failure_with_a_live_server_keeps_lease_and_blocks_successor(tmp_path, monkeypatch, children, error):
    child = _alias_child(19711)
    children.append(child)
    runtime = _lost_runtime(tmp_path, server_pid=child.pid, server_start=guard.proc_start(child.pid), port=19711)
    _inject_lookup_failure(monkeypatch, child.pid, error)

    with pytest.raises(GpuError, match="not confirmed"):
        runtime.gpu._reap_lost(runtime.state)

    assert child.poll() is None  # 신원을 확인하지 못했으므로 신호도 보내지 않는다
    _assert_lease_and_state_preserved(runtime)


def test_missing_server_start_evidence_keeps_lease_and_does_not_signal(tmp_path, children):
    child = _alias_child(19712)
    children.append(child)
    runtime = _lost_runtime(tmp_path, server_pid=child.pid, server_start=None, port=19712)

    with pytest.raises(GpuError, match="not confirmed"):
        runtime.gpu._reap_lost(runtime.state)

    assert child.poll() is None
    _assert_lease_and_state_preserved(runtime)


@pytest.mark.parametrize("pid", [None, 0])
def test_missing_server_pid_evidence_keeps_lease(tmp_path, pid):
    runtime = _lost_runtime(tmp_path, server_pid=pid, server_start=1.0, port=19713)
    with pytest.raises(GpuError, match="not confirmed"):
        runtime.gpu._reap_lost(runtime.state)
    _assert_lease_and_state_preserved(runtime)


def test_present_process_that_is_not_our_server_is_never_signalled_and_keeps_lease(tmp_path, children):
    bystander = subprocess.Popen(["sleep", "120"], start_new_session=True)
    children.append(bystander)
    runtime = _lost_runtime(tmp_path, server_pid=bystander.pid, server_start=guard.proc_start(bystander.pid),
                            port=19714)

    with pytest.raises(GpuError, match="not confirmed"):
        runtime.gpu._reap_lost(runtime.state)

    assert bystander.poll() is None
    _assert_lease_and_state_preserved(runtime)


def test_unknown_then_confirmed_absence_reconciles_on_a_later_attempt(tmp_path, monkeypatch, children):
    child = _alias_child(19715)
    children.append(child)
    runtime = _lost_runtime(tmp_path, server_pid=child.pid, server_start=guard.proc_start(child.pid), port=19715)
    with monkeypatch.context() as patch:
        _inject_lookup_failure(patch, child.pid, psutil.AccessDenied(child.pid))
        with pytest.raises(GpuError):
            runtime.gpu._reap_lost(runtime.state)
    assert runtime.manager.current() is not None

    reconciled = runtime.gpu._reap_lost(runtime.state)  # 조회가 다시 되고 우리 서버가 확인되어 정리된다

    assert reconciled["stop_reason"] == "supervisor_lost"
    assert _wait(lambda: child.poll() is not None)
    assert runtime.manager.current() is None


def test_own_live_cpu_child_is_stopped_then_lease_released_and_successor_may_acquire(tmp_path, children):
    child = _alias_child(19716)
    children.append(child)
    runtime = _lost_runtime(tmp_path, server_pid=child.pid, server_start=guard.proc_start(child.pid), port=19716)

    reconciled = runtime.gpu._reap_lost(runtime.state)

    assert reconciled["phase"] == "stopped"
    assert child.poll() is not None
    assert runtime.manager.current() is None
    proof = runtime.manager.record_path.read_text()
    assert "supervisor_lost" in proof and str(child.pid) in proof
    successor = ResourceLeaseManager(data_dir=runtime.data).acquire(
        owner_task="successor", owner_attempt="a", runtime_instance="successor-runtime",
        resource_family="comfy", claim={})
    successor._lock_fd.close()


def test_server_that_exited_normally_before_reaping_releases_the_lease(tmp_path):
    child = subprocess.Popen(["true"])
    start = guard.proc_start(child.pid)
    child.wait()  # 정상 종료 후 회수까지 끝난 상태
    runtime = _lost_runtime(tmp_path, server_pid=child.pid, server_start=start, port=19717)

    reconciled = runtime.gpu._reap_lost(runtime.state)

    assert reconciled["phase"] == "stopped" and runtime.manager.current() is None


def test_pid_reused_by_an_unrelated_process_is_identity_mismatch_and_is_left_alone(tmp_path, children):
    unrelated = subprocess.Popen(["sleep", "120"], start_new_session=True)
    children.append(unrelated)
    recorded_start = guard.proc_start(unrelated.pid) - 3600
    runtime = _lost_runtime(tmp_path, server_pid=unrelated.pid, server_start=recorded_start, port=19718)

    reconciled = runtime.gpu._reap_lost(runtime.state)

    assert reconciled["phase"] == "stopped" and runtime.manager.current() is None
    assert unrelated.poll() is None  # 같은 pid 를 쓰는 다른 프로세스는 건드리지 않는다


def test_no_such_process_reconciles(tmp_path, monkeypatch):
    runtime = _lost_runtime(tmp_path, server_pid=4242, server_start=100.0, port=19719)
    _inject_lookup_failure(monkeypatch, 4242, psutil.NoSuchProcess(4242))

    assert runtime.gpu._reap_lost(runtime.state)["phase"] == "stopped"
    assert runtime.manager.current() is None


# ---- watchdog: 조회 실패한 서버를 "이미 종료"로 보고 감시를 접지 않는다

def test_watchdog_keeps_guarding_a_server_whose_lookup_is_unknown(tmp_path, monkeypatch, children):
    script = tmp_path / "fake-llama"
    script.write_text(f"import time\ntime.sleep(120)\n# {ALIAS}\n")
    port = 19721
    supervisor = subprocess.Popen(["sleep", "120"], start_new_session=True)
    children.append(supervisor)
    server = subprocess.Popen([sys.executable, str(script), "--alias", ALIAS, "--port", str(port)],
                              start_new_session=True)
    children.append(server)
    supervisor_start = guard.proc_start(supervisor.pid)
    assert _wait(lambda: guard.find_our_servers(str(script), port, supervisor_start - 1) == [server.pid])
    result: list[list[int]] = []
    watcher = threading.Thread(target=lambda: result.append(guard.watch(
        supervisor_pid=supervisor.pid, supervisor_start=supervisor_start, server_path=str(script), port=port,
        poll=0.05, grace=1.0)), daemon=True)
    watcher.start()
    time.sleep(0.6)  # watchdog 이 서버를 보고 신원을 기록한 뒤에 조회가 막힌다
    assert watcher.is_alive()
    _inject_lookup_failure(monkeypatch, server.pid, psutil.AccessDenied(server.pid))

    time.sleep(1.0)
    assert watcher.is_alive(), "unknown lookup must not be treated as 'server already exited'"

    supervisor.kill()
    watcher.join(timeout=10)
    assert not watcher.is_alive()
    assert result == [[]]  # 신원을 확인하지 못해 신호는 보내지 않았다
    assert server.poll() is None
