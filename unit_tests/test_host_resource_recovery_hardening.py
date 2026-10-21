"""0704b82 독립 재검수 결함 4건 회귀 검사.

1. setuid login 처럼 argv 조회가 거부되는 프로세스 때문에 엄격 열거가 영구 불완전이 되던 문제
2. force-release 가 다른 소비자(comfy) lease 를 별칭 검사만으로 해제하던 문제
3. state 가 없거나 불일치하는 고아 lease 를 열거 없이 회수하던 문제
4. 시계가 뒤로 감기면 pending 서버를 놓치던 문제
자체 소유 CPU child, 가짜 process_iter, psutil 주입만 쓴다(운영 프로세스·GPU 불사용, 알려진 sandbox 차단 재시도 없음).
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from ttakkari import host_gpu, managed_comfy
from ttakkari import host_gpu_guard as guard
from ttakkari.host_gpu import GpuError, HostGpu
from ttakkari.host_gpu_guard import ALIAS
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager

REASON = "checked the process table and the Comfy port by hand"
ME = SimpleNamespace(real=os.getuid())


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


def _dead() -> tuple[int, float]:
    child = subprocess.Popen(["true"])
    start = guard.proc_start(child.pid)
    child.wait()
    return child.pid, start


def _orphan(box, *, task="host_gpu", family="llm", instance=None, created_ago=0.0, drop_owner=False) -> dict:
    lease = box.manager.acquire(owner_task=task, owner_attempt="a", runtime_instance=instance or f"{task}:a",
                                resource_family=family, claim={})
    lease._lock_fd.close()
    record = json.loads(box.manager.record_path.read_text())
    pid, start = _dead()
    record.update(owner_pid=pid, owner_start=start, created_at=record["created_at"] - created_ago)
    if drop_owner:
        record.pop("owner_pid")
        record.pop("owner_start")
    box.manager.record_path.write_text(json.dumps(record))
    return record


def _alias_child(script: Path, port: int) -> subprocess.Popen:
    script.write_text("import time\ntime.sleep(120)\n")
    return subprocess.Popen([sys.executable, str(script), "--alias", ALIAS, "--port", str(port)],
                            start_new_session=True)


def _setuid_file(path: Path, mode: int = 0o4755) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


def _blocked(*, created: float, exe: object, pid: int = 999998) -> SimpleNamespace:
    return SimpleNamespace(info={"pid": pid, "cmdline": None, "create_time": created, "uids": ME,
                                 "status": psutil.STATUS_RUNNING, "exe": exe})


def _with_blocked(monkeypatch, *blocked):
    real = psutil.process_iter

    def fake(attrs=None, ad_value=None):
        yield from real(attrs, ad_value)
        yield from blocked

    monkeypatch.setattr(psutil, "process_iter", fake)


# ---- 1. setuid 바이너리처럼 우리 서버일 수 없는 argv 거부 프로세스는 제외, exe 도 못 읽으면 불완전

def test_setuid_foreign_binary_rule(tmp_path, monkeypatch):
    login = _setuid_file(tmp_path / "login")
    plain = _setuid_file(tmp_path / "plain", 0o755)
    assert host_gpu._setuid_foreign_binary(str(login), None) is True
    assert host_gpu._setuid_foreign_binary(str(login), os.path.realpath(tmp_path / "other-server")) is True
    assert host_gpu._setuid_foreign_binary(str(login), os.path.realpath(login)) is False  # 우리 서버 파일이면 제외하지 않는다
    assert host_gpu._setuid_foreign_binary(str(plain), None) is False
    with monkeypatch.context() as patch:  # 파일시스템이 setgid 를 지워 버릴 수 있어 mode 만 주입한다
        patch.setattr(host_gpu.os, "stat", lambda _p: SimpleNamespace(st_mode=stat.S_IFREG | 0o2755))
        assert host_gpu._setuid_foreign_binary("/usr/bin/some-setgid", None) is True
    for bad in (None, "", str(tmp_path / "missing"), str(tmp_path), 42):
        assert host_gpu._setuid_foreign_binary(bad, None) is False


def test_strict_enumeration_skips_unreadable_setuid_binaries_but_not_other_unreadable_processes(
        tmp_path, monkeypatch):
    login = _setuid_file(tmp_path / "login")
    plain = _setuid_file(tmp_path / "plain", 0o755)
    now = time.time()
    cases = [
        (_blocked(created=now + 5, exe=str(login)), []),                       # setuid login: 제외
        (_blocked(created=now + 5, exe=str(plain)), None),                     # 일반 바이너리: 불완전
        (_blocked(created=now + 5, exe=None), None),                           # exe 도 못 읽음: 불완전
        (_blocked(created=now + 5, exe=str(tmp_path / "gone")), None),         # exe 가 사라짐: 불완전
    ]
    for proc, expected in cases:
        monkeypatch.setattr(psutil, "process_iter", lambda *_a, _p=proc, **_k: iter([_p]))
        assert host_gpu.find_alias_processes_strict(now) == expected
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: iter([_blocked(created=now + 5, exe=str(login))]))
    assert host_gpu.find_our_servers_strict(str(login), 1, now) is None  # 우리 서버 실행 파일 자체라면 제외 불가


def test_force_release_succeeds_when_a_setuid_login_started_after_the_lease(box, monkeypatch, tmp_path):
    record = _orphan(box)
    login = _setuid_file(tmp_path / "login")
    _with_blocked(monkeypatch, _blocked(created=record["created_at"] + 60, exe=str(login)))

    result = box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])

    assert result["released_lease"] == record["lease_id"] and box.manager.current() is None


def test_force_release_still_refuses_when_an_unreadable_process_started_after_the_lease_has_no_readable_exe(
        box, monkeypatch):
    record = _orphan(box)
    _with_blocked(monkeypatch, _blocked(created=record["created_at"] + 60, exe=None))
    with pytest.raises(GpuError, match="enumerated completely"):
        box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])
    assert box.manager.current() is not None


def test_pending_spawn_recovery_tolerates_a_setuid_login_started_after_the_supervisor(box, monkeypatch, tmp_path):
    record = _orphan(box)
    since = time.time() - 30
    host_gpu._write_state(box.dir, {
        "launch_id": "p", "phase": "starting", "lease_id": record["lease_id"],
        "lease_generation_token": record["generation_token"], "runtime_instance": record["runtime_instance"],
        "supervisor_pid": 999999, "supervisor_start": since, "server_pid": None, "server_start": None,
        "server_spawn": "pending", "port": 19861, "command": [str(tmp_path / "srv"), "--port", "19861"],
        "started_at": since, "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0, "swap_limit_bytes": 1})
    _with_blocked(monkeypatch, _blocked(created=since + 5, exe=str(_setuid_file(tmp_path / "login"))))

    assert box.gpu._current()["phase"] == "stopped"
    assert box.manager.current() is None


def test_the_real_process_table_does_not_block_force_release_of_an_old_lease(box):
    """이 host 의 실제 프로세스 표(터미널마다 있는 setuid login 포함)로 7시간 된 lease 를 해제한다."""
    record = _orphan(box, created_ago=7 * 3600)

    result = box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])

    assert result["released_lease"] == record["lease_id"] and box.manager.current() is None


# ---- 2. 다른 소비자 lease: 소비자별 런타임 검증 없이는 해제하지 않는다

def _comfy_lease(box, *, runtime=None, record_job=True, mismatch=None, hide_pid=False):
    task = uuid.uuid4().hex
    record = _orphan(box, task=task, family="comfy", instance=uuid.uuid4().hex)
    pid, start = runtime if runtime else _dead()
    if record_job:
        job = {"task_id": task, "lease_id": record["lease_id"], "generation_token": record["generation_token"],
               "runtime_instance": record["runtime_instance"], "runtime_pid": pid, "runtime_created_at": start,
               "status": "needs_reconcile"}
        if hide_pid:
            job.pop("runtime_pid")
            job.pop("runtime_created_at")
        if mismatch:
            job[mismatch] = "other"
        path = box.data / "managed-comfy" / "tasks" / f"{task}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(job))
    return record


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def comfy_port(monkeypatch):
    port = _closed_port()
    monkeypatch.setattr(managed_comfy, "COMFY_PORT", port)
    return port


def test_unverifiable_consumer_lease_is_refused_even_when_no_alias_process_exists(box, children):
    runtime = subprocess.Popen(["sleep", "120"])  # 별칭 없는 다른 소비자 런타임 대용
    children.append(runtime)
    record = _orphan(box, task="comfy", family="llm")

    with pytest.raises(GpuError, match="no runtime verifier"):
        box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])

    assert runtime.poll() is None and box.manager.current() is not None


def test_comfy_lease_with_a_live_runtime_pid_is_refused(box, children, comfy_port):
    runtime = subprocess.Popen(["sleep", "120"])
    children.append(runtime)
    record = _comfy_lease(box, runtime=(runtime.pid, guard.proc_start(runtime.pid)))

    with pytest.raises(GpuError, match="not confirmed absent"):
        box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])

    assert runtime.poll() is None and box.manager.current() is not None


def test_comfy_lease_with_an_unknown_runtime_pid_lookup_is_refused(box, children, comfy_port, monkeypatch):
    runtime = subprocess.Popen(["sleep", "120"])
    children.append(runtime)
    record = _comfy_lease(box, runtime=(runtime.pid, guard.proc_start(runtime.pid)))
    real = psutil.Process

    def lookup(target=None):
        if target == runtime.pid:
            raise psutil.AccessDenied(target)
        return real(target)

    monkeypatch.setattr(psutil, "Process", lookup)
    with pytest.raises(GpuError, match="observed unknown"):
        box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])
    assert box.manager.current() is not None


def test_comfy_lease_is_refused_while_the_fixed_port_answers(box, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(managed_comfy, "COMFY_PORT", listener.getsockname()[1])
        record = _comfy_lease(box)
        with pytest.raises(GpuError, match="answers or its state is unknown"):
            box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])
    assert box.manager.current() is not None


@pytest.mark.parametrize("mismatch", ["lease_id", "generation_token", "runtime_instance", "task_id"])
def test_comfy_lease_with_a_mismatching_job_record_is_refused(box, comfy_port, mismatch):
    record = _comfy_lease(box, mismatch=mismatch)
    with pytest.raises(GpuError, match=f"does not match the lease \\({mismatch}\\)"):
        box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])
    assert box.manager.current() is not None


def test_comfy_lease_without_a_job_record_is_refused(box, comfy_port):
    record = _comfy_lease(box, record_job=False)
    with pytest.raises(GpuError, match="job record is missing"):
        box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])
    assert box.manager.current() is not None


def test_comfy_lease_with_a_confirmed_stopped_runtime_is_released_with_consumer_evidence(box, comfy_port):
    record = _comfy_lease(box)
    host_gpu._write_state(box.dir, {"launch_id": "unrelated", "phase": "ready", "supervisor_pid": 1})

    result = box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])

    assert result["released_lease"] == record["lease_id"] and result["state_stopped"] is False
    assert result["consumer_verification"]["port_connection_refused"] is True
    assert result["consumer_verification"]["runtime_process_state"] == "absent"
    assert box.manager.current() is None
    assert host_gpu._read_state(box.dir)["phase"] == "ready"  # 다른 소비자 lease 해제가 host_gpu state 를 건드리지 않는다


def test_comfy_lease_whose_runtime_pid_was_never_recorded_needs_the_explicit_flag(box, comfy_port, capsys):
    record = _comfy_lease(box, hide_pid=True)
    with pytest.raises(GpuError, match="never recorded"):
        box.gpu.force_release(reason=REASON, lease_id=record["lease_id"])
    assert box.manager.current() is not None

    cli = ["force-release", "--data-dir", str(box.data), "--reason", REASON, "--lease-id", record["lease_id"],
           "--confirm-no-live-process"]
    assert host_gpu.main(cli) == 2  # 플래그 없이는 CLI 도 거부
    assert host_gpu.main([*cli, "--accept-unrecorded-comfy-runtime"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["consumer_verification"]["runtime_process_state"] == "never_recorded_operator_accepted"
    assert box.manager.current() is None


# ---- 3. state 없음/불일치 고아 lease 는 별칭 서버가 없음을 열거로 확인한 뒤에만 회수

def test_orphan_lease_without_state_is_kept_while_an_alias_server_still_runs(box, children, tmp_path):
    _orphan(box)
    server = _alias_child(tmp_path / "fake-llama", 19871)
    children.append(server)

    with pytest.raises(GpuError, match=f"{server.pid}"):
        box.gpu.status()

    assert server.poll() is None and box.manager.current() is not None
    with pytest.raises(LeaseBusy):
        ResourceLeaseManager(data_dir=box.data).acquire(
            owner_task="successor", owner_attempt="s", runtime_instance="s:s", resource_family="comfy", claim={})

    server.kill()
    server.wait()
    box.gpu.status()  # 서버가 사라진 뒤 재시도로 회수된다
    assert box.manager.current() is None


def test_orphan_lease_with_another_launchs_state_is_also_checked_by_enumeration(box, children, tmp_path):
    _orphan(box)
    host_gpu._write_state(box.dir, {"launch_id": "old", "phase": "stopped", "lease_id": "older-lease"})
    server = _alias_child(tmp_path / "fake-llama", 19872)
    children.append(server)
    with pytest.raises(GpuError, match="still exist"):
        box.gpu.status()
    assert box.manager.current() is not None


def test_orphan_lease_with_incomplete_enumeration_is_kept(box, monkeypatch):
    record = _orphan(box)
    _with_blocked(monkeypatch, _blocked(created=record["created_at"] + 60, exe=None))
    with pytest.raises(GpuError, match="complete process table"):
        box.gpu.status()
    assert box.manager.current() is not None


def test_orphan_lease_is_reclaimed_when_enumeration_is_complete_and_empty(box):
    _orphan(box)
    box.gpu.status()
    proof = json.loads(box.manager.record_path.read_text())["release_proof"]["evidence"]
    assert proof["alias_processes"] == 0 and proof["server_started"] is False


# ---- 4. 시계 역행: 서버가 감독자 시작보다 이르게 관측돼도 pending 서버를 놓치지 않는다

@pytest.mark.parametrize("skew", [3.0, 120.0, 250.0])
def test_pending_server_is_found_even_when_the_clock_stepped_back(box, children, tmp_path, skew):
    record = _orphan(box)
    script = tmp_path / "fake-llama"
    server = _alias_child(script, 19880)
    children.append(server)
    started = guard.proc_start(server.pid)
    since = started + skew  # 감독자 시작 기록이 서버 create_time 보다 늦다(시계가 뒤로 감김)
    host_gpu._write_state(box.dir, {
        "launch_id": "p", "phase": "starting", "lease_id": record["lease_id"],
        "lease_generation_token": record["generation_token"], "runtime_instance": record["runtime_instance"],
        "supervisor_pid": 999999, "supervisor_start": since, "server_pid": None, "server_start": None,
        "server_spawn": "pending", "port": 19880, "command": [str(script), "--port", "19880"],
        "started_at": since, "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0, "swap_limit_bytes": 1})

    recovered = box.gpu._current()

    assert recovered["phase"] == "stopped"
    assert server.wait(10) is not None  # 놓치지 않고 정리한 뒤에만 회수했다
    assert box.manager.current() is None


def test_match_margin_is_wider_than_the_skip_skew_so_clock_steps_do_not_hide_our_server():
    assert host_gpu.PENDING_MATCH_MARGIN >= 60 > guard.START_SKEW


def test_transient_enumeration_incompleteness_is_retried_but_persistent_incompleteness_is_not_ignored(monkeypatch):
    monkeypatch.setattr(host_gpu, "ENUMERATION_RETRY_DELAY", 0.0)
    now = time.time()
    calls = {"n": 0}
    real_iter_results = [[_blocked(created=now + 5, exe=None)], []]  # 첫 열거는 불완전, 두 번째는 완전

    def flaky(*_a, **_k):
        calls["n"] += 1
        return iter(real_iter_results[min(calls["n"] - 1, 1)])

    monkeypatch.setattr(psutil, "process_iter", flaky)
    assert host_gpu.find_alias_processes_strict(now) == []
    assert calls["n"] == 2

    calls["n"] = 0
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: (calls.__setitem__("n", calls["n"] + 1)
                                                                  or iter([_blocked(created=now + 5, exe=None)])))
    assert host_gpu.find_alias_processes_strict(now) is None
    assert calls["n"] == host_gpu.ENUMERATION_ATTEMPTS
