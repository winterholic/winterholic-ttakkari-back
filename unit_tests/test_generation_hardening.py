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
    assert resolve_source_root().resolve() == Path(__file__).resolve().parents[1]


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
