"""P4 제품 여정: Core API 프로세스를 배포하지 않고 실행부 프로세스만 교체한다.

실제 entrypoint(`python -m ttakkari.cli serve` / `python -m ttakkari.cli executor --activate`)를 자기 자식
프로세스로 임시 loopback 포트·주입된 테스트 DB에 띄운다. 작업 유형은 Run 이고, 엔진은 conftest 가 주입한
fake_agents/fake_claude.py(TTAKKARI_CLAUDE_BIN)뿐이라 외부 모델·MCP·네트워크를 쓰지 않는다.
검증은 자식 returncode, HTTP, DB 상태로만 한다(프로세스 목록 조회 없음). 운영 DB·포트·launchctl 은 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sys
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from test_generation_processes import wait_status
from test_run_executor import executor_schema  # noqa: F401 -- shared local fixture

import ttakkari
from ttakkari.db import sessionmaker
from ttakkari.generation import activate
from ttakkari.models import (
    CtxDocument,
    CtxSource,
    Run,
    RunEvent,
    RuntimeControl,
    RuntimeGeneration,
    RuntimeLease,
)

CORE_REVISION = "a" * 40
SRC = Path(ttakkari.__file__).resolve().parents[1]


def child_env(**extra: str) -> dict[str, str]:
    # 자식이 pytest 와 같은 소스 트리의 ttakkari 를 import 하도록 고정한다(공유 venv 의 editable 설치와 무관).
    path = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH", "")]))
    return os.environ | {"PYTHONPATH": path, "TTAKKARI_RUNTIME_GENERATION": "true",
                         "TTAKKARI_RUN_EXECUTOR_MODE": "remote", "TTAKKARI_RUNTIME_POLL_SECONDS": "0.1",
                         "TTAKKARI_LOCAL_STT_ENABLED": "false", "TTAKKARI_DESK_WARM_POOL": "false"} | extra


async def reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 30)
    except TimeoutError:
        proc.kill()
        await proc.wait()


def redact(text: str) -> str:
    """로그를 단언 메시지·증거에 쓰기 전에 DB URL·비밀번호를 지운다."""
    url = make_url(os.environ["TTAKKARI_TEST_DB"])
    for secret in filter(None, (os.environ["TTAKKARI_TEST_DB"], url.password, os.environ.get("TTAKKARI_JWT_SECRET"))):
        text = text.replace(secret, "<redacted>")
    return re.sub(r"postgresql\S*://\S+", "<redacted-url>", text)


def tail(log: Path, size: int) -> str:
    return redact(log.read_text(errors="replace")[-size:])


OBSERVE_INTERVAL = 0.05


class CoreObserver:
    """Core child 를 표본 추출로 반복 관측한다(returncode·PID·`/api/ready` 200·generation).

    유한 표본이므로 표본 사이의 순간 중단까지 보증하지 않는다. 실패는 첫 예외를 저장했다가 check()/종료 시 전파한다.
    """

    def __init__(self, proc, client, generation: str, pid: int):
        self.proc, self.client, self.generation, self.pid = proc, client, generation, pid
        self.samples, self.failure, self.task = 0, None, None
        self.started = self.stopped = 0.0

    async def _sample(self) -> None:
        assert self.proc.returncode is None and self.proc.pid == self.pid, f"core exited: {self.proc.returncode}"
        ready = await self.client.get("/api/ready", timeout=1.0)
        assert ready.status_code == 200, f"core ready {ready.status_code}"
        assert ready.json()["generation"] == self.generation, "core generation changed"

    async def _run(self) -> None:
        try:
            while True:
                await self._sample()
                self.samples += 1
                await asyncio.sleep(OBSERVE_INTERVAL)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- 첫 실패를 저장해 await 지점에서 다시 던진다
            self.failure = error

    def check(self) -> None:
        if self.failure is not None:
            raise AssertionError(f"core observer failed after {self.samples} samples") from self.failure

    def mark(self) -> tuple[int, float]:
        self.check()
        return self.samples, time.monotonic()

    def window(self, mark: tuple[int, float], minimum: int) -> tuple[int, float]:
        """mark 이후 표본이 minimum 개 이상 통과했음을 확인하고 (표본 수, 경과 초)를 돌려준다."""
        self.check()
        count, elapsed = self.samples - mark[0], time.monotonic() - mark[1]
        assert count >= minimum, f"only {count} core samples in {elapsed:.2f}s window (need {minimum})"
        return count, elapsed

    async def __aenter__(self):
        self.started = time.monotonic()
        self.task = asyncio.create_task(self._run())
        while self.samples < 1:  # 관측이 시작되기 전에 본문이 진행되지 않게 한다
            self.check()
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.stopped = time.monotonic()
        print(f"core observer: {self.samples} samples / {self.stopped - self.started:.1f}s, interval {OBSERVE_INTERVAL}s")
        if exc_type is None:
            self.check()


@asynccontextmanager
async def core_process(tmp_path):
    """`ttakkari.cli serve` 자식. Core 는 이 컨텍스트 동안 한 번만 뜨고 교체되지 않는다."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = child_env(TTAKKARI_RUNTIME_API_URL=origin, TTAKKARI_RELEASE_REVISION=CORE_REVISION,
                    TTAKKARI_RUNTIME_SSE_DRAIN_SECONDS="1")
    log = tmp_path / "core.log"
    with log.open("wb") as output:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ttakkari.cli", "serve", "--host", "127.0.0.1", "--port", str(port),
            env=env, stdout=output, stderr=output, start_new_session=True)
        try:
            async with httpx.AsyncClient(base_url=origin, timeout=3) as client:
                for _ in range(200):
                    assert proc.returncode is None, tail(log, 2000)
                    try:
                        response = await client.get("/api/ready")
                        if response.status_code == 200:
                            yield proc, client, response.json()
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                raise AssertionError("core readiness missing: " + tail(log, 2000))
        finally:
            await reap(proc)


