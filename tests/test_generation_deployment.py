from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from ttakkari.config import get_settings
from ttakkari.deployment import JobStore

pytestmark = pytest.mark.contract


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


def test_executor_migration_follows_tts_and_uses_reserved_integration_revision():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    source = Path(__file__).resolve().parents[1]
    config = Config(str(source / "alembic.ini"))
    config.set_main_option("script_location", str(source / "alembic"))
    directory = ScriptDirectory.from_config(config)
    executor = directory.get_revision("0064_run_executor")
    assert executor.down_revision == "0063_collector_stage_items"


def test_qwen_usage_and_resource_history_migrations_are_allowed_online():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from ttakkari.generation_deployment import Releases
    source = Path(__file__).resolve().parents[1]
    config = Config(str(source / "alembic.ini"))
    config.set_main_option("script_location", str(source / "alembic"))
    directory = ScriptDirectory.from_config(config)
    head = directory.get_current_head()
    contract = Releases.contract(source)
    # head 값은 하드코딩하지 않고 도출한다. 온라인 허용 목록은 체인으로 검증해
    # 다음 마이그레이션의 online_migration_from 누락을 잡는다.
    assert contract["schema_revision"] == head
    floor = "0032_weather_briefings"
    current, chain = head, []
    while current != floor:
        down = directory.get_revision(current).down_revision
        current = down[0] if isinstance(down, tuple) else down
        chain.append(current)
    assert chain and set(chain) <= set(contract["online_migration_from"])


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
        self.executor_calls = []

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

    async def ensure_executor(self, release, revision):
        # Mirrors Slots.ensure_executor(release, revision): a release whose contract
        # does not enable the run_executor reports activation disabled, not an error.
        self.executor_calls.append((release, revision))
        return {"ready": False, "activation": "disabled"}

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
    assert [revision for _, revision in slots.executor_calls] == ["a" * 40]
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
    assert [revision for _, revision in slots.executor_calls] == ["a" * 40]


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


def _gate_controller(tmp_path, command):
    from types import SimpleNamespace

    from ttakkari.generation_deployment import GenerationController

    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    control = object.__new__(GenerationController)
    control.store = SimpleNamespace(jobs=jobs, root=tmp_path)
    control.command = command
    return control


