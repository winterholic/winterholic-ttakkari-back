from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from ttakkari.config import get_settings
from ttakkari.deployment import Controller, JobStore


@pytest.fixture(autouse=True)
def external_controller(monkeypatch):
    monkeypatch.delenv("TTAKKARI_RUN_ID", raising=False)


def test_bootstrap_needs_explicit_authorization_and_stops_after_failure(tmp_path):
    from ttakkari.generation_bootstrap import requested
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    root = tmp_path / "state"
    root.mkdir()
    assert not requested(source, root)
    (source / "deploy/generation-bootstrap.json").write_text('{"enabled":true,"protocol":1}')
    assert requested(source, root)
    (root / "bootstrap-failed.json").write_text('{}')
    assert not requested(source, root)


def test_completed_bootstrap_selects_generation_queue(tmp_path, monkeypatch):
    from ttakkari.deployment import strategy
    monkeypatch.setattr(get_settings(), "deploy_strategy", "idle")
    monkeypatch.setattr(get_settings(), "home", tmp_path)
    root = tmp_path / "deploy"
    root.mkdir()
    assert strategy() == "idle"
    (root / "generations-enabled.json").write_text('{"protocol":1,"strategy":"generations"}')
    assert strategy() == "generations"
    store = JobStore(root)
    assert store.request("a" * 40)["strategy"] == "generations"
    assert store.request("b" * 40)["strategy"] == "generations"


class Host:
    def __init__(self):
        self.calls = []
        self.row = None
        self.active = None
        self.fail_verify = False

    async def prepare(self, job):
        self.calls.append("prepare")
        return Path("frozen-release")

    async def idle(self):
        self.calls.append("idle")

    async def candidate(self, release, revision):
        self.calls.append("candidate")
        self.row = {"generation": str(uuid.uuid4()), "revision": revision, "protocol": 1, "ready": True}
        return self.row

    async def stop_legacy(self):
        self.calls.append("stop_legacy")

    async def proxy(self):
        self.calls.append("proxy")

    async def verify(self, revision):
        self.calls.append("verify")
        if self.fail_verify:
            raise RuntimeError("public health failed")

    async def activate(self, generation):
        self.calls.append("activate")
        self.active = generation

    async def pin_supervisor(self, release, revision):
        self.calls.append("pin")

    async def restore_legacy(self):
        self.calls.append("restore")


async def test_bootstrap_checks_public_route_before_activation_and_enabling(tmp_path):
    from ttakkari.generation_bootstrap import Bootstrap
    controller = Controller(tmp_path)
    job = controller.store.request("a" * 40)
    host = Host()
    await Bootstrap(controller, host=host).process(job)
    assert host.calls == ["prepare", "idle", "candidate", "stop_legacy", "proxy", "verify", "activate", "verify", "pin"]
    assert host.active == host.row["generation"]
    assert controller.store.get(job["id"])["status"] == "succeeded"
    assert json.loads((tmp_path / "generations-enabled.json").read_text())["revision"] == "a" * 40
    lease = controller.admission.acquire(exclusive=True)
    assert lease is not None
    lease.close()


async def test_bootstrap_failed_health_restores_legacy_and_does_not_enable(tmp_path):
    from ttakkari.generation_bootstrap import Bootstrap
    controller = Controller(tmp_path)
    job = controller.store.request("a" * 40)
    host = Host()
    host.fail_verify = True
    await Bootstrap(controller, host=host).process(job)
    assert "activate" not in host.calls and host.calls[-1] == "restore"
    assert not (tmp_path / "generations-enabled.json").exists()
    assert (tmp_path / "bootstrap-failed.json").exists()
    assert controller.store.get(job["id"])["status"] == "failed"


async def test_bootstrap_keeps_activated_generation_on_supervisor_pin_failure(tmp_path):
    from ttakkari.generation_bootstrap import Bootstrap
    controller = Controller(tmp_path)
    job = controller.store.request("a" * 40)
    host = Host()

    async def fail(*args):
        raise RuntimeError("pin failed")

    host.pin_supervisor = fail
    await Bootstrap(controller, host=host).process(job)
    assert host.active and "restore" not in host.calls
    assert controller.store.get(job["id"])["status"] == "recovery_pending"


