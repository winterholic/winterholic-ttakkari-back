from __future__ import annotations

import json
import sys
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


def test_qwen_usage_and_resource_history_migrations_are_allowed_online():
    from ttakkari.generation_deployment import Releases
    source = Path(__file__).resolve().parents[1]
    contract = Releases.contract(source)
    assert contract["schema_revision"] == "0040_ops_event_receipts"
    assert "0039_ops_log_issues" in contract["online_migration_from"]
    assert "0038_qwen_flash_usage" in contract["online_migration_from"]
    assert "0034_knowledge_ledger" in contract["online_migration_from"]
    assert "0036_system_resource_samples" in contract["online_migration_from"]
    assert "0035_qwen_token_usage" in contract["online_migration_from"]
    assert "0037_worker_routing" in contract["online_migration_from"]


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


async def test_overlapping_deployments_use_third_slot_and_retire_without_rerouting(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "a" * 40)
    old.update(state="active", owned_runs=1)
    slots.route = 0
    store = JobStore(tmp_path)
    active = uuid.UUID(old["generation"])
    control = GenerationController(store, slots=slots)

    async def state():
        return active

    async def activate(gid, *, expected):
        nonlocal active
        assert active == expected
        active = gid

    async def prepare(_):
        return Path("candidate")

    monkeypatch.setattr(control, "active", state)
    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)
    first = store.request("b" * 40)
    await control.process(first)
    assert first["status"] == "draining" and slots.route == 1
    second = store.request("c" * 40)
    await control.process(second)
    assert second["status"] == "draining" and slots.route == 2
    latest_owner = active
    third = store.request("d" * 40)
    await control.process(third)
    assert third["status"] == "waiting_slot" and not slots.stopped
    await control.process(store.get(first["id"]))
    assert slots.route == 2 and active == latest_owner
    old["drain_ready"] = True
    await control.process(store.get(first["id"]))
    assert store.get(first["id"])["status"] == "succeeded"
    assert slots.stopped == [0] and slots.route == 2
    assert (tmp_path / "current").read_text().strip() == "c" * 40
    slots.rows[1]["drain_ready"] = True
    await control.process(store.get(second["id"]))
    assert slots.route == 2 and active == latest_owner
    third = store.get(third["id"])
    await control.process(third)
    assert third["status"] == "draining" and slots.route == 0


