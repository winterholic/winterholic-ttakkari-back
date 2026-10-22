"""host_gpu lease 회수 교착 회귀 검사(독립 재검수 3913144 기준).

교착 1: state 를 stopped 로 먼저 쓰고 reconcile 이 실패하면 state 는 비활성, lease 는 live 라 영구 거부.
교착 2: lease 획득과 state.json 쓰기 사이에서 감독자가 죽으면 live lease 만 남아 회수 경로가 없음.
수동 복구: 자동 복구가 거부하는 상태를 운영자가 안전 확인 후에만 해제.
조회 실패는 psutil 주입과 자체 소유 CPU child 로만 검사한다(알려진 ps/sysctl/Metal 차단은 재시도하지 않는다).
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from ttakkari import host_gpu
from ttakkari import host_gpu_guard as guard
from ttakkari.host_gpu import GpuError, HostGpu, Supervisor
from ttakkari.host_gpu_guard import ALIAS
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager

SLEEP = "import time; time.sleep(120)"


@pytest.fixture
def children():
    spawned: list[subprocess.Popen] = []
    yield spawned
    for child in spawned:
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.fixture
def box(tmp_path):
    data = tmp_path / "data"
    return SimpleNamespace(tmp=tmp_path, data=data, dir=host_gpu.state_dir(data),
                           manager=ResourceLeaseManager(data_dir=data),
                           gpu=HostGpu(SimpleNamespace(data_dir=data)))


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _dead_pid() -> tuple[int, float]:
    child = subprocess.Popen(["true"])
    start = guard.proc_start(child.pid)
    child.wait()
    return child.pid, start


def _acquire(box, *, owner_task="host_gpu", owner: tuple | None = None, drop_owner=False):
    """감독자가 죽은 live lease. owner 신원은 기록 JSON 을 고쳐 죽은(또는 지정한) 프로세스로 둔다."""
    lease = box.manager.acquire(owner_task=owner_task, owner_attempt="a", runtime_instance=f"{owner_task}:a",
                                resource_family="llm", claim={"gpu_layers": 0}, ttl_seconds=10)
    lease._lock_fd.close()  # 소유 프로세스가 죽어 flock 만 풀렸다
    record = json.loads(box.manager.record_path.read_text())
    if drop_owner:
        record.pop("owner_pid")
        record.pop("owner_start")
    else:
        pid, start = owner or _dead_pid()
        record.update(owner_pid=pid, owner_start=start)
    box.manager.record_path.write_text(json.dumps(record))
    return record


def _state(box, lease, **over):
    state = {"launch_id": "L", "phase": "ready", "lease_id": lease["lease_id"],
             "lease_generation_token": lease["generation_token"], "runtime_instance": lease["runtime_instance"],
             "supervisor_pid": 999999, "supervisor_start": time.time() - 5, "server_pid": None,
             "server_start": None, "port": 19801, "started_at": time.time(), "deadline_epoch": time.time() + 60,
             "baseline_swap_bytes": 0, "swap_limit_bytes": 1, **over}
    host_gpu._write_state(box.dir, state)
    return state


def _alias_child(port, script: Path | None = None):
    argv = [sys.executable, "-c", SLEEP] if script is None else [sys.executable, str(script)]
    return subprocess.Popen([*argv, "--alias", ALIAS, "--port", str(port)], start_new_session=True)


def _successor(box):
    return ResourceLeaseManager(data_dir=box.data).acquire(
        owner_task="successor", owner_attempt="s", runtime_instance="successor:s", resource_family="comfy", claim={})


def _blocked(box):
    with pytest.raises(LeaseBusy):
        _successor(box)


# ---- 교착 1: lease 회수가 성공한 뒤에만 stopped 를 확정한다

def test_reconcile_failure_keeps_state_live_and_a_later_attempt_recovers(box, monkeypatch):
    dead_pid, dead_start = _dead_pid()
    lease = _acquire(box)
    state = _state(box, lease, server_pid=dead_pid, server_start=dead_start)
    real = ResourceLeaseManager.reconcile
    monkeypatch.setattr(ResourceLeaseManager, "reconcile",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(GpuError, match="state stays live"):
        box.gpu._current()

    assert host_gpu._read_state(box.dir)["phase"] == "ready"  # stopped 로 먼저 쓰지 않는다
    assert box.manager.current()["lease_id"] == lease["lease_id"]
    _blocked(box)
    monkeypatch.setattr(ResourceLeaseManager, "reconcile", real)

    recovered = box.gpu._current()  # 같은 경로가 다시 시도된다

    assert recovered["phase"] == "stopped" and recovered["stop_reason"] == "supervisor_lost"
    assert box.manager.current() is None
    _successor(box)._lock_fd.close()
    assert state["lease_id"] == lease["lease_id"]


def test_state_write_failure_after_reconcile_is_finished_by_the_next_attempt(box, monkeypatch):
    dead_pid, dead_start = _dead_pid()
    lease = _acquire(box)
    _state(box, lease, server_pid=dead_pid, server_start=dead_start)
    real = host_gpu._write_state
    monkeypatch.setattr(host_gpu, "_write_state", lambda *a, **k: (_ for _ in ()).throw(OSError("EIO")))

    with pytest.raises(OSError, match="EIO"):
        box.gpu._current()

    assert box.manager.current() is None  # lease 는 이미 회수됐다
    monkeypatch.setattr(host_gpu, "_write_state", real)
    assert host_gpu._read_state(box.dir)["phase"] == "ready"

    recovered = box.gpu._current()  # released 로 확인되므로 state 만 확정한다

    assert recovered["phase"] == "stopped"
    _successor(box)._lock_fd.close()


def test_a_lease_that_no_longer_matches_is_not_reclaimed_by_a_stale_state(box):
    dead_pid, dead_start = _dead_pid()
    lease = _acquire(box)
    _state(box, lease, server_pid=dead_pid, server_start=dead_start,
           lease_generation_token="stale-generation")

    with pytest.raises(GpuError, match="no longer matches"):
        box.gpu._current()

    assert box.manager.current()["lease_id"] == lease["lease_id"]


# ---- 교착 2: lease 획득과 state 쓰기 사이에서 죽은 감독자

def test_orphan_lease_without_any_state_is_reclaimed_when_owner_is_confirmed_absent(box):
    lease = _acquire(box)

    box.gpu.status()

    assert box.manager.current() is None
    proof = json.loads(box.manager.record_path.read_text())["release_proof"]["evidence"]
    assert proof["owner_process_state"] == "absent" and proof["server_started"] is False
    assert proof["orphan_lease"] is True and lease["lease_id"]
    _successor(box)._lock_fd.close()


def test_orphan_lease_with_another_launchs_stopped_state_is_reclaimed(box):
    old = {"launch_id": "old", "phase": "stopped", "lease_id": "older-lease", "server_pid": 1, "started_at": 1.0}
    box.dir.mkdir(parents=True, exist_ok=True)
    host_gpu._write_state(box.dir, old)
    _acquire(box)

    box.gpu.status()

    assert box.manager.current() is None


@pytest.mark.parametrize("error", [psutil.AccessDenied(1), OSError("ps blocked"), psutil.Error("boom")])
def test_orphan_lease_owner_lookup_failure_keeps_the_lease(box, monkeypatch, error):
    pid, start = _dead_pid()
    lease = _acquire(box, owner=(pid, start))
    real = psutil.Process

    def lookup(target=None):
        if target == pid:
            raise error
        return real(target)

    monkeypatch.setattr(psutil, "Process", lookup)

    with pytest.raises(GpuError, match="not confirmed"):
        box.gpu.status()

    assert box.manager.current()["lease_id"] == lease["lease_id"]
    _blocked(box)


def test_orphan_lease_without_owner_identity_is_preserved(box):
    lease = _acquire(box, drop_owner=True)
    with pytest.raises(GpuError, match="not confirmed"):
        box.gpu.status()
    assert box.manager.current()["lease_id"] == lease["lease_id"]
    _blocked(box)


def test_orphan_lease_whose_owner_process_is_still_alive_is_preserved(box, children):
    owner = subprocess.Popen(["sleep", "120"])
    children.append(owner)
    lease = _acquire(box, owner=(owner.pid, guard.proc_start(owner.pid)))

    with pytest.raises(GpuError, match="observed present"):
        box.gpu.status()

    assert owner.poll() is None and box.manager.current()["lease_id"] == lease["lease_id"]


def test_orphan_lease_owner_pid_reuse_is_identity_mismatch_and_leaves_that_process_alone(box, children):
    unrelated = subprocess.Popen(["sleep", "120"])
    children.append(unrelated)
    _acquire(box, owner=(unrelated.pid, guard.proc_start(unrelated.pid) - 3600))

    box.gpu.status()

    assert box.manager.current() is None and unrelated.poll() is None


def test_state_stopped_but_lease_live_is_recovered_only_when_the_server_is_absent(box, children):
    lease = _acquire(box)
    child = _alias_child(19802)
    children.append(child)
    _state(box, lease, phase="stopped", server_pid=child.pid, server_start=guard.proc_start(child.pid),
           port=19802)

    with pytest.raises(GpuError, match="server stop is not confirmed"):
        box.gpu.status()
    assert box.manager.current() is not None

    child.kill()
    child.wait()
    box.gpu.status()
    assert box.manager.current() is None


def test_a_different_consumers_lease_is_never_reclaimed_by_host_gpu(box):
    lease = _acquire(box, owner_task="comfy")

    box.gpu.status()

    assert box.manager.current()["lease_id"] == lease["lease_id"]
    _blocked(box)


def test_live_owner_holding_the_lock_is_left_alone(box):
    box.dir.mkdir(parents=True, exist_ok=True)
    lease = box.manager.acquire(owner_task="host_gpu", owner_attempt="a", runtime_instance="host_gpu:a",
                                resource_family="llm", claim={})
    try:
        box.gpu.status()
        assert box.manager.current()["lease_id"] == lease.lease_id
    finally:
        lease._lock_fd.close()


# ---- 서버 pid 를 적기 전에 죽은 감독자(server_spawn == pending)

def _pending_state(box, lease, script: Path, port: int, **over):
    fields = {"phase": "starting", "server_spawn": "pending", "port": port,
              "command": [str(script), "--port", str(port)], "supervisor_start": time.time() - 30, **over}
    return _state(box, lease, **fields)


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "fake-llama"
    path.write_text("import time\ntime.sleep(120)\n")
    return path


def test_pending_spawn_with_a_live_server_is_stopped_then_reclaimed(box, children, script):
    lease = _acquire(box)
    port = 19811
    child = _alias_child(port, script)
    children.append(child)
    assert _wait(lambda: host_gpu.find_our_servers_strict(str(script), port, time.time() - 60) == [
        (child.pid, guard.proc_start(child.pid))])
    _pending_state(box, lease, script, port)

    recovered = box.gpu._current()

    assert recovered["phase"] == "stopped"
    assert child.poll() is not None
    assert box.manager.current() is None


def test_pending_spawn_without_any_server_is_reclaimed(box, script):
    lease = _acquire(box)
    _pending_state(box, lease, script, 19812)
    assert box.gpu._current()["phase"] == "stopped"
    assert box.manager.current() is None


def test_pending_spawn_with_incomplete_process_enumeration_keeps_the_lease(box, monkeypatch, script):
    lease = _acquire(box)
    _pending_state(box, lease, script, 19813)
    unreadable = SimpleNamespace(info={"pid": 4321, "cmdline": None, "create_time": time.time(),
                                       "uids": SimpleNamespace(real=__import__("os").getuid()), "status": "running"})
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: iter([unreadable]))

    with pytest.raises(GpuError, match="observed unknown"):
        box.gpu._current()

    assert host_gpu._read_state(box.dir)["phase"] == "starting"
    assert box.manager.current()["lease_id"] == lease["lease_id"]


def test_pending_spawn_with_missing_command_evidence_keeps_the_lease(box, script):
    lease = _acquire(box)
    _pending_state(box, lease, script, 19814, command=None)
    with pytest.raises(GpuError, match="observed unknown"):
        box.gpu._current()
    assert box.manager.current() is not None


def test_strict_enumeration_skips_zombies_and_other_users_and_flags_unreadable_same_user_processes(monkeypatch):
    me = SimpleNamespace(real=__import__("os").getuid())
    other = SimpleNamespace(real=me.real + 1)

    def proc(**info):
        return SimpleNamespace(info={"pid": 1, "cmdline": ["x"], "create_time": 100.0, "uids": me,
                                     "status": "running", **info})

    ours = proc(pid=7, cmdline=["/srv/llama", "--alias", ALIAS, "--port", "9"])
    cases = [
        ([ours, proc(uids=other, cmdline=None), proc(status=psutil.STATUS_ZOMBIE, cmdline=None)], [(7, 100.0)]),
        ([ours, proc(cmdline=None)], None),
        ([ours, proc(create_time=None)], None),
        ([ours, proc(status=None)], None),
        ([ours, proc(uids=None, cmdline=None)], None),
    ]
    for procs, expected in cases:
        monkeypatch.setattr(psutil, "process_iter", lambda *_a, _p=procs, **_k: iter(_p))
        assert host_gpu.find_our_servers_strict("/srv/llama", 9, 0.0) == expected

    # 서버가 뜰 수 있었던 시각보다 먼저 시작한 프로세스는 argv 를 읽지 못해도 후보가 아니다. 이후에 시작했다면 불완전이다.
    protected = [proc(pid=8, cmdline=None, create_time=100.0)]
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: iter(protected))
    assert host_gpu.find_our_servers_strict("/srv/llama", 9, 1000.0) == []
    assert host_gpu.find_our_servers_strict("/srv/llama", 9, 50.0) is None
    assert host_gpu.find_alias_processes_strict(None) is None

    def boom(*_a, **_k):
        raise psutil.Error("enumeration failed")

    monkeypatch.setattr(psutil, "process_iter", boom)
    assert host_gpu.find_our_servers_strict("/srv/llama", 9, 0.0) is None


# ---- 획득 시점 신원 기록과 감독자의 state-먼저 순서

def test_lease_records_the_owner_process_identity_at_acquisition(box):
    lease = box.manager.acquire(owner_task="x", owner_attempt="a", runtime_instance="x:a",
                                resource_family="llm", claim={})
    record = box.manager.current()
    assert record["owner_pid"] == __import__("os").getpid()
    assert guard.same_process(record["owner_pid"], record["owner_start"])
    lease._lock_fd.close()


def test_supervisor_writes_a_durable_state_before_it_spawns_the_server(tmp_path, monkeypatch):
    server = tmp_path / "llama-server"
    server.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    server.chmod(0o755)
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    directory = host_gpu.state_dir(tmp_path / "data")
    spec = {"launch_id": "order", "request": {"model": "tiny", "gpu_layers": 0, "device": "none"},
            "state_dir": str(directory), "server_path": str(server), "model_path": str(model), "max_seconds": 60,
            "start_timeout": 30, "swap_limit_bytes": 1 << 30, "port_min": 19820, "port_max": 19850,
            "reserved_ports": []}
    seen: list[dict] = []
    real = subprocess.Popen

    def spying_popen(cmd, *a, **k):
        if str(cmd[0]) == str(server):
            seen.append(host_gpu._read_state(directory))
        return real(cmd, *a, **k)

    monkeypatch.setattr(host_gpu.subprocess, "Popen", spying_popen)
    sup = Supervisor(spec, poll=0.1, health=lambda _p: False)
    sup.max_seconds = 0.5
    sup.run()

    before = seen[0]
    assert before["server_spawn"] == "pending" and before["server_pid"] is None
    assert before["lease_id"] and before["phase"] == "starting"
    assert host_gpu._read_state(directory)["phase"] == "stopped"


# ---- 수동 복구: force-release

def _legacy_live_state(box, **over):
    state = {"launch_id": "legacy", "phase": "ready", "supervisor_pid": 999999, "supervisor_start": time.time() - 5,
             "server_pid": None, "server_start": None, "port": 19831, "started_at": time.time(),
             "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0, "swap_limit_bytes": 1, **over}
    host_gpu._write_state(box.dir, state)
    return state


REASON = "checked ps: no llama-server for alias ttakkari-gpu-test"


def test_automatic_recovery_refuses_legacy_state_and_force_release_clears_it_after_verification(box):
    _legacy_live_state(box)
    with pytest.raises(GpuError, match="lease owner evidence is missing"):
        box.gpu.status()

    result = box.gpu.force_release(reason=REASON)

    assert result["operator_override"] is True and result["released_lease"] is None
    assert host_gpu._read_state(box.dir)["stop_reason"] == "operator_force_release"
    box.gpu.status()
    _successor(box)._lock_fd.close()


def test_force_release_clears_a_live_lease_whose_owner_evidence_is_missing(box):
    lease = _acquire(box, drop_owner=True)
    _state(box, lease)  # 서버 pid·start 근거 없음
    with pytest.raises(GpuError):
        box.gpu.status()

    with pytest.raises(GpuError, match="--lease-id must equal"):
        box.gpu.force_release(reason=REASON)
    with pytest.raises(GpuError, match="--lease-id must equal"):
        box.gpu.force_release(reason=REASON, lease_id="someone-elses-lease")
    assert box.manager.current() is not None

    result = box.gpu.force_release(reason=REASON, lease_id=lease["lease_id"])

    assert result["released_lease"] == lease["lease_id"] and box.manager.current() is None
    proof = json.loads(box.manager.record_path.read_text())["release_proof"]["evidence"]
    assert proof["operator_override"] and proof["reason"] == REASON and proof["alias_processes"] == 0
    assert host_gpu._read_state(box.dir)["phase"] == "stopped"
    _successor(box)._lock_fd.close()


def test_force_release_refuses_while_any_test_server_process_is_alive_and_never_signals(box, children):
    lease = _acquire(box, drop_owner=True)
    child = _alias_child(19832)
    children.append(child)

    with pytest.raises(GpuError, match=str(child.pid)):
        box.gpu.force_release(reason=REASON, lease_id=lease["lease_id"])

    assert child.poll() is None and box.manager.current() is not None


def test_force_release_refuses_when_the_process_table_cannot_be_enumerated(box, monkeypatch):
    lease = _acquire(box, drop_owner=True)
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: (_ for _ in ()).throw(psutil.Error("blocked")))
    with pytest.raises(GpuError, match="enumerated completely"):
        box.gpu.force_release(reason=REASON, lease_id=lease["lease_id"])
    assert box.manager.current() is not None


def test_force_release_refuses_while_a_live_owner_holds_the_lock(box):
    box.dir.mkdir(parents=True, exist_ok=True)
    holder = (box.dir / "lock").open("a+")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        _legacy_live_state(box)
        with pytest.raises(GpuError, match="holds the shared host resource lock"):
            box.gpu.force_release(reason=REASON)
        assert host_gpu._read_state(box.dir)["phase"] == "ready"
    finally:
        holder.close()


def test_force_release_requires_a_real_reason_and_something_to_release(box):
    with pytest.raises(GpuError, match="reason"):
        box.gpu.force_release(reason="ok")
    with pytest.raises(GpuError, match="nothing to release"):
        box.gpu.force_release(reason=REASON)


def test_recorded_pid_of_an_unrelated_live_process_needs_explicit_acceptance(box, children):
    unrelated = subprocess.Popen(["sleep", "120"], start_new_session=True)
    children.append(unrelated)
    lease = _acquire(box)
    _state(box, lease, server_pid=unrelated.pid, server_start=guard.proc_start(unrelated.pid))
    with pytest.raises(GpuError, match="not confirmed"):
        box.gpu.status()  # 자동 복구는 거부

    with pytest.raises(GpuError, match="still exists"):
        box.gpu.force_release(reason=REASON, lease_id=lease["lease_id"])
    assert box.manager.current() is not None

    result = box.gpu.force_release(reason=REASON, lease_id=lease["lease_id"], accept_unrelated_pid=True)

    assert result["unrelated_pid_accepted"][0]["pid"] == unrelated.pid
    assert unrelated.poll() is None  # 신호를 보내지 않는다
    assert box.manager.current() is None


def test_recorded_supervisor_or_owner_that_is_still_alive_cannot_be_forced_even_with_acceptance(box, children):
    owner = subprocess.Popen(["sleep", "120"])
    children.append(owner)
    lease = _acquire(box, owner=(owner.pid, guard.proc_start(owner.pid)))
    with pytest.raises(GpuError, match="lease_owner pid"):
        box.gpu.force_release(reason=REASON, lease_id=lease["lease_id"], accept_unrelated_pid=True)
    assert box.manager.current() is not None


def test_cli_force_release_requires_confirmation_and_reports_refusals(box, capsys):
    _legacy_live_state(box)
    base = ["force-release", "--data-dir", str(box.data), "--reason", REASON]
    with pytest.raises(SystemExit):
        host_gpu.main(base)  # --confirm-no-live-process 없이는 실행되지 않는다
    assert host_gpu._read_state(box.dir)["phase"] == "ready"

    assert host_gpu.main([*base, "--reason", "no", "--confirm-no-live-process"]) == 2
    assert "refused" in capsys.readouterr().err

    assert host_gpu.main([*base, "--confirm-no-live-process"]) == 0
    assert json.loads(capsys.readouterr().out)["operator_override"] is True
    assert host_gpu._read_state(box.dir)["phase"] == "stopped"
