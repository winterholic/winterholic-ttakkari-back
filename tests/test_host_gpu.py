"""ADR 0037: 호스트 GPU 시험 경로. 실제 모델·GPU 없이 가짜 llama-server 로 인자 검증·단일 실행·스왑 가드·타임아웃을 본다."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest
from pydantic import ValidationError

from ttakkari import host_gpu
from ttakkari.host_gpu import (
    ALIAS,
    GIB,
    Busy,
    GpuError,
    HostGpu,
    Invalid,
    LaunchMismatch,
    StartRequest,
    Supervisor,
    build_args,
)
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager

FAKE_SERVER = textwrap.dedent('''\
    #!{python}
    import json, sys, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    argv = sys.argv[1:]
    port = int(argv[argv.index("--port") + 1])
    print("ggml_metal_init: found device: Apple M5 Max", flush=True)
    print("load_tensors: offloaded 41/41 layers to GPU (MTL0)", flush=True)
    READY_AT = time.time() + 0.3
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            ok = time.time() >= READY_AT
            self.send_response(200 if ok else 503); self.end_headers()
            self.wfile.write(json.dumps({{"status": "ok" if ok else "loading"}}).encode())
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = {{"content": "hello", "timings": {{"prompt_n": 10, "prompt_per_second": 100.0,
                     "predicted_n": 5, "predicted_per_second": 40.0}}}}
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(body).encode())
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


@pytest.fixture
def env(tmp_path):
    server = tmp_path / "llama-server"
    server.write_text(FAKE_SERVER.format(python=sys.executable))
    server.chmod(0o755)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    data = tmp_path / "data"
    settings = SimpleNamespace(
        data_dir=data, host_gpu_enabled=True, host_gpu_server_path=str(server),
        host_gpu_models={"tiny": str(model)}, host_gpu_max_runtime_seconds=300, host_gpu_start_timeout_seconds=30,
        host_gpu_swap_growth_gb=1.0, host_gpu_port_min=19500, host_gpu_port_max=19599,
        host_gpu_reserved_ports=[18987, 57170], qwen_flash_monitor_port=18987)
    yield SimpleNamespace(tmp=tmp_path, server=server, model=model, settings=settings,
                          dir=host_gpu.state_dir(data), gpu=HostGpu(settings))
    state = host_gpu._read_state(host_gpu.state_dir(data))
    for key in ("server_pid", "supervisor_pid"):
        if state and state.get(key) and psutil.pid_exists(state[key]) and state.get("phase") in ("starting", "ready"):
            host_gpu._terminate_group(state[key], grace=2)


def _spec(env, *, max_seconds=60, swap_limit=GIB, launch_id="t1", **request):
    return {"launch_id": launch_id, "request": {"model": "tiny", "gpu_layers": 0, "device": "none", **request},
            "state_dir": str(env.dir), "server_path": str(env.server), "model_path": str(env.model),
            "max_seconds": max_seconds, "start_timeout": 30, "swap_limit_bytes": swap_limit,
            "port_min": 19500, "port_max": 19599, "reserved_ports": [18987, 57170]}


def _wait(pred, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _run_supervisor(sup: Supervisor) -> threading.Thread:
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    return t


def _phase(env):
    return (host_gpu._read_state(env.dir) or {}).get("phase")


# ---- 인자 검증

def test_start_request_rejects_unknown_fields_and_out_of_range_values():
    assert StartRequest(model="m").gpu_layers == 999
    assert StartRequest(model="m").spec_type == "none"
    assert StartRequest(model="m").parallel == 1
    for bad in ({"model": "m", "extra_args": "--api-key x"}, {"model": "m", "gpu_layers": 1000},
                {"model": "m", "gpu_layers": -1}, {"model": "m", "ctx_size": 100}, {"model": "m", "ctx_size": 10**7},
                {"model": "m", "threads": 0}, {"model": "m", "n_cpu_moe": 999}, {"model": "m", "flash_attn": "yes"},
                {"model": "m", "device": "MTL0"}, {"model": "m", "max_seconds": 5}, {"model": "m", "gpu_layers": "1;rm"},
                {"model": ""}, {"model": "m", "threads": 10**6},
                {"model": "m", "spec_type": "invalid"}, {"model": "m", "spec_type": "ngram-simple"},
                {"model": "m", "parallel": 0}, {"model": "m", "parallel": 3}, {"model": "m", "parallel": -1}):
        with pytest.raises(ValidationError):
            StartRequest(**bad)


def test_start_request_rejects_contradictory_combinations():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        StartRequest(model="m", cpu_moe=True, n_cpu_moe=4)
    with pytest.raises(ValidationError, match="cannot offload"):
        StartRequest(model="m", device="none", gpu_layers=99)  # 운영 서버가 CPU 만 쓴 인자 조합
    assert StartRequest(model="m", device="none", gpu_layers=0).device == "none"


@pytest.mark.parametrize("parallel", [True, False])
def test_start_request_rejects_boolean_parallel(parallel):
    with pytest.raises(ValidationError):
        StartRequest(model="m", parallel=parallel)


def test_build_args_maps_only_allowlisted_flags():
    req = StartRequest(model="m", gpu_layers=40, n_cpu_moe=12, ctx_size=4096, threads=4, ubatch_size=256,
                       batch_size=1024, flash_attn="on")
    argv = build_args(Path("/srv/llama-server"), Path("/m/model.gguf"), 19501, req)
    assert argv[:3] == ["/srv/llama-server", "--model", "/m/model.gguf"]
    assert argv[argv.index("--alias") + 1] == ALIAS and argv[argv.index("--host") + 1] == "127.0.0.1"
    for flag, value in (("--gpu-layers", "40"), ("--n-cpu-moe", "12"), ("--ctx-size", "4096"), ("--threads", "4"),
                        ("--threads-batch", "4"), ("--ubatch-size", "256"), ("--batch-size", "1024"),
                        ("--flash-attn", "on"), ("--port", "19501"), ("--parallel", "1")):
        assert argv[argv.index(flag) + 1] == value, flag
    assert "--cpu-moe" not in argv and "--device" not in argv and "--api-key" not in argv and "--spec-type" not in argv
    cpu = build_args(Path("s"), Path("m"), 19501, StartRequest(model="m", cpu_moe=True, device="none", gpu_layers=0))
    assert "--cpu-moe" in cpu and cpu[cpu.index("--device") + 1] == "none"


def test_build_args_maps_spec_type_and_parallel():
    req = StartRequest(model="m", spec_type="ngram-mod", parallel=2, ctx_size=4096)
    argv = build_args(Path("/srv/llama-server"), Path("/m/model.gguf"), 19501, req)
    assert argv[argv.index("--parallel") + 1] == "2"
    assert argv[argv.index("--spec-type") + 1] == "ngram-mod"
    assert argv[argv.index("--ctx-size") + 1] == "4096"
    # spec_type=none일 때는 --spec-type 인자가 전달되지 않는다
    none_req = StartRequest(model="m", spec_type="none", parallel=1)
    none_argv = build_args(Path("/srv/llama-server"), Path("/m/model.gguf"), 19501, none_req)
    assert none_argv[none_argv.index("--parallel") + 1] == "1"
    assert "--spec-type" not in none_argv


def test_long_context_options_are_bounded_and_map_to_fixed_flags():
    req = StartRequest(model="qwen3.5-4b-q4_k_m", ctx_size=1010000, cache_type_k="q8_0", cache_type_v="q8_0",
                       rope_scaling="yarn")
    argv = build_args(Path("server"), Path("model"), 19300, req)
    for flag, value in (("--ctx-size", "1010000"), ("--cache-type-k", "q8_0"), ("--cache-type-v", "q8_0"),
                        ("--rope-scaling", "yarn"), ("--rope-scale", "4"), ("--yarn-orig-ctx", "262144")):
        assert argv[argv.index(flag) + 1] == value
    for bad in ({"ctx_size": 1010001}, {"ctx_size": 1010000}, {"cache_type_k": "injected"},
                {"cache_type_v": "injected"}, {"rope_scaling": "dynamic"}):
        with pytest.raises(ValidationError):
            StartRequest(model="m", **bad)
    default = StartRequest(model="m")
    assert default.ctx_size == 8192
    argv = build_args(Path("server"), Path("model"), 19300, default)
    assert "--cache-type-k" not in argv and "--cache-type-v" not in argv and "--rope-scaling" not in argv
    assert StartRequest(model="m", ctx_size=262144).ctx_size == 262144
    assert StartRequest(model="m", ctx_size=524288, parallel=2).ctx_size == 524288
    with pytest.raises(ValidationError):
        StartRequest(model="m", ctx_size=524289, parallel=2)


async def test_mcp_forwards_long_context_options(monkeypatch):
    from unittest.mock import AsyncMock

    from ttakkari.orchestrator import mcp_server

    agent = AsyncMock(return_value="ok")
    monkeypatch.setattr(mcp_server, "_agent", agent)
    await mcp_server.host_gpu_start(model="qwen3.5-4b-q4_k_m", ctx_size=1010000, cache_type_k="q8_0",
                                    cache_type_v="q8_0", rope_scaling="yarn")
    method, route, payload = agent.await_args.args
    assert method == "POST" and route == "/host-gpu/start"
    req = StartRequest.model_validate(payload)
    assert req.ctx_size == 1010000 and req.cache_type_k == req.cache_type_v == "q8_0"
    assert req.rope_scaling == "yarn"


def test_start_checks_the_model_allowlist_and_files(env):
    with pytest.raises(Invalid, match="allowlisted"):
        env.gpu.start(StartRequest(model="/etc/passwd"))
    with pytest.raises(Invalid, match="allowlisted"):
        env.gpu.start(StartRequest(model="tiny; rm -rf /"))
    env.model.unlink()
    with pytest.raises(Invalid, match="model file is missing"):
        env.gpu.start(StartRequest(model="tiny"))
    env.settings.host_gpu_enabled = False
    with pytest.raises(host_gpu.Disabled):
        env.gpu.start(StartRequest(model="tiny"))
    assert host_gpu._read_state(env.dir) is None  # 거부된 요청은 프로세스도 상태도 만들지 않는다


def test_pick_port_skips_reserved_busy_and_other_process_ports(monkeypatch):
    import socket
    monkeypatch.setattr(host_gpu, "_ports_in_use_by_processes", lambda: {19601})
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 19602))
        assert host_gpu.pick_port(19600, 19603, {19600}) == 19603
        with pytest.raises(Busy, match="no free test port"):
            host_gpu.pick_port(19600, 19602, {19600})
    # 운영 포트는 범위 안에 있어도 고르지 않는다.
    assert host_gpu.pick_port(18987, 18988, {18987}) == 18988


# ---- 감독자: 단일 실행·가드

def test_supervisor_runs_until_stopped_and_cleans_up(env):
    sup = Supervisor(_spec(env), poll=0.1)
    t = _run_supervisor(sup)
    state = _wait(lambda: _phase(env) == "ready" and host_gpu._read_state(env.dir), what="ready")
    pid = state["server_pid"]
    assert state["port"] >= 19500 and state["port"] not in (18987, 57170)
    assert ALIAS in psutil.Process(pid).cmdline()
    sup.stop.set()
    t.join(20)
    final = host_gpu._read_state(env.dir)
    assert final["phase"] == "stopped" and final["stop_reason"] == "requested"
    assert _gone(pid)


def test_only_one_supervisor_can_own_an_instance(env):
    first = Supervisor(_spec(env, launch_id="a"), poll=0.1)
    t = _run_supervisor(first)
    _wait(lambda: _phase(env) == "ready", what="first ready")
    second = Supervisor(_spec(env, launch_id="b"), poll=0.1)
    assert second.run() == host_gpu.EXIT_BUSY
    assert host_gpu._read_state(env.dir)["launch_id"] == "a"  # 두 번째는 상태를 건드리지 않는다
    first.stop.set()
    t.join(20)
    assert Supervisor(_spec(env, launch_id="c"), poll=0.1, max_seconds=0.5).run() == 0  # 끝난 뒤에는 다시 가능


def test_swap_growth_of_one_gib_stops_the_instance(env):
    samples = {"value": 5 * GIB}
    sup = Supervisor(_spec(env), poll=0.1, swap_reader=lambda: samples["value"])
    t = _run_supervisor(sup)
    state = _wait(lambda: _phase(env) == "ready" and host_gpu._read_state(env.dir), what="ready")
    samples["value"] = 5 * GIB + GIB - 1  # 한도 직전까지는 유지
    time.sleep(0.5)
    assert _phase(env) == "ready"
    samples["value"] = 5 * GIB + GIB
    t.join(20)
    final = host_gpu._read_state(env.dir)
    assert final["phase"] == "stopped" and final["stop_reason"].startswith("swap_guard")
    assert final["peak_swap_growth_bytes"] >= GIB - 1 and final["baseline_swap_bytes"] == 5 * GIB
    assert _gone(state["server_pid"])


def test_instance_stops_at_the_max_runtime(env):
    sup = Supervisor(_spec(env), poll=0.1, max_seconds=1.0)
    t0 = time.monotonic()
    t = _run_supervisor(sup)
    t.join(20)
    final = host_gpu._read_state(env.dir)
    assert final["stop_reason"] == "max_runtime" and final["phase"] == "stopped"
    assert time.monotonic() - t0 < 15


def test_unreadable_swap_monitor_fails_closed(env):
    def broken():
        raise OSError("sysctl denied")
    sup = Supervisor(_spec(env), poll=0.05, swap_reader=lambda: 0)
    sup.swap_reader = lambda: 0
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            broken()
        return 0
    sup.swap_reader = flaky
    t = _run_supervisor(sup)
    t.join(20)
    assert host_gpu._read_state(env.dir)["stop_reason"] == "swap_monitor_failed"


def test_server_that_never_gets_healthy_is_stopped_at_the_start_timeout(env):
    spec = _spec(env)
    spec["start_timeout"] = 0.5
    sup = Supervisor(spec, poll=0.1, health=lambda port: False)
    _run_supervisor(sup).join(20)
    assert host_gpu._read_state(env.dir)["stop_reason"] == "start_timeout"


def test_unconfirmed_server_stop_keeps_the_lease_until_hostgpu_observes_absence(env, monkeypatch):
    manager = ResourceLeaseManager(data_dir=env.settings.data_dir)
    sup = Supervisor(_spec(env), poll=0.1)
    with monkeypatch.context() as patch:
        patch.setattr(host_gpu, "_terminate_group", lambda *a, **k: None)  # 정리 신호가 듣지 않는 서버
        t = _run_supervisor(sup)
        state = _wait(lambda: _phase(env) == "ready" and host_gpu._read_state(env.dir), what="ready")
        sup.stop.set()
        t.join(30)
    assert not t.is_alive()
    sup.lease._lock_fd.close()  # 감독자 프로세스가 끝나 flock 만 풀린다
    try:
        pid = state["server_pid"]
        assert psutil.pid_exists(pid)  # 서버는 아직 살아 있다
        left = host_gpu._read_state(env.dir)
        assert left["phase"] == "ready" and left["stop_unconfirmed"] is True
        assert manager.current()["lease_id"] == state["lease_id"]
        with pytest.raises(LeaseBusy):
            ResourceLeaseManager(data_dir=env.settings.data_dir).acquire(
                owner_task="successor", owner_attempt="a", runtime_instance="successor-runtime",
                resource_family="comfy", claim={})

        # 이후 HostGpu 가 신원을 관찰하고 우리 서버를 정리해 부재가 확인되면 그때 lease 가 풀린다.
        assert env.gpu.status()["stop_reason"] == "supervisor_lost"
        assert manager.current() is None
    finally:
        if psutil.pid_exists(state["server_pid"]):
            host_gpu._terminate_group(state["server_pid"], grace=2)


def test_server_exit_is_recorded(env):
    env.server.write_text(f"#!{sys.executable}\nimport sys\nprint('bad flag', flush=True)\nsys.exit(7)\n")
    sup = Supervisor(_spec(env), poll=0.1)
    _run_supervisor(sup).join(20)
    final = host_gpu._read_state(env.dir)
    assert final["stop_reason"] == "server_exited:7" and final["server_exit_code"] == 7


# ---- 백엔드 창구(실제 분리 감독자 프로세스)

def test_start_status_bench_stop_through_a_detached_supervisor(env):
    started = env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none", threads=2, ctx_size=1024))
    assert started["phase"] in ("starting", "ready") and started["running"] is True
    assert started["execution_path"] == "host_gpu" and started["measured_on"] == "host_gpu_path"
    ready = _wait(lambda: (s := env.gpu.status())["ready"] and s, what="ready")
    assert ready["request"]["threads"] == 2 and ready["port"] not in (18987, 57170)
    assert ready["request"]["spec_type"] == "none" and ready["request"]["parallel"] == 1
    assert ready["slot_ctx_size"] == 1024
    assert ready["context"] == {"total": 1024, "per_slot": 1024, "parallel_slots": 1}
    assert ready["remaining_seconds"] <= 300 and ready["swap"]["limit_bytes"] == GIB
    assert any("offloaded 41/41 layers" in line for line in ready["device_evidence"])
    sup_pid = host_gpu._read_state(env.dir)["supervisor_pid"]
    assert os.getsid(sup_pid) == sup_pid != os.getsid(os.getpid())  # 새 세션 리더라 백엔드 세대가 내려가도 신호를 받지 않는다
    assert "ttakkari.host_gpu" in " ".join(psutil.Process(sup_pid).cmdline())

    with pytest.raises(Busy, match="already running"):
        env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none"))

    bench = env.gpu.bench("hello", 16, 2)
    assert bench["measured_on"] == "host_gpu_path" and "host GPU path" in bench["note"]
    assert bench["median_generation_tokens_per_second"] == 40.0 and bench["median_prompt_tokens_per_second"] == 100.0
    assert len(bench["runs"]) == 2 and bench["request"]["gpu_layers"] == 0

    stopped = env.gpu.stop()
    assert stopped["stopped_now"] is True and stopped["running"] is False
    assert stopped["phase"] == "stopped" and stopped["stop_reason"] == "requested"
    # stop()은 상태 파일이 stopped가 되면 돌아오고 감독자 프로세스 종료는 그 뒤라 잠깐 기다린다.
    _wait(lambda: _gone(sup_pid),
          what="supervisor exit")
    with pytest.raises(Busy, match="no ready"):
        env.gpu.bench("hello")
    assert env.gpu.stop()["stopped_now"] is False
    again = env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none"))  # 끝난 뒤에는 다시 시작 가능
    assert again["running"] is True
    env.gpu.stop()


def test_status_reveals_total_and_per_slot_context(env):
    env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none", threads=2, ctx_size=4096,
                               spec_type="ngram-mod", parallel=2))
    ready = _wait(lambda: (s := env.gpu.status())["ready"] and s, what="ready")
    try:
        assert ready["request"]["spec_type"] == "ngram-mod"
        assert ready["request"]["parallel"] == 2
        assert ready["request"]["ctx_size"] == 4096
        assert ready["slot_ctx_size"] == 2048
        assert ready["context"] == {"total": 4096, "per_slot": 2048, "parallel_slots": 2}
        server_pid = host_gpu._read_state(env.dir)["server_pid"]
        cmd = psutil.Process(server_pid).cmdline()
        assert "--parallel" in cmd and cmd[cmd.index("--parallel") + 1] == "2"
        assert "--spec-type" in cmd and cmd[cmd.index("--spec-type") + 1] == "ngram-mod"
    finally:
        env.gpu.stop()


def test_bench_requires_a_ready_instance(env):
    with pytest.raises(Busy, match="no ready"):
        env.gpu.bench("hello")
    assert env.gpu.status() == {"execution_path": "host_gpu", "measured_on": "host_gpu_path",
                                "note": host_gpu.MEASURED_NOTE, "phase": "idle", "running": False}


def _dead_supervisor_lease(env) -> tuple[ResourceLeaseManager, dict]:
    """감독자가 죽어 flock 만 풀리고 lease 기록은 live 로 남은 상태(공용 lease 도입 이후 상태 파일이 가진 증거)."""
    manager = ResourceLeaseManager(data_dir=env.settings.data_dir)
    lease = manager.acquire(owner_task="host_gpu", owner_attempt="lost", runtime_instance="host-gpu:lost",
                            resource_family="llm", claim={"gpu_layers": 0})
    lease._lock_fd.close()
    return manager, {"lease_id": lease.lease_id, "lease_generation_token": lease.generation_token,
                     "runtime_instance": lease.runtime_instance}


def test_lost_supervisor_is_reaped_without_touching_unrelated_processes(env):
    bystander = subprocess.Popen(["sleep", "60"])
    own = subprocess.Popen([str(env.server), "--port", "19590", "--alias", ALIAS, "--model", "x"],
                           start_new_session=True)
    manager, lease_fields = _dead_supervisor_lease(env)
    try:
        host_gpu._write_state(env.dir, {
            **lease_fields,
            "launch_id": "lost", "phase": "ready", "supervisor_pid": bystander.pid,
            "supervisor_start": host_gpu._proc_start(bystander.pid), "server_pid": own.pid,
            "server_start": host_gpu._proc_start(own.pid), "port": 19590, "started_at": time.time(),
            "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0, "swap_limit_bytes": GIB})
        time.sleep(0.3)
        status = env.gpu.status()  # 잠금을 쥔 감독자가 없으므로 사라진 것으로 본다
        assert status["phase"] == "stopped" and status["stop_reason"] == "supervisor_lost"
        _wait(lambda: own.poll() is not None, 10, "our own orphan server stopped")
        assert bystander.poll() is None  # 관계없는 프로세스(감독자 pid 로 적혀 있어도)는 신호를 받지 않는다
        assert manager.current() is None  # 서버 종료가 관찰된 뒤에만 lease 가 풀린다
    finally:
        bystander.kill()
        own.kill()
        bystander.wait()
        own.wait()


def test_server_pid_that_is_not_our_alias_is_never_killed(env):
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    manager, lease_fields = _dead_supervisor_lease(env)
    try:
        host_gpu._write_state(env.dir, {
            **lease_fields,
            "launch_id": "x", "phase": "ready", "supervisor_pid": 999999, "supervisor_start": 1.0,
            "server_pid": other.pid, "server_start": host_gpu._proc_start(other.pid), "port": 19591,
            "started_at": time.time(), "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0,
            "swap_limit_bytes": GIB})
        # 기록된 pid+시작 시각과 같은 프로세스가 살아 있는데 우리 서버로 확인되지 않는다: 신호도 보내지 않고 부재도 아니다.
        with pytest.raises(GpuError, match="not confirmed"):
            env.gpu.status()
        time.sleep(0.3)
        assert other.poll() is None
        assert manager.current()["lease_id"] == lease_fields["lease_id"]
        with pytest.raises(LeaseBusy):
            ResourceLeaseManager(data_dir=env.settings.data_dir).acquire(
                owner_task="successor", owner_attempt="a", runtime_instance="successor-runtime",
                resource_family="comfy", claim={})
    finally:
        other.kill()
        other.wait()


def test_stop_refuses_to_signal_a_process_that_is_not_our_supervisor(env):
    import fcntl
    bystander = subprocess.Popen(["sleep", "60"])
    env.dir.mkdir(parents=True, exist_ok=True)
    holder = open(env.dir / "lock", "a+")  # noqa: SIM115 -- 감독자가 살아 있는 것처럼 잠금을 쥔다
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        host_gpu._write_state(env.dir, {
            "launch_id": "y", "phase": "ready", "supervisor_pid": bystander.pid,
            "supervisor_start": host_gpu._proc_start(bystander.pid), "server_pid": 999999, "port": 19592,
            "started_at": time.time(), "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0,
            "swap_limit_bytes": GIB})
        with pytest.raises(GpuError, match="identity mismatch"):
            env.gpu.stop()
        assert bystander.poll() is None
    finally:
        holder.close()
        bystander.kill()
        bystander.wait()


def test_state_file_is_private_and_holds_no_secrets(env):
    env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none"))
    _wait(lambda: env.gpu.status()["ready"], what="ready")
    try:
        raw = (env.dir / "state.json").read_text()
        # lease 세대 토큰은 로컬 소유권 펜스일 뿐 인증 자격이 아니다. 같은 값이 lease.json 에도 있고 파일은 0600 이다.
        public = json.loads(raw)
        assert public.pop("lease_generation_token")
        assert "api-key" not in raw and "token" not in json.dumps(public).lower()
        assert (env.dir / "state.json").stat().st_mode & 0o077 == 0
        assert not list(env.dir.glob("spec-*.json"))  # 시작이 끝나면 스펙 파일은 지운다
        assert json.loads(raw)["command"][0] == str(env.server)
    finally:
        env.gpu.stop()


# ---- 감독자가 SIGKILL 로 죽는 경우(finally 정리가 돌지 않는다)

def _gone(pid: int) -> bool:
    # pid_exists 와 status 사이에 프로세스가 사라질 수 있다(부하가 높을 때 게이트에서 실제로 발생했다).
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_sigkilled_supervisor_leaves_no_unguarded_server(env):
    import signal
    env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none", threads=2, ctx_size=1024))
    state = _wait(lambda: env.gpu.status()["ready"] and host_gpu._read_state(env.dir), what="ready")
    sup_pid, server_pid, port = state["supervisor_pid"], state["server_pid"], state["port"]
    assert psutil.pid_exists(state["watchdog_pid"]) and "ttakkari.host_gpu_guard" in " ".join(psutil.Process(state["watchdog_pid"]).cmdline())
    # 같은 별칭·포트를 argv 에 달고 있어도 실행 파일이 다른 프로세스, 관계없는 프로세스는 신호를 받으면 안 된다.
    lookalike = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "--alias", ALIAS, "--port", str(port)],
                                 start_new_session=True)
    bystander = subprocess.Popen(["sleep", "120"], start_new_session=True)
    try:
        t0 = time.monotonic()
        os.kill(sup_pid, signal.SIGKILL)  # 정리 코드가 실행될 수 없는 종료
        _wait(lambda: _gone(server_pid), 15, "watchdog stops the orphaned test server")
        assert time.monotonic() - t0 < 10  # 짧은 시간 안에
        assert lookalike.poll() is None and bystander.poll() is None
        status = env.gpu.status()  # 잠금이 풀렸으므로 다음 조회는 사라진 감독자로 정리 처리한다
        assert status["running"] is False and status["stop_reason"] == "supervisor_lost"
        _wait(lambda: _gone(state["watchdog_pid"]), 10, "watchdog exits after cleaning up")
        # 정리 뒤에는 새 인스턴스를 다시 시작할 수 있다.
        again = env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none"))
        assert again["running"] is True
        env.gpu.stop()
    finally:
        for p in (lookalike, bystander):
            p.kill()
            p.wait()


def test_watchdog_exits_quietly_when_the_supervisor_stops_normally(env):
    env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none"))
    state = _wait(lambda: env.gpu.status()["ready"] and host_gpu._read_state(env.dir), what="ready")
    env.gpu.stop()
    _wait(lambda: _gone(state["watchdog_pid"]), 10, "watchdog exit")
    assert _gone(state["server_pid"])


def test_watchdog_only_matches_our_server_by_path_alias_port_and_start_time(env):
    port = 19588
    ours = subprocess.Popen([str(env.server), "--port", str(port), "--alias", ALIAS, "--model", "x"], start_new_session=True)
    other_port = subprocess.Popen([str(env.server), "--port", "19589", "--alias", ALIAS, "--model", "x"],
                                  start_new_session=True)
    other_bin = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--alias", ALIAS, "--port", str(port)])
    try:
        time.sleep(0.5)
        found = host_gpu.guard.find_our_servers(str(env.server), port, since=time.time() - 60)
        assert found == [ours.pid]
        assert host_gpu.guard.find_our_servers(str(env.server), port, since=time.time() + 600) == []  # 감독자보다 먼저 뜬 것
        # 같은 pid 라도 시작 시각이 다르면(재사용된 pid) 건드리지 않는다.
        assert host_gpu.guard.kill_our_server(ours.pid, 1.0, port) is False and ours.poll() is None
        assert host_gpu.guard.kill_our_server(ours.pid, host_gpu._proc_start(ours.pid), port, grace=3) is True
        _wait(lambda: ours.poll() is not None, 10, "ours stopped")
        assert other_port.poll() is None and other_bin.poll() is None
    finally:
        for p in (ours, other_port, other_bin):
            p.kill()
            p.wait()


def test_supervisor_refuses_to_run_the_server_without_a_working_watchdog(env, monkeypatch):
    monkeypatch.setattr(host_gpu, "WATCHDOG_READY_SECONDS", 1.0)
    monkeypatch.setattr(host_gpu, "sys", SimpleNamespace(executable="/usr/bin/false"))  # 즉시 죽는 watchdog
    assert Supervisor(_spec(env), poll=0.1).run() == 0
    final = host_gpu._read_state(env.dir)
    assert final["phase"] == "stopped" and final["stop_reason"].startswith("start_failed: watchdog did not start")
    assert "server_pid" not in final  # 감시 없이는 서버를 띄우지 않는다


# ---- API

async def test_host_gpu_api_rejects_boolean_parallel_before_start(client, auth, make_dir, monkeypatch):
    from test_orchestrator import _orch, _session

    from ttakkari.orchestrator.runtime import issue_token

    starts = []

    def fake_start(self, request):
        starts.append(request.model_dump())
        return request.model_dump()

    monkeypatch.setattr(HostGpu, "start", fake_start)
    session = await _session(client, auth, make_dir)
    headers = _orch(issue_token(session["id"]))
    for parallel in (True, False):
        response = await client.post("/api/agent/host-gpu/start", json={"model": "tiny", "parallel": parallel},
                                     headers=headers)
        assert response.status_code == 422
        assert response.json()["code"] == "validation"
        assert any(error["loc"] == ["body", "parallel"] for error in response.json()["errors"])
    assert starts == []
    for parallel in (1, 2):
        response = await client.post("/api/agent/host-gpu/start", json={"model": "tiny", "parallel": parallel},
                                     headers=headers)
        assert response.status_code == 202
        assert response.json()["parallel"] == parallel
    assert [item["parallel"] for item in starts] == [1, 2]
    long_request = {"model": "tiny", "ctx_size": 1010000, "cache_type_k": "q8_0", "cache_type_v": "q8_0", "rope_scaling": "yarn"}
    response = await client.post("/api/agent/host-gpu/start", json=long_request, headers=headers)
    assert response.status_code == 202
    assert all(response.json()[key] == value for key, value in long_request.items())
    invalid = await client.post("/api/agent/host-gpu/start", json={**long_request, "ctx_size": 1010001}, headers=headers)
    assert invalid.status_code == 422 and len(starts) == 3


async def test_host_gpu_api_validates_requests_and_reports_idle(client, auth, make_dir, monkeypatch, tmp_path):
    from test_orchestrator import _orch, _session

    from ttakkari.config import get_settings
    from ttakkari.orchestrator.runtime import issue_token
    monkeypatch.setattr(get_settings(), "host_gpu_models", {"tiny": str(tmp_path / "missing.gguf")})
    session = await _session(client, auth, make_dir)
    h = _orch(issue_token(session["id"]))
    status = await client.get("/api/agent/host-gpu/status", headers=h)
    assert status.status_code == 200 and status.json()["measured_on"] == "host_gpu_path"
    assert (await client.post("/api/agent/host-gpu/start", json={"model": "tiny", "argv": ["--api-key", "x"]},
                              headers=h)).status_code == 422
    assert (await client.post("/api/agent/host-gpu/start", json={"model": "other"}, headers=h)).status_code == 422
    assert (await client.post("/api/agent/host-gpu/start", json={"model": "tiny"}, headers=h)).status_code == 422
    assert (await client.post("/api/agent/host-gpu/start", json={"model": "tiny", "spec_type": "invalid"},
                              headers=h)).status_code == 422
    assert (await client.post("/api/agent/host-gpu/start", json={"model": "tiny", "parallel": 4},
                              headers=h)).status_code == 422
    assert (await client.post("/api/agent/host-gpu/bench", json={"prompt": "x"}, headers=h)).status_code == 409
    assert (await client.post("/api/agent/host-gpu/bench", json={"prompt": "x", "n_predict": 9999}, headers=h)).status_code == 422
    assert (await client.get("/api/agent/host-gpu/status")).status_code == 401


async def test_host_gpu_is_available_to_work_sessions(client, auth, make_dir, monkeypatch):
    from test_orchestrator import _orch, _session

    from ttakkari.api import host_env
    monkeypatch.setattr(host_env, "_host", lambda: SimpleNamespace(status=lambda: {"phase": "absent"}, start=lambda body: {"phase": "starting"}))

    from ttakkari.orchestrator.runtime import issue_token
    session = await _session(client, auth, make_dir, pool="work")
    h = _orch(issue_token(session["id"]))
    assert (await client.get("/api/agent/host-gpu/status", headers=h)).status_code == 200
    assert (await client.post("/api/agent/host-gpu/start", json={"model": "tiny"}, headers=h)).status_code == 202


# ---- stop(expected_launch_id): 다른 launch 를 종료하지 않는다 (원자적 소유권 검사)

def _live_state(launch_id: str) -> dict:
    return {"launch_id": launch_id, "phase": "ready", "supervisor_pid": 4242, "supervisor_start": 1.0,
            "lease_id": f"lease-{launch_id}", "lease_generation_token": f"gen-{launch_id}"}


@pytest.fixture
def stop_env(env):
    """No process is spawned: state, lease manager and signalling are autospec'd stand-ins."""
    states = {"current": _live_state("A")}
    with patch.object(HostGpu, "_current", autospec=True, side_effect=lambda self: states["current"]), \
         patch.object(HostGpu, "status", autospec=True, return_value={"phase": "stopped", "running": False}), \
         patch.object(ResourceLeaseManager, "request_cancel", autospec=True) as request_cancel, \
         patch("ttakkari.host_gpu._same_process", autospec=True, return_value=True), \
         patch("ttakkari.host_gpu._cmdline", autospec=True, return_value=["python", "-m", "ttakkari.host_gpu"]), \
         patch("ttakkari.host_gpu._lock_held", autospec=True, return_value=False), \
         patch("ttakkari.host_gpu.os.kill", autospec=True) as kill:
        yield SimpleNamespace(env=env, states=states, request_cancel=request_cancel, kill=kill)


