# ruff: noqa: UP028, PLW1510  (독립 재검수자가 작성한 원본 그대로 보관한다. 이 줄만 저장소 lint 용으로 추가했다.)
"""G1 0704b82 독립 재검수 CPU 전용 재현 스크립트(v2). 이전 31 checks 를 새 소유자 모델에 맞춰 다시 쓰고 확장했다.

- 자체 소유 가벼운 python sleep 자식만 만들고 신호도 그 자식에게만 보낸다. 운영 프로세스·GPU·서버·DB 는 쓰지 않는다.
- 소유자는 실제 자식 프로세스가 lease 를 잡았다가 SIGKILL 로 죽는 방식으로 만든다(flock 은 OS 가 푼다).
- 사용: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<exactsrc>/src <python> g1_rereview_repro_v2.py
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psutil

from ttakkari import host_gpu
from ttakkari import host_gpu_guard as guard
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager

PORT = 19777
results: list[tuple[str, bool, str]] = []
CHILD_LEASE = r"""
import json, sys, time
from pathlib import Path
from ttakkari.host_resource import ResourceLeaseManager
data, task = Path(sys.argv[1]), sys.argv[2]
h = ResourceLeaseManager(data_dir=data).acquire(owner_task=task, owner_attempt="a", runtime_instance=task + "-rt",
                                                resource_family="llm", claim={})
print(json.dumps({"lease_id": h.lease_id, "token": h.generation_token, "runtime_instance": h.runtime_instance}), flush=True)
time.sleep(300)
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(f"{'OK  ' if cond else 'FAIL'} {name} {detail}")


def kill(*procs):
    for p in procs:
        with contextlib.suppress(Exception):
            os.killpg(p.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            p.kill()
        with contextlib.suppress(Exception):
            p.wait(5)


def alias_child(tmp: Path, port: int = PORT) -> subprocess.Popen:
    script = tmp / "fake-server.py"
    script.write_text("import time\ntime.sleep(120)\n")
    return subprocess.Popen([sys.executable, str(script), "--alias", guard.ALIAS, "--port", str(port)],
                            start_new_session=True)


def plain_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)


def lease_owner(data: Path, task: str = "host_gpu"):
    """lease 를 잡은 살아 있는 자식 소유자. 반환: (Popen, info)."""
    p = subprocess.Popen([sys.executable, "-c", CHILD_LEASE, str(data), task], stdout=subprocess.PIPE,
                         start_new_session=True)
    info = json.loads(p.stdout.readline())
    return p, info


def dead_owner(data: Path, task: str = "host_gpu"):
    p, info = lease_owner(data, task)
    kill(p)
    return info


def mk_gpu(data: Path) -> host_gpu.HostGpu:
    return host_gpu.HostGpu(SimpleNamespace(data_dir=data))


