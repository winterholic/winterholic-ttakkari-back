"""접수 Qwen 서버 수명주기(runs/desk_qwen_service): 자식 프로세스 소유·health·실패·종료·고아 정리·세대 독립·외부 서버 재사용.

llama-server 대신 tests/fake_agents/fake_llama_server.py 를 진짜 자식 프로세스로 띄운다(실제 spawn·시그널·포트를 쓴다).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from helpers import run_events, start_run, wait_run
from test_desk import _desk, _leads

from ttakkari.config import get_settings
from ttakkari.runs import desk_qwen
from ttakkari.runs import desk_qwen_service as svc

FAKE = Path(__file__).parent / "fake_agents" / "fake_llama_server.py"


@pytest.fixture(autouse=True)
def managed(monkeypatch, tmp_path):
    s = get_settings()
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x" * 1024)
    monkeypatch.setattr(s, "home", tmp_path / "home")
    monkeypatch.setattr(s, "desk_qwen_enabled", True)
    monkeypatch.setattr(s, "desk_qwen_manage", True)
    monkeypatch.setattr(s, "desk_qwen_base_url", "")
    monkeypatch.setattr(s, "desk_qwen_server_path", str(FAKE))
    monkeypatch.setattr(s, "desk_qwen_model_path", str(model))
    monkeypatch.setattr(s, "desk_qwen_min_free_memory_gb", 0.001)
    monkeypatch.setattr(s, "desk_qwen_start_timeout_seconds", 20.0)
    monkeypatch.setattr(svc, "HEALTH_INTERVAL", 0.2)
    monkeypatch.setattr(svc, "BACKOFF_START", 0.2)
    desk_qwen.reset_cooldown()
    yield model
    desk_qwen.reset_cooldown()


async def until(cond, timeout=20.0, what="조건"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{what} 이(가) {timeout}초 안에 충족되지 않음")


def alive(pid: int) -> bool:
    return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE


def spawn_fake(model: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(FAKE), "--model", str(model), "--host", "127.0.0.1", "--port", str(port)],
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def test_managed_server_becomes_ready_and_stop_kills_the_child():
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "ready", what="ready")
    pid = q._child.pid
    assert alive(pid) and q.endpoint.startswith("http://127.0.0.1:")
    await q.stop()
    await until(lambda: not alive(pid), what="child gone")
    assert q.endpoint is None and not list((get_settings().home / "qwen").glob("*.json"))  # pid 파일도 정리


def test_child_argv_keeps_the_shape_the_qwen_local_harness_identifies_it_by(managed):
    """qwen-local-harness 의 가드는 argv 로 접수 서버를 알아본다(runner.managed_intake_signature).

    TTAKKARI_HOME 이 다른 백엔드(개발·검증 인스턴스)의 자식은 pid 기록을 공용 디렉터리에서 못 찾으므로
    이 argv 와 살아 있는 부모(백엔드)만이 소유 증거다. 플래그가 바뀌면 qwen-local 이 다시 막힌다.
    """
    argv = svc._server_args(Path("/srv/llama-server"), Path("/m/Qwen3.5-4B-Q4_K_M.gguf"), 51011)

    def value(flag: str) -> str:
        return argv[argv.index(flag) + 1]

    assert argv[0] == "/srv/llama-server"
    assert (value("--device"), value("--gpu-layers"), value("--host"), value("--port")) == ("none", "0", "127.0.0.1", "51011")
    assert value("--model") == "/m/Qwen3.5-4B-Q4_K_M.gguf" and "--no-webui" in argv
    assert "--alias" not in argv and "--api-key" not in argv  # 하네스 자신의 Flash-Next 서버와 구분되는 표지


async def test_managed_child_is_a_direct_child_and_its_pid_record_names_the_live_owner(managed):
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "ready", what="ready")
    child = q._child.pid
    assert psutil.Process(child).ppid() == os.getpid()  # 소유자 = 직계 부모(가드의 소유 증거)
    [record_file] = list((get_settings().home / "qwen").glob("*.json"))
    rec = json.loads(record_file.read_text())
    assert rec["owner_pid"] == os.getpid() and rec["child_pid"] == child
    assert {"port", "server", "model", "owner_start", "child_start"} <= set(rec)
    await q.stop()


async def test_start_is_idempotent_and_never_spawns_twice():
    q = svc.QwenServer()
    await q.start()
    await q.start()
    await until(lambda: q.state == "ready")
    await q.start()
    assert q.spawn_count == 1
    await q.stop()
    await q.stop()  # 두 번 불러도 안전


async def test_child_listens_on_loopback_only_with_a_minimal_environment(monkeypatch):
    monkeypatch.setenv("TTAKKARI_JWT_SECRET", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "ready")
    proc = psutil.Process(q._child.pid)
    cmd = proc.cmdline()
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert all(c.laddr.ip == "127.0.0.1" for c in proc.net_connections(kind="inet") if c.status == psutil.CONN_LISTEN)
    assert set(svc._child_env()) <= {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
    await q.stop()


async def test_port_collision_retries_on_another_port(monkeypatch):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    taken = holder.getsockname()[1]
    real, calls = svc._pick_port, []

    def pick():
        calls.append(1)
        return taken if len(calls) == 1 else real()
    monkeypatch.setattr(svc, "_pick_port", pick)
    q = svc.QwenServer()
    try:
        await q.start()
        await until(lambda: q.state == "ready", what="ready after collision")
        assert q.spawn_count == 2 and not q.endpoint.endswith(str(taken))
    finally:
        await q.stop()
        holder.close()


async def test_crashed_child_is_restarted_and_requests_fall_back_meanwhile(managed):
    managed.with_name("model.gguf.behavior").write_text(json.dumps({"exit_after": 0.8}))
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "ready", what="first ready")
    first = q._child.pid
    await until(lambda: q.state != "ready", what="detect crash")
    assert q.endpoint is None  # 죽은 동안은 주소가 없어 접수가 CLI 로 간다
    await until(lambda: q.spawn_count >= 2, what="respawn")
    await q.stop()
    assert not alive(first)


async def test_missing_binary_or_model_fails_safely_without_crashing(monkeypatch, managed):
    monkeypatch.setattr(get_settings(), "desk_qwen_server_path", "/nonexistent/llama-server")
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "failed", what="failed")
    assert "missing" in q.reason and q.spawn_count == 0 and q.endpoint is None
    await q.stop()


async def test_insufficient_memory_does_not_spawn(monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_qwen_min_free_memory_gb", 10_000.0)
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "failed")
    assert "insufficient memory" in q.reason and q.spawn_count == 0
    await q.stop()


async def test_slow_start_is_not_ready_until_health_passes(managed):
    managed.with_name("model.gguf.behavior").write_text(json.dumps({"delay": 1.5}))
    q = svc.QwenServer()
    await q.start()
    await asyncio.sleep(0.5)
    assert q.state == "starting" and q.endpoint is None and not desk_qwen.available()
    await until(lambda: q.state == "ready", what="ready after delay")
    await q.stop()


# ---- 고아 정리: 소유자가 죽은 기록의 자식만, 명령줄·시작 시각이 맞을 때만

def _record(child: subprocess.Popen, port: int, owner_pid: int, owner_start: float | None, child_start: float | None | str = "auto") -> Path:
    d = get_settings().home / "qwen"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{owner_pid}-t.json"
    f.write_text(json.dumps({"owner_pid": owner_pid, "owner_start": owner_start, "child_pid": child.pid, "port": port,
                             "child_start": svc._proc_start(child.pid) if child_start == "auto" else child_start}))
    return f


def spawn_sleep() -> subprocess.Popen:
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def dead_owner() -> tuple[int, float]:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    start = svc._proc_start(p.pid)
    p.wait()
    return p.pid, start or 0.0


async def test_orphan_with_dead_owner_is_reaped(managed):
    port = svc._pick_port()
    child = spawn_fake(managed, port)
    await until(lambda: svc._proc_start(child.pid) is not None)
    f = _record(child, port, *dead_owner())
    assert svc.reap_orphans(FAKE) == [child.pid]
    child.wait(timeout=5)
    assert not f.exists()


async def test_child_of_a_live_owner_generation_is_never_touched(managed):
    port = svc._pick_port()
    child = spawn_fake(managed, port)
    try:
        f = _record(child, port, os.getpid(), svc._proc_start(os.getpid()))  # 다른 세대(살아 있는 소유자)
        assert svc.reap_orphans(FAKE) == [] and child.poll() is None and f.exists()
    finally:
        child.kill()
        child.wait()


async def test_reaper_does_not_kill_an_unrelated_process_that_reused_the_pid(managed):
    other = spawn_sleep()
    try:
        f = _record(other, 4242, *dead_owner())  # 명령줄이 서버가 아님
        assert svc.reap_orphans(FAKE) == [] and other.poll() is None and not f.exists()
        port = svc._pick_port()
        child = spawn_fake(managed, port)
        try:
            f2 = _record(child, port, *dead_owner(), child_start=1.0)  # 시작 시각이 기록과 다름(pid 재사용)
            assert svc.reap_orphans(FAKE) == [] and child.poll() is None and not f2.exists()
        finally:
            child.kill()
            child.wait()
    finally:
        other.kill()
        other.wait()


async def test_startup_reaps_orphans_but_leaves_a_sibling_generations_server_alone(managed):
    sibling = svc.QwenServer()  # 다른 세대처럼 자기 자식을 가진 서버
    await sibling.start()
    await until(lambda: sibling.state == "ready")
    port = svc._pick_port()
    orphan = spawn_fake(managed, port)
    _record(orphan, port, *dead_owner())
    mine = svc.QwenServer()
    await mine.start()
    await until(lambda: mine.state == "ready")
    orphan.wait(timeout=5)  # 새로 뜬 쪽이 고아를 정리
    assert alive(sibling._child.pid) and sibling.state == "ready"
    await mine.stop()
    assert alive(sibling._child.pid)  # 한 세대가 끝나도 다른 세대 모델은 살아 있다
    assert await svc.probe(sibling.endpoint)
    await sibling.stop()


async def test_two_generations_use_separate_ports_and_stopping_one_keeps_the_other():
    a, b = svc.QwenServer(), svc.QwenServer()
    await a.start()
    await b.start()
    await until(lambda: a.state == "ready" and b.state == "ready")
    assert a.endpoint != b.endpoint and a._child.pid != b._child.pid
    await a.stop()
    assert b.state == "ready" and await svc.probe(b.endpoint) and alive(b._child.pid)
    await b.stop()


# ---- 외부 서버: 소유하지 않는다

async def test_configured_external_server_is_reused_never_owned_or_killed(managed, monkeypatch):
    port = svc._pick_port()
    ext = spawn_fake(managed, port)
    try:
        monkeypatch.setattr(get_settings(), "desk_qwen_base_url", f"http://127.0.0.1:{port}")
        q = svc.QwenServer()
        await q.start()
        await until(lambda: q.external_ready is True, what="external healthy")
        assert q.state == "external" and q.spawn_count == 0
        await q.stop()
        assert ext.poll() is None  # 소유하지 않은 프로세스는 끝내지 않는다
    finally:
        ext.kill()
        ext.wait()


async def test_unreachable_user_supplied_url_never_starts_a_local_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_qwen_base_url", "http://127.0.0.1:1")
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.external_ready is False, what="external unhealthy")
    assert q.spawn_count == 0 and q._child is None
    from ttakkari.runs.desk_qwen_service import service
    monkeypatch.setattr(service, "external_ready", False)
    assert desk_qwen.endpoint() is None  # 건강하지 않은 외부 서버로는 요청을 보내지 않는다
    await q.stop()


async def test_disabled_never_spawns(monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_qwen_enabled", False)
    q = svc.QwenServer()
    await q.start()
    assert q.state == "disabled" and q.spawn_count == 0
    await q.stop()


# ---- 접수가 실제로 이 프로세스의 서버로 간다 / 배선

async def test_desk_request_goes_to_the_managed_server_of_this_process(client, auth, make_dir):
    svc.service.state = "stopped"
    await svc.service.start()
    try:
        await until(lambda: svc.service.state == "ready", what="service ready")
        desk = await _desk(client, auth, make_dir)
        run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "코드 고쳐줘"))["id"], timeout=30)
        route = next(e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.route")["payload"]
        assert run["engine"] == "qwen" and route["engine"] == "qwen"
        assert route["qwen"]["prompt_version"] == desk_qwen.PROMPT_VERSION
        assert len(await _leads(client, auth, desk["id"])) == 1
    finally:
        await svc.service.stop()


async def test_requests_use_cli_while_the_model_is_not_ready(client, auth, make_dir):
    assert svc.service.endpoint is None and not desk_qwen.available()
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "안녕 DESK_ANSWER"))["id"])
    assert run["engine"] == "codex"  # 준비 전에는 기다리지 않고 Luna 로 간다


async def test_status_endpoint_reports_state_without_internal_details(client):
    r = await client.get("/api/qwen-status")
    body = r.json()
    assert r.status_code == 200 and set(body) == {"enabled", "mode", "state", "reason", "ready"}


def test_lifespans_stop_the_qwen_service_and_the_generation_one_starts_it_through_start_api_services():
    import inspect

    from ttakkari import generation_service, main
    main_src = inspect.getsource(main.lifespan)
    assert "qwen_service.start()" in main_src and "qwen_service.stop()" in main_src
    # start() moved into start_api_services; the behavior test below proves it still runs.
    generation_src = inspect.getsource(generation_service.lifespan)
    assert "start_api_services(" in generation_src and "qwen_service.stop()" in generation_src


@pytest.mark.parametrize("mode", ["local", "remote"])
async def test_generation_api_start_services_starts_the_qwen_service_in_every_mode(monkeypatch, mode):
    """Qwen is per-API-generation state, so remote-executor APIs must start it too.

    Not run through generation_service.lifespan: its teardown stops the shared RunManager and
    disposes the engine, which would leave every later test's Runs refused.
    """
    from ttakkari import generation_service
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs.desk_qwen_service import service as qwen_service
    from ttakkari.tts import jobs as tts_jobs

    calls: list[str] = []

    def recorder(name):
        async def inner():
            calls.append(name)
        return inner

    monkeypatch.setattr(qwen_service, "start", recorder("qwen"))
    monkeypatch.setattr(preview, "start_worker", recorder("preview"))
    monkeypatch.setattr(stt_jobs, "start", recorder("stt"))
    monkeypatch.setattr(tts_jobs, "start", recorder("tts"))

    await generation_service.start_api_services(mode)

    assert "qwen" in calls and "tts" in calls
    # Durable workers belong to the executor when the API is remote.
    assert ("preview" in calls and "stt" in calls) == (mode == "local")


async def test_server_is_warmed_before_it_is_marked_ready(managed):
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "ready")
    assert managed.with_name("model.gguf.posts").read_text().count("x") == 1  # 준비 표시 전에 분류 호출 1회
    await q.stop()


async def test_warmup_failure_does_not_block_readiness(monkeypatch):
    async def boom(base):
        raise RuntimeError("warm failed")
    monkeypatch.setattr(desk_qwen, "warmup", boom)
    q = svc.QwenServer()
    await q.start()
    await until(lambda: q.state == "ready", what="ready despite warm-up failure")
    await q.stop()
