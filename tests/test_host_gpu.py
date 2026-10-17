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
    StartRequest,
    Supervisor,
    build_args,
)

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
    for bad in ({"model": "m", "extra_args": "--api-key x"}, {"model": "m", "gpu_layers": 1000},
                {"model": "m", "gpu_layers": -1}, {"model": "m", "ctx_size": 100}, {"model": "m", "ctx_size": 10**7},
                {"model": "m", "threads": 0}, {"model": "m", "n_cpu_moe": 999}, {"model": "m", "flash_attn": "yes"},
                {"model": "m", "device": "MTL0"}, {"model": "m", "max_seconds": 5}, {"model": "m", "gpu_layers": "1;rm"},
                {"model": ""}, {"model": "m", "threads": 10**6}):
        with pytest.raises(ValidationError):
            StartRequest(**bad)


def test_start_request_rejects_contradictory_combinations():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        StartRequest(model="m", cpu_moe=True, n_cpu_moe=4)
    with pytest.raises(ValidationError, match="cannot offload"):
        StartRequest(model="m", device="none", gpu_layers=99)  # 운영 서버가 CPU 만 쓴 인자 조합
    assert StartRequest(model="m", device="none", gpu_layers=0).device == "none"


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
    assert "--cpu-moe" not in argv and "--device" not in argv and "--api-key" not in argv
    cpu = build_args(Path("s"), Path("m"), 19501, StartRequest(model="m", cpu_moe=True, device="none", gpu_layers=0))
    assert "--cpu-moe" in cpu and cpu[cpu.index("--device") + 1] == "none"


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
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


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
    assert not psutil.pid_exists(state["server_pid"]) or psutil.Process(state["server_pid"]).status() == psutil.STATUS_ZOMBIE


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
    assert not psutil.pid_exists(sup_pid) or psutil.Process(sup_pid).status() == psutil.STATUS_ZOMBIE
    with pytest.raises(Busy, match="no ready"):
        env.gpu.bench("hello")
    assert env.gpu.stop()["stopped_now"] is False
    again = env.gpu.start(StartRequest(model="tiny", gpu_layers=0, device="none"))  # 끝난 뒤에는 다시 시작 가능
    assert again["running"] is True
    env.gpu.stop()


def test_bench_requires_a_ready_instance(env):
    with pytest.raises(Busy, match="no ready"):
        env.gpu.bench("hello")
    assert env.gpu.status() == {"execution_path": "host_gpu", "measured_on": "host_gpu_path",
                                "note": host_gpu.MEASURED_NOTE, "phase": "idle", "running": False}


def test_lost_supervisor_is_reaped_without_touching_unrelated_processes(env):
    bystander = subprocess.Popen(["sleep", "60"])
    own = subprocess.Popen([str(env.server), "--port", "19590", "--alias", ALIAS, "--model", "x"],
                           start_new_session=True)
    try:
        host_gpu._write_state(env.dir, {
            "launch_id": "lost", "phase": "ready", "supervisor_pid": bystander.pid,
            "supervisor_start": host_gpu._proc_start(bystander.pid), "server_pid": own.pid,
            "server_start": host_gpu._proc_start(own.pid), "port": 19590, "started_at": time.time(),
            "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0, "swap_limit_bytes": GIB})
        time.sleep(0.3)
        status = env.gpu.status()  # 잠금을 쥔 감독자가 없으므로 사라진 것으로 본다
        assert status["phase"] == "stopped" and status["stop_reason"] == "supervisor_lost"
        _wait(lambda: own.poll() is not None, 10, "our own orphan server stopped")
        assert bystander.poll() is None  # 관계없는 프로세스(감독자 pid 로 적혀 있어도)는 신호를 받지 않는다
    finally:
        bystander.kill()
        own.kill()
        bystander.wait()
        own.wait()


def test_server_pid_that_is_not_our_alias_is_never_killed(env):
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        host_gpu._write_state(env.dir, {
            "launch_id": "x", "phase": "ready", "supervisor_pid": 999999, "supervisor_start": 1.0,
            "server_pid": other.pid, "server_start": host_gpu._proc_start(other.pid), "port": 19591,
            "started_at": time.time(), "deadline_epoch": time.time() + 60, "baseline_swap_bytes": 0,
            "swap_limit_bytes": GIB})
        assert env.gpu.status()["stop_reason"] == "supervisor_lost"
        time.sleep(0.3)
        assert other.poll() is None
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
        assert "api-key" not in raw and "token" not in raw.lower()
        assert not list(env.dir.glob("spec-*.json"))  # 시작이 끝나면 스펙 파일은 지운다
        assert json.loads(raw)["command"][0] == str(env.server)
    finally:
        env.gpu.stop()


# ---- API

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
    assert (await client.post("/api/agent/host-gpu/bench", json={"prompt": "x"}, headers=h)).status_code == 409
    assert (await client.post("/api/agent/host-gpu/bench", json={"prompt": "x", "n_predict": 9999}, headers=h)).status_code == 422
    assert (await client.get("/api/agent/host-gpu/status")).status_code == 401


async def test_host_gpu_is_personal_scope_only(client, auth, make_dir):
    from test_orchestrator import _orch, _session

    from ttakkari.orchestrator.runtime import issue_token
    session = await _session(client, auth, make_dir, pool="work")
    h = _orch(issue_token(session["id"]))
    assert (await client.get("/api/agent/host-gpu/status", headers=h)).status_code == 403
    assert (await client.post("/api/agent/host-gpu/start", json={"model": "tiny"}, headers=h)).status_code == 403
