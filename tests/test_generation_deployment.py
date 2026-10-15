from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from ttakkari.config import get_settings
from ttakkari.deployment import JobStore


def test_generation_queue_accepts_new_revision_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    store = JobStore(tmp_path)
    first = store.request("a" * 40)
    second = store.request("b" * 40)
    assert first["id"] != second["id"]
    assert store.request("b" * 40)["id"] == second["id"]
    assert len([j for j in store.all() if j["status"] == "requested"]) == 2


def test_release_archive_does_not_follow_later_checkout_changes(tmp_path, make_git_dir):
    from conftest import git

    from ttakkari.generation_deployment import Releases
    source = make_git_dir()
    (source / "deploy").mkdir()
    (source / "deploy/generation-contract.json").write_text(json.dumps({"protocol": 1,
        "schema_revision": "0030_runtime_generations", "online_migration_from": []}))
    (source / "version.txt").write_text("old code")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "generation release")
    sha = git(source, "rev-parse", "HEAD")
    releases = Releases(source, tmp_path)
    path = releases.archive(sha)
    (source / "version.txt").write_text("new code while old Run is alive")
    assert releases.archive(sha) == path
    assert (path / "version.txt").read_text() == "old code"
    with pytest.raises(ValueError):
        releases.archive("../../escape")


class FakeSlots:
    def __init__(self):
        self.rows = {}
        self.route = None
        self.stopped = []
        self.fail_switch = False

    async def start(self, slot, release, revision):
        self.rows[slot] = {"generation": str(uuid.uuid4()), "revision": revision, "protocol": 1,
                           "ready": True, "state": "standby", "drain_ready": False, "owned_runs": 0}
        return self.rows[slot]

    async def snapshot(self, slot):
        return self.rows.get(slot)

    async def switch(self, slot):
        if self.fail_switch and slot == 1:
            raise RuntimeError("proxy failed")
        self.route = slot

    async def verify(self, revision):
        assert self.rows[self.route]["revision"] == revision

    async def stop(self, slot):
        self.stopped.append(slot)
        self.rows.pop(slot, None)


async def test_controller_waits_to_retire_old_generation_without_restarting_it(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "a" * 40)
    old["state"] = "active"
    slots.route = 0
    store = JobStore(tmp_path)
    job = store.request("b" * 40)
    active = uuid.UUID(old["generation"])
    transitions = []

    async def state():
        return active

    async def switch(gid, *, expected):
        nonlocal active
        assert active == expected
        active = gid
        transitions.append(gid)

    async def prepare(_job):
        return Path("candidate")

    c = GenerationController(store, slots=slots)
    monkeypatch.setattr(c, "active", state)
    monkeypatch.setattr(c, "activate", switch)
    monkeypatch.setattr(c, "prepare", prepare)
    await c.process(job)
    assert store.get(job["id"])["status"] == "draining"
    assert slots.route == 1 and not slots.stopped
    assert len(transitions) == 1
    old["drain_ready"] = True
    await c.process(store.get(job["id"]))
    assert store.get(job["id"])["status"] == "succeeded"
    assert slots.stopped == [0] and len(transitions) == 1


async def test_failed_proxy_switch_rolls_back_new_claim_owner_without_killing_candidate(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "a" * 40)
    old["state"] = "active"
    slots.route = 0
    store = JobStore(tmp_path)
    job = store.request("b" * 40)
    active = uuid.UUID(old["generation"])

    async def state():
        return active

    async def switch(gid, *, expected):
        nonlocal active
        assert active == expected
        active = gid

    async def prepare(_job):
        return Path("candidate")

    c = GenerationController(store, slots=slots)
    monkeypatch.setattr(c, "active", state)
    monkeypatch.setattr(c, "activate", switch)
    monkeypatch.setattr(c, "prepare", prepare)
    slots.fail_switch = True
    await c.process(job)
    assert active == uuid.UUID(old["generation"]) and slots.route == 0
    assert store.get(job["id"])["status"] == "rolling_back"
    assert not slots.stopped  # It might already own a Run; drain first.
    slots.rows[1]["drain_ready"] = True
    await c.process(store.get(job["id"]))
    assert store.get(job["id"])["status"] == "failed"
    assert slots.stopped == [1]