def _toy_command(runs):
    """게이트가 만든 pytest 명령을 현재 인터프리터로 진짜 실행한다(release 폴더가 cwd)."""
    import asyncio

    from ttakkari.generation_deployment import StageError

    async def command(job, release, *args, stage="command", env=None, timeout=None):
        runs.append(args[1:])
        proc = await asyncio.create_subprocess_exec(
            sys.executable, *args[1:], cwd=release,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await proc.communicate()
        if proc.returncode:
            raise StageError(stage, args, proc.returncode, output.decode())
    return command


def _toy_project(root: Path, kinds: str) -> Path:
    """kinds 문자 하나가 시험 하나: o=통과, t=처음 한 번만 실패(흔들림), f=항상 실패. 실행 횟수를 ran_* 에 남긴다."""
    release = root / "release"
    state = root / "state"
    release.mkdir()
    state.mkdir()
    for index, kind in enumerate(kinds):
        name = f"t{index}"
        lines = ["from pathlib import Path", f"def test_{name}():",
                 f"    Path({str(state / ('ran_' + name))!r}).open('a').write('x')"]
        if kind == "t":
            marker = state / ("seen_" + name)
            lines += [f"    m = Path({str(marker)!r})", "    if not m.exists():", "        m.touch()",
                      "        assert False, 'transient'"]
        elif kind == "f":
            lines += ["    assert False, 'permanent'"]
        (release / f"test_{name}.py").write_text("\n".join(lines) + "\n")
    return release


def _executions(root: Path, count: int) -> list[int]:
    state = root / "state"
    return [len((state / f"ran_t{i}").read_text()) if (state / f"ran_t{i}").exists() else 0 for i in range(count)]


@pytest.mark.parametrize(("kinds", "passes", "executions"), [
    # 흔들린 시험 하나: 재시도로 통과하고 중단 지점 뒤(t2)까지 끝까지 실행된다.
    ("otoo", True, [1, 2, 1, 1]),
    # 흔들린 시험 뒤에 진짜 결함: 예전에는 재시도만 통과하고 t2 는 실행조차 안 된 채 게이트가 통과했다. 이제 t2 가 실행돼 실패한다.
    ("otf", False, [1, 2, 2]),
    # 서로 다른 흔들림 둘은 시험마다 한 번씩 재시도해 통과한다.
    ("toot", True, [2, 1, 1, 2]),
    # 진짜 결함은 재시도에서도 실패한다. 뒤 시험은 실행하지 않고 바로 멈춘다(빠른 실패).
    ("ofo", False, [1, 2, 0]),
])
async def test_gate_stepwise_retry_runs_everything_after_the_stop_point(tmp_path, kinds, passes, executions):
    from ttakkari.generation_deployment import StageError

    runs: list[tuple] = []
    release = _toy_project(tmp_path, kinds)
    control = _gate_controller(tmp_path, _toy_command(runs))
    if passes:
        await control.run_pytest({"id": "toy"}, release, {})
    else:
        with pytest.raises(StageError):
            await control.run_pytest({"id": "toy"}, release, {})
    assert _executions(tmp_path, len(kinds)) == executions
    assert all("--sw" in args and "-x" not in args and "--lf" not in args for args in runs)
    assert not (tmp_path / "jobs" / "toy.pytest-cache").exists()  # 작업별 캐시는 끝나면 지운다


async def test_gate_ignores_a_stale_stepwise_cache_left_in_the_release_folder(tmp_path):
    """같은 릴리스 폴더를 재사용해도 이전 실행의 중단 지점이 새 게이트에 섞여 앞부분 시험이 건너뛰어지면 안 된다."""
    runs: list[tuple] = []
    release = _toy_project(tmp_path, "ooo")
    stale = release / ".pytest_cache/v/cache"
    stale.mkdir(parents=True)
    (stale / "stepwise").write_text(json.dumps({
        "last_failed": "test_t2.py::test_t2", "last_test_count": 3, "last_cache_date_str": "2026-10-23T00:00:00"}))
    control = _gate_controller(tmp_path, _toy_command(runs))
    stale_job_cache = tmp_path / "jobs" / "toy.pytest-cache/main/v/cache"
    stale_job_cache.mkdir(parents=True)
    (stale_job_cache / "stepwise").write_text((stale / "stepwise").read_text())
    await control.run_pytest({"id": "toy"}, release, {})
    assert _executions(tmp_path, 3) == [1, 1, 1]


async def test_gate_does_not_retry_collection_errors_or_timeouts(tmp_path):
    from ttakkari.generation_deployment import StageError

    runs: list[tuple] = []
    release = _toy_project(tmp_path, "oo")
    (release / "test_broken.py").write_text("import module_that_does_not_exist_xyz\n")
    control = _gate_controller(tmp_path, _toy_command(runs))
    with pytest.raises(StageError):  # 수집 오류는 --sw 에서도 exit 2 지만 실패 시험이 기록되지 않으므로 재시도하지 않는다
        await control.run_pytest({"id": "toy"}, release, {})
    assert len(runs) == 1

    calls: list[tuple] = []

    async def timed_out(job, release, *args, stage="command", env=None, timeout=None):
        calls.append(args)
        raise StageError(stage, args, None, "timed out")

    with pytest.raises(StageError):
        await _gate_controller(tmp_path, timed_out).run_pytest({"id": "t2"}, release, {})
    assert len(calls) == 1


async def test_gate_runs_previous_failures_first_then_the_whole_suite(tmp_path):
    from ttakkari.generation_deployment import StageError

    calls: list[tuple] = []
    outcomes: list[int | None] = []

    async def command(job, release, *args, stage="command", env=None, timeout=None):
        calls.append(args)
        if outcomes and (code := outcomes.pop(0)) is not None:
            raise StageError(stage, args, code, "x")

    control = _gate_controller(tmp_path, command)
    job = {"id": "j1", "revision": "a" * 40}
    # 첫 게이트: 전체 실행이 같은 시험에서 두 번 실패 → 그 시험이 기록된다. (stepwise 캐시가 없으면 재시도 없이 실패한다)
    outcomes[:] = [2]
    with pytest.raises(StageError):
        await control.run_pytest(job, tmp_path, {})
    assert not (tmp_path / "gate-last-failed.json").exists()  # 실패 시험을 알 수 없으면 기록하지 않는다

    (tmp_path / "gate-last-failed.json").write_text(json.dumps(
        {"nodeids": ["tests/test_x.py::test_y", "-x", "--collect-only", "plain-text"]}))
    calls.clear()
    outcomes[:] = [None, None]
    await control.run_pytest({"id": "j2", "revision": "b" * 40}, tmp_path, {})
    assert calls[0][-1] == "tests/test_x.py::test_y" and "-x" not in calls[0] and "--collect-only" not in calls[0]
    assert "--sw" not in calls[0] and "--sw" in calls[1] and "not security" in calls[1]
    assert not (tmp_path / "gate-last-failed.json").exists()  # 통과하면 지운다

    # 직전 실패가 이번에도 실패하면 한 번 재시도한 뒤 전체 실행 없이 실패한다.
    (tmp_path / "gate-last-failed.json").write_text(json.dumps({"nodeids": ["tests/test_x.py::test_y"]}))
    calls.clear()
    outcomes[:] = [1, 1]
    with pytest.raises(StageError):
        await control.run_pytest({"id": "j3", "revision": "c" * 40}, tmp_path, {})
    assert len(calls) == 2 and all("--sw" not in c for c in calls)
    assert json.loads((tmp_path / "gate-last-failed.json").read_text())["nodeids"] == ["tests/test_x.py::test_y"]

    # 시험이 없어졌거나 수집되지 않으면(exit 4) 속도용 단계라 건너뛰고 전체 실행으로 간다.
    calls.clear()
    outcomes[:] = [4, None]
    await control.run_pytest({"id": "j4", "revision": "d" * 40}, tmp_path, {})
    assert len(calls) == 2 and "--sw" in calls[1]


async def test_security_tests_run_once_between_the_security_gate_and_the_full_run(tmp_path):
    """보안 묶음(-m security)은 security_gate 단계가 실행하고, 전체 실행은 -m 'not security' 로 겹치지 않는다. 합치면 전부다."""
    from ttakkari.security.gate import build_security_gate_command

    runs: list[tuple] = []
    release = tmp_path / "release"
    release.mkdir()
    (release / "pytest.ini").write_text("[pytest]\nmarkers =\n    security: s\n")
    state = tmp_path / "state"
    state.mkdir()
    for name, mark in (("sec1", "@pytest.mark.security\n"), ("sec2", "@pytest.mark.security\n"),
                       ("plain1", ""), ("plain2", "")):
        (release / f"test_{name}.py").write_text(
            "import pytest\nfrom pathlib import Path\n"
            f"{mark}def test_{name}():\n    Path({str(state / name)!r}).open('a').write('x')\n")
    command = _toy_command(runs)
    control = _gate_controller(tmp_path, command)
    await command({"id": "g"}, release, *build_security_gate_command(sys.executable, timeout=120), stage="security_gate")
    assert sorted(p.name for p in state.iterdir()) == ["sec1", "sec2"]
    await control.run_pytest({"id": "toy"}, release, {})
    assert {p.name: p.read_text() for p in state.iterdir()} == {n: "x" for n in ("sec1", "sec2", "plain1", "plain2")}


# ---- 스키마 계약: 별칭(이름이 바뀐 옛 리비전)·유휴 대기 -------------------------------------

def test_contract_aliases_must_map_retired_ids_onto_known_revisions():
    from ttakkari.generation_deployment import validate_contract
    base = {"protocol": 1, "schema_revision": "0003_c", "online_migration_from": ["0001_a", "0002_b"]}
    assert validate_contract(base | {"schema_aliases": {"0002_old": "0002_b"}})
    for bad in ({"0002_old": "9999_unknown"}, {"0002_old": "0002_old"}, ["0002_b"]):
        with pytest.raises(TypeError):
            validate_contract(base | {"schema_aliases": bad})


def test_committed_contract_aliases_are_not_real_revisions_and_resolve_to_online_range():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from ttakkari.generation_deployment import Releases
    source = Path(__file__).resolve().parents[1]
    config = Config(str(source / "alembic.ini"))
    config.set_main_option("script_location", str(source / "alembic"))
    real = {r.revision for r in ScriptDirectory.from_config(config).walk_revisions()}
    contract = Releases.contract(source)
    # 실제 리비전을 별칭으로 가리면 alembic 이 알지 못하는 상태만 고쳐 쓴다는 보증이 깨진다.
    assert not set(contract.get("schema_aliases", {})) & real
    for old, new in contract["schema_aliases"].items():
        assert new in contract["online_migration_from"]
        # 릴리스보다 오래된 감독은 schema_aliases 를 모르고 online_migration_from 만 본다.
        assert old in contract["online_migration_from"]


class _FakeDb:
    def __init__(self, version):
        self.version, self.executed = version, []  # executed: 감독이 DB 에 쓴 문장(있으면 안 된다)

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, _stmt):
        return self.version