async def start_executor(tmp_path, name: str, revision: str | None, **extra: str):
    """`ttakkari.cli executor --activate` 자식. revision=None 이면 pinned revision 설정을 비운다."""
    log = tmp_path / f"executor-{name}.log"
    env = child_env(TTAKKARI_RELEASE_REVISION=revision or "", **extra)
    with log.open("wb") as output:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ttakkari.cli", "executor", "--activate",
            env=env, stdout=output, stderr=output, start_new_session=True)
    return proc, log


async def wait_executor_active(proc, log, revision: str) -> RuntimeGeneration:
    for _ in range(300):
        assert proc.returncode is None, tail(log, 3000)
        async with sessionmaker()() as db:
            control = await db.get(RuntimeControl, 1)
            row = await db.get(RuntimeGeneration, control.executor_generation) if control and control.executor_generation else None
            if row and row.pid == proc.pid and row.state == "active":
                assert row.role == "executor" and row.revision == revision
                return row
        await asyncio.sleep(0.05)
    raise AssertionError("executor registration missing: " + tail(log, 3000))


async def stop_executor(proc) -> int:
    proc.terminate()
    return await asyncio.wait_for(proc.wait(), 30)


async def submit(core, auth, session_id: str, prompt: str) -> str:
    response = await core.post(f"/api/sessions/{session_id}/runs", headers=auth, json={"prompt": prompt})
    assert response.status_code == 202, response.text
    return response.json()["id"]


async def db_status(run_id: str) -> str:
    async with sessionmaker()() as db:
        return (await db.get(Run, uuid.UUID(run_id))).status


async def assert_core_untouched(proc, core, core_generation: str, core_pid: int) -> None:
    assert proc.returncode is None and proc.pid == core_pid
    ready = await core.get("/api/ready")
    assert ready.status_code == 200 and ready.json()["generation"] == core_generation
    async with sessionmaker()() as db:
        row = await db.get(RuntimeGeneration, uuid.UUID(core_generation))
        assert row.pid == core_pid and row.role == "api" and row.revision == CORE_REVISION and row.state == "active"


async def assert_stays_queued(run_id: str, core, auth, seconds: float = 1.5) -> None:
    for _ in range(int(seconds / 0.1)):
        assert await db_status(run_id) == "queued"
        assert (await core.get(f"/api/runs/{run_id}", headers=auth)).json()["status"] == "queued"
        await asyncio.sleep(0.1)