async def test_bootstrap_resumes_after_activation_without_starting_a_second_candidate(tmp_path):
    from ttakkari.generation_bootstrap import Bootstrap
    controller = Controller(tmp_path)
    job = controller.store.request("a" * 40)
    host = Host()
    original = host.pin_supervisor

    async def crash(*args):
        raise asyncio.CancelledError()

    host.pin_supervisor = crash
    with pytest.raises(asyncio.CancelledError):
        await Bootstrap(controller, host=host).process(job)
    host.pin_supervisor = original
    await Bootstrap(controller, host=host).process(controller.store.get(job["id"]))
    assert host.calls.count("candidate") == 1
    assert controller.store.get(job["id"])["status"] == "succeeded"


async def test_holding_backend_rejects_new_requests(client, auth, monkeypatch):
    from ttakkari.main import app
    monkeypatch.setattr(app.state, "bootstrap_hold", True, raising=False)
    assert (await client.get("/api/health")).status_code == 200
    response = await client.get("/api/workspaces", headers=auth)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    ready = await client.get("/api/bootstrap-ready")
    assert ready.status_code == 200 and ready.json()["holding"] is True


def test_transition_waits_for_a_verified_legacy_deploy(tmp_path, monkeypatch):
    from ttakkari import deployment
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "deploy/generation-bootstrap.json").write_text('{"enabled":true,"protocol":1}')
    monkeypatch.setattr(deployment, "BACK", source)
    monkeypatch.setattr(deployment, "revision", lambda: "a" * 40)
    controller = Controller(tmp_path / "state")
    job = controller.store.request("a" * 40)
    assert controller.bootstrap_job() is None
    controller.store.update(job["id"], status="verifying")
    assert controller.bootstrap_job() is None
    controller.store.update(job["id"], status="succeeded")
    assert controller.bootstrap_job()["bootstrap"] is True
    assert controller.store.get(job["id"])["status"] == "bootstrapping"


async def test_migration_safety_rejects_busy_legacy_work(client, auth, new_session):
    from helpers import start_run, wait_run

    from ttakkari.generation_bootstrap import assert_legacy_jobs_idle
    from ttakkari.runs.manager import manager
    _, session = await new_session()
    run = await start_run(client, auth, session["id"], "SLEEP")
    for _ in range(50):
        if (await client.get(f"/api/runs/{run['id']}", headers=auth)).json()["status"] == "running":
            break
        await asyncio.sleep(0.02)
    with pytest.raises(RuntimeError, match="legacy Run/STT/preview"):
        await assert_legacy_jobs_idle()
    await manager.cancel(uuid.UUID(run["id"]))
    await wait_run(client, auth, run["id"])
    await assert_legacy_jobs_idle()


async def test_bootstrap_holding_lifespan_does_not_recover_or_start_consumers(monkeypatch):
    from fastapi import FastAPI

    from ttakkari import generation_bootstrap, main

    async def forbidden(*args):
        raise AssertionError("holding app must not recover or launch consumers")

    monkeypatch.setattr(generation_bootstrap, "holding", lambda: True)
    monkeypatch.setattr(main, "init_engine", lambda: None)
    monkeypatch.setattr(main, "dispose_engine", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "recover_interrupted", forbidden)
    monkeypatch.setattr(main.preview, "start_worker", forbidden)
    monkeypatch.setattr(main.local_stt_jobs, "start", forbidden)
    app = FastAPI()
    async with main.lifespan(app):
        assert app.state.bootstrap_hold


async def test_run_cannot_invoke_bootstrap_service_control(tmp_path, monkeypatch):
    from ttakkari.generation_bootstrap import Bootstrap
    monkeypatch.setenv("TTAKKARI_RUN_ID", str(uuid.uuid4()))
    controller = Controller(tmp_path)
    host = Host()
    with pytest.raises(RuntimeError, match="independent"):
        await Bootstrap(controller, host=host).process(controller.store.request("a" * 40))
    assert not host.calls


@pytest.mark.parametrize("stream_body,ok", [("event: end\ndata: {}\n\n", True),
                                            ("event: ping\ndata: {}\n\n", False),
                                            ("id: 1\nevent: end\ndata: {}\n\n", False)])
