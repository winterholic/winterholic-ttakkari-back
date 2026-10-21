"""SEC2-GATE-01: 보안 회귀 묶음 배포 게이트 단위/통합 테스트.

1. 보안 테스트가 실패하는 후보는 거부된다.
2. 통과하는 후보는 정상 진행된다.
3. strict xfail은 통과로 취급되어 배포를 불필요하게 막지 않는다.
4. 게이트 실행 자체 오류(타임아웃, 프로세스 에러 등) 시 fail-closed로 안전하게 거절된다.
5. 운영 DB 접근 거부와 격리 테스트 DB 동작을 검증한다.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari.deployment import JobStore
from ttakkari.generation_deployment import GenerationController, StageError
from ttakkari.security.gate import build_security_gate_command

pytestmark = [pytest.mark.security, pytest.mark.contract]


def test_build_security_gate_command_structure():
    cmd = build_security_gate_command("/opt/venv/bin/python", timeout=150)
    assert cmd[:6] == ("/opt/venv/bin/python", "-m", "pytest", "-q", "-m", "security")
    assert "-o" in cmd
    idx = cmd.index("-o")
    assert cmd[idx + 1] == "xfail_strict=false"
    assert "--timeout=150" in cmd
    assert "--lf" not in cmd

    # retry command (--lf)
    retry_cmd = build_security_gate_command("python", last_failed=True)
    assert "--lf" in retry_cmd


@pytest.mark.asyncio
async def test_security_gate_retry_shares_one_300_second_budget(tmp_path, monkeypatch):
    """Failed-test retry consumes the original gate deadline instead of getting a fresh 300s."""
    from ttakkari import generation_deployment

    store = JobStore(tmp_path / "deploy")
    controller = GenerationController(store)
    now = [10.0]
    monkeypatch.setattr(generation_deployment.time, "monotonic", lambda: now[0])
    calls = []

    async def command(job, release, *args, stage="command", env=None, timeout=None):
        calls.append((args, timeout))
        if len(calls) == 1:
            now[0] += 200
            raise StageError("security_gate", args, 1, "one failed test")

    monkeypatch.setattr(controller, "command", command)
    await controller.run_security_gate({"id": "job"}, tmp_path, {})

    assert calls[0][1] == 300
    assert calls[1][1] == 100
    assert "--lf" in calls[1][0]


@pytest.mark.asyncio
async def test_legacy_security_gate_uses_same_bounded_test_command(tmp_path, monkeypatch):
    from ttakkari.deployment import Controller

    controller = Controller(tmp_path)
    calls = []

    async def capture(job, *args, env=None, timeout=None):
        calls.append((args, timeout))

    monkeypatch.setattr(controller, "command", capture)
    await controller.run_security_gate({"id": "legacy-job"}, {})

    args, timeout = calls[0]
    assert "--timeout=120" in args
    assert "-o" in args and "xfail_strict=false" in args
    assert timeout == 300


@pytest.mark.asyncio
async def test_legacy_command_timeout_terminates_its_process_group(tmp_path, monkeypatch):
    from ttakkari import generation_deployment
    from ttakkari.deployment import Controller

    controller = Controller(tmp_path)
    terminated = []

    class SlowProcess:
        pid = 12345

        async def wait(self):
            await asyncio.sleep(1)

    async def terminate(proc):
        terminated.append(proc.pid)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: asyncio.sleep(0, result=SlowProcess()))
    monkeypatch.setattr(generation_deployment, "_terminate_group", terminate)
    with pytest.raises(RuntimeError, match="시간 초과"):
        await controller.command({"id": "legacy-timeout"}, "slow-command", timeout=0.001)
    assert terminated == [12345]


def test_production_db_is_rejected_by_conftest():
    """운영 DB 이름(ttakkari)으로 테스트가 실행되는 것을 차단하는지 확인."""
    import conftest

    assert hasattr(conftest, "TEST_DB")
    conftest.assert_test_db_isolated(conftest.TEST_DB)


def test_production_db_with_query_parameters_is_still_rejected():
    import conftest

    with pytest.raises(AssertionError, match="운영 DB"):
        conftest.assert_test_db_isolated(
            "postgresql+asyncpg://localhost/ttakkari?sslmode=require"
        )


@pytest.mark.asyncio
async def test_security_gate_rejects_failing_candidate(tmp_path, monkeypatch):
    """보안 테스트가 실패하는 가짜 후보는 거부되고, 에러 단계와 요약이 남는다."""
    import uuid

    store = JobStore(tmp_path / "deploy")
    job = store.request("a" * 40)
    controller = GenerationController(store)

    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True)
    (release_dir / ".release.json").write_text('{"revision": "' + "a" * 40 + '"}')

    # 가짜 archive / environment
    monkeypatch.setattr(controller.releases, "archive", lambda _rev: release_dir)
    monkeypatch.setattr(controller, "environment", AsyncMock())

    # 보안 게이트 실패 흉내 (pytest 실패)
    async def failing_command(job, release, *args, stage="command", env=None, timeout=None):
        if stage == "security_gate":
            raise StageError("security_gate", args, 1, "security regression tests failed: 2 failures")

    monkeypatch.setattr(controller, "command", failing_command)

    # test-db 생성도 모킹
    executed_sql = []

    class DummyAdmin:
        async def execute(self, q):
            executed_sql.append(q)
        async def close(self):
            pass

    import asyncpg
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=DummyAdmin()))

    # prepare 호출 시 StageError 발생 확인
    with pytest.raises(StageError) as exc_info:
        await controller.prepare(job)

    assert exc_info.value.stage == "security_gate"
    assert "security regression tests failed" in str(exc_info.value)
    expected_db = "ttakkari_gen_" + uuid.UUID(job["id"]).hex
    assert executed_sql == [f'CREATE DATABASE "{expected_db}"', f'DROP DATABASE "{expected_db}"']

    # process() 흐름에서 record_failure에 의해 job status가 failed로 변경되는지 검증
    active_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    slots = MagicMock()
    async def mock_snapshot(slot):
        if slot == 0:
            return {"generation": str(active_uuid), "revision": "0" * 40}
        return None
    slots.snapshot = AsyncMock(side_effect=mock_snapshot)
    slots.origin = lambda i: f"http://127.0.0.1:{8787 + i}"
    controller.slots = slots
    monkeypatch.setattr(controller, "active", AsyncMock(return_value=active_uuid))
    monkeypatch.setattr(controller, "ensure_supervisor_capable", AsyncMock(return_value=True))

    # 첫 패스는 background task를 띄우고, 태스크 완료 후 둘째 패스가 결과를 수습한다.
    await controller.process(job)
    await asyncio.sleep(0.05)
    await controller.process(job)

    updated = store.get(job["id"])
    assert updated["status"] == "failed"
    assert updated["error_stage"] == "security_gate"
    assert "security regression tests failed" in (updated.get("error_summary") or "")


@pytest.mark.asyncio
async def test_security_gate_allows_passing_candidate(tmp_path, monkeypatch):
    """보안 게이트를 통과한 후보는 prepared로 진행된다."""
    import uuid

    store = JobStore(tmp_path / "deploy")
    job = store.request("b" * 40, skip_tests=True)
    controller = GenerationController(store)

    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True)
    (release_dir / ".release.json").write_text('{"revision": "' + "b" * 40 + '"}')
    (release_dir / "deploy").mkdir()
    (release_dir / "deploy/generation-contract.json").write_text(
        '{"protocol": 1, "schema_revision": "0001_initial", "online_migration_from": []}'
    )

    monkeypatch.setattr(controller.releases, "archive", lambda _rev: release_dir)
    monkeypatch.setattr(controller, "environment", AsyncMock())

    commands_run = []

    async def passing_command(job, release, *args, stage="command", env=None, timeout=None):
        commands_run.append((stage, args, env))

    monkeypatch.setattr(controller, "command", passing_command)

    executed_sql = []

    class DummyAdmin:
        async def execute(self, q):
            executed_sql.append(q)
        async def close(self):
            pass

    import asyncpg
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=DummyAdmin()))

    # DB schema 일치 모킹
    from sqlalchemy.ext.asyncio import AsyncSession
    monkeypatch.setattr(AsyncSession, "scalar", AsyncMock(return_value="0001_initial"))

    result = await controller.prepare(job)
    assert result == release_dir

    stages = [s for s, _, _ in commands_run]
    assert "security_gate" in stages
    expected_db = "ttakkari_gen_" + uuid.UUID(job["id"]).hex
    gate_env = next(env for stage, _, env in commands_run if stage == "security_gate")
    assert gate_env["TTAKKARI_TEST_DB"].endswith("/" + expected_db)
    assert not gate_env["TTAKKARI_TEST_DB"].endswith("/ttakkari")
    assert executed_sql == [f'CREATE DATABASE "{expected_db}"', f'DROP DATABASE "{expected_db}"']

    updated = store.get(job["id"])
    assert updated["status"] == "prepared"
    assert updated["tested_revision"] == "b" * 40


@pytest.mark.asyncio
async def test_security_gate_fail_closed_on_execution_error(tmp_path, monkeypatch):
    """게이트 실행 자체에 시스템 오류(바이너리 부재, 실행 실패 등)가 발생하면 fail-closed로 거부된다."""
    store = JobStore(tmp_path / "deploy")
    job = store.request("c" * 40)
    controller = GenerationController(store)

    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True)
    (release_dir / ".release.json").write_text('{"revision": "' + "c" * 40 + '"}')

    monkeypatch.setattr(controller.releases, "archive", lambda _rev: release_dir)
    monkeypatch.setattr(controller, "environment", AsyncMock())

    async def broken_command(job, release, *args, stage="command", env=None, timeout=None):
        if stage == "security_gate":
            raise StageError("security_gate", args, None, "could not start (FileNotFoundError): python not found")

    monkeypatch.setattr(controller, "command", broken_command)

    class DummyAdmin:
        async def execute(self, q):
            pass
        async def close(self):
            pass

    import asyncpg
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=DummyAdmin()))

    with pytest.raises(StageError) as exc_info:
        await controller.prepare(job)

    assert exc_info.value.stage == "security_gate"
    assert "FileNotFoundError" in str(exc_info.value)
