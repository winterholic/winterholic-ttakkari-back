"""배포 감독 하드닝 회귀 테스트(DB 불필요).

작업 트리가 없는 이 샌드박스에서도 돌 수 있게 파일·서브프로세스만 쓴다.
DB를 쓰는 전체 스위트는 배포 게이트 pytest에서 함께 검증한다.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

from ttakkari.config import get_settings
from ttakkari.deployment import JobStore, resolve_source_root


def test_resolve_source_root_rejects_a_release_like_path(tmp_path, monkeypatch):
    release_like = tmp_path / "releases" / ("b" * 40)
    release_like.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "deploy_source_root", release_like)
    with pytest.raises(RuntimeError, match="Git 작업 트리"):
        resolve_source_root()


def test_resolve_source_root_returns_the_repo_top_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "deploy_source_root", None)
    try:
        result = resolve_source_root()
    except RuntimeError:
        pytest.skip("release archive has no git checkout")
    assert result.resolve() == Path(__file__).resolve().parents[1]


def test_release_archive_rejects_a_non_git_source(tmp_path):
    from ttakkari.generation_deployment import Releases
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="Git 작업 트리"):
        Releases(plain, tmp_path).archive("a" * 40)


async def test_pin_supervisor_rejects_a_release_path_source_and_keeps_the_old_marker(
    tmp_path, monkeypatch,
):
    import plistlib

    from ttakkari import deployment
    from ttakkari.generation_bootstrap import Services
    controller = deployment.Controller(tmp_path / "state")
    host = Services(controller)
    host.launch_agents = tmp_path / "LaunchAgents"
    host.launch_agents.mkdir()
    (controller.store.root / "services").mkdir()
    plist = host.launch_agents / f"{deployment.LABEL}.plist"
    plist.write_bytes(plistlib.dumps({"Label": deployment.LABEL}))
    marker = controller.store.root / "supervisor.json"
    marker.write_text(json.dumps({"revision": "a" * 40, "release": "/releases/a",
                                  "source": "/repo/back"}))
    # 릴리스 디렉터리에서 감독 교체를 돌리면 소스가 .git 없는 경로로 잡힌다.
    release_like = tmp_path / "releases" / ("b" * 40)
    release_like.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "deploy_source_root", release_like)
    with pytest.raises(RuntimeError, match="Git 작업 트리"):
        await host.pin_supervisor(release_like, "b" * 40)
    assert json.loads(marker.read_text())["source"] == "/repo/back"
    assert plistlib.loads(plist.read_bytes()) == {"Label": deployment.LABEL}


def test_old_contract_defaults_to_a_two_slot_supervisor():
    from ttakkari.generation_deployment import (
        required_supervisor_capabilities,
        supervisor_satisfies,
    )
    assert required_supervisor_capabilities({"protocol": 1}) == {"protocol": 1, "slots": 2}
    assert supervisor_satisfies({"protocol": 1})


class FakeSlots:
    def __init__(self):
        self.rows = {}
        self.route = None
        self.stopped = []

    @staticmethod
    def origin(slot):
        return f"http://127.0.0.1:{8791 + slot}"

    async def snapshot(self, slot):
        return self.rows.get(slot)

    async def start(self, slot, release, revision):
        self.rows[slot] = {"generation": str(uuid.uuid4()), "revision": revision, "protocol": 1,
                           "ready": True, "state": "standby", "drain_ready": False}
        return self.rows[slot]

    async def switch(self, slot):
        self.route = slot

    async def verify(self, revision):
        assert self.rows[self.route]["revision"] == revision

    async def stop(self, slot):
        self.stopped.append(slot)
        self.rows.pop(slot, None)


def _three_slot_demanding_contract(*args, **kwargs):
    return {"protocol": 1, "schema_revision": "x", "online_migration_from": [],
            "supervisor": {"protocol": 1, "slots": 3}}


async def test_mismatched_supervisor_reports_upgrade_required(tmp_path, monkeypatch):
    from ttakkari import generation
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(generation, "PORTS", (8791, 8792))
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(gd, "contract_for_revision", _three_slot_demanding_contract)
    store = JobStore(tmp_path)
    job = store.request("a" * 40)
    control = gd.GenerationController(store, slots=FakeSlots())

    async def no_active():
        return None

    monkeypatch.setattr(control, "active", no_active)
    await control.process(job)
    record = store.get(job["id"])
    assert record["status"] == "supervisor_upgrade_required"
    assert record["error_stage"] == "supervisor"
    assert "supervisor" in record["error_summary"]


async def test_capable_release_triggers_exactly_one_supervisor_upgrade(tmp_path, monkeypatch):
    from ttakkari import generation
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(generation, "PORTS", (8791, 8792))
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(gd, "contract_for_revision", _three_slot_demanding_contract)
    store = JobStore(tmp_path)
    active_revision = "c" * 40
    release = tmp_path / "releases" / active_revision
    (release / "deploy").mkdir(parents=True)
    (release / "deploy/generation-contract.json").write_text(json.dumps(
        {"protocol": 1, "schema_revision": "x", "online_migration_from": [],
         "supervisor": {"protocol": 1, "slots": 3}}))
    receipt = store.request(active_revision)
    store.update(receipt["id"], status="draining", release_path=str(release))
    job = store.request("a" * 40)
    control = gd.GenerationController(store, slots=FakeSlots())
    active_id = uuid.uuid4()
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=active_id))

    async def info(gid):
        assert gid == active_id
        return {"id": gid, "revision": active_revision, "api_url": "http://127.0.0.1:8791"}

    monkeypatch.setattr(control, "generation_info", info)
    calls = []

    async def upgrade(controller, revision):
        calls.append(revision)
        return {"revision": revision, "status": "requested"}

    monkeypatch.setattr(gd, "upgrade_supervisor", upgrade)
    await control.process(job)
    assert calls == [active_revision]
    assert store.get(job["id"])["supervisor_upgrade_attempted_for"] == active_revision
    # 같은 활성 리비전에는 다시 kickstart하지 않고 상태만 보고한다.
    await control.process(store.get(job["id"]))
    assert calls == [active_revision]
    assert store.get(job["id"])["status"] == "supervisor_upgrade_required"


async def test_slow_prepare_does_not_block_another_deployment(tmp_path, monkeypatch):
    from ttakkari import generation_deployment as gd
    from ttakkari.generation_deployment import _PREPARE_TASKS
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(gd, "contract_for_revision",
                        lambda source, rev: {"protocol": 1, "schema_revision": "x",
                                             "online_migration_from": []})
    store = JobStore(tmp_path)
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "0" * 40)
    holder = {"active": uuid.UUID(old["generation"])}
    control = gd.GenerationController(store, slots=slots)
    gate = asyncio.Event()

    async def active():
        return holder["active"]

    async def activate(gid, *, expected):
        assert holder["active"] == expected
        holder["active"] = gid

    async def prepare(job):
        if job["revision"] == "a" * 40:
            await gate.wait()
            return Path("candidate-a")
        return Path("candidate-b")

    monkeypatch.setattr(control, "active", active)
    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)
    slow = store.request("a" * 40)
    fast = store.request("b" * 40)
    try:
        await control.process(slow)
        record = store.get(slow["id"])
        assert record["status"] == "requested"
        assert record["prepare_stage"] == "queued" and record["prepare_heartbeat"]
        await control.process(fast)
        assert store.get(fast["id"])["status"] == "draining"
        gate.set()
        await control.process(store.get(slow["id"]))
        assert store.get(slow["id"])["status"] == "draining"
    finally:
        gate.set()
        for task in list(_PREPARE_TASKS.values()):
            task.cancel()


async def test_command_failure_carries_stage_and_exit_code(tmp_path):
    from ttakkari.generation_deployment import GenerationController, StageError
    store = JobStore(tmp_path)
    control = GenerationController(store, slots=FakeSlots())
    job = store.request("a" * 40)
    with pytest.raises(StageError) as excinfo:
        await control.command(job, tmp_path, sys.executable, "-c",
                              "import sys; sys.exit(7)", stage="pytest")
    assert (excinfo.value.stage, excinfo.value.exit_code) == ("pytest", 7)
    record = store.get(job["id"])
    assert record["prepare_stage"] == "pytest" and record["prepare_heartbeat"]


async def test_command_timeout_is_a_structured_stage_error(tmp_path):
    from ttakkari.generation_deployment import GenerationController, StageError
    store = JobStore(tmp_path)
    control = GenerationController(store, slots=FakeSlots())
    job = store.request("a" * 40)
    with pytest.raises(StageError) as excinfo:
        await control.command(job, tmp_path, sys.executable, "-c",
                              "import time; time.sleep(30)", stage="uv sync", timeout=0.2)
    assert excinfo.value.stage == "uv sync" and excinfo.value.exit_code is None


async def test_launchd_failure_is_recorded_as_launch_not_pytest(tmp_path):
    from ttakkari.generation_deployment import GenerationController

    class DeadSlots(FakeSlots):
        async def start(self, slot, release, revision):
            raise RuntimeError("launchd operation failed: No such process")

    store = JobStore(tmp_path)
    control = GenerationController(store, slots=DeadSlots())
    job = store.request("a" * 40)
    store.update(job["id"], status="starting", candidate_slot=1,
                 previous_slot=0, previous_generation=str(uuid.uuid4()),
                 release_path=str(tmp_path))
    await control.process(store.get(job["id"]))
    record = store.get(job["id"])
    assert record["status"] == "failed"
    assert record["error_stage"] == "launch"
    assert record["error_summary"].startswith("launch:")
    assert "pytest" not in record["error_stage"]


async def test_unreadable_contract_falls_through_to_prepare_in_one_pass(tmp_path, monkeypatch):
    # 계약을 읽지 못하면(테스트용 가짜 SHA) 게이트를 건너뛰고 기존 흐름대로 간다.
    from ttakkari.generation_deployment import GenerationController
    store = JobStore(tmp_path)
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "0" * 40)
    holder = {"active": uuid.UUID(old["generation"])}
    control = GenerationController(store, slots=slots)
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=holder["active"]))

    async def activate(gid, *, expected):
        assert holder["active"] == expected
        holder["active"] = gid

    async def prepare(_job):
        return Path("candidate")

    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)
    job = store.request("b" * 40)
    await control.process(job)
    assert store.get(job["id"])["status"] == "draining"
    assert slots.route == 1


def test_restart_gate_is_reentrant_in_the_same_process(tmp_path):
    from ttakkari.deploy_gate import RestartGate
    gate = RestartGate(tmp_path / "backend-restart.lock")
    outer = gate._acquire()
    assert outer is not None
    inner = RestartGate(tmp_path / "backend-restart.lock")._acquire()
    assert inner is not None
    RestartGate._release(inner)
    RestartGate._release(outer)
    again = gate._acquire()
    assert again is not None
    RestartGate._release(again)


async def test_supervisor_upgrade_works_while_the_deploy_lock_is_held(tmp_path, monkeypatch):
    # 실제 루프 경로: serve가 backend-restart.lock을 쥔 채 process()를 부른다.
    from ttakkari import generation
    from ttakkari import generation_deployment as gd
    from ttakkari.deploy_gate import RestartGate
    monkeypatch.setattr(generation, "PORTS", (8791, 8792))
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(gd, "contract_for_revision", _three_slot_demanding_contract)
    monkeypatch.delenv("TTAKKARI_RUN_ID", raising=False)
    store = JobStore(tmp_path)
    active_revision = "c" * 40
    release = tmp_path / "releases" / active_revision
    (release / "deploy").mkdir(parents=True)
    (release / "deploy/generation-contract.json").write_text(json.dumps(
        {"protocol": 1, "schema_revision": "x", "online_migration_from": [],
         "supervisor": {"protocol": 1, "slots": 3}}))
    (release / ".venv/bin").mkdir(parents=True)
    (release / ".venv/bin/python").touch()
    (release / ".release.json").write_text(json.dumps({"revision": active_revision}))
    receipt = store.request(active_revision)
    store.update(receipt["id"], status="draining", release_path=str(release))
    job = store.request("a" * 40)
    slots = FakeSlots()
    control = gd.GenerationController(store, slots=slots)
    active_id = uuid.uuid4()
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=active_id))

    async def info(gid):
        return {"id": gid, "revision": active_revision, "api_url": "http://127.0.0.1:8791"}

    monkeypatch.setattr(control, "generation_info", info)
    # 갱신 대상 릴리스가 현재 ready 소유자임을 증명한다.
    row = await slots.start(0, release, active_revision)
    row["generation"] = str(active_id)
    slots.route = 0
    monkeypatch.setattr(gd, "GenerationController", lambda _: control)
    calls = []

    async def pin(self, release_path, revision):
        calls.append(("pin", revision))

    async def launch(*args):
        calls.append(args)

    from ttakkari.generation_bootstrap import Services
    monkeypatch.setattr(Services, "pin_supervisor", pin)
    monkeypatch.setattr(slots, "_launchctl", launch, raising=False)
    gate = RestartGate(store.root / "backend-restart.lock")
    outer = gate._acquire()
    assert outer is not None
    try:
        await control.process(job)
    finally:
        RestartGate._release(outer)
    assert ("pin", active_revision) in calls
    record = store.get(job["id"])
    assert record["supervisor_upgrade_attempted_for"] == active_revision
    attempts = record["supervisor_upgrade_attempts"]
    assert len(attempts) == 1 and attempts[0]["ok"] is True
    assert record["supervisor_upgrade_next_retry_at"] is None


async def test_failed_supervisor_upgrade_is_persisted_with_backoff(tmp_path, monkeypatch):
    from ttakkari import generation
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(generation, "PORTS", (8791, 8792))
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(gd, "contract_for_revision", _three_slot_demanding_contract)
    store = JobStore(tmp_path)
    active_revision = "c" * 40
    release = tmp_path / "releases" / active_revision
    (release / "deploy").mkdir(parents=True)
    (release / "deploy/generation-contract.json").write_text(json.dumps(
        {"protocol": 1, "schema_revision": "x", "online_migration_from": [],
         "supervisor": {"protocol": 1, "slots": 3}}))
    receipt = store.request(active_revision)
    store.update(receipt["id"], status="draining", release_path=str(release))
    job = store.request("a" * 40)
    control = gd.GenerationController(store, slots=FakeSlots())
    active_id = uuid.uuid4()
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=active_id))

    async def info(gid):
        return {"id": gid, "revision": active_revision, "api_url": "http://127.0.0.1:8791"}

    monkeypatch.setattr(control, "generation_info", info)
    calls = []

    async def boom(controller, revision):
        calls.append(revision)
        raise RuntimeError("kickstart refused")

    monkeypatch.setattr(gd, "upgrade_supervisor", boom)
    await control.process(job)
    record = store.get(job["id"])
    assert record["status"] == "supervisor_upgrade_required"
    assert len(record["supervisor_upgrade_attempts"]) == 1
    assert record["supervisor_upgrade_attempts"][0]["error"] == "kickstart refused"
    assert record["supervisor_upgrade_next_retry_at"]
    # 백오프 중에는 다시 시도하지 않는다.
    await control.process(store.get(job["id"]))
    assert calls == [active_revision]


async def test_command_timeout_kills_grandchildren_too(tmp_path):
    import re

    from ttakkari.generation_deployment import GenerationController, StageError
    store = JobStore(tmp_path)
    control = GenerationController(store, slots=FakeSlots())
    job = store.request("a" * 40)
    script = ("import subprocess, sys, time; "
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "print(f'GRANDCHILD {p.pid}', flush=True); time.sleep(60)")
    with pytest.raises(StageError) as excinfo:
        await control.command(job, tmp_path, sys.executable, "-c", script,
                              stage="pytest", timeout=2)
    assert excinfo.value.stage == "pytest" and excinfo.value.exit_code is None
    match = re.search(r"GRANDCHILD (\d+)", (store.jobs / f"{job['id']}.log").read_text())
    assert match, "grandchild pid must be visible in the job log"
    grandchild = int(match.group(1))
    import os
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("grandchild survived the group kill")


async def test_activate_cutover_readiness_drain_failures_carry_their_stage(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController

    async def run_case(fail_at):
        store = JobStore(tmp_path / fail_at)
        slots = FakeSlots()
        old = await slots.start(0, Path("unused"), "0" * 40)
        holder = {"active": uuid.UUID(old["generation"])}
        control = GenerationController(store, slots=slots)
        monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=holder["active"]))

        async def activate(gid, *, expected):
            # 롤백 때 구 세대로의 복귀는 성공해야 원래 단계의 기록이 남는다.
            if fail_at == "activate" and str(gid) != old["generation"]:
                raise RuntimeError("fence lost")
            holder["active"] = gid

        async def switch(slot):
            if fail_at == "cutover" and slot == 1:
                raise RuntimeError("proxy load refused")
            slots.route = slot

        async def verify(revision):
            if fail_at == "readiness":
                raise RuntimeError("candidate readiness timed out")
            assert slots.rows[slots.route]["revision"] == revision

        async def prepare(_job):
            return Path("candidate")

        monkeypatch.setattr(control, "activate", activate)
        monkeypatch.setattr(slots, "switch", switch)
        monkeypatch.setattr(slots, "verify", verify)
        monkeypatch.setattr(control, "prepare", prepare)
        job = store.request("b" * 40)
        if fail_at == "drain":
            # 회수 대상 세대에 닿지 않으면 retire가 실패한다.
            slots.rows.pop(0)
            store.update(job["id"], status="draining", candidate_slot=1, previous_slot=0,
                         previous_generation=old["generation"], candidate_generation=str(uuid.uuid4()),
                         release_path=str(tmp_path))
            holder["active"] = uuid.uuid4()
        await control.process(store.get(job["id"]))
        return store.get(job["id"])

    for fail_at in ("activate", "cutover", "readiness"):
        record = await run_case(fail_at)
        assert record["error_stage"] == fail_at, fail_at
        assert record["error_command"] is None and "command is null" in record["error_summary"]
        assert record["error_exit"] is None and "exit code is null" in record["error_summary"]
    record = await run_case("drain")
    assert record["status"] == "recovery_pending" and record["error_stage"] == "drain"


async def test_identity_mismatch_is_a_launch_stage_error(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    store = JobStore(tmp_path)
    slots = FakeSlots()
    await slots.start(1, Path("unused"), "b" * 40)
    control = GenerationController(store, slots=slots)
    job = store.request("a" * 40)
    store.update(job["id"], status="starting", candidate_slot=1,
                 previous_slot=0, previous_generation=str(uuid.uuid4()),
                 release_path=str(tmp_path))
    await control.process(store.get(job["id"]))
    record = store.get(job["id"])
    assert record["status"] == "failed"
    assert record["error_stage"] == "launch"
    assert "identity mismatch" in record["error"]


async def test_prepare_db_failure_is_a_test_db_stage_error(tmp_path, monkeypatch):
    import asyncpg

    from ttakkari.generation_deployment import GenerationController, StageError
    store = JobStore(tmp_path)
    control = GenerationController(store, slots=FakeSlots())
    release = tmp_path / "release"
    (release / "deploy").mkdir(parents=True)
    (release / "deploy/generation-contract.json").write_text(json.dumps(
        {"protocol": 1, "schema_revision": "x", "online_migration_from": []}))
    (release / ".environment-built").touch()

    def archive(revision):
        return release

    async def connect(*args, **kwargs):
        raise ConnectionRefusedError("db down")

    monkeypatch.setattr(control.releases, "archive", archive)
    monkeypatch.setattr(asyncpg, "connect", connect)
    job = store.request("a" * 40)
    with pytest.raises(StageError) as excinfo:
        await control.prepare(job)
    assert excinfo.value.stage == "test-db"


async def test_mismatched_recorded_release_is_actually_reprepared(tmp_path, monkeypatch):
    # 통합 수준: 기록된 릴리스가 검사본과 어긋나면 새로 준비한 릴리스로 진행한다.
    from ttakkari.generation_deployment import GenerationController
    store = JobStore(tmp_path)
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "0" * 40)
    holder = {"active": uuid.UUID(old["generation"])}
    control = GenerationController(store, slots=slots)
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=holder["active"]))

    async def activate(gid, *, expected):
        assert holder["active"] == expected
        holder["active"] = gid

    monkeypatch.setattr(control, "activate", activate)
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / ".release.json").write_text(json.dumps({"revision": "9" * 40}))
    prepared = {"called": False}

    async def prepare(job):
        prepared["called"] = True
        fresh = tmp_path / "fresh"
        fresh.mkdir(exist_ok=True)
        (fresh / ".release.json").write_text(json.dumps({"revision": job["revision"]}))
        control.phase(job, "prepared", release_path=str(fresh),
                      tested_revision=job["revision"], tested_contract="x")
        return fresh

    monkeypatch.setattr(control, "prepare", prepare)
    job = store.request("b" * 40)
    store.update(job["id"], release_path=str(stale))
    await control.process(job)
    assert prepared["called"] is True
    record = store.get(job["id"])
    assert record["release_path"] == str(tmp_path / "fresh")
    assert record["status"] == "draining"


def test_resolve_source_root_rejects_an_unrelated_git_repo(tmp_path, monkeypatch):
    import subprocess

    from ttakkari.deployment import resolve_source_root
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    (other / "file.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=other, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=other, check=True)
    monkeypatch.setattr(get_settings(), "deploy_source_root", other)
    with pytest.raises(RuntimeError, match="백엔드 저장소"):
        resolve_source_root()


def test_resolve_source_root_accepts_a_repo_with_the_identity_file(tmp_path, monkeypatch):
    import subprocess

    from ttakkari.deployment import resolve_source_root
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "deploy").mkdir()
    (repo / "deploy/generation-contract.json").write_text("{}")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=repo, check=True)
    monkeypatch.setattr(get_settings(), "deploy_source_root", repo)
    assert resolve_source_root() == repo


def test_stale_release_binding_demands_repreparation(tmp_path):
    from ttakkari.generation_deployment import GenerationController, StageError
    store = JobStore(tmp_path)
    control = GenerationController(store, slots=FakeSlots())
    job = store.request("a" * 40)
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / ".release.json").write_text(json.dumps({"revision": "b" * 40}))
    with pytest.raises(StageError, match="re-preparation"):
        control.verify_test_binding(job, stale)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / ".release.json").write_text(json.dumps({"revision": "a" * 40}))
    control.verify_test_binding(job, fresh)