def test_stop_refuses_a_different_launch_without_touching_its_lease_or_process(stop_env):
    stop_env.states["current"] = _live_state("B")  # another run's launch is now the live one

    with pytest.raises(LaunchMismatch):
        stop_env.env.gpu.stop(expected_launch_id="A")

    assert stop_env.request_cancel.call_count == 0
    assert stop_env.kill.call_count == 0


def test_stop_refuses_when_no_state_exists_for_the_expected_launch(stop_env):
    stop_env.states["current"] = None

    with pytest.raises(LaunchMismatch):
        stop_env.env.gpu.stop(expected_launch_id="A")

    assert stop_env.kill.call_count == 0


def test_stop_with_matching_launch_signals_only_that_states_lease_and_supervisor(stop_env):
    result = stop_env.env.gpu.stop(expected_launch_id="A")

    assert result["stopped_now"] is True
    stop_env.request_cancel.assert_called_once()
    assert stop_env.request_cancel.call_args.args[1:] == ("lease-A", "gen-A")
    stop_env.kill.assert_called_once_with(4242, host_gpu.signal.SIGTERM)


def test_stop_without_expected_launch_id_keeps_the_old_contract(stop_env):
    stop_env.states["current"] = _live_state("B")

    result = stop_env.env.gpu.stop()

    assert result["stopped_now"] is True
    assert stop_env.request_cancel.call_args.args[1:] == ("lease-B", "gen-B")
    assert list(__import__("inspect").signature(HostGpu.stop).parameters) == ["self", "expected_launch_id"]