def _contract(**extra):
    return {"protocol": 1, "schema_revision": "0003_c", "online_migration_from": ["0001_a", "0002_b"],
            "schema_aliases": {"0002_old": "0002_b"}} | extra


@pytest.mark.parametrize(("stamp", "mode"), [
    ("0003_c", "current"), ("0002_b", "online"), ("0002_old", "online"), ("0099_unknown", "idle")])
async def test_schema_plan_classifies_stamp(tmp_path, monkeypatch, stamp, mode):
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(gd, "sessionmaker", lambda: _FakeDb(stamp))
    control = gd.GenerationController(JobStore(tmp_path))
    assert await control.schema_plan(_contract()) == (mode, stamp)


async def _prepare_with(tmp_path, monkeypatch, stamp):
    """아카이브·검사·DB 를 대체한 prepare. 스키마 판정 이후의 동작만 본다."""
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    db = _FakeDb(stamp)
    monkeypatch.setattr(gd, "sessionmaker", lambda: db)
    release = tmp_path / "release"
    (release / "deploy").mkdir(parents=True)
    (release / "deploy/generation-contract.json").write_text(json.dumps(_contract()))

    class Admin:
        async def execute(self, *_):
            pass

        async def close(self):
            pass

    async def connect(*_):
        return Admin()

    async def noop(*_, **__):
        pass

    store = JobStore(tmp_path / "deploy")
    job = store.request("b" * 40)
    control = gd.GenerationController(store)
    migrations = []

    async def migrate(_job, _release, *, online):
        migrations.append(online)

    monkeypatch.setattr(gd.asyncpg, "connect", connect)
    monkeypatch.setattr(control.releases, "archive", lambda _sha: release)
    monkeypatch.setattr(control, "environment", noop)
    monkeypatch.setattr(control, "run_security_gate", noop)
    monkeypatch.setattr(control, "run_pytest", noop)
    monkeypatch.setattr(control, "migrate", migrate)
    return gd, control, store, job, db, migrations, release


