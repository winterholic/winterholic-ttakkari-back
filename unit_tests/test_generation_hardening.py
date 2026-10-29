"""배포 감독 하드닝 회귀 테스트(DB 불필요).

작업 트리가 없는 이 샌드박스에서도 돌 수 있게 파일·서브프로세스만 쓴다.
DB를 쓰는 전체 스위트는 배포 게이트 pytest에서 함께 검증한다.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from git_isolation import git_argv, isolated_git_env, scrub_process_env

from ttakkari.config import get_settings
from ttakkari.deployment import JobStore, resolve_source_root

# 이 파일만 단독으로 도는 pre-push 게이트에서도 훅의 GIT_DIR 이 resolve_source_root 의 git 호출에 새지 않게 한다.
scrub_process_env()


def _init_repo(root: Path, rel: str, text: str = "x") -> None:
    """임시 저장소를 명시적 -C 와 격리 환경으로 만든다. 호출 환경의 GIT_* 는 받지 않는다."""
    def run(*args: str) -> None:
        subprocess.run(git_argv(root, *args), env=isolated_git_env(), check=True, capture_output=True)
    run("init", "-q")
    (root / rel).write_text(text)
    run("add", "-A")
    run("commit", "-qm", "x")


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


@pytest.fixture(autouse=True)
def _no_previous_tts_queue(monkeypatch):
    """이 파일의 대상은 배포 단계 기록이다. 구세대 TTS 큐 조회는 DB 없이 빈 큐로 둔다(전용 시험: tests/test_tts_first_transition.py)."""
    from ttakkari.generation_deployment import GenerationController

    async def empty(_self, _previous):
        return 0

    monkeypatch.setattr(GenerationController, "_previous_tts_queued", empty)


class FakeSlots:
    def __init__(self):
        self.rows = {}
        self.route = None
        self.stopped = []
        self.executor_calls = []
        self.fail_executor = False

    @staticmethod
    def origin(slot):
        return f"http://127.0.0.1:{8791 + slot}"

    async def snapshot(self, slot):
        return self.rows.get(slot)

    async def start(self, slot, release, revision):
        self.rows[slot] = {"generation": str(uuid.uuid4()), "revision": revision, "protocol": 1,
                           "ready": True, "state": "standby", "drain_ready": False, "http_inflight": 0}
        return self.rows[slot]

    async def switch(self, slot):
        self.route = slot

    async def close_tts_ingress(self, slot):
        pass

    async def wait_tts_transport_drained(self, slot):
        return True  # simulated transport; stock Caddy has no completion ack

    async def verify(self, revision):
        assert self.rows[self.route]["revision"] == revision

    async def ensure_executor(self, release, revision):
        self.executor_calls.append((release, revision))
        if self.fail_executor:
            raise RuntimeError("executor activation failed")
        return {"ready": False, "activation": "disabled"}

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
    assert "supervisor_upgrade_next_retry_at" not in record


async def test_failed_supervisor_upgrade_stops_without_retry(tmp_path, monkeypatch):
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
    assert "supervisor_upgrade_next_retry_at" not in record
    # 같은 revision에는 다시 시도하지 않는다.
    await control.process(store.get(job["id"]))
    assert calls == [active_revision]


async def test_new_job_reattempts_upgrade_for_a_new_active_revision(tmp_path, monkeypatch):
    # 수동 개입 후 활성 revision이 바뀌면 새 작업에서 재평가가 막히지 않는다.
    from ttakkari import generation
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(generation, "PORTS", (8791, 8792))
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(gd, "contract_for_revision", _three_slot_demanding_contract)
    store = JobStore(tmp_path)
    old_revision, new_revision = "c" * 40, "d" * 40
    for revision in (old_revision, new_revision):
        release = tmp_path / "releases" / revision
        (release / "deploy").mkdir(parents=True)
        (release / "deploy/generation-contract.json").write_text(json.dumps(
            {"protocol": 1, "schema_revision": "x", "online_migration_from": [],
             "supervisor": {"protocol": 1, "slots": 3}}))
        receipt = store.request(revision)
        store.update(receipt["id"], status="draining", release_path=str(release))
    first = store.request("a" * 40)
    control = gd.GenerationController(store, slots=FakeSlots())
    active_id = uuid.uuid4()
    current = {"revision": old_revision}
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=active_id))

    async def info(gid):
        return {"id": gid, "revision": current["revision"], "api_url": "http://127.0.0.1:8791"}

    monkeypatch.setattr(control, "generation_info", info)
    calls = []

    async def boom(controller, revision):
        calls.append(revision)
        raise RuntimeError("kickstart refused")

    monkeypatch.setattr(gd, "upgrade_supervisor", boom)
    await control.process(first)
    assert calls == [old_revision]
    assert store.get(first["id"])["status"] == "supervisor_upgrade_required"
    # 사용자가 손을 댄 뒤 활성 revision이 바뀌면 새 작업은 재시도한다.
    current["revision"] = new_revision
    second = store.request("b" * 40)
    await control.process(second)
    assert calls == [old_revision, new_revision]
    assert store.get(second["id"])["supervisor_upgrade_attempted_for"] == new_revision


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


async def test_command_timeout_kills_a_term_ignoring_grandchild(tmp_path):
    import re

    from ttakkari.generation_deployment import GenerationController, StageError
    store = JobStore(tmp_path)
    control = GenerationController(store, slots=FakeSlots())
    job = store.request("a" * 40)
    grandchild_code = ("import signal, time; "
                       "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
    script = ("import subprocess, sys, time; "
              f"p = subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
              "print(f'GRANDCHILD {p.pid}', flush=True); time.sleep(60)")
    with pytest.raises(StageError):
        await control.command(job, tmp_path, sys.executable, "-c", script,
                              stage="pytest", timeout=2)
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
        pytest.fail("TERM-ignoring grandchild survived the group kill")


async def test_db_create_schema_record_failures_carry_their_stage(tmp_path, monkeypatch):
    from ttakkari import generation_deployment as gd
    from ttakkari.generation_deployment import GenerationController, StageError

    async def run_prepare(fail_at):
        store = JobStore(tmp_path / f"prep-{fail_at}")
        control = GenerationController(store, slots=FakeSlots())
        release = tmp_path / f"release-{fail_at}"
        (release / "deploy").mkdir(parents=True)
        (release / "deploy/generation-contract.json").write_text(json.dumps(
            {"protocol": 1, "schema_revision": "x-new", "online_migration_from": []}))
        (release / ".environment-built").touch()

        def archive(revision):
            return release

        monkeypatch.setattr(control.releases, "archive", archive)
        if fail_at == "db_create":
            import asyncpg

            class FakeAdmin:
                async def execute(self, sql):
                    if sql.startswith("CREATE DATABASE"):
                        raise OSError("disk full")

                async def close(self):
                    pass

            async def connect(*args, **kwargs):
                return FakeAdmin()

            job = store.request("a" * 40)
            with monkeypatch.context() as patch:
                patch.setattr(asyncpg, "connect", connect)
                with pytest.raises(StageError) as excinfo:
                    await control.prepare(job)
            return excinfo.value.stage
        else:
            import asyncpg

            class FakeAdmin:
                async def execute(self, sql):
                    pass

                async def close(self):
                    pass

            async def connect(*args, **kwargs):
                return FakeAdmin()

            class FakeDB:
                async def scalar(self, query):
                    return "x-old"

            class FakeSession:
                async def __aenter__(self):
                    return FakeDB()

                async def __aexit__(self, *args):
                    return False

            monkeypatch.setattr(asyncpg, "connect", connect)
            monkeypatch.setattr(gd, "sessionmaker", lambda: FakeSession())
            monkeypatch.setattr(control, "run_security_gate", lambda *args: asyncio.sleep(0))
            job = store.request("a" * 40, skip_tests=True)
            with pytest.raises(StageError) as excinfo:
                await control.prepare(job)
            return excinfo.value.stage

    assert await run_prepare("db_create") == "db_create"
    assert await run_prepare("schema_contract") == "schema_contract"


async def test_post_activation_active_failure_is_an_activate_stage_error(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    store = JobStore(tmp_path)
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "0" * 40)
    holder = {"active": uuid.UUID(old["generation"])}
    calls = {"n": 0}
    control = GenerationController(store, slots=slots)

    async def active():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("db flap")
        return holder["active"]

    async def activate(gid, *, expected):
        holder["active"] = gid

    async def prepare(_job):
        return Path("candidate")

    monkeypatch.setattr(control, "active", active)
    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)
    job = store.request("b" * 40)
    await control.process(job)
    record = store.get(job["id"])
    assert record["status"] == "rolling_back"
    assert record["error_stage"] == "activate"


async def test_record_active_failure_is_a_record_stage_error(tmp_path, monkeypatch):
    from ttakkari.generation_deployment import GenerationController
    store = JobStore(tmp_path)
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "0" * 40)
    holder = {"active": uuid.UUID(old["generation"])}
    control = GenerationController(store, slots=slots)
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=holder["active"]))

    async def activate(gid, *, expected):
        holder["active"] = gid

    async def prepare(_job):
        return Path("candidate")

    def record_active(job):
        raise OSError("disk full")

    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)
    monkeypatch.setattr(control, "record_active", record_active)
    job = store.request("b" * 40)
    await control.process(job)
    record = store.get(job["id"])
    assert record["status"] == "rolling_back"
    assert record["error_stage"] == "record"


def test_failure_without_any_stage_context_is_recorded_unknown(tmp_path):
    from ttakkari.generation_deployment import GenerationController
    store = JobStore(tmp_path)
    control = GenerationController(store, slots=FakeSlots())
    job = store.request("a" * 40)
    control.record_failure(job, "failed", RuntimeError("boom"))
    record = store.get(job["id"])
    assert record["error_stage"] == "unknown"
    assert "stage unknown" in record["error_summary"]
    assert record["error_command"] is None and record["error_exit"] is None


async def test_upgrade_attempt_is_persisted_before_kickstart(tmp_path, monkeypatch):
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
    seen = {}

    async def dying_upgrade(controller, revision):
        # kickstart가 이 프로세스를 죽이는 순간 디스크에 남아 있는 기록.
        seen.update(controller.store.get(job["id"]))
        raise RuntimeError("SIGTERM during kickstart")

    monkeypatch.setattr(gd, "upgrade_supervisor", dying_upgrade)
    await control.process(job)
    assert seen["supervisor_upgrade_attempted_for"] == active_revision
    assert len(seen["supervisor_upgrade_attempts"]) == 1
    assert seen["supervisor_upgrade_attempts"][0]["ok"] is False
    record = store.get(job["id"])
    assert "SIGTERM during kickstart" in record["supervisor_upgrade_attempts"][0]["error"]


async def test_no_retry_after_restart_with_insufficient_capability(tmp_path, monkeypatch):
    # 갱신 후 다시 떴는데도 부족하면 같은 대상을 다시 시도하지 않는다.
    from ttakkari import generation
    from ttakkari import generation_deployment as gd
    monkeypatch.setattr(generation, "PORTS", (8791, 8792))
    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.setattr(gd, "contract_for_revision", _three_slot_demanding_contract)
    store = JobStore(tmp_path)
    job = store.request("a" * 40)
    store.update(job["id"], supervisor_upgrade_attempted_for="c" * 40,
                 supervisor_upgrade_attempts=[
                     {"revision": "c" * 40, "at": "2026-10-18T00:00:00+00:00",
                      "ok": True, "error": None}])
    control = gd.GenerationController(store, slots=FakeSlots())
    active_id = uuid.uuid4()
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=active_id))

    async def info(gid):
        return {"id": gid, "revision": "c" * 40, "api_url": "http://127.0.0.1:8791"}

    monkeypatch.setattr(control, "generation_info", info)
    calls = []

    async def upgrade(controller, revision):
        calls.append(revision)

    monkeypatch.setattr(gd, "upgrade_supervisor", upgrade)
    await control.process(store.get(job["id"]))
    assert calls == []
    assert store.get(job["id"])["status"] == "supervisor_upgrade_required"


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


async def test_ensure_executor_failure_rolls_back(tmp_path, monkeypatch):
    """ensure_executor 실패 시 readiness 단계 오류로 기록되고 이전 세대로 롤백된다."""
    from ttakkari.generation_deployment import GenerationController
    store = JobStore(tmp_path)
    slots = FakeSlots()
    old = await slots.start(0, Path("unused"), "0" * 40)
    holder = {"active": uuid.UUID(old["generation"])}
    control = GenerationController(store, slots=slots)
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=holder["active"]))

    async def activate(gid, *, expected):
        holder["active"] = gid

    async def prepare(_job):
        return Path("candidate")

    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)

    slots.fail_executor = True
    job = store.request("b" * 40)
    await control.process(job)

    record = store.get(job["id"])
    assert record["status"] in {"rolling_back", "failed"}
    assert record["error_stage"] == "readiness"
    assert "executor activation failed" in record["error_summary"]
    # 구 세대로의 원복(스위치 및 active 복귀) 확인
    assert holder["active"] == uuid.UUID(old["generation"])
    assert slots.route == 0
    assert len(slots.executor_calls) == 1


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
    from ttakkari.deployment import resolve_source_root
    other = tmp_path / "other"
    other.mkdir()
    _init_repo(other, "file.txt")
    monkeypatch.setattr(get_settings(), "deploy_source_root", other)
    with pytest.raises(RuntimeError, match="백엔드 저장소"):
        resolve_source_root()


def test_resolve_source_root_accepts_a_repo_with_the_identity_file(tmp_path, monkeypatch):
    from ttakkari.deployment import resolve_source_root
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "deploy").mkdir()
    _init_repo(repo, "deploy/generation-contract.json", "{}")
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


def test_supervisor_and_queue_diagnostics_detect_mismatch_and_blocked_foreground(tmp_path, monkeypatch):
    from ttakkari import deployment
    from ttakkari.generation import _lock

    pinned_rev = "e817fc19471bdc48054158cada4a99466f7eaa6f"
    other_rev = "48eac0c612d7186e8c418fe607a036ea2ab4da09"
    rel_pinned = tmp_path / "releases" / pinned_rev
    rel_other = tmp_path / "releases" / other_rev
    rel_pinned.mkdir(parents=True)
    rel_other.mkdir(parents=True)
    (rel_pinned / ".release.json").write_text(json.dumps({"revision": pinned_rev}))
    (rel_other / ".release.json").write_text(json.dumps({"revision": other_rev}))

    (tmp_path / "supervisor.json").write_text(json.dumps({
        "revision": pinned_rev,
        "release": str(rel_pinned),
        "source": "/Users/winterholic/private-secret-checkout",
    }))
    (tmp_path / "supervisor-runtime.json").write_text(json.dumps({
        "revision": other_rev,
        "release": str(rel_other),
        "pid": 999999,
        "ports": [8791, 8792, 8793],
    }))
    (tmp_path / "supervisor-upgrade.json").write_text(json.dumps({
        "revision": pinned_rev,
        "release": str(rel_pinned),
        "status": "requested",
    }))

    ctrl_lock = _lock(tmp_path / "controller.lock")
    try:
        sup = deployment.supervisor_diagnostics(tmp_path)
    finally:
        if ctrl_lock:
            ctrl_lock.close()

    assert sup["pinned_revision"] == pinned_rev
    assert sup["pinned_release_exists"] is True
    assert sup["pinned_release_matches_revision"] is True
    assert sup["runtime_revision"] == other_rev
    assert sup["matches_pinned_revision"] is False
    assert sup["matches_pinned_release"] is False
    assert sup["pid_alive"] is False
    assert sup["controller_lock_held"] is True
    assert sup["restart_lock_held"] is False
    assert sup["last_upgrade"]["revision"] == pinned_rev
    assert "private-secret-checkout" not in json.dumps(sup)

    store = JobStore(tmp_path)
    draining_job = store._write({
        "id": str(uuid.uuid4()),
        "revision": "1" * 40,
        "skip_tests": False,
        "status": "draining",
        "created_at": "2026-10-28T20:00:00+00:00",
        "error": None,
        "strategy": "generations",
    })
    blocking_job = store._write({
        "id": "abcb3f7d-a291-4789-9117-ec5c08220eba",
        "revision": "2" * 40,
        "skip_tests": False,
        "status": "recovery_pending",
        "created_at": "2026-10-28T21:52:32+00:00",
        "error": "draining generation unreachable; work survival is unknown",
        "error_stage": "drain",
        "strategy": "generations",
        "activated_revision": "2" * 40,
    })
    blocked_job = store._write({
        "id": "f407df99-51b1-4843-8fa0-a3fe4ceebefb",
        "revision": "3" * 40,
        "skip_tests": False,
        "status": "requested",
        "created_at": "2026-10-29T16:33:58+00:00",
        "error": None,
        "strategy": "generations",
    })
    q = deployment.queue_diagnostics(store)
    assert q["cutover_blocked"] is True
    assert [j["id"] for j in q["draining_jobs"]] == [draining_job["id"]]
    assert q["foreground_job"]["id"] == blocking_job["id"]
    assert [j["id"] for j in q["blocking_jobs"]] == [blocking_job["id"]]
    assert [j["id"] for j in q["blocked_jobs"]] == [blocked_job["id"]]


def _setup_abcb_recovery_fixture(tmp_path, monkeypatch):
    from ttakkari import generation_deployment as gd

    monkeypatch.setattr(get_settings(), "deploy_strategy", "generations")
    monkeypatch.delenv("TTAKKARI_RUN_ID", raising=False)

    old_rev = "c70bce7ba8ab888ea4989f2ab3ef887ff41f0809"
    abcb_rev = "12347b3f24f56f2a7c029e8ca3f394495dd240c8"
    active_rev = "48eac0c612d7186e8c418fe607a036ea2ab4da09"
    next_rev = "133ab4e0f20d16c092580a2bb9cd5a8bb9fce01e"

    old_gen = "01c6010d-6ecd-4bc9-a879-12b60dea1215"
    cand_gen = "18c94d7e-2bf0-4ba0-ae76-467154b56f5c"
    active_gen = "bb450175-d7a4-495e-ae0f-e3202ce0a456"

    slots = FakeSlots()
    # Slot 0: free (18c94d7e and 22b7400c already retired)
    # Slot 1: old_gen (01c6010d) alive and draining GAME Run e6346a73
    slots.rows[1] = {
        "generation": old_gen,
        "revision": old_rev,
        "protocol": 1,
        "ready": True,
        "state": "draining",
        "drain_ready": False,
        "owned_runs": 1,
        "http_inflight": 0,
    }
    # Slot 2: newer active generation bb450175
    slots.rows[2] = {
        "generation": active_gen,
        "revision": active_rev,
        "protocol": 1,
        "ready": True,
        "state": "active",
        "drain_ready": False,
        "owned_runs": 0,
        "http_inflight": 0,
    }
    slots.route = 2

    store = JobStore(tmp_path)
    abcb = store._write({
        "id": "abcb3f7d-a291-4789-9117-ec5c08220eba",
        "revision": abcb_rev,
        "skip_tests": False,
        "status": "recovery_pending",
        "created_at": "2026-10-28T21:52:32.996586+00:00",
        "error": "draining generation unreachable; work survival is unknown",
        "error_stage": "drain",
        "strategy": "generations",
        "candidate_slot": 0,
        "previous_slot": 1,
        "previous_generation": old_gen,
        "previous_revision": old_rev,
        "candidate_generation": cand_gen,
        "activated_revision": abcb_rev,
    })
    f407 = store._write({
        "id": "f407df99-51b1-4843-8fa0-a3fe4ceebefb",
        "revision": next_rev,
        "skip_tests": False,
        "status": "requested",
        "created_at": "2026-10-29T16:33:58.602253+00:00",
        "error": None,
        "strategy": "generations",
    })

    control = gd.GenerationController(store, slots=slots)
    db_state = {
        "active_generation": active_gen,
        "active_revision": active_rev,
        "active_state": "active",
        "active_api_url": slots.origin(2),
        "epoch": 123,
        "previous_generation": old_gen,
        "previous_revision": old_rev,
        "previous_state": "draining",
        "previous_api_url": slots.origin(1),
        "previous_leases": 2,
        "previous_process_groups": 2,
        "previous_lock_held": True,
        "previous_pid_alive": True,
    }

    async def fake_db_state(_prev_id, _active_id, *, mark_offline=False):
        if mark_offline:
            db_state["previous_state"] = "offline"
        return dict(db_state)

    monkeypatch.setattr(control, "_drain_recovery_db_state", fake_db_state, raising=False)
    fences = {
        "job_id": abcb["id"],
        "expected_revision": abcb_rev,
        "expected_previous_generation": old_gen,
        "expected_previous_revision": old_rev,
        "expected_previous_slot": 1,
        "expected_candidate_generation": cand_gen,
        "expected_active_generation": active_gen,
        "expected_active_revision": active_rev,
        "expected_epoch": 123,
    }
    return control, store, slots, db_state, fences, abcb, f407


async def test_recover_drain_resumes_live_draining_slot_and_unblocks_newer_foreground(tmp_path, monkeypatch):
    from ttakkari.deployment import Controller

    control, store, slots, _db_state, fences, abcb, f407 = _setup_abcb_recovery_fixture(tmp_path, monkeypatch)

    # Before recovery: abcb blocks f407 in pending_jobs()
    ext = Controller(tmp_path)
    assert [j["id"] for j in ext.pending_jobs()] == [abcb["id"]]

    # Dry-run preview verifies fences and reports resume_draining without mutating job status
    preview = await control.recover_drain(**fences, apply=False)
    assert preview["ok"] is True
    assert preview["applied"] is False
    assert preview["action"] == "resume_draining"
    assert store.get(abcb["id"])["status"] == "recovery_pending"

    # Apply recovery: transitions abcb -> draining, preserves slot 1 (GAME) and slot 2 (newer active)
    applied = await control.recover_drain(**fences, apply=True)
    assert applied["ok"] is True
    assert applied["applied"] is True
    assert applied["action"] == "resume_draining"
    saved = store.get(abcb["id"])
    assert saved["status"] == "draining"
    assert saved["error"] is None and saved["error_stage"] is None
    assert saved["recovery_receipt"]["action"] == "resume_draining"
    assert saved["recovery_receipt"]["expected_epoch"] == 123
    assert slots.stopped == []
    assert slots.route == 2

    # Idempotency: calling recover_drain again with same fences returns without error or duplicate write
    again = await control.recover_drain(**fences, apply=True)
    assert again["ok"] is True
    assert again["idempotent"] is True
    assert again["applied"] is False

    # Now pending_jobs() schedules abcb in drains and f407 in foreground!
    assert [j["id"] for j in ext.pending_jobs()] == [abcb["id"], f407["id"]]

    # Supervisor pass keeps slot 1 draining (GAME preserved) while f407 cuts over to free slot 0
    active_holder = {"id": uuid.UUID(fences["expected_active_generation"])}
    monkeypatch.setattr(control, "active", lambda: asyncio.sleep(0, result=active_holder["id"]))

    async def activate(gid, *, expected):
        assert active_holder["id"] == expected
        active_holder["id"] = gid

    async def prepare(_job):
        return Path("candidate-f407")

    monkeypatch.setattr(control, "activate", activate)
    monkeypatch.setattr(control, "prepare", prepare)

    for job in ext.pending_jobs():
        await control.process(job)

    assert store.get(abcb["id"])["status"] == "draining"
    assert slots.rows[1]["generation"] == fences["expected_previous_generation"]
    assert slots.stopped == []
    assert store.get(f407["id"])["status"] == "draining"
    assert store.get(f407["id"])["candidate_slot"] == 0
    assert slots.route == 0


async def test_recover_drain_retires_unreachable_slot_only_when_proven_dead_and_lease_free(tmp_path, monkeypatch):
    control, store, slots, db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(tmp_path, monkeypatch)
    # Make slot 1 unreachable
    slots.rows.pop(1, None)

    # Counterexample A: unreachable while live leases remain -> refuse
    with pytest.raises(RuntimeError, match="lease"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"

    # Counterexample B: leases=0 but process_groups > 0 -> refuse
    db_state["previous_leases"] = 0
    with pytest.raises(RuntimeError, match="process group"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"

    # Counterexample C: process_groups=0 but runtime lock still held -> refuse
    db_state["previous_process_groups"] = 0
    with pytest.raises(RuntimeError, match="runtime lock"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"

    # Counterexample D: lock free but OS PID still alive -> refuse
    db_state["previous_lock_held"] = False
    with pytest.raises(RuntimeError, match="process is still alive"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"

    # Proven dead, 0 leases, 0 process groups, lock free -> retires cleanly to succeeded without rerouting newer active
    db_state["previous_pid_alive"] = False
    res = await control.recover_drain(**fences, apply=True)
    assert res["ok"] is True
    assert res["applied"] is True
    assert res["action"] == "retire_verified_dead"
    saved = store.get(abcb["id"])
    assert saved["status"] == "succeeded"
    assert saved["retired_generation"] == fences["expected_previous_generation"]
    assert db_state["previous_state"] == "offline"
    assert slots.route == 2


async def test_recover_drain_refuses_identity_mismatch_epoch_stale_and_concurrent_lock(tmp_path, monkeypatch):
    from ttakkari.deploy_gate import RestartGate

    control, store, slots, db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(tmp_path, monkeypatch)

    # 1. TTAKKARI_RUN_ID guard on apply=True
    monkeypatch.setenv("TTAKKARI_RUN_ID", "run-123")
    with pytest.raises(RuntimeError, match="external host command"):
        await control.recover_drain(**fences, apply=True)
    monkeypatch.delenv("TTAKKARI_RUN_ID", raising=False)

    # 2. Slot identity mismatch (different generation on slot 1)
    slots.rows[1]["generation"] = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="identity mismatch"):
        await control.recover_drain(**fences, apply=True)
    slots.rows[1]["generation"] = fences["expected_previous_generation"]

    # 3. Epoch stale in DB RuntimeControl
    db_state["epoch"] = 124
    with pytest.raises(RuntimeError, match="epoch"):
        await control.recover_drain(**fences, apply=True)
    db_state["epoch"] = 123

    # 4. Active generation fence mismatch
    db_state["active_generation"] = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="active generation"):
        await control.recover_drain(**fences, apply=True)
    db_state["active_generation"] = fences["expected_active_generation"]

    # 5. Job journal fence mismatch (or pre-cutover job without activated_revision)
    with pytest.raises(ValueError, match="fence mismatch"):
        await control.recover_drain(**(fences | {"expected_candidate_generation": str(uuid.uuid4())}), apply=True)

    store.update(abcb["id"], activated_revision=None)
    with pytest.raises(ValueError, match="cutover"):
        await control.recover_drain(**fences, apply=True)
    store.update(abcb["id"], activated_revision=fences["expected_revision"])

    # 6. Concurrent lock contention on backend-restart.lock
    monkeypatch.setattr(RestartGate, "_acquire", lambda _self: None)
    with pytest.raises(RuntimeError, match="lock is held"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"


def test_deployment_cli_diagnose_and_recover_drain_subcommands(tmp_path, monkeypatch, capsys):
    from ttakkari import deployment
    from ttakkari import generation_deployment as gd

    control, _store, _slots, _db_state, fences, abcb, f407 = _setup_abcb_recovery_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(deployment, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(gd, "GenerationController", lambda _s: control)

    # 1. diagnose CLI is read-only and works even inside TTAKKARI_RUN_ID
    monkeypatch.setenv("TTAKKARI_RUN_ID", "run-readonly-check")
    monkeypatch.setattr(sys, "argv", ["ttakkari.deployment", "diagnose", "--no-db"])
    deployment.main()
    diag = json.loads(capsys.readouterr().out)
    assert diag["deploy_queue"]["cutover_blocked"] is True
    assert diag["deploy_queue"]["foreground_job"]["id"] == abcb["id"]
    assert [j["id"] for j in diag["deploy_queue"]["blocked_jobs"]] == [f407["id"]]
    monkeypatch.delenv("TTAKKARI_RUN_ID", raising=False)

    # 2. recover-drain CLI dry-run and apply
    cli_args = [
        "ttakkari.deployment",
        "recover-drain",
        "--job-id", fences["job_id"],
        "--expected-revision", fences["expected_revision"],
        "--expected-previous-generation", fences["expected_previous_generation"],
        "--expected-previous-revision", fences["expected_previous_revision"],
        "--expected-previous-slot", str(fences["expected_previous_slot"]),
        "--expected-candidate-generation", fences["expected_candidate_generation"],
        "--expected-active-generation", fences["expected_active_generation"],
        "--expected-active-revision", fences["expected_active_revision"],
        "--expected-epoch", str(fences["expected_epoch"]),
    ]
    monkeypatch.setattr(sys, "argv", cli_args)
    deployment.main()
    dry = json.loads(capsys.readouterr().out)
    assert dry["ok"] is True and dry["applied"] is False

    monkeypatch.setattr(sys, "argv", [*cli_args, "--apply"])
    deployment.main()
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] is True and applied["applied"] is True and applied["action"] == "resume_draining"


async def test_drain_recovery_db_state_evaluates_real_orm_rows_and_os_locks(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from ttakkari import generation_deployment as gd
    from ttakkari.generation import _lock
    from ttakkari.models import RuntimeControl, RuntimeGeneration

    home = tmp_path / "home"
    monkeypatch.setattr(get_settings(), "home", home)
    prev_id = uuid.uuid4()
    active_id = uuid.uuid4()
    control_row = SimpleNamespace(id=1, active_generation=active_id, executor_generation=None, epoch=123)
    prev_row = SimpleNamespace(
        id=prev_id, revision="a" * 40, state="draining",
        api_url="http://127.0.0.1:8792", pid=os.getpid(), process_started_at=None,
    )
    active_row = SimpleNamespace(
        id=active_id, revision="b" * 40, state="active",
        api_url="http://127.0.0.1:8793", pid=os.getpid(), process_started_at=None,
    )

    class FakeSession:
        def __init__(self):
            self.committed = False
            self.scalar_vals = [2, 1]

        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, model, ident):
            if model is RuntimeControl:
                return control_row
            if model is RuntimeGeneration and ident == prev_id:
                return prev_row
            if model is RuntimeGeneration and ident == active_id:
                return active_row
            return None

        async def scalar(self, _stmt):
            return self.scalar_vals.pop(0)

        async def commit(self):
            self.committed = True

    fake_db = FakeSession()
    monkeypatch.setattr(gd, "sessionmaker", lambda: fake_db)
    controller = gd.GenerationController(JobStore(tmp_path / "deploy"), slots=FakeSlots())

    held = _lock(home / "runtime" / f"{prev_id}.lock")
    try:
        state = await controller._drain_recovery_db_state(prev_id, active_id, mark_offline=False)
        assert state["epoch"] == 123
        assert state["previous_leases"] == 2
        assert state["previous_process_groups"] == 1
        assert state["previous_lock_held"] is True
        assert state["previous_pid_alive"] is True
        assert prev_row.state == "draining"
    finally:
        if held:
            held.close()

    fake_db.scalar_vals = [0, 0]
    prev_row.pid = 999999
    state2 = await controller._drain_recovery_db_state(prev_id, active_id, mark_offline=True)
    assert state2["previous_lock_held"] is False
    assert state2["previous_pid_alive"] is False
    assert prev_row.state == "offline"
    assert fake_db.committed is True


async def test_lock_held_and_probe_distinguish_unknown_from_free_and_fail_closed(tmp_path, monkeypatch):
    import errno
    import fcntl

    from ttakkari import deployment
    from ttakkari.generation import _lock

    lock_path = tmp_path / "runtime" / f"{uuid.uuid4()}.lock"
    assert deployment._probe_lock(lock_path) == ("free", None)
    assert deployment._lock_held(lock_path) is False

    held = _lock(lock_path)
    try:
        lock_path.chmod(0o444)
        assert deployment._probe_lock(lock_path) == ("held", None)
        assert deployment._lock_held(lock_path) is True
    finally:
        lock_path.chmod(0o644)
        if held:
            held.close()

    assert deployment._probe_lock(lock_path) == ("free", None)
    assert deployment._lock_held(lock_path) is False

    # PermissionError on stat/open and EIO on flock must return unknown (None), never False
    orig_stat = Path.stat

    def deny_stat(self, *args, **kwargs):
        if self == lock_path:
            raise PermissionError(errno.EACCES, "stat denied")
        return orig_stat(self, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "stat", deny_stat)
        state, reason = deployment._probe_lock(lock_path)
        assert state == "unknown" and reason == "PermissionError"
        assert deployment._lock_held(lock_path) is None

    orig_open = Path.open

    def deny_open(self, *args, **kwargs):
        if self == lock_path:
            raise PermissionError(errno.EACCES, "open denied")
        return orig_open(self, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "open", deny_open)
        state, reason = deployment._probe_lock(lock_path)
        assert state == "unknown" and reason == "PermissionError"
        assert deployment._lock_held(lock_path) is None

    with monkeypatch.context() as mp:
        mp.setattr(fcntl, "flock", lambda *_a, **_kw: (_ for _ in ()).throw(OSError(errno.EIO, "io error")))
        state, reason = deployment._probe_lock(lock_path)
        assert state == "unknown" and reason == "OSError"
        assert deployment._lock_held(lock_path) is None

    # recover_drain must refuse both resume_draining and retire_verified_dead when lock state is unknown (None)
    control, store, slots, db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(tmp_path / "jobs", monkeypatch)
    db_state["previous_lock_held"] = None
    with pytest.raises(RuntimeError, match="lock.*unknown"):
        await control.recover_drain(**fences, apply=False)
    with pytest.raises(RuntimeError, match="lock.*unknown"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"

    slots.rows.pop(1, None)
    db_state["previous_leases"] = 0
    db_state["previous_process_groups"] = 0
    db_state["previous_pid_alive"] = False
    with pytest.raises(RuntimeError, match="lock.*unknown"):
        await control.recover_drain(**fences, apply=False)
    with pytest.raises(RuntimeError, match="lock.*unknown"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"


async def test_pid_probe_distinguishes_unknown_from_dead_and_refuses_recovery(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    import psutil

    from ttakkari import generation_deployment as gd
    from ttakkari.models import RuntimeControl, RuntimeGeneration

    home = tmp_path / "home"
    monkeypatch.setattr(get_settings(), "home", home)
    prev_id = uuid.uuid4()
    active_id = uuid.uuid4()
    control_row = SimpleNamespace(id=1, active_generation=active_id, executor_generation=None, epoch=123)
    prev_row = SimpleNamespace(
        id=prev_id, revision="a" * 40, state="draining",
        api_url="http://127.0.0.1:8792", pid=os.getpid(), process_started_at=100.0,
    )
    active_row = SimpleNamespace(
        id=active_id, revision="b" * 40, state="active",
        api_url="http://127.0.0.1:8793", pid=os.getpid(), process_started_at=100.0,
    )

    class SimpleDB:
        def __init__(self):
            self.committed = False

        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, model, ident):
            if model is RuntimeControl:
                return control_row
            if model is RuntimeGeneration and ident == prev_id:
                return prev_row
            if model is RuntimeGeneration and ident == active_id:
                return active_row
            return None

        async def scalar(self, _stmt):
            return 0

        async def commit(self):
            self.committed = True

    fake_db = SimpleDB()
    monkeypatch.setattr(gd, "sessionmaker", lambda: fake_db)
    controller = gd.GenerationController(JobStore(tmp_path / "deploy"), slots=FakeSlots())

    for exc in (psutil.AccessDenied(prev_row.pid), PermissionError("denied"), psutil.ZombieProcess(prev_row.pid)):
        with monkeypatch.context() as mp:
            mp.setattr(gd.psutil, "Process", lambda _pid, _e=exc: (_ for _ in ()).throw(_e))
            st = await controller._drain_recovery_db_state(prev_id, active_id, mark_offline=False)
            assert st["previous_pid_alive"] is None
            with pytest.raises(RuntimeError):
                await controller._drain_recovery_db_state(prev_id, active_id, mark_offline=True)
            assert prev_row.state == "draining"
            assert fake_db.committed is False

    # Invalid/missing PID must also be unknown (None), never dead (False), even on __new__ instance and minimal control row
    control_minimal = SimpleNamespace(active_generation=active_id, epoch=123)

    async def minimal_get(model, ident):
        return control_minimal if model is RuntimeControl else (prev_row if ident == prev_id else active_row)

    monkeypatch.setattr(fake_db, "get", minimal_get)
    prev_row.pid = None
    raw_instance = gd.GenerationController.__new__(gd.GenerationController)
    st_none = await raw_instance._drain_recovery_db_state(prev_id, active_id, mark_offline=False)
    assert st_none["previous_pid_alive"] is None
    with pytest.raises(RuntimeError):
        await raw_instance._drain_recovery_db_state(prev_id, active_id, mark_offline=True)
    assert prev_row.state == "draining"

    # recover_drain refuses unknown PID on both actions
    control, store, slots, db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(tmp_path / "jobs", monkeypatch)
    db_state["previous_pid_alive"] = None
    with pytest.raises(RuntimeError, match="PID.*unknown"):
        await control.recover_drain(**fences, apply=True)
    slots.rows.pop(1, None)
    db_state["previous_leases"] = 0
    db_state["previous_process_groups"] = 0
    db_state["previous_lock_held"] = False
    with pytest.raises(RuntimeError, match="PID.*unknown"):
        await control.recover_drain(**fences, apply=True)
    assert store.get(abcb["id"])["status"] == "recovery_pending"


async def test_recover_drain_rechecks_fences_and_drift_after_every_await(tmp_path, monkeypatch):
    # Part A: resume_draining must reject any drift occurring during HTTP snapshot awaits
    mutations_resume = [
        ("epoch", 124, "epoch"),
        ("active_generation", str(uuid.uuid4()), "active generation"),
        ("previous_leases", 5, "drift"),
        ("previous_process_groups", 4, "drift"),
        ("previous_lock_held", False, "lock"),
        ("previous_lock_held", None, "lock.*unknown"),
        ("previous_pid_alive", False, "PID|process"),
        ("previous_pid_alive", None, "PID.*unknown"),
        ("previous_state", "offline", "draining"),
    ]
    for idx, (field, mutated_val, pattern) in enumerate(mutations_resume):
        sub = tmp_path / f"resume_{idx}"
        control, store, slots, db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(sub, monkeypatch)
        orig_snapshot = slots.snapshot

        async def mutating_snapshot(slot, _orig=orig_snapshot, _ds=db_state, _f=field, _v=mutated_val):
            res = await _orig(slot)
            if slot == 1:
                _ds[_f] = _v
            return res

        monkeypatch.setattr(slots, "snapshot", mutating_snapshot)
        with pytest.raises(RuntimeError, match=pattern):
            await control.recover_drain(**fences, apply=True)
        assert store.get(abcb["id"])["status"] == "recovery_pending"

    # Part B: retire_verified_dead must never discard latest mark_offline observation
    mutations_retire = [
        ("epoch", 124, "epoch"),
        ("previous_leases", 1, "lease"),
        ("previous_process_groups", 1, "process group"),
        ("previous_lock_held", True, "runtime lock"),
        ("previous_lock_held", None, "lock.*unknown"),
        ("previous_pid_alive", True, "process is still alive"),
        ("previous_pid_alive", None, "PID.*unknown"),
    ]
    for idx, (field, mutated_val, pattern) in enumerate(mutations_retire):
        sub = tmp_path / f"retire_{idx}"
        control, store, slots, db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(sub, monkeypatch)
        slots.rows.pop(1, None)
        db_state["previous_leases"] = 0
        db_state["previous_process_groups"] = 0
        db_state["previous_lock_held"] = False
        db_state["previous_pid_alive"] = False

        async def mutating_db_state(_prev, _active, *, mark_offline=False, _ds=db_state, _f=field, _v=mutated_val):
            cur = dict(_ds)
            if mark_offline:
                cur[_f] = _v
            return cur

        monkeypatch.setattr(control, "_drain_recovery_db_state", mutating_db_state, raising=False)
        with pytest.raises(RuntimeError, match=pattern):
            await control.recover_drain(**fences, apply=True)
        saved = store.get(abcb["id"])
        assert saved["status"] == "recovery_pending"
        assert "recovery_intent" not in saved


async def test_recover_drain_acquires_canonical_for_update_lock_order_and_rolls_back_on_conflict(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from sqlalchemy.exc import DBAPIError

    from ttakkari import generation_deployment as gd
    from ttakkari.generation import _lock
    from ttakkari.models import RuntimeControl, RuntimeGeneration

    home = tmp_path / "home"
    monkeypatch.setattr(get_settings(), "home", home)
    control, store, slots, _unused_db, fences, abcb, _f407 = _setup_abcb_recovery_fixture(tmp_path / "jobs", monkeypatch)

    prev_id = uuid.UUID(fences["expected_previous_generation"])
    active_id = uuid.UUID(fences["expected_active_generation"])

    control_row = SimpleNamespace(id=1, active_generation=active_id, executor_generation=None, epoch=123)
    active_row = SimpleNamespace(
        id=active_id, revision=fences["expected_active_revision"], state="active",
        api_url=slots.origin(2), pid=os.getpid(), process_started_at=None,
    )
    prev_row = SimpleNamespace(
        id=prev_id, revision=fences["expected_previous_revision"], state="draining",
        api_url=slots.origin(1), pid=os.getpid(), process_started_at=None,
    )
    lease_rows = [SimpleNamespace(run_id=uuid.uuid4(), generation_id=prev_id) for _ in range(2)]
    group_rows = [SimpleNamespace(id=uuid.uuid4(), generation_id=prev_id) for _ in range(2)]

    events: list[str] = []

    class FakeLockedSession:
        def __init__(self, *, fail_lock_timeout: bool = False, inject_on_lock=None):
            self.fail_lock_timeout = fail_lock_timeout
            self.inject_on_lock = inject_on_lock
            self.lock_sequence: list[str] = []
            self.committed = 0
            self.rolled_back = 0
            self.in_tx = False

        def __call__(self):
            return self

        async def __aenter__(self):
            self.in_tx = True
            events.append("db_enter")
            return self

        async def __aexit__(self, *args):
            self.in_tx = False
            events.append("db_exit")
            return False

        async def execute(self, stmt):
            sql = str(stmt)
            events.append(f"sql:{sql}")
            return SimpleNamespace()

        async def get(self, model, ident):
            if model is RuntimeControl:
                return control_row
            if model is RuntimeGeneration and ident == active_id:
                return active_row
            if model is RuntimeGeneration and ident == prev_id:
                return prev_row
            return None

        async def scalar(self, stmt):
            for_update = getattr(stmt, "_for_update_arg", None) is not None
            froms = [getattr(f, "name", str(f)) for f in stmt.get_final_froms()]
            table = froms[0] if froms else "unknown"
            if for_update:
                if self.fail_lock_timeout:
                    orig = Exception("lock timeout")
                    orig.sqlstate = "55P03"
                    raise DBAPIError("SELECT ... FOR UPDATE", {}, orig)
                label = table
                if table == "runtime_generations":
                    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
                    label = f"runtime_generations:{'active' if active_id.hex in compiled or str(active_id) in compiled else 'previous'}"
                self.lock_sequence.append(label)
                if self.inject_on_lock and len(self.lock_sequence) == 1:
                    self.inject_on_lock()
                if table == "runtime_control":
                    return control_row
                if label == "runtime_generations:active":
                    return active_row
                return prev_row
            if table == "runtime_leases":
                return len(lease_rows)
            if table == "runtime_process_groups":
                return len(group_rows)
            return 0

        async def scalars(self, stmt):
            assert getattr(stmt, "_for_update_arg", None) is not None
            froms = [getattr(f, "name", str(f)) for f in stmt.get_final_froms()]
            table = froms[0]
            self.lock_sequence.append(table)
            if table == "runtime_leases":
                return list(lease_rows)
            if table == "runtime_process_groups":
                return list(group_rows)
            return []

        async def commit(self):
            self.committed += 1
            events.append("db_commit")

        async def rollback(self):
            self.rolled_back += 1
            events.append("db_rollback")

    # Restore real _drain_recovery_db_state on control
    monkeypatch.setattr(
        control,
        "_drain_recovery_db_state",
        gd.GenerationController._drain_recovery_db_state.__get__(control, gd.GenerationController),
    )

    orig_write = store._write

    def tracked_write(job_dict):
        events.append(f"journal_write:{job_dict.get('status')}")
        return orig_write(job_dict)

    monkeypatch.setattr(store, "_write", tracked_write)

    # 1. resume_draining: acquires 5 FOR UPDATE locks in canonical order and writes journal BEFORE releasing DB row locks
    held_prev = _lock(home / "runtime" / f"{prev_id}.lock")
    try:
        locked_db = FakeLockedSession()
        monkeypatch.setattr(gd, "sessionmaker", lambda: locked_db)
        res = await control.recover_drain(**fences, apply=True)
        assert res["action"] == "resume_draining" and res["applied"] is True
        assert locked_db.lock_sequence == [
            "runtime_control",
            "runtime_generations:active",
            "runtime_leases",
            "runtime_generations:previous",
            "runtime_process_groups",
        ]
        # Journal write to draining must occur before the locked DB session exits
        last_enter = max(i for i, e in enumerate(events) if e == "db_enter")
        j_idx = next(i for i, e in enumerate(events) if e == "journal_write:draining" and i > last_enter)
        exit_idx = next(i for i, e in enumerate(events) if e == "db_exit" and i > last_enter)
        assert last_enter < j_idx < exit_idx
    finally:
        if held_prev:
            held_prev.close()

    # Reset job to recovery_pending and test retire_verified_dead conflict, lock timeout, and clean commit
    store._write(abcb | {"status": "recovery_pending"})
    slots.rows.pop(1, None)
    lease_rows.clear()
    group_rows.clear()
    prev_row.pid = 999999

    # 2. Lock timeout (55P03) explicitly rolls back and leaves job in recovery_pending without intent leak
    timeout_db = FakeLockedSession(fail_lock_timeout=True)
    monkeypatch.setattr(gd, "sessionmaker", lambda: timeout_db)
    with pytest.raises(RuntimeError, match="lock"):
        await control.recover_drain(**fences, apply=True)
    assert timeout_db.rolled_back >= 1
    assert timeout_db.committed == 0
    assert prev_row.state == "draining"
    assert store.get(abcb["id"])["status"] == "recovery_pending"
    assert "recovery_intent" not in store.get(abcb["id"])

    # 3. Concurrent lease inserted right before lock acquisition -> refuses, rolls back, no offline commit
    def inject_lease():
        lease_rows.append(SimpleNamespace(run_id=uuid.uuid4(), generation_id=prev_id))

    race_db = FakeLockedSession(inject_on_lock=inject_lease)
    monkeypatch.setattr(gd, "sessionmaker", lambda: race_db)
    with pytest.raises(RuntimeError, match="lease"):
        await control.recover_drain(**fences, apply=True)
    assert race_db.committed == 0
    assert race_db.rolled_back >= 1
    assert prev_row.state == "draining"
    assert store.get(abcb["id"])["status"] == "recovery_pending"
    assert "recovery_intent" not in store.get(abcb["id"])

    # 4. Clean dead retire acquires canonical lock order, commits offline, then writes succeeded
    lease_rows.clear()
    clean_db = FakeLockedSession()
    monkeypatch.setattr(gd, "sessionmaker", lambda: clean_db)
    res2 = await control.recover_drain(**fences, apply=True)
    assert res2["action"] == "retire_verified_dead" and res2["applied"] is True
    assert clean_db.lock_sequence == [
        "runtime_control",
        "runtime_generations:active",
        "runtime_leases",
        "runtime_generations:previous",
        "runtime_process_groups",
    ]
    assert clean_db.committed == 1
    assert prev_row.state == "offline"
    assert store.get(abcb["id"])["status"] == "succeeded"
    assert "recovery_intent" not in store.get(abcb["id"])


async def test_recover_drain_crash_consistency_between_db_offline_and_journal_succeeded(tmp_path, monkeypatch):
    from ttakkari import deployment

    control, store, slots, db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(tmp_path, monkeypatch)
    slots.rows.pop(1, None)
    db_state["previous_leases"] = 0
    db_state["previous_process_groups"] = 0
    db_state["previous_lock_held"] = False
    db_state["previous_pid_alive"] = False

    orig_write = store._write
    fail_succeeded_once = {"armed": True}

    def flaky_write(job_dict):
        if fail_succeeded_once["armed"] and job_dict.get("status") == "succeeded":
            fail_succeeded_once["armed"] = False
            raise OSError("simulated crash after DB offline commit before journal succeeded")
        return orig_write(job_dict)

    monkeypatch.setattr(store, "_write", flaky_write)

    # 1. Crash right after DB marked offline -> raises OSError, keeps recovery_pending + recovery_intent, blocks cutover
    with pytest.raises(OSError, match="simulated crash"):
        await control.recover_drain(**fences, apply=True)
    assert db_state["previous_state"] == "offline"
    mid = store.get(abcb["id"])
    assert mid["status"] == "recovery_pending"
    assert mid["recovery_intent"]["action"] == "retire_verified_dead"
    q_mid = deployment.queue_diagnostics(store)
    assert q_mid["cutover_blocked"] is True
    assert q_mid["foreground_job"]["recovery_intent_pending"] is True

    # 2. While DB is already offline with retire intent, a spurious HTTP slot response cannot flip job to draining
    slots.rows[1] = {
        "generation": fences["expected_previous_generation"],
        "revision": fences["expected_previous_revision"],
        "drain_ready": False,
    }
    with pytest.raises(RuntimeError):
        await control.recover_drain(**fences, apply=True)
    slots.rows.pop(1, None)

    # 3. Re-entry after filesystem recovery converges cleanly to succeeded and clears recovery_intent
    converged = await control.recover_drain(**fences, apply=True)
    assert converged["ok"] is True and converged["applied"] is True and converged["action"] == "retire_verified_dead"
    final_job = store.get(abcb["id"])
    assert final_job["status"] == "succeeded"
    assert "recovery_intent" not in final_job
    assert deployment.queue_diagnostics(store)["cutover_blocked"] is False

    # 4. Subsequent re-entry with matching fences is idempotent; mismatched epoch or active_revision is rejected
    idem = await control.recover_drain(**fences, apply=True)
    assert idem["idempotent"] is True and idem["applied"] is False
    with pytest.raises(RuntimeError):
        await control.recover_drain(**(fences | {"expected_epoch": 999}), apply=True)
    with pytest.raises(RuntimeError):
        await control.recover_drain(**(fences | {"expected_active_revision": "f" * 40}), apply=True)


async def test_fenced_deploy_recovery_api_enforces_owner_session_schema_and_active_generation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from pydantic import ValidationError

    from ttakkari import deployment, generation
    from ttakkari import generation_deployment as gd
    from ttakkari.api import deployments as deploy_api
    from ttakkari.models import (
        ChatSession,
        RuntimeControl,
        RuntimeGeneration,
        SessionKind,
    )

    control, _store, _slots, _db_state, fences, abcb, _f407 = _setup_abcb_recovery_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(deployment, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(gd, "GenerationController", lambda _s: control)

    # 1. Arbitrary shell/path fields are rejected by strict Pydantic schema
    valid_payload = {
        "expected_revision": fences["expected_revision"],
        "expected_previous_generation": fences["expected_previous_generation"],
        "expected_previous_revision": fences["expected_previous_revision"],
        "expected_previous_slot": fences["expected_previous_slot"],
        "expected_candidate_generation": fences["expected_candidate_generation"],
        "expected_active_generation": fences["expected_active_generation"],
        "expected_active_revision": fences["expected_active_revision"],
        "expected_epoch": fences["expected_epoch"],
        "idempotency_key": f"recover-drain:{abcb['id']}:{fences['expected_previous_generation']}:{fences['expected_epoch']}",
        "apply": True,
    }
    with pytest.raises(ValidationError):
        deploy_api.RecoverDrainIn(**(valid_payload | {"command": "sh -c id"}))
    with pytest.raises(ValidationError):
        deploy_api.RecoverDrainIn(**(valid_payload | {"release_path": "/tmp/evil"}))

    root_sid = uuid.uuid4()
    worker_sid = uuid.uuid4()
    active_uuid = uuid.UUID(fences["expected_active_generation"])

    sessions = {
        root_sid: SimpleNamespace(id=root_sid, kind=SessionKind.orchestrator, pool="personal", archived_at=None),
        worker_sid: SimpleNamespace(id=worker_sid, kind=SessionKind.worker, pool="personal", archived_at=None),
    }
    ctrl_row = SimpleNamespace(id=1, active_generation=active_uuid, executor_generation=None, epoch=123)
    active_row = SimpleNamespace(id=active_uuid, revision=fences["expected_active_revision"], state="active")

    class ApiFakeDB:
        def __init__(self):
            self.added = []
            self.commits = 0

        async def get(self, model, ident):
            if model is ChatSession:
                return sessions.get(ident)
            if model is RuntimeControl:
                return ctrl_row
            if model is RuntimeGeneration and ident == active_uuid:
                return active_row
            return None

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            self.commits += 1

    api_db = ApiFakeDB()
    body = deploy_api.RecoverDrainIn(**valid_payload)

    # 2. Worker session is forbidden (403)
    with pytest.raises(HTTPException) as exc403:
        await deploy_api.recover_drain_endpoint(uuid.UUID(abcb["id"]), body, worker_sid, api_db)
    assert exc403.value.status_code == 403

    # 3. Mismatched idempotency_key is rejected (422)
    bad_key_body = deploy_api.RecoverDrainIn(**(valid_payload | {"idempotency_key": "wrong-key"}))
    with pytest.raises(HTTPException) as exc422:
        await deploy_api.recover_drain_endpoint(uuid.UUID(abcb["id"]), bad_key_body, root_sid, api_db)
    assert exc422.value.status_code == 422

    # 4. Non-active or draining API process refuses recovery (409)
    monkeypatch.setattr(generation, "current", SimpleNamespace(id=uuid.uuid4(), draining=False))
    with pytest.raises(HTTPException) as exc409:
        await deploy_api.recover_drain_endpoint(uuid.UUID(abcb["id"]), body, root_sid, api_db)
    assert exc409.value.status_code == 409

    # 5. Active Run markers (env TTAKKARI_RUN_ID, Run-bound bearer when env is unset,
    #    legacy bearer missing rid, mismatched session/Run marker, or active session Run row)
    #    are all rejected on apply=True without mutating the journal.
    import httpx

    from ttakkari.db import get_db
    from ttakkari.generation import GenerationRuntime
    from ttakkari.main import create_app
    from ttakkari.models import Run, RunStatus
    from ttakkari.orchestrator.runtime import issue_token

    current_rt = GenerationRuntime(tmp_path, revision=fences["expected_active_revision"], api_url="http://127.0.0.1:8793")
    current_rt.id = active_uuid
    current_rt.state = "active"
    monkeypatch.setattr(generation, "current", current_rt)
    monkeypatch.setenv("TTAKKARI_RUN_ID", "run-inside-sandbox")
    with pytest.raises(HTTPException) as exc_run:
        await deploy_api.recover_drain_endpoint(uuid.UUID(abcb["id"]), body, root_sid, api_db)
    assert exc_run.value.status_code == 409
    monkeypatch.delenv("TTAKKARI_RUN_ID", raising=False)

    run_id = uuid.uuid4()
    mismatched_run_id = uuid.uuid4()
    runs = {
        run_id: SimpleNamespace(id=run_id, session_id=root_sid, status=RunStatus.running),
        mismatched_run_id: SimpleNamespace(id=mismatched_run_id, session_id=worker_sid, status=RunStatus.running),
    }
    orig_get = api_db.get

    async def get_with_runs(model, ident):
        if model is Run:
            return runs.get(ident)
        return await orig_get(model, ident)

    api_db.get = get_with_runs

    # Direct controller call with run_id rejects apply=True even when TTAKKARI_RUN_ID is unset
    with pytest.raises(RuntimeError, match="never an agent Run"):
        await control.recover_drain(**fences, apply=True, run_id=run_id)
    assert control.store.get(abcb["id"])["status"] == "recovery_pending"

    # Direct endpoint call with (sid, run_id) tuple rejects apply=True when TTAKKARI_RUN_ID is unset
    with pytest.raises(HTTPException) as exc_tuple_run:
        await deploy_api.recover_drain_endpoint(uuid.UUID(abcb["id"]), body, (root_sid, run_id), api_db)
    assert exc_tuple_run.value.status_code == 409
    assert control.store.get(abcb["id"])["status"] == "recovery_pending"

    # Real HTTP ASGI boundary without TTAKKARI_RUN_ID in os.environ:
    app = create_app()

    async def db_override():
        yield api_db

    app.dependency_overrides[get_db] = db_override
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 5a. Run-bound bearer token with running Run -> rejected (409) on apply=True
            run_token = issue_token(root_sid, run_id)
            resp_run_apply = await client.post(
                f"/api/agent/deploy/{abcb['id']}/recover-drain",
                json=valid_payload,
                headers={"Authorization": f"Bearer {run_token}"},
            )
            assert resp_run_apply.status_code == 409
            assert control.store.get(abcb["id"])["status"] == "recovery_pending"

            # 5b. Legacy bearer token missing Run marker (rid=None) -> rejected (409) on apply=True
            legacy_token = issue_token(root_sid, None)
            resp_legacy_apply = await client.post(
                f"/api/agent/deploy/{abcb['id']}/recover-drain",
                json=valid_payload,
                headers={"Authorization": f"Bearer {legacy_token}"},
            )
            assert resp_legacy_apply.status_code == 409
            assert control.store.get(abcb["id"])["status"] == "recovery_pending"

            # 5c. Mismatched Run marker (Run belongs to another session) -> rejected (409) even on dry-run
            mismatched_token = issue_token(root_sid, mismatched_run_id)
            resp_mismatched = await client.post(
                f"/api/agent/deploy/{abcb['id']}/recover-drain",
                json=valid_payload | {"apply": False},
                headers={"Authorization": f"Bearer {mismatched_token}"},
            )
            assert resp_mismatched.status_code == 409
            assert control.store.get(abcb["id"])["status"] == "recovery_pending"

            # 5d. Matching Run-bound bearer on dry-run (apply=False) is allowed and does not mutate journal
            resp_dry_run = await client.post(
                f"/api/agent/deploy/{abcb['id']}/recover-drain",
                json=valid_payload | {"apply": False},
                headers={"Authorization": f"Bearer {run_token}"},
            )
            assert resp_dry_run.status_code == 200
            assert resp_dry_run.json()["applied"] is False
            assert control.store.get(abcb["id"])["status"] == "recovery_pending"
    finally:
        app.dependency_overrides.clear()

    # 6. Matching active API owner outside agent Run succeeds and writes AuditLog
    api_db.commits = 0
    api_db.added.clear()
    out = await deploy_api.recover_drain_endpoint(uuid.UUID(abcb["id"]), body, root_sid, api_db)
    assert out["ok"] is True and out["applied"] is True and out["action"] == "resume_draining"
    assert api_db.commits == 1
    assert any(getattr(x, "action", None) == "deployment.recover_drain" for x in api_db.added)
