"""P4 제품 여정: Core API 프로세스를 배포하지 않고 실행부 프로세스만 교체한다.

실제 entrypoint(`python -m ttakkari.cli serve` / `python -m ttakkari.cli executor --activate`)를 자기 자식
프로세스로 임시 loopback 포트·주입된 테스트 DB에 띄운다. 작업 유형은 Run 이고, 엔진은 conftest 가 주입한
fake_agents/fake_claude.py(TTAKKARI_CLAUDE_BIN)뿐이라 외부 모델·MCP·네트워크를 쓰지 않는다.
검증은 자식 returncode, HTTP, DB 상태로만 한다(프로세스 목록 조회 없음). 운영 DB·포트·launchctl 은 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from test_generation_processes import wait_status
from test_run_executor import executor_schema  # noqa: F401 -- shared local fixture

import ttakkari
from ttakkari.db import sessionmaker
from ttakkari.generation import activate
from ttakkari.models import Run, RuntimeControl, RuntimeGeneration

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
                    assert proc.returncode is None, log.read_text()[-2000:]
                    try:
                        response = await client.get("/api/ready")
                        if response.status_code == 200:
                            yield proc, client, response.json()
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                raise AssertionError("core readiness missing: " + log.read_text()[-2000:])
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
        assert proc.returncode is None, log.read_text()[-3000:]
        async with sessionmaker()() as db:
            control = await db.get(RuntimeControl, 1)
            row = await db.get(RuntimeGeneration, control.executor_generation) if control and control.executor_generation else None
            if row and row.pid == proc.pid and row.state == "active":
                assert row.role == "executor" and row.revision == revision
                return row
        await asyncio.sleep(0.05)
    raise AssertionError("executor registration missing: " + log.read_text()[-3000:])


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
    assert url.host in {"127.0.0.1", "localhost", "::1"} and url.database == "ttakkari_test"


@pytest.mark.usefixtures("executor_schema")
async def test_executor_replacement_keeps_core_and_accepted_work(tmp_path, new_session, auth):
    """실행부 종료→새 revision 기동 동안 Core 는 같은 PID·HTTP 정상, 접수된 작업은 새 실행부가 완료한다."""
    _, sess = await new_session()
    async with core_process(tmp_path) as (core_proc, core, ready):
        core_pid, core_generation = core_proc.pid, ready["generation"]
        await activate(uuid.UUID(core_generation), expected=None)
        old, old_log = await start_executor(tmp_path, "old", "e" * 40)
        new = None
        try:
            first_row = await wait_executor_active(old, old_log, "e" * 40)
            # 기준선: 구 실행부가 Core 가 접수한 작업을 완료한다.
            baseline = await submit(core, auth, sess["id"], "baseline before replacement")
            await wait_status(core, baseline, auth, "succeeded")

            assert await stop_executor(old) == 0
            await assert_core_untouched(core_proc, core, core_generation, core_pid)

            # 실행부가 없는 동안에도 Core 는 작업을 접수해 DB 에 queued 로 남긴다.
            accepted = await submit(core, auth, sess["id"], "accepted while executor is down")
            async with sessionmaker()() as db:
                run = await db.get(Run, uuid.UUID(accepted))
                assert run.status == "queued" and run.pid is None and run.exit_code is None
            await assert_stays_queued(accepted, core, auth)
            await assert_core_untouched(core_proc, core, core_generation, core_pid)

            new, new_log = await start_executor(tmp_path, "new", "f" * 40)
            second_row = await wait_executor_active(new, new_log, "f" * 40)
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
        finally:
            for proc in (old, new):
                if proc is not None:
                    await reap(proc)


@pytest.mark.usefixtures("executor_schema")
async def test_failed_executor_start_is_isolated_and_rollback_resumes_work(tmp_path, new_session, auth):
    """잘못된 새 실행부 기동은 Core·살아 있는 실행부에 영향이 없고, 이전 revision 으로 rollback 하면 처리가 재개된다."""
    _, sess = await new_session()
    async with core_process(tmp_path) as (core_proc, core, ready):
        core_pid, core_generation = core_proc.pid, ready["generation"]
        await activate(uuid.UUID(core_generation), expected=None)
        good, good_log = await start_executor(tmp_path, "good", "e" * 40)
        started = [good]
        try:
            good_row = await wait_executor_active(good, good_log, "e" * 40)

            # 1) 살아 있는 실행부가 있을 때 새 revision 이 뜨려 하면 기존 소유자 잠금으로 거절된다.
            dup, dup_log = await start_executor(tmp_path, "dup", "f" * 40)
            started.append(dup)
            assert await asyncio.wait_for(dup.wait(), 30) != 0
            assert "already has a live owner" in dup_log.read_text()
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
            broken_db = make_url(os.environ["TTAKKARI_TEST_DB"]).set(port=1).render_as_string(hide_password=False)
            for name, revision, extra in (("norev", None, {}), ("baddb", "f" * 40, {"TTAKKARI_DATABASE_URL": broken_db})):
                bad, bad_log = await start_executor(tmp_path, name, revision, **extra)
                started.append(bad)
                assert await asyncio.wait_for(bad.wait(), 60) != 0
                if name == "norev":
                    assert "pinned immutable release revision" in bad_log.read_text()
                await assert_core_untouched(core_proc, core, core_generation, core_pid)
                await assert_stays_queued(accepted, core, auth, seconds=0.5)
            async with sessionmaker()() as db:
                workers = list(await db.scalars(select(RuntimeGeneration.id).where(
                    RuntimeGeneration.role == "executor", RuntimeGeneration.revision == "f" * 40)))
                assert workers == []  # 실패한 revision 은 등록조차 남기지 않았다

            # 3) 이전 정상 revision 으로 rollback 하면 유실 없이 접수 작업을 완료한다.
            back, back_log = await start_executor(tmp_path, "rollback", "e" * 40)
            started.append(back)
            back_row = await wait_executor_active(back, back_log, "e" * 40)
            assert back_row.id != good_row.id and back_row.pid == back.pid
            await wait_status(core, accepted, auth, "succeeded")
            after = await submit(core, auth, sess["id"], "new work after rollback")
            await wait_status(core, after, auth, "succeeded")
            async with sessionmaker()() as db:
                assert (await db.get(Run, uuid.UUID(accepted))).exit_code == 0
            await assert_core_untouched(core_proc, core, core_generation, core_pid)
        finally:
            for proc in started:
                await reap(proc)