def test_child_commands_are_real_ttakkari_entrypoints():
    # 가짜 스크립트가 아니라 이 소스 트리의 ttakkari 패키지를 import 하는 자식인지 고정한다.
    assert (SRC / "ttakkari" / "cli.py").is_file() and (SRC / "ttakkari" / "executor.py").is_file()
    assert child_env()["PYTHONPATH"].split(os.pathsep)[0] == str(SRC)
    url = make_url(os.environ["TTAKKARI_TEST_DB"])
    # 로컬 pytest 는 ttakkari_test, 배포 release 단계는 후보별 ttakkari_gen_<hex> 를 쓴다. 운영 DB 만 막는다.
    assert url.host in {"127.0.0.1", "localhost", "::1"}
    assert url.database.startswith(("ttakkari_test", "ttakkari_gen_"))


@pytest.mark.usefixtures("executor_schema")
async def test_executor_replacement_keeps_core_and_accepted_work(tmp_path, new_session, auth):
    """실행부 종료→새 revision 기동 동안 Core 는 같은 PID·HTTP 정상, 접수된 작업은 새 실행부가 완료한다."""
    _, sess = await new_session()
    async with core_process(tmp_path) as (core_proc, core, ready), AsyncExitStack() as stack:
        core_pid, core_generation = core_proc.pid, ready["generation"]
        await activate(uuid.UUID(core_generation), expected=None)
        # 교체 시작 전부터 테스트 끝까지 Core 를 표본 관측한다. 종료 시 관측 실패를 await 해 전파한다.
        obs = await stack.enter_async_context(CoreObserver(core_proc, core, core_generation, core_pid))
        old, old_log = await start_executor(tmp_path, "old", "e" * 40)
        new = None
        try:
            first_row = await wait_executor_active(old, old_log, "e" * 40)
            # 기준선: 구 실행부가 Core 가 접수한 작업을 완료한다.
            baseline = await submit(core, auth, sess["id"], "baseline before replacement")
            await wait_status(core, baseline, auth, "succeeded")

            replacement = obs.mark()
            assert await stop_executor(old) == 0
            await assert_core_untouched(core_proc, core, core_generation, core_pid)

            # 실행부가 없는 동안에도 Core 는 작업을 접수해 DB 에 queued 로 남긴다.
            accepted = await submit(core, auth, sess["id"], "accepted while executor is down")
            async with sessionmaker()() as db:
                run = await db.get(Run, uuid.UUID(accepted))
                assert run.status == "queued" and run.pid is None and run.exit_code is None
            await assert_stays_queued(accepted, core, auth)
            await assert_core_untouched(core_proc, core, core_generation, core_pid)

            boot = obs.mark()
            new, new_log = await start_executor(tmp_path, "new", "f" * 40)
            second_row = await wait_executor_active(new, new_log, "f" * 40)
            registered = obs.window(boot, 3)  # 새 실행부 기동·등록 구간에 표본 0회로 통과하지 못하게 한다
            assert second_row.id != first_row.id and second_row.pid == new.pid != old.pid
            done = await wait_status(core, accepted, auth, "succeeded")
            assert done["status"] == "succeeded"
            async with sessionmaker()() as db:
                run = await db.get(Run, uuid.UUID(accepted))
                assert run.status == "succeeded" and run.exit_code == 0
                assert (await db.get(RuntimeGeneration, first_row.id)).state == "offline"
                assert (await db.get(RuntimeControl, 1)).executor_generation == second_row.id
            await assert_core_untouched(core_proc, core, core_generation, core_pid)
            assert new.returncode is None
            whole = obs.window(replacement, 20)
            print(f"replacement window: boot->active {registered[0]} samples/{registered[1]:.2f}s, "
                  f"stop->done {whole[0]} samples/{whole[1]:.2f}s")
        finally:
            for proc in (old, new):
                if proc is not None:
                    await reap(proc)