async def test_retired_revision_alias_is_migrated_online_by_the_release(tmp_path, monkeypatch):
    _gd, control, store, job, db, migrations, release = await _prepare_with(tmp_path, monkeypatch, "0002_old")
    assert await control.prepare(job) == release
    # 정렬은 릴리스의 alembic/env.py 가 한다. 감독은 DB 를 고쳐 쓰지 않고 online 이행만 맡긴다.
    assert not db.executed and migrations == [True]
    assert store.get(job["id"])["status"] == "prepared"


async def test_unknown_schema_waits_for_idle_instead_of_failing_and_keeps_tested_release(tmp_path, monkeypatch):
    gd, control, store, job, db, migrations, release = await _prepare_with(tmp_path, monkeypatch, "0099_unknown")
    with pytest.raises(gd.IdleRequired):
        await control.prepare(job)
    saved = store.get(job["id"])
    assert saved["status"] == "waiting_idle" and saved["idle_required"] is True
    assert saved["release_path"] == str(release) and saved["tested_revision"] == "b" * 40
    assert saved["error_stage"] == "schema_contract" and "0099_unknown" in saved["error_summary"]
    assert not db.executed and not migrations  # 아무것도 건드리지 않는다


async def _idle_deploy(tmp_path, monkeypatch, runs):
    from ttakkari import deploy_gate, deployment
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    slots = FakeSlots()
    old = await slots.start(0, Path("old"), "a" * 40)
    old.update(state="active", owned_runs=1)
    slots.route = 0
    store = JobStore(tmp_path)
    job = store.request("b" * 40)
    store.update(job["id"], status="waiting_idle", idle_required=True, release_path="candidate",
                 tested_revision="b" * 40)
    job = store.get(job["id"])
    control = gd.GenerationController(store, slots=slots)
    active = uuid.UUID(old["generation"])
    state = {"active": active, "runs": runs, "migrations": [], "lock_free_during_cutover": None}

    async def active_fn():
        return state["active"]

    async def activate(gid, *, expected):
        state["active"] = gid

    async def count(_own):
        return state["runs"]

    async def migrate(_job, _release, *, online):
        state["migrations"].append(online)

    async def plan(_contract):
        return "idle", "0099_unknown"

    real_switch = slots.switch

    async def switch(slot):
        # 컷오버 동안 유휴 잠금이 유지돼 새 Run 접수가 끼어들지 못해야 한다.
        probe = deployment.Admission(tmp_path).acquire()
        state["lock_free_during_cutover"] = probe is not None
        if probe:
            probe.close()
        await real_switch(slot)

    monkeypatch.setattr(control, "active", active_fn)
    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "verify_test_binding", lambda *_: None)
    monkeypatch.setattr(control, "schema_plan", plan)
    monkeypatch.setattr(control, "migrate", migrate)
    monkeypatch.setattr(control.releases, "contract", lambda _p: _contract())
    monkeypatch.setattr(slots, "switch", switch)
    monkeypatch.setattr(deploy_gate, "_active_run_count", count)
    return control, store, job, state, tmp_path


