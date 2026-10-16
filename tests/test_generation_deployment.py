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


def test_application_generation_contract_matches_its_migration_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from ttakkari.generation_deployment import Releases
    source = Path(__file__).resolve().parents[1]
    config = Config(str(source / "alembic.ini"))
    config.set_main_option("script_location", str(source / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert Releases.contract(source)["schema_revision"] == head, (
        "Generation deployment would skip required migrations: update the schema contract and review online compatibility"
    )


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

    @staticmethod
    def origin(slot):
        return f"http://127.0.0.1:{8791 + slot}"

    def persisted_slot(self):
        return self.route

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


async def test_external_controller_cuts_over_busy_generation_without_legacy_idle_gate(tmp_path, monkeypatch):
    from ttakkari import deployment, generation_deployment
    slots = FakeSlots()
    old = await slots.start(0, Path("old"), "a" * 40)
    old.update(state="active", owned_runs=2, drain_ready=False)
    slots.route = 0
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    external = deployment.Controller(tmp_path)
    job = external.store.request("b" * 40)
    control = generation_deployment.GenerationController(external.store, slots=slots)
    active_id = uuid.UUID(old["generation"])

    async def active():
        return active_id

    async def activate(gid, *, expected):
        nonlocal active_id
        assert active_id == expected
        active_id = gid

    async def prepare(_job):
        return Path("candidate")

    async def forbidden(*args):
        raise AssertionError("generation deployment must not wait for all Runs or restart legacy")

    monkeypatch.setattr(control, "active", active)
    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)
    monkeypatch.setattr(generation_deployment, "GenerationController", lambda _store: control)
    monkeypatch.setattr(external, "wait_idle", forbidden)
    monkeypatch.setattr(external, "restart", forbidden)
    monkeypatch.setattr(deployment, "_active_run_count", forbidden)
    await external.process(job)
    assert external.store.get(job["id"])["status"] == "draining"
    assert slots.route == 1 and not slots.stopped
    assert slots.rows[0]["owned_runs"] == 2


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


async def test_proxy_switch_persists_its_own_restart_config(tmp_path, monkeypatch):
    import httpx

    from ttakkari.generation_deployment import Slots
    loaded = {}

    def respond(request):
        if request.method == "POST":
            loaded.update(json.loads(request.content))
            return httpx.Response(200)
        return httpx.Response(200, json=loaded)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    await Slots(tmp_path).switch(1)
    saved = json.loads((tmp_path / "proxy.json").read_text())
    assert saved == loaded
    assert saved["admin"]["config"]["persist"] is False
    assert saved["apps"]["http"]["servers"]["ttakkari"]["routes"][-1]["handle"][0]["upstreams"][0]["dial"] == "127.0.0.1:8792"


async def test_supervisor_repairs_proxy_without_restarting_a_live_generation(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    row = await slots.start(1, Path("release"), "a" * 40)
    slots.route = 0
    control = GenerationController(JobStore(tmp_path), slots=slots)

    async def active():
        return uuid.UUID(row["generation"])

    async def info(gid):
        return {"id": gid, "revision": "a" * 40, "api_url": "http://127.0.0.1:8792"}

    async def current_slot():
        return slots.route

    async def ensure_proxy(slot):
        assert slot == 1

    async def forbidden(*args):
        raise AssertionError("live generation must not restart")

    monkeypatch.setattr(control, "active", active)
    monkeypatch.setattr(control, "generation_info", info, raising=False)
    monkeypatch.setattr(slots, "current_slot", current_slot, raising=False)
    monkeypatch.setattr(slots, "ensure_proxy", ensure_proxy, raising=False)
    monkeypatch.setattr(slots, "start", forbidden)
    await control.reconcile_services()
    assert slots.route == 1 and not slots.stopped
    monkeypatch.setattr(slots, "switch", forbidden)
    await control.reconcile_services()  # No reload/fsync on each supervisor poll.


async def test_supervisor_recreates_active_slot_and_rechecks_its_boot_identity(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    active_id = uuid.uuid4()
    control = GenerationController(JobStore(tmp_path), slots=slots)

    async def active():
        return active_id

    async def info(gid):
        return {"id": gid, "revision": "a" * 40, "api_url": "http://127.0.0.1:8791"}

    original_start = slots.start

    async def restarted(slot, release, revision):
        nonlocal active_id
        row = await original_start(slot, release, revision)
        active_id = uuid.UUID(row["generation"])
        return row

    async def current_slot():
        return slots.route

    async def ensure_proxy(slot):
        assert slot == 0

    monkeypatch.setattr(control, "active", active)
    monkeypatch.setattr(control, "generation_info", info, raising=False)
    monkeypatch.setattr(slots, "current_slot", current_slot, raising=False)
    monkeypatch.setattr(slots, "ensure_proxy", ensure_proxy, raising=False)
    monkeypatch.setattr(slots, "start", restarted)
    release = tmp_path / "releases" / ("a" * 40) / "deploy"
    release.mkdir(parents=True)
    (release / "generation-contract.json").write_text(json.dumps({
        "protocol": 1, "schema_revision": "0030_runtime_generations", "online_migration_from": []}))
    await control.reconcile_services()
    assert slots.route == 0 and len(slots.rows) == 1


async def test_proxy_service_uses_its_own_persisted_config(tmp_path, monkeypatch):
    import plistlib

    from ttakkari.generation_deployment import Slots
    slots = Slots(tmp_path)
    booted = False

    async def current():
        return 1 if booted else None

    async def launch(*args):
        nonlocal booted
        assert args[0] == "bootstrap"
        service = plistlib.loads(Path(args[-1]).read_bytes())
        assert service["ProgramArguments"][-2:] == ["--config", str(tmp_path / "proxy.json")]
        assert service["KeepAlive"] is True
        assert service["ThrottleInterval"] == 10
        assert "LaunchAgents" not in args[-1]
        booted = True

    monkeypatch.setattr(slots, "current_slot", current)
    monkeypatch.setattr(slots, "_launchctl", launch)
    monkeypatch.setattr("ttakkari.generation_deployment.shutil.which", lambda _: "/fake/caddy")
    await slots.ensure_proxy(1)
    assert booted and slots.persisted_slot() == 1


async def test_supervisor_never_routes_to_an_unowned_boot(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    await slots.start(0, Path("release"), "a" * 40)
    control = GenerationController(JobStore(tmp_path), slots=slots)

    async def active():
        return uuid.uuid4()

    async def info(_):
        return {"revision": "a" * 40, "api_url": slots.origin(0)}

    monkeypatch.setattr(control, "active", active)
    monkeypatch.setattr(control, "generation_info", info)
    with pytest.raises(RuntimeError, match="identity"):
        await control.reconcile_services()
    assert slots.route is None