async def test_old_drain_cannot_stop_a_reused_slot_or_roll_back_latest(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    reused = await slots.start(0, Path("unused"), "d" * 40)
    reused["drain_ready"] = True
    latest = await slots.start(2, Path("unused"), "c" * 40)
    slots.route = 2
    store = JobStore(tmp_path)
    job = store.request("b" * 40)
    job.update(store.update(job["id"], status="draining", previous_slot=0, candidate_slot=1,
        previous_generation=str(uuid.uuid4()), candidate_generation=str(uuid.uuid4()), previous_revision="a" * 40))
    control = GenerationController(store, slots=slots)

    async def active():
        return uuid.UUID(latest["generation"])

    monkeypatch.setattr(control, "active", active)
    await control.process(job)
    assert job["status"] == "recovery_pending" and "identity" in job["error"]
    assert not slots.stopped and slots.route == 2


async def test_old_rollback_cannot_replace_a_newer_active_generation(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    previous = await slots.start(0, Path("unused"), "a" * 40)
    latest = await slots.start(2, Path("unused"), "c" * 40)
    slots.route = 2
    store = JobStore(tmp_path)
    job = store.request("b" * 40)
    job.update(store.update(job["id"], status="rolling_back", previous_slot=0, candidate_slot=1,
        previous_generation=previous["generation"], candidate_generation=str(uuid.uuid4())))
    control = GenerationController(store, slots=slots)

    async def active():
        return uuid.UUID(latest["generation"])

    monkeypatch.setattr(control, "active", active)
    await control.process(job)
    assert job["status"] == "recovery_pending" and "stale rollback" in job["error"]
    assert not slots.stopped and slots.route == 2


async def test_supervisor_loop_advances_next_job_while_old_generation_drains(tmp_path, monkeypatch):
    import asyncio

    from ttakkari import deployment
    from ttakkari.generation_deployment import GenerationController
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(deployment, "revision", lambda: "a" * 40)
    external = deployment.Controller(tmp_path)
    first = external.store.request("a" * 40)
    external.store.update(first["id"], status="draining")
    second = external.store.request("b" * 40)
    calls = []

    async def reconcile(_):
        pass

    async def process(job):
        calls.append(job["id"])
        if job["id"] == second["id"]:
            raise asyncio.CancelledError()

    monkeypatch.setattr(GenerationController, "reconcile_services", reconcile)
    monkeypatch.setattr(external, "process", process)
    with pytest.raises(asyncio.CancelledError):
        await external.serve()
    assert calls == [first["id"], second["id"]]


async def test_supervisor_upgrade_is_forbidden_inside_a_run(tmp_path, monkeypatch):
    from ttakkari.deployment import Controller
    from ttakkari.generation_deployment import upgrade_supervisor
    monkeypatch.setenv("TTAKKARI_RUN_ID", "any-run")
    with pytest.raises(RuntimeError, match="external"):
        await upgrade_supervisor(Controller(tmp_path), "a" * 40)
    assert not (tmp_path / "supervisor.json").exists()


async def test_external_supervisor_upgrade_requires_verified_current_release(tmp_path, monkeypatch):
    from ttakkari import deployment, generation_deployment
    from ttakkari.generation_bootstrap import Services
    monkeypatch.delenv("TTAKKARI_RUN_ID", raising=False)
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    external = deployment.Controller(tmp_path)
    sha = "a" * 40
    job = external.store.request(sha)
    slots = FakeSlots()
    row = await slots.start(1, Path("unused"), sha)
    slots.route = 1
    control = generation_deployment.GenerationController(external.store, slots=slots)

    async def active():
        return uuid.UUID(row["generation"])

    async def info(gid):
        return {"id": gid, "revision": sha, "api_url": slots.origin(1)}

    control.active, control.generation_info = active, info
    monkeypatch.setattr(generation_deployment, "GenerationController", lambda _: control)
    calls = []

    async def pin(self, release, revision):
        calls.append((release, revision))

    async def launch(*args):
        calls.append(args)

    monkeypatch.setattr(Services, "pin_supervisor", pin)
    monkeypatch.setattr(slots, "_launchctl", launch, raising=False)
    with pytest.raises(RuntimeError, match="verified"):
        await generation_deployment.upgrade_supervisor(external, sha)
    assert calls == []
    release = tmp_path / "releases" / sha
    (release / "deploy").mkdir(parents=True)
    (release / ".venv/bin").mkdir(parents=True)
    (release / ".venv/bin/python").touch()
    (release / ".release.json").write_text(json.dumps({"revision": sha}))
    (release / "deploy/generation-contract.json").write_text(json.dumps({
        "protocol": 1, "schema_revision": "0033_weather_analysis_engine", "online_migration_from": []}))
    external.store.update(job["id"], status="succeeded", release_path=str(release))
    result = await generation_deployment.upgrade_supervisor(external, sha)
    assert calls[0] == (release, sha)
    assert calls[1][0:2] == ("kickstart", "-k")
    assert result["status"] == "requested" and result["revision"] == sha


def test_controller_schedules_drains_and_next_deployment_in_same_pass(tmp_path, monkeypatch):
    from ttakkari.deployment import Controller
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    control = Controller(tmp_path)
    first = control.store.request("a" * 40)
    control.store.update(first["id"], status="draining")
    second = control.store.request("b" * 40)
    third = control.store.request("c" * 40)
    assert [j["id"] for j in control.pending_jobs()] == [first["id"], second["id"]]
    control.store.update(first["id"], status="recovery_pending")
    assert [j["id"] for j in control.pending_jobs()] == [first["id"]]
    assert third["status"] == "requested"


async def test_third_slot_proxy_config_is_persisted(tmp_path, monkeypatch):
    import httpx

    from ttakkari.generation_deployment import Slots
    loaded = {}

    def respond(request):
        if request.method == "POST":
            loaded.update(json.loads(request.content))
        return httpx.Response(200, json=loaded)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    slots = Slots(tmp_path)
    await slots.switch(2)
    assert slots.persisted_slot() == 2
    assert Slots.origin(2) == "http://127.0.0.1:8793"


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


async def test_release_command_failure_includes_safe_stage_and_log_tail(tmp_path):
    from ttakkari.generation_deployment import GenerationController

    store = JobStore(tmp_path / "deploy")
    job = store.request("a" * 40)
    controller = GenerationController(store)
    command = "import sys; print('last diagnostic'); print('token=secret-value'); sys.exit(7)"

    with pytest.raises(RuntimeError) as exc_info:
        await controller.command(
            job, tmp_path, sys.executable, "-c", command,
            stage="pytest", env={"TTAKKARI_API_TOKEN": "secret-value"},
        )

    message = str(exc_info.value)
    assert "pytest" in message
    assert "last diagnostic" in message
    assert "secret-value" not in message
    assert "REDACTED" in message


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


@pytest.mark.parametrize("target_slot", [0, 2])
async def test_supervisor_recreates_active_slot_and_rechecks_its_boot_identity(tmp_path, monkeypatch, target_slot):
    from ttakkari.generation_deployment import GenerationController
    slots = FakeSlots()
    active_id = uuid.uuid4()
    control = GenerationController(JobStore(tmp_path), slots=slots)

    async def active():
        return active_id

    async def info(gid):
        return {"id": gid, "revision": "a" * 40, "api_url": slots.origin(target_slot)}

    original_start = slots.start

    async def restarted(slot, release, revision):
        nonlocal active_id
        row = await original_start(slot, release, revision)
        active_id = uuid.UUID(row["generation"])
        return row

    async def current_slot():
        return slots.route

    async def ensure_proxy(slot):
        assert slot == target_slot

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
    assert slots.route == target_slot and len(slots.rows) == 1


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