@pytest.mark.usefixtures("executor_schema")
async def test_failed_executor_start_is_isolated_and_rollback_resumes_work(tmp_path, new_session, auth):
    """잘못된 새 실행부 기동은 Core·살아 있는 실행부에 영향이 없고, 이전 revision 으로 rollback 하면 처리가 재개된다."""
    _, sess = await new_session()
    async with core_process(tmp_path) as (core_proc, core, ready), AsyncExitStack() as stack:
        core_pid, core_generation = core_proc.pid, ready["generation"]
        await activate(uuid.UUID(core_generation), expected=None)
        # 교체 시작 전부터 테스트 끝까지 Core 를 표본 관측한다. 종료 시 관측 실패를 await 해 전파한다.
        obs = await stack.enter_async_context(CoreObserver(core_proc, core, core_generation, core_pid))
        good, good_log = await start_executor(tmp_path, "good", "e" * 40)
        started = [good]
        try:
            good_row = await wait_executor_active(good, good_log, "e" * 40)

            # 1) 살아 있는 실행부가 있을 때 새 revision 이 뜨려 하면 기존 소유자 잠금으로 거절된다.
            mark = obs.mark()
            dup, dup_log = await start_executor(tmp_path, "dup", "f" * 40)
            started.append(dup)
            assert await asyncio.wait_for(dup.wait(), 30) != 0
            assert "already has a live owner" in tail(dup_log, 20000)
            dup_window = obs.window(mark, 3)
            assert good.returncode is None
            async with sessionmaker()() as db:
                assert (await db.get(RuntimeControl, 1)).executor_generation == good_row.id
                assert (await db.get(RuntimeGeneration, good_row.id)).state == "active"
            alive = await submit(core, auth, sess["id"], "still served by the previous executor")
            await wait_status(core, alive, auth, "succeeded")
            await assert_core_untouched(core_proc, core, core_generation, core_pid)

            # 2) 이전 실행부를 내린 뒤 잘못된 설정(pinned revision 없음, 접속 불가 DB)의 새 실행부는 기동 실패한다.
            assert await stop_executor(good) == 0
            accepted = await submit(core, auth, sess["id"], "accepted before failed rollout")
            fail_windows = []
            broken_db = make_url(os.environ["TTAKKARI_TEST_DB"]).set(port=1).render_as_string(hide_password=False)
            for name, revision, extra in (("norev", None, {}), ("baddb", "f" * 40, {"TTAKKARI_DATABASE_URL": broken_db})):
                mark = obs.mark()
                bad, bad_log = await start_executor(tmp_path, name, revision, **extra)
                started.append(bad)
                assert await asyncio.wait_for(bad.wait(), 60) != 0
                text = tail(bad_log, 20000)
                if name == "norev":
                    assert "pinned immutable release revision" in text
                else:
                    # 접속 불가 DB 여야 한다. import 오류 등 다른 기동 실패가 green 이 되지 않게 원인을 고정한다.
                    assert "ConnectionRefusedError" in text and "Connect call failed" in text
                    # 이 소스 트리의 제품 register 경로가 DB 접속을 시도하다 실패했다(다른 체크아웃 import 도 거른다).
                    assert str(SRC / "ttakkari" / "executor.py") in text
                    assert not re.search(r"ImportError|ModuleNotFoundError|SyntaxError", text)
                fail_windows.append((name, obs.window(mark, 3)))
                await assert_core_untouched(core_proc, core, core_generation, core_pid)
                await assert_stays_queued(accepted, core, auth, seconds=0.5)
            async with sessionmaker()() as db:
                workers = list(await db.scalars(select(RuntimeGeneration.id).where(
                    RuntimeGeneration.role == "executor", RuntimeGeneration.revision == "f" * 40)))
                assert workers == []  # 실패한 revision 은 등록조차 남기지 않았다

            # 3) 이전 정상 revision 으로 rollback 하면 유실 없이 접수 작업을 완료한다.
            mark = obs.mark()
            back, back_log = await start_executor(tmp_path, "rollback", "e" * 40)
            started.append(back)
            back_row = await wait_executor_active(back, back_log, "e" * 40)
            rollback_window = obs.window(mark, 3)
            assert back_row.id != good_row.id and back_row.pid == back.pid
            await wait_status(core, accepted, auth, "succeeded")
            after = await submit(core, auth, sess["id"], "new work after rollback")
            await wait_status(core, after, auth, "succeeded")
            async with sessionmaker()() as db:
                assert (await db.get(Run, uuid.UUID(accepted))).exit_code == 0
            await assert_core_untouched(core_proc, core, core_generation, core_pid)
            print(f"failed-start windows: dup {dup_window[0]} samples/{dup_window[1]:.2f}s; "
                  + "; ".join(f"{n} {w[0]}/{w[1]:.2f}s" for n, w in fail_windows)
                  + f"; rollback boot->active {rollback_window[0]}/{rollback_window[1]:.2f}s")
        finally:
            for proc in started:
                await reap(proc)


# ---- P4 확장: 실행 중 교체 / register 후 crash / 다른 큐. 시나리오마다 독립 Core·DB 상태로 돈다.