def test_unstarted_ancestor_deployments_are_coalesced(tmp_path, make_git_dir, monkeypatch):
    from conftest import git
    source = make_git_dir()
    (source / "deploy").mkdir()
    (source / "deploy/generation-contract.json").write_text('{"protocol":1,"schema_revision":"0030_runtime_generations","online_migration_from":[]}')
    git(source, "add", "-A")
    git(source, "commit", "-qm", "contract")
    settings = get_settings()
    monkeypatch.setattr(settings, "deploy_strategy", "generations")
    monkeypatch.setattr(settings, "deploy_source_root", source)
    first_sha = git(source, "rev-parse", "HEAD")
    store = JobStore(tmp_path)
    first = store.request(first_sha)
    (source / "README.md").write_text("next cumulative change")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "next")
    second = store.request(git(source, "rev-parse", "HEAD"))
    assert store.get(first["id"])["status"] == "superseded"
    assert store.get(first["id"])["superseded_by"] == second["id"]


async def test_controller_recovers_a_crash_after_old_slot_was_retired(tmp_path, monkeypatch):
    import asyncio

    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "a" * 40)
    old["state"] = "active"
    old["drain_ready"] = True
    slots.route = 0
    store = JobStore(tmp_path)
    job = store.request("b" * 40)
    active = uuid.UUID(old["generation"])
    c = GenerationController(store, slots=slots)

    async def state():
        return active

    async def activate(gid, *, expected):
        nonlocal active
        assert active == expected
        active = gid

    async def prepare(_job):
        return Path("candidate")

    original_stop = slots.stop

    async def crash_after_stop(slot):
        await original_stop(slot)
        raise asyncio.CancelledError()

    async def retired(slot, _generation):
        return slot in slots.stopped

    monkeypatch.setattr(c, "active", state)
    monkeypatch.setattr(c, "activate", activate)
    monkeypatch.setattr(c, "prepare", prepare)
    monkeypatch.setattr(slots, "stop", crash_after_stop)
    monkeypatch.setattr(slots, "retired", retired, raising=False)
    with pytest.raises(asyncio.CancelledError):
        await c.process(job)
    assert store.get(job["id"])["status"] == "retiring"
    await c.process(store.get(job["id"]))
    assert store.get(job["id"])["status"] == "succeeded"
    assert slots.route == 1 and slots.stopped == [0]


async def test_superseded_reservation_is_not_started_by_a_stale_controller(tmp_path, monkeypatch):
    from ttakkari.deployment import Controller
    from ttakkari.generation_deployment import GenerationController
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    c = Controller(tmp_path)
    job = c.store.request("a" * 40)
    c.store.update(job["id"], status="superseded", superseded_by="new-job")
    started = []

    async def spy(self, job):
        started.append(job["id"])

    monkeypatch.setattr(GenerationController, "process", spy)
    await c.process(job)
    assert started == []
    assert c.store.get(job["id"])["status"] == "superseded"


async def test_release_build_pins_browser_mcp_runtime_without_install_scripts(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    release = tmp_path / "release"
    (release / ".browser-runtime").mkdir(parents=True)
    (release / ".browser-runtime/package-lock.json").write_text("{}")
    store = JobStore(tmp_path / "deploy")
    job = store.request("a" * 40)
    controller = GenerationController(store)
    calls = []

    async def command(_job, _release, *args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(controller, "command", command)
    await controller.environment(job, release)
    assert calls[0][:3] == ("uv", "sync", "--frozen")
    assert any(args[0:2] == ("npm", "ci") and "--ignore-scripts" in args for args in calls)
    calls.clear()
    await controller.environment(job, release)
    assert calls == []  # Never mutate an already prepared, potentially live venv/runtime.


def test_archive_test_environment_uses_candidate_revision_without_requiring_git(monkeypatch):
    from ttakkari.generation_deployment import test_environment
    monkeypatch.setenv("TTAKKARI_RELEASE_REVISION", "a" * 40)
    monkeypatch.setenv("TTAKKARI_RUNTIME_GENERATION", "true")
    monkeypatch.setenv("TTAKKARI_DEPLOY_SOURCE_ROOT", "/mutable-checkout")
    env = test_environment("b" * 40, "postgresql+asyncpg://localhost/isolated")
    assert env["TTAKKARI_RELEASE_REVISION"] == "b" * 40
    assert env["TTAKKARI_TEST_DB"].endswith("/isolated")
    assert "TTAKKARI_RUNTIME_GENERATION" not in env
    assert "TTAKKARI_DEPLOY_SOURCE_ROOT" not in env