async def test_public_smoke_verifies_authenticated_read_and_sse_cursor(
    tmp_path, monkeypatch, client, auth, new_session, stream_body, ok
):
    import httpx
    from helpers import start_run, wait_run

    from ttakkari.generation_bootstrap import Services
    _, session = await new_session()
    run = await start_run(client, auth, session["id"], "bootstrap smoke fixture")
    assert (await wait_run(client, auth, run["id"]))["status"] == "succeeded"
    host = Services(Controller(tmp_path))
    monkeypatch.setattr(host.slots, "verify", lambda _: asyncio.sleep(0))
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path.endswith("/stream"):
            assert request.headers["last-event-id"].isdigit()
            return httpx.Response(200, text=stream_body, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=[])

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    if ok:
        await host.verify("a" * 40)
        assert json.loads((tmp_path / "bootstrap-smoke.json").read_text())["sse_cursor"] is True
    else:
        with pytest.raises(RuntimeError, match="SSE"):
            await host.verify("a" * 40)
        assert not (tmp_path / "bootstrap-smoke.json").exists()


async def test_bootstrap_reports_its_verified_outcome_to_the_waiting_user(tmp_path, monkeypatch):
    from ttakkari.generation_bootstrap import Bootstrap
    from ttakkari.push import service
    notifications = []

    async def notify(kind, title, body, **kwargs):
        notifications.append((title, body))
        return {"sent": 1, "failed": 0, "removed": 0}

    monkeypatch.setattr(service, "notify", notify)
    controller = Controller(tmp_path)
    job = controller.store.request("a" * 40)
    await Bootstrap(controller, host=Host()).process(job)
    assert notifications and "활성화" in notifications[0][0]
    assert controller.store.get(job["id"])["bootstrap_notification"]["sent"] == 1


async def test_supervisor_plist_and_crash_redirect_use_the_frozen_release(tmp_path, monkeypatch, make_git_dir):
    import plistlib

    from conftest import git

    from ttakkari import deployment
    from ttakkari.generation_bootstrap import Services
    # pin은 유효한 프로젝트 저장소를 요구한다. 릴리스 아카이브 안에서 돌 때도 마찬가지다.
    source = make_git_dir()
    (source / "deploy").mkdir()
    (source / "deploy/generation-contract.json").write_text("{}")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "contract")
    monkeypatch.setattr(get_settings(), "deploy_source_root", source)
    controller = Controller(tmp_path / "state")
    host = Services(controller)
    host.launch_agents = tmp_path / "LaunchAgents"
    host.launch_agents.mkdir()
    (controller.store.root / "services").mkdir()
    plist = host.launch_agents / f"{deployment.LABEL}.plist"
    original = {"Label": deployment.LABEL, "WorkingDirectory": "/mutable",
                "ProgramArguments": ["uv", "run"], "EnvironmentVariables": {"PATH": "/fixed-bin"}}
    plist.write_bytes(plistlib.dumps(original))
    release = tmp_path / "release"
    await host.pin_supervisor(release, "a" * 40)
    actual = plistlib.loads(plist.read_bytes())
    assert actual["WorkingDirectory"] == str(release)
    assert actual["ProgramArguments"][0] == str(release / ".venv/bin/python")
    assert actual["EnvironmentVariables"]["TTAKKARI_DEPLOY_STRATEGY"] == "generations"
    assert plistlib.loads((controller.store.root / "services/legacy-deployer.plist").read_bytes()) == original
    entered = []
    monkeypatch.setattr(deployment.os, "chdir", lambda path: entered.append(path))
    monkeypatch.setattr(deployment.os, "execve", lambda *args: entered.append(args))
    controller.enter_pinned_supervisor()
    assert entered[0] == release
    assert entered[1][0] == str(release / ".venv/bin/python")
    assert entered[1][2]["TTAKKARI_RELEASE_REVISION"] == "a" * 40


async def test_failed_preparation_restarts_the_holding_backend_into_normal_legacy_mode(tmp_path, monkeypatch):
    from ttakkari.generation_bootstrap import Services
    controller = Controller(tmp_path)
    host = Services(controller)
    host.expected_revision = "a" * 40
    calls = []
    monkeypatch.setattr(host.slots, "snapshot", lambda _: asyncio.sleep(0, result=None))
    monkeypatch.setattr(host.slots, "current_slot", lambda: asyncio.sleep(0, result=None))
    monkeypatch.setattr(host, "registered", lambda _: asyncio.sleep(0, result=True))

    async def launch(*args):
        calls.append(args[0])

    async def verify(*args, **kwargs):
        calls.append("verified")

    monkeypatch.setattr(host.slots, "_launchctl", launch)
    monkeypatch.setattr(controller, "verify", verify)
    await host.restore_legacy()
    assert calls == ["bootout", "bootstrap", "verified"]