@asynccontextmanager
async def observed_core(tmp_path):
    async with core_process(tmp_path) as (proc, core, ready), AsyncExitStack() as stack:
        generation = ready["generation"]
        await activate(uuid.UUID(generation), expected=None)
        obs = await stack.enter_async_context(CoreObserver(proc, core, generation, proc.pid))
        yield proc, core, generation, obs


async def poll(predicate, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := await predicate():
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


async def run_row(run_id: str) -> Run:
    async with sessionmaker()() as db:
        return await db.get(Run, uuid.UUID(run_id))


async def lease_count(run_id: str) -> int:
    async with sessionmaker()() as db:
        return await db.scalar(select(func.count()).select_from(RuntimeLease).where(
            RuntimeLease.run_id == uuid.UUID(run_id)))


async def generation_state(row_id) -> str:
    async with sessionmaker()() as db:
        return (await db.get(RuntimeGeneration, row_id)).state


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # 이 테스트의 Run 이 기록한 PID 하나만 신호 0 으로 확인한다(프로세스 목록 조회 아님)
    except ProcessLookupError:
        return False
    return True


async def reconcile(tmp_path, name: str, *flags: str) -> tuple[int, dict]:
    """제품 CLI `executor-reconcile`(운영자 소유 회수 경로)을 자식으로 실행하고 JSON 결과를 읽는다."""
    log = tmp_path / f"reconcile-{name}.log"
    with log.open("wb") as output:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ttakkari.cli", "executor-reconcile", *flags,
            env=child_env(), stdout=output, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        code = await asyncio.wait_for(proc.wait(), 120)
    text = log.read_text(errors="replace")
    start = text.find('{"mode"')
    assert start >= 0, tail(log, 2000)
    return code, json.loads(text[start:].splitlines()[0])


@pytest.mark.usefixtures("executor_schema")
async def test_sigterm_during_running_run_drains_then_new_executor_takes_over(tmp_path, new_session, auth):
    """계약(§전환·drain 5): SIGTERM 은 새 claim 을 닫고 진행 Run 의 자연 완료를 기다린 뒤 exit 0 한다."""
    _, sess = await new_session()
    async with observed_core(tmp_path) as (core_proc, core, core_generation, obs):
        old, old_log = await start_executor(tmp_path, "old", "e" * 40)
        started = [old]
        try:
            old_row = await wait_executor_active(old, old_log, "e" * 40)
            running = await submit(core, auth, sess["id"], "HOLD8 running while executor receives SIGTERM")
            before = await wait_status(core, running, auth, "running")
            assert await lease_count(running) == 1

            mark = obs.mark()
            old.terminate()
            await poll(lambda: _is_state(old_row.id, "draining"), 10, "executor draining state")
            assert old.returncode is None  # 진행 Run 이 있으면 종료하지 않고 기다린다
            during = await run_row(running)
            assert during.status == "running" and during.pid == before["pid"] and pid_alive(before["pid"])

            # claim-stop: drain 중 접수된 새 작업은 소유자가 없어 queued 로 남는다.
            queued = await submit(core, auth, sess["id"], "accepted while old executor drains")
            dup, dup_log = await start_executor(tmp_path, "dup", "f" * 40)
            started.append(dup)
            assert await asyncio.wait_for(dup.wait(), 30) != 0
            assert "already has a live owner" in tail(dup_log, 20000)
            assert old.returncode is None and (await run_row(running)).status == "running"
            assert await db_status(queued) == "queued"

            # 진행 Run 은 같은 엔진 프로세스로 자연 완료하고, 그 뒤에야 실행부가 exit 0 한다.
            assert await asyncio.wait_for(old.wait(), 60) == 0
            done = await run_row(running)
            assert done.status == "succeeded" and done.exit_code == 0 and done.pid == before["pid"]
            assert await lease_count(running) == 0
            assert await db_status(queued) == "queued"  # 실행부가 없으니 아직 처리되지 않았다
            await assert_core_untouched(core_proc, core, core_generation, core_proc.pid)

            new, new_log = await start_executor(tmp_path, "new", "f" * 40)
            started.append(new)
            new_row = await wait_executor_active(new, new_log, "f" * 40)
            assert new_row.id != old_row.id
            await wait_status(core, queued, auth, "succeeded")
            window = obs.window(mark, 40)
            print(f"sigterm-drain window: {window[0]} samples/{window[1]:.2f}s")
        finally:
            for proc in started:
                await reap(proc)


async def _is_state(row_id, state: str) -> bool:
    return await generation_state(row_id) == state


async def crash_executor_with_orphan(tmp_path, core, auth, sess, obs, started):
    """실행부를 띄워 Run 하나를 claim 시킨 뒤 SIGKILL 한다. (orphan run id, 엔진 PID, 죽은 실행부 행)를 돌려준다."""
    old, old_log = await start_executor(tmp_path, "old", "e" * 40)
    started.append(old)
    old_row = await wait_executor_active(old, old_log, "e" * 40)
    orphan = await submit(core, auth, sess["id"], "SLEEP orphaned by executor crash")
    engine_pid = (await wait_status(core, orphan, auth, "running"))["pid"]
    assert await lease_count(orphan) == 1

    async def engine_started():
        # running 직후에 죽이면 fake 엔진이 출력 중 파이프가 끊겨 같이 죽는 경합이 생긴다. init 이벤트를 기다린다.
        async with sessionmaker()() as db:
            return await db.scalar(select(func.count()).select_from(RunEvent).where(
                RunEvent.run_id == uuid.UUID(orphan), RunEvent.type == "agent.session"))

    await poll(engine_started, 10, "engine init event")
    await asyncio.sleep(0.5)
    old.kill()
    assert await asyncio.wait_for(old.wait(), 30) == -9
    await asyncio.sleep(1.0)
    return orphan, engine_pid, old_row


@pytest.mark.skipif(bool(os.environ.get("TTAKKARI_RUN_ID")),
                    reason="제품 CLI 가 agent Run 환경의 executor-reconcile --apply 를 거절한다(운영자 소유 경로). 가드를 우회하지 않는다")
@pytest.mark.usefixtures("executor_schema")
async def test_operator_reconcile_releases_sigkill_orphan_and_new_executor_resumes(tmp_path, new_session, auth):
    """운영자 shell 에서만 실행된다: reconcile --apply 가 orphan Run 을 interrupted 로 풀고 새 실행부가 처리를 재개한다."""
    _, sess = await new_session()
    async with observed_core(tmp_path) as (_core_proc, core, _core_generation, obs):
        started: list = []
        engine_pid = None
        try:
            mark = obs.mark()
            orphan, engine_pid, _ = await crash_executor_with_orphan(tmp_path, core, auth, sess, obs, started)
            code, applied = await reconcile(tmp_path, "apply", "--apply")
            row = next(r for r in applied["leases"] if r["run_id"] == orphan)
            assert code == 0 and applied["reconciled"] == 1 and row["action"] == "interrupted_and_released"
            released = await run_row(orphan)
            assert released.status == "interrupted" and released.pid is None
            assert "executor orphan reconciled" in released.error
            assert await lease_count(orphan) == 0
            await poll(lambda: _gone(engine_pid), 15, "orphan engine exit")

            new, new_log = await start_executor(tmp_path, "new", "f" * 40)
            started.append(new)
            await wait_executor_active(new, new_log, "f" * 40)
            fresh = await submit(core, auth, sess["id"], "work after orphan reconcile")
            await wait_status(core, fresh, auth, "succeeded")
            assert (await run_row(orphan)).status == "interrupted"  # 자동 재실행되지 않는다
            window = obs.window(mark, 40)
            print(f"sigkill-reconcile window: {window[0]} samples/{window[1]:.2f}s")
        finally:
            for proc in started:
                await reap(proc)
            if engine_pid is not None and pid_alive(engine_pid):
                os.kill(engine_pid, signal.SIGKILL)


@pytest.mark.usefixtures("executor_schema")
async def test_sigkill_after_claim_keeps_ownership_and_blocks_new_executor(tmp_path, new_session, auth):
    """register·claim 후 SIGKILL: 소유권 자동 만료·회수는 없다. 새 실행부는 거절되고 dry-run 만 orphan 을 보고한다."""
    _, sess = await new_session()
    async with observed_core(tmp_path) as (core_proc, core, core_generation, obs):
        started: list = []
        engine_pid = None
        try:
            mark = obs.mark()
            orphan, engine_pid, old_row = await crash_executor_with_orphan(tmp_path, core, auth, sess, obs, started)
            crashed = await run_row(orphan)
            assert crashed.status == "running" and crashed.pid == engine_pid and pid_alive(engine_pid)
            assert await lease_count(orphan) == 1
            assert await generation_state(old_row.id) == "active"  # 죽은 소유자도 시간 경과로 만료되지 않는다
            await assert_core_untouched(core_proc, core, core_generation, core_proc.pid)

            # 새 실행부는 기존 lease 가 있어 활성화를 거절한다(새 lease/fence 로 우회하지 않는다).
            blocked, blocked_log = await start_executor(tmp_path, "blocked", "f" * 40)
            started.append(blocked)
            assert await asyncio.wait_for(blocked.wait(), 60) != 0
            assert "existing lease requires drain or orphan reconciliation" in tail(blocked_log, 20000)
            assert (await run_row(orphan)).status == "running"
            assert await generation_state(old_row.id) == "active"

            # 제품의 회수 경로는 운영자 소유 reconcile 이다. dry-run 은 증거만 보고하고 아무것도 바꾸지 않는다.
            code, dry = await reconcile(tmp_path, "dry", "--dry-run")
            row = next(r for r in dry["leases"] if r["run_id"] == orphan)
            assert code == 0 and dry["mode"] == "dry-run" and dry["reconciled"] == 0
            assert row["disposition"] == "process_group_live" and row["action"] == "would_reconcile"
            assert (await run_row(orphan)).status == "running" and await lease_count(orphan) == 1
            assert pid_alive(engine_pid)
            window = obs.window(mark, 40)
            print(f"sigkill-blocked window: {window[0]} samples/{window[1]:.2f}s")
        finally:
            for proc in started:
                await reap(proc)
            if engine_pid is not None and pid_alive(engine_pid):
                os.kill(engine_pid, signal.SIGKILL)


async def _gone(pid: int) -> bool:
    return not pid_alive(pid)


@pytest.fixture
def pause_inprocess_consumers():
    # conftest 의 세션 lifespan 이 pytest 프로세스 안에서 background_consumers 를 이미 돌린다. 멈추지 않으면 그
    # 소비자가 Core 가 기록한 요청을 실행부보다 먼저 가져가 이 여정이 실행부 증거가 되지 못한다.
    from ttakkari import background_consumers
    background_consumers.consumers.pause_claiming()
    try:
        yield
    finally:
        background_consumers.consumers.resume_claiming()


@pytest.mark.usefixtures("executor_schema", "pause_inprocess_consumers")
async def test_idle_replacement_consumes_context_index_request(tmp_path, make_dir, auth):
    """다른 큐: Core 가 기록한 컨텍스트 색인 요청(hash embedder, 모델 없음)을 새 실행부가 소비한다."""
    root = make_dir()
    (root / "note.md").write_text("# executor index journey\n\nrecorded by core, consumed by executor\n")
    async with observed_core(tmp_path) as (core_proc, core, core_generation, obs):
        old, old_log = await start_executor(tmp_path, "old", "e" * 40)
        new = None
        try:
            await wait_executor_active(old, old_log, "e" * 40)
            mark = obs.mark()
            assert await stop_executor(old) == 0

            response = await core.post("/api/assistant/sources", headers=auth, json={"path": str(root)})
            assert response.status_code == 201, response.text
            sid = uuid.UUID(response.json()["id"])

            async def source() -> CtxSource:
                async with sessionmaker()() as db:
                    return await db.get(CtxSource, sid)

            for _ in range(15):  # 실행부가 없는 동안 요청은 DB 에 남고 처리되지 않는다
                src = await source()
                assert src.index_requested_at is not None and src.index_started_at is None
                assert src.last_indexed_at is None
                await asyncio.sleep(0.1)
            await assert_core_untouched(core_proc, core, core_generation, core_proc.pid)

            new, new_log = await start_executor(tmp_path, "new", "f" * 40)
            await wait_executor_active(new, new_log, "f" * 40)

            async def indexed():
                src = await source()
                # last_indexed_at 이 먼저 기록되고 실행권(index_started_at)은 release 에서 나중에 풀린다.
                return src if src.last_indexed_at is not None and src.index_started_at is None else None

            src = await poll(indexed, 30, "context index completion")
            assert src.index_requested_at is None and src.index_started_at is None and src.last_error is None
            async with sessionmaker()() as db:
                documents = await db.scalar(select(func.count()).select_from(CtxDocument).where(
                    CtxDocument.source_id == sid))
            assert documents >= 1
            window = obs.window(mark, 20)
            print(f"context-index window: {window[0]} samples/{window[1]:.2f}s")
        finally:
            for proc in (old, new):
                if proc is not None:
                    await reap(proc)