def live_state(info, server_pid, server_start, *, port=PORT, phase="ready", **extra):
    return {"lease_id": info["lease_id"], "lease_generation_token": info["token"],
            "runtime_instance": info["runtime_instance"], "launch_id": "lost", "phase": phase,
            "supervisor_pid": 999999, "supervisor_start": 1.0, "server_pid": server_pid,
            "server_start": server_start, "port": port, "started_at": time.time(),
            "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0, "swap_limit_bytes": 1, **extra}


def successor(data: Path) -> str:
    try:
        h = ResourceLeaseManager(data_dir=data).acquire(owner_task="successor", owner_attempt="a",
                                                         runtime_instance="succ", resource_family="comfy", claim={})
    except LeaseBusy as exc:
        return f"busy({exc})"
    h.release(proof={"runtime_status": "stopped", "evidence": {"runtime_instance": "succ", "x": 1}})
    return "acquired"


def current(gpu):
    try:
        s = gpu._current()
        return "ok:" + (s or {}).get("phase", "none") if s is not None else "ok:None"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


def lease_live(data: Path) -> bool:
    return ResourceLeaseManager(data_dir=data).current() is not None


def denied_for(pids):
    real = psutil.Process

    def fake(pid=None):
        if pid in pids:
            raise psutil.AccessDenied(pid)
        return real(pid)
    return fake


def main() -> None:
    # ===== 교착 1 =====
    # 1a reconcile 실패 주입: state 는 live 로 남고, 다음 시도가 회수한다
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp); pid, start = child.pid, guard.proc_start(child.pid); kill(child)
        info = dead_owner(data)
        gpu = mk_gpu(data); host_gpu._write_state(gpu.dir, live_state(info, pid, start))
        with mock.patch.object(host_gpu.ResourceLeaseManager, "reconcile", side_effect=OSError("disk full")):
            r1 = current(gpu)
        ph = host_gpu._read_state(gpu.dir)["phase"]
        check("1a reconcile 실패 시 state 는 live 유지", ph == "ready" and r1 == "GpuError", f"r1={r1} phase={ph}")
        r2 = current(gpu)
        check("1a 재시도로 stopped + lease 회수", r2 == "ok:stopped" and not lease_live(data), r2)
        check("1a 후속 취득 성공", successor(data) == "acquired")

    # 1b reconcile 성공 + state 쓰기 실패 -> 다음 시도가 released 확인 후 state 만 확정
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp); pid, start = child.pid, guard.proc_start(child.pid); kill(child)
        info = dead_owner(data)
        gpu = mk_gpu(data); host_gpu._write_state(gpu.dir, live_state(info, pid, start))
        real_write = host_gpu._write_state
        with mock.patch.object(host_gpu, "_write_state", side_effect=OSError("write failed")):
            r1 = current(gpu)
        check("1b state 쓰기 실패 주입: lease 는 이미 해제, state 는 live", not lease_live(data)
              and real_write is not None and host_gpu._read_state(gpu.dir)["phase"] == "ready", f"r1={r1}")
        r2 = current(gpu)
        check("1b 재시도로 state 확정", r2 == "ok:stopped", r2)
        check("1b 후속 취득 성공", successor(data) == "acquired")

    # 1c state 의 lease 가 기록과 불일치(다른 lease live)면 보존
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp); pid, start = child.pid, guard.proc_start(child.pid); kill(child)
        other = dead_owner(data, "comfy")
        stale = {**other, "lease_id": "deadbeef", "token": "x"}
        gpu = mk_gpu(data); host_gpu._write_state(gpu.dir, live_state(stale, pid, start))
        r = current(gpu)
        check("1c 불일치 lease 는 건드리지 않고 GpuError", r == "GpuError" and lease_live(data), r)

    # ===== 이전 반례(fail-open) 재확인 =====
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp)
        try:
            info = dead_owner(data)
            gpu = mk_gpu(data)
            host_gpu._write_state(gpu.dir, live_state(info, child.pid, guard.proc_start(child.pid)))
            with mock.patch.object(guard.psutil, "Process", denied_for({child.pid})):
                r = current(gpu)
                succ = successor(data)
            check("A 조회 거부 + 생존 자식 -> GpuError, lease 보존, 후속 거부",
                  r == "GpuError" and lease_live(data) and succ.startswith("busy") and child.poll() is None,
                  f"{r} {succ}")
            host_gpu._write_state(gpu.dir, live_state(info, child.pid, None))
            r = current(gpu)
            check("A server_start None + 생존 -> 보존, 신호 없음", r == "GpuError" and child.poll() is None
                  and lease_live(data), r)
            host_gpu._write_state(gpu.dir, live_state(info, child.pid, guard.proc_start(child.pid)))
            r = current(gpu)
            check("A 조회 복구 후 우리 서버 종료 확인 -> 정리", r == "ok:stopped" and child.wait(5) is not None
                  and not lease_live(data), r)
        finally:
            kill(child)

    # ===== 교착 2: orphan lease =====
    # 2a 소유자 ABSENT + state 없음 -> 회수
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        gpu = mk_gpu(data)
        r = current(gpu)
        rec = json.loads((data / "host-resource" / "lease.json").read_text())
        check("2a 소유자 ABSENT, state 없음 -> 회수", r == "ok:None" and not lease_live(data)
              and rec["release_proof"]["evidence"].get("orphan_lease") is True, r)
        check("2a owner_pid/owner_start 기록", isinstance(rec.get("owner_pid"), int) and isinstance(rec.get("owner_start"), float))
        check("2a 후속 취득 성공", successor(data) == "acquired")

    # 2b 소유자 PRESENT(살아 있는 자식이 flock 보유) -> 건드리지 않음
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        p, info = lease_owner(data)
        try:
            gpu = mk_gpu(data)
            r = current(gpu)
            check("2b 살아 있는 소유자(flock 보유) -> 손대지 않음", lease_live(data) and r.startswith("ok"), r)
        finally:
            kill(p)

    # 2c 소유자 flock 은 풀렸지만 pid 가 살아 있음(PRESENT) / 조회 거부(UNKNOWN) -> 보존
    for mode in ("PRESENT", "UNKNOWN"):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t) / "data"
            p, info = lease_owner(data)
            try:
                # flock 만 푼 상태를 만든다: 소유자 자식은 살아 있으나 lock fd 를 닫게 할 수 없으므로 lease.json 의 owner 를
                # 살아 있는 다른 자식 신원으로 바꿔 놓고 소유자 자식은 죽인다.
                alive = plain_child()
                rec_path = data / "host-resource" / "lease.json"
                rec = json.loads(rec_path.read_text())
                rec["owner_pid"], rec["owner_start"] = alive.pid, guard.proc_start(alive.pid)
                rec_path.write_text(json.dumps(rec)); kill(p)
                gpu = mk_gpu(data)
                if mode == "UNKNOWN":
                    with mock.patch.object(guard.psutil, "Process", denied_for({alive.pid})):
                        r = current(gpu)
                else:
                    r = current(gpu)
                check(f"2c 소유자 {mode} -> 보존", r == "GpuError" and lease_live(data) and alive.poll() is None, r)
            finally:
                kill(alive)

    # 2d pid 재사용(owner_start 불일치) -> 소유자 ABSENT 로 회수, 재사용 프로세스 무사
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        p, info = lease_owner(data)
        alive = plain_child()
        rec_path = data / "host-resource" / "lease.json"
        rec = json.loads(rec_path.read_text())
        rec["owner_pid"], rec["owner_start"] = alive.pid, guard.proc_start(alive.pid) - 3600
        rec_path.write_text(json.dumps(rec)); kill(p)
        r = current(mk_gpu(data))
        check("2d pid 재사용 소유자 -> 회수, 재사용 프로세스 무사", r == "ok:None" and alive.poll() is None, r)
        kill(alive)

    # 2e 소유자 신원 없는(legacy) lease -> UNKNOWN 보존
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        rec_path = data / "host-resource" / "lease.json"
        rec = json.loads(rec_path.read_text()); rec.pop("owner_pid"); rec.pop("owner_start")
        rec_path.write_text(json.dumps(rec))
        r = current(mk_gpu(data))
        check("2e 소유자 신원 없는 lease -> 보존(force-release 필요)", r == "GpuError" and lease_live(data), r)

    # 2f 다른 소비자(comfy) lease 는 소유자가 죽어도 손대지 않음
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        dead_owner(data, "comfy")
        r = current(mk_gpu(data))
        check("2f comfy lease 불간섭", lease_live(data) and r == "ok:None", r)

    # 2g stopped 인 matching state + 소유자 ABSENT + 기록된 서버 생존 -> 보존
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp)
        try:
            info = dead_owner(data)
            gpu = mk_gpu(data)
            host_gpu._write_state(gpu.dir, live_state(info, child.pid, guard.proc_start(child.pid), phase="stopped"))
            r = current(gpu)
            check("2g stopped state + 서버 생존 -> 보존", r == "GpuError" and lease_live(data) and child.poll() is None, r)
        finally:
            kill(child)

    # 2h 다른 launch 의 stopped state + 소유자 ABSENT -> server_started False 로 회수
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        gpu = mk_gpu(data)
        host_gpu._write_state(gpu.dir, {"launch_id": "old", "phase": "stopped", "lease_id": "old-lease"})
        r = current(gpu)
        check("2h 다른 launch 의 stopped state -> 회수", r == "ok:stopped" and not lease_live(data), r)

    # 2i [잔여 위험 탐침] state 가 사라졌는데 별칭 서버가 살아 있고 소유자는 ABSENT -> 열거 없이 회수하나?
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp)
        try:
            info = dead_owner(data)
            r = current(mk_gpu(data))
            print(f"PROBE 2i state 소실 + 생존 별칭 서버: result={r} lease_live={lease_live(data)} child_alive={child.poll() is None}")
            results.append(("2i state 소실 시 생존 서버가 있어도 회수(state-first 가정 의존)", not (child.poll() is None and not lease_live(data)), "PROBE"))
        finally:
            kill(child)

    # ===== pending 서버 열거 =====
    def pending_state(info, command0, since, port=PORT):
        return {"lease_id": info["lease_id"], "lease_generation_token": info["token"],
                "runtime_instance": info["runtime_instance"], "launch_id": "p", "phase": "starting",
                "supervisor_pid": 999999, "supervisor_start": since, "server_pid": None, "server_start": None,
                "server_spawn": "pending", "port": port, "command": [command0, "--port", str(port)],
                "started_at": since, "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0,
                "swap_limit_bytes": 1}

    # 3a pending + 감독자 사망 + 서버가 실제로 떠 있음 -> 열거 후 정리 -> 회수
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        since = time.time() - 5
        info = dead_owner(data)
        child = alias_child(tmp)
        try:
            gpu = mk_gpu(data); host_gpu._write_state(gpu.dir, pending_state(info, str(tmp / "fake-server.py"), since))
            r = current(gpu)
            check("3a pending + 생존 서버 -> 정리 후 회수", r == "ok:stopped" and child.wait(5) is not None
                  and not lease_live(data), r)
        finally:
            kill(child)

    # 3b pending + 서버 없음 -> 두 번 열거 비어 있음 -> 회수
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        info = dead_owner(data)
        gpu = mk_gpu(data); host_gpu._write_state(gpu.dir, pending_state(info, str(tmp / "fake-server.py"), time.time() - 5))
        r = current(gpu)
        check("3b pending + 서버 없음 -> 회수", r == "ok:stopped" and not lease_live(data), r)

    # 3c 열거 불완전(argv 조회 거부 프로세스가 since 이후에 시작) -> UNKNOWN 보존
    real_iter = psutil.process_iter

    def fake_iter_factory(created_for_blocked, *, blocked_status=psutil.STATUS_RUNNING):
        def fake_iter(attrs=None, ad_value=None):
            for proc in real_iter(attrs, ad_value):
                yield proc
            blocked = SimpleNamespace(info={"pid": 999998, "cmdline": None, "create_time": created_for_blocked,
                                            "uids": SimpleNamespace(real=os.getuid()), "status": blocked_status})
            yield blocked
        return fake_iter

    def pend_run(offset_from_since: float, label: str, expect: str, *, with_child=False, child_start_delta=None):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t); data = tmp / "data"
            since = time.time() - 10
            info = dead_owner(data)
            gpu = mk_gpu(data)
            host_gpu._write_state(gpu.dir, pending_state(info, str(tmp / "fake-server.py"), since))
            with mock.patch.object(host_gpu.psutil, "process_iter", fake_iter_factory(since + offset_from_since)):
                r = current(gpu)
            ok = (expect == "preserve" and r == "GpuError" and lease_live(data)) or \
                 (expect == "recover" and r == "ok:stopped" and not lease_live(data))
            check(label, ok, f"{r} lease_live={lease_live(data)}")

    pend_run(+0.5, "3c argv 거부 프로세스가 since 이후 시작 -> 보존(불완전)", "preserve")
    pend_run(-1.4, "3d 경계: since-1.4s(skew 안) 거부 프로세스 -> 보존", "preserve")
    pend_run(-1.6, "3e 경계: since-1.6s(skew 밖) 거부 프로세스 -> 후보 아님, 회수", "recover")
    pend_run(-3600, "3f 한참 이전에 시작한 거부 프로세스 -> 건너뜀, 회수", "recover")

    # 3g 시계 오차 반례: 서버 create_time 이 감독자 시작보다 3초 이른 것으로 관측(시계가 뒤로 감김). argv 는 읽힌다.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp)
        try:
            info = dead_owner(data)
            since = guard.proc_start(child.pid) + 3.0  # 감독자 시작이 서버보다 3s 늦게 기록됨(시계 뒤로 감김 모사)
            gpu = mk_gpu(data); host_gpu._write_state(gpu.dir, pending_state(info, str(tmp / "fake-server.py"), since))
            r = current(gpu)
            alive = child.poll() is None
            print(f"PROBE 3g 시계 역행 3s: result={r} lease_live={lease_live(data)} server_alive={alive}")
            results.append(("3g 시계 역행(>1.5s) 시 생존 서버를 놓치고 회수", not (alive and not lease_live(data)), "PROBE"))
        finally:
            kill(child)

    # 3h 서버가 우리 argv 가 아닌 비슷한 프로세스(포트 다름)는 건드리지 않음
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp, port=PORT + 1)
        try:
            info = dead_owner(data)
            gpu = mk_gpu(data); host_gpu._write_state(gpu.dir, pending_state(info, str(tmp / "fake-server.py"), time.time() - 5))
            r = current(gpu)
            check("3h 포트가 다른 프로세스는 신호 없음", child.poll() is None and r == "ok:stopped", r)
        finally:
            kill(child)

    # ===== force-release =====
    def fr(gpu, **kw):
        kw.setdefault("reason", "verified no live test server by ps and lsof")
        try:
            return "released:" + str(gpu.force_release(**kw)["released_lease"])
        except host_gpu.GpuError as exc:
            return "refused:" + str(exc)[:70]

    # 4a flock 소유자 생존 -> 거부
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        p, info = lease_owner(data)
        try:
            r = fr(mk_gpu(data), lease_id=info["lease_id"])
            check("4a flock 소유자 생존 -> 거부", r.startswith("refused") and lease_live(data), r)
        finally:
            kill(p)

    # 4b 별칭 프로세스 생존 -> 거부, 신호 없음
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t); data = tmp / "data"
        child = alias_child(tmp, port=12345)
        try:
            info = dead_owner(data)
            r = fr(mk_gpu(data), lease_id=info["lease_id"])
            check("4b 별칭 프로세스 -> 거부, 신호 없음", r.startswith("refused") and lease_live(data) and child.poll() is None, r)
        finally:
            kill(child)

    # 4c 열거 불완전 -> 거부
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        rec = json.loads((data / "host-resource" / "lease.json").read_text())
        with mock.patch.object(host_gpu.psutil, "process_iter", fake_iter_factory(rec["created_at"] + 0.5)):
            r = fr(mk_gpu(data), lease_id=info["lease_id"])
        check("4c 불완전 열거 -> 거부", r.startswith("refused") and "enumerated" in r and lease_live(data), r)

    # 4d supervisor 생존 -> 거부
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        sup = plain_child()
        try:
            gpu = mk_gpu(data)
            host_gpu._write_state(gpu.dir, live_state(info, 999999, 1.0, supervisor_pid=sup.pid, supervisor_start=guard.proc_start(sup.pid)))
            r = fr(gpu, lease_id=info["lease_id"])
            check("4d supervisor 생존 -> 거부", r.startswith("refused") and "supervisor" in r and lease_live(data), r)
            with mock.patch.object(guard.psutil, "Process", denied_for({sup.pid})):
                r = fr(gpu, lease_id=info["lease_id"])
            check("4d' supervisor 조회 거부(UNKNOWN) -> 거부", r.startswith("refused") and lease_live(data), r)
        finally:
            kill(sup)

    # 4e 소유자(lease owner) 생존 -> 거부
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        p, info = lease_owner(data)
        alive = plain_child()
        try:
            rec_path = data / "host-resource" / "lease.json"
            rec = json.loads(rec_path.read_text())
            rec["owner_pid"], rec["owner_start"] = alive.pid, guard.proc_start(alive.pid)
            rec_path.write_text(json.dumps(rec)); kill(p)
            r = fr(mk_gpu(data), lease_id=info["lease_id"])
            check("4e lease 소유자 pid 생존 -> 거부", r.startswith("refused") and "lease_owner" in r and lease_live(data), r)
        finally:
            kill(alive)

    # 4f 서버 pid 생존: 플래그 없으면 거부 / 플래그 있고 별칭 아님이면 허용(설계). 생존 서버 신원이 별칭이면 4b 가 막는다.
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        other = plain_child()
        try:
            gpu = mk_gpu(data)
            host_gpu._write_state(gpu.dir, live_state(info, other.pid, guard.proc_start(other.pid), supervisor_start=time.time() - 100))
            r1 = fr(gpu, lease_id=info["lease_id"])
            check("4f 서버 pid 생존 + 플래그 없음 -> 거부", r1.startswith("refused") and lease_live(data), r1)
            r2 = fr(gpu, lease_id=info["lease_id"], accept_unrelated_pid=True)
            check("4f' 비별칭 pid + --accept-unrelated-pid -> 설계상 허용(프로세스 무사)", r2.startswith("released")
                  and other.poll() is None, r2)
        finally:
            kill(other)

    # 4g reason/lease-id/해제 대상 없음
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        gpu = mk_gpu(data)
        check("4g reason 짧음 -> 거부", fr(gpu, lease_id=info["lease_id"], reason="short").startswith("refused"))
        check("4g lease-id 불일치 -> 거부", fr(gpu, lease_id="nope").startswith("refused"))
        check("4g lease-id 생략 -> 거부", fr(gpu).startswith("refused") and lease_live(data))
        r = fr(gpu, lease_id=info["lease_id"])
        check("4h 모든 조건 충족 -> 해제 + proof", r.startswith("released") and not lease_live(data), r)
        check("4h 해제 후 후속 취득", successor(data) == "acquired")
        check("4i 해제할 것 없음 -> 거부", fr(gpu).startswith("refused"))

    # 4j legacy state(lease 없음) + live phase -> 해제 가능(lease_id 생략)
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        gpu = mk_gpu(data)
        host_gpu._write_state(gpu.dir, {"launch_id": "legacy", "phase": "ready", "supervisor_pid": 999999,
                                        "supervisor_start": time.time() - 100, "server_pid": 999998, "server_start": 1.0,
                                        "port": PORT, "started_at": time.time() - 100})
        r0 = current(gpu)
        r = fr(gpu)
        check("4j legacy state: 자동은 거부, force-release 로 복구", r0 == "GpuError" and r.startswith("released")
              and host_gpu._read_state(gpu.dir)["phase"] == "stopped", f"{r0} {r}")

    # 4k [탐침] 다른 소비자(comfy) lease + 소유자 ABSENT + 살아 있는 (별칭 없는) comfy 서버 -> force-release 가 해제하나?
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data, "comfy")
        comfy_like = plain_child()  # 별칭 없는 가벼운 CPU 자식. 실제 Comfy 서버 대용
        try:
            r = fr(mk_gpu(data), lease_id=info["lease_id"])
            print(f"PROBE 4k comfy lease + 생존 non-alias 런타임: {r} lease_live={lease_live(data)} runtime_alive={comfy_like.poll() is None}")
            results.append(("4k force-release 가 생존 comfy 런타임이 있는 comfy lease 를 해제하지 않음",
                            not (comfy_like.poll() is None and not lease_live(data)), "PROBE"))
        finally:
            kill(comfy_like)

    # 4l CLI: 필수 플래그 누락은 argparse 거부, 거부 시 종료 코드 2
    with tempfile.TemporaryDirectory() as t:
        data = Path(t) / "data"
        info = dead_owner(data)
        base = [sys.executable, "-m", "ttakkari.host_gpu", "force-release", "--data-dir", str(data),
                "--reason", "verified no live test server by ps and lsof", "--lease-id", info["lease_id"]]
        p1 = subprocess.run(base, capture_output=True, text=True)
        check("4l CLI --confirm-no-live-process 없으면 거부", p1.returncode != 0 and lease_live(data), f"rc={p1.returncode}")
        p2 = subprocess.run(base + ["--confirm-no-live-process"], capture_output=True, text=True)
        check("4l CLI 정상 해제 rc=0", p2.returncode == 0 and not lease_live(data), f"rc={p2.returncode} {p2.stderr[-80:]}")
        p3 = subprocess.run(base + ["--confirm-no-live-process"], capture_output=True, text=True)
        check("4l CLI 대상 없으면 rc=2", p3.returncode == 2, f"rc={p3.returncode} {p3.stderr.strip()[:60]}")

    failed = [r for r in results if not r[1]]
    print(f"\nTOTAL {len(results)} checks, {len(failed)} failed")
    for n, _, d in failed:
        print("FAILED:", n, d)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