async def test_waiting_idle_does_not_migrate_or_fail_while_runs_are_active(tmp_path, monkeypatch):
    control, store, job, state, _ = await _idle_deploy(tmp_path, monkeypatch, runs=2)
    await control.process(job)
    saved = store.get(job["id"])
    assert saved["status"] == "waiting_idle" and saved["active_runs"] == 2
    assert "2 active Run" in saved["error_summary"] and saved["idle_since"]
    assert state["migrations"] == [] and "candidate_slot" not in saved
    await control.process(store.get(job["id"]))  # 같은 대기 상태는 다시 쓰지 않아도 유지된다
    assert store.get(job["id"])["status"] == "waiting_idle"


async def test_waiting_idle_migrates_offline_and_cuts_over_once_runs_finish_then_releases_lock(tmp_path, monkeypatch):
    from ttakkari import deployment
    control, store, job, state, root = await _idle_deploy(tmp_path, monkeypatch, runs=1)
    await control.process(job)
    assert store.get(job["id"])["status"] == "waiting_idle"
    state["runs"] = 0
    await control.process(store.get(job["id"]))
    saved = store.get(job["id"])
    assert saved["status"] == "draining" and saved["idle_required"] is False
    assert saved["error_summary"] is None and saved["active_runs"] is None
    assert state["migrations"] == [False]  # 온라인 모드가 아닌 일반 마이그레이션
    assert state["lock_free_during_cutover"] is False
    lease = deployment.Admission(root).acquire(exclusive=True)
    assert lease is not None  # 컷오버 뒤에는 접수 잠금을 놓는다
    lease.close()


async def test_idle_migration_failure_is_recorded_as_failed_and_releases_the_lock(tmp_path, monkeypatch):
    from ttakkari import deployment
    from ttakkari.generation_deployment import StageError
    control, store, job, _state, root = await _idle_deploy(tmp_path, monkeypatch, runs=0)

    async def broken(_job, _release, *, online):
        raise StageError("migrate", None, 1, "Can't locate revision 0099_unknown")

    monkeypatch.setattr(control, "migrate", broken)
    await control.process(job)
    saved = store.get(job["id"])
    assert saved["status"] == "failed" and "0099_unknown" in saved["error"]
    lease = deployment.Admission(root).acquire(exclusive=True)
    assert lease is not None
    lease.close()


def test_newer_reservation_supersedes_a_job_waiting_for_idle(tmp_path, make_git_dir, monkeypatch):
    from conftest import git
    source = make_git_dir()
    (source / "deploy").mkdir()
    (source / "deploy/generation-contract.json").write_text(
        '{"protocol":1,"schema_revision":"0030_runtime_generations","online_migration_from":[]}')
    git(source, "add", "-A")
    git(source, "commit", "-qm", "contract")
    settings = get_settings()
    monkeypatch.setattr(settings, "deploy_strategy", "generations")
    monkeypatch.setattr(settings, "deploy_source_root", source)
    store = JobStore(tmp_path)
    waiting = store.request(git(source, "rev-parse", "HEAD"))
    store.update(waiting["id"], status="waiting_idle")
    (source / "README.md").write_text("next cumulative change")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "next")
    newer = store.request(git(source, "rev-parse", "HEAD"))
    assert store.get(waiting["id"])["status"] == "superseded"
    assert store.get(waiting["id"])["superseded_by"] == newer["id"]


def test_realign_rewrites_only_a_known_alias_and_commits():
    from sqlalchemy import create_engine, text

    from ttakkari.schema_aliases import realign
    engine = create_engine("sqlite://")
    aliases = {"0059_kb_immutability_truncate": "0059_collector_intake"}
    with engine.connect() as conn:
        assert realign(conn, aliases) is None  # alembic_version 이 아직 없는 새 DB
        assert not conn.in_transaction()  # alembic 이 외부 트랜잭션으로 오해하지 않도록 닫아 둔다
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0059_kb_immutability_truncate')"))
        conn.commit()
        assert realign(conn, aliases) == ("0059_kb_immutability_truncate", "0059_collector_intake")
    with engine.connect() as other:  # 다른 연결에서 보여야 커밋된 것이다
        assert other.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0059_collector_intake"
        assert realign(other, aliases) is None  # 이미 정렬됨
        assert not other.in_transaction()
        other.execute(text("UPDATE alembic_version SET version_num = '0099_unknown'"))
        other.commit()
        assert realign(other, aliases) is None  # 모르는 값은 건드리지 않는다
        assert other.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0099_unknown"
