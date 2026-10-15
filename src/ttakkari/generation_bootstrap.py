"""One authorized, idle transition; service operations run only in the deployer.

The first legacy deploy is performed by the existing controller. Its replacement
starts in a holding state, then this controller prepares an immutable standby,
checks the public proxy before activation, and pins the supervisor. A journal
distinguishes a pre-activation rollback from an activation needing reconciliation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import plistlib
import shutil
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker

log = logging.getLogger(__name__)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def requested(source: Path, root: Path) -> bool:
    if (root / "generations-enabled.json").exists() or (root / "bootstrap-failed.json").exists():
        return False
    path = source / "deploy/generation-bootstrap.json"
    if not path.exists():
        return False
    plan = json.loads(path.read_text())
    return plan.get("enabled") is True and plan.get("protocol") == 1


def holding() -> bool:
    from ttakkari.deployment import BACK, state_dir
    settings = get_settings()
    return (not settings.runtime_generation and not os.environ.get("TTAKKARI_NO_ENV_FILE")
            and requested(BACK, state_dir()))


async def assert_legacy_jobs_idle() -> None:
    # This also runs immediately before the old controller's first migration.
    # No new model/preview inference may be interrupted by its legacy restart.
    async with sessionmaker()() as db:
        busy = await db.scalar(text("""SELECT
            (SELECT count(*) FROM runs WHERE status = 'running') +
            (SELECT count(*) FROM local_stt_jobs WHERE status = 'running') +
            (SELECT count(*) FROM artifacts WHERE preview_status = 'processing')"""))
    if busy:
        raise RuntimeError("initial transition deferred: legacy Run/STT/preview work remains")


class Services:
    def __init__(self, controller):
        from ttakkari.generation_deployment import GenerationController
        self.controller = controller
        self.generation = GenerationController(controller.store)
        self.slots = self.generation.slots
        self.root = controller.store.root
        self.launch_agents = Path.home() / "Library/LaunchAgents"
        self.legacy = self.launch_agents / "net.winterholic.ttakkari.backend.plist"
        self.backup = self.root / "services/legacy-backend.plist"

    async def prepare(self, job):
        from ttakkari.generation_deployment import Releases, proxy_config
        release = await asyncio.to_thread(self.generation.releases.archive, job["revision"])
        await self.generation.environment(job, release)
        async with sessionmaker()() as db:
            schema = await db.scalar(text("SELECT version_num FROM alembic_version"))
        if schema != Releases.contract(release)["schema_revision"]:
            raise RuntimeError("bootstrap migration has not completed")
        caddy = shutil.which("caddy")
        if caddy is None:
            raise RuntimeError("Caddy installation required; legacy backend preserved")
        self.expected_revision = job["revision"]
        if await self.generation.active() is not None:
            raise RuntimeError("an active generation already exists; explicit reconciliation required")
        config = self.root / "bootstrap-proxy.json"
        write_json(config, proxy_config(0))
        await self.generation.command(job, release, caddy, "validate", "--config", str(config))
        # Validate the actual archive and its isolated Python before touching services.
        await self.generation.command(job, release, str(release / ".venv/bin/python"), "-c",
            "import ttakkari.generation_bootstrap, ttakkari.generation_service")
        return release

    async def idle(self):
        await assert_legacy_jobs_idle()
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get("http://127.0.0.1:8787/api/bootstrap-ready")
            response.raise_for_status()
            if response.json().get("holding") is not True:
                raise RuntimeError("legacy backend did not enter the holding state")
            if response.json().get("revision") != self.expected_revision:
                raise RuntimeError("holding backend revision mismatch")

    async def candidate(self, release, revision):
        return await self.slots.start(0, release, revision)

    async def registered(self, label):
        process = await asyncio.create_subprocess_exec("launchctl", "print", f"gui/{os.getuid()}/{label}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await process.wait() == 0

    async def stop_legacy(self):
        label = "net.winterholic.ttakkari.backend"
        if await self.registered(label):
            await self.slots._launchctl("bootout", f"gui/{os.getuid()}/{label}")
        if self.legacy.exists():
            self.backup.parent.mkdir(parents=True, exist_ok=True)
            self.legacy.replace(self.backup)

    async def proxy(self):
        await self.slots.ensure_proxy(0)
        if await self.slots.current_slot() != 0:
            raise RuntimeError("bootstrap proxy points to an unexpected slot")

    async def verify(self, revision):
        from sqlalchemy import select

        from ttakkari.models import Run
        from ttakkari.security.auth import issue_access_token
        await self.slots.verify(revision)
        headers = {"Authorization": "Bearer " + issue_access_token()[0]}
        async with sessionmaker()() as db:
            completed = (await db.execute(select(Run.id, Run.last_seq).where(
                Run.status.in_(("succeeded", "failed", "cancelled")))
                .order_by(Run.created_at.desc()).limit(1))).first()
        async with httpx.AsyncClient(timeout=10) as client:
            base = "https://ttakkari-api.winterholic.net"
            response = await client.get(base + "/api/workspaces", headers=headers)
            response.raise_for_status()
            if not isinstance(response.json(), list):
                raise RuntimeError("authenticated read returned an invalid body")  # noqa: TRY004 -- remote protocol error
            if completed:
                run_id, cursor = completed
                response = await client.get(base + f"/api/runs/{run_id}/stream",
                    headers=headers | {"Last-Event-ID": str(cursor)})
                response.raise_for_status()
                lines = response.text.splitlines()
                if "event: end" not in lines or any(line.startswith("id:") for line in lines):
                    raise RuntimeError("SSE cursor did not resume at the terminal event")
        write_json(self.root / "bootstrap-smoke.json", {"revision": revision, "health": True,
            "authenticated_read": True, "sse_cursor": completed is not None})

    async def activate(self, generation):
        current = await self.generation.active()
        candidate = uuid.UUID(generation)
        if current != candidate:
            if current is not None:
                raise RuntimeError("unexpected active generation during bootstrap")
            await self.generation.activate(candidate, expected=None)

    async def pin_supervisor(self, release, revision):
        from ttakkari.deployment import BACK, LABEL
        source = get_settings().deploy_source_root or BACK
        path = self.launch_agents / f"{LABEL}.plist"
        original = plistlib.loads(path.read_bytes())
        backup = self.root / "services/legacy-deployer.plist"
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        env = original.get("EnvironmentVariables", {}) | {
            "TTAKKARI_HOME": str(get_settings().home),
            "TTAKKARI_DEPLOY_SOURCE_ROOT": str(source),
            "TTAKKARI_RELEASE_REVISION": revision,
            "TTAKKARI_DEPLOY_STRATEGY": "generations"}
        original.update(WorkingDirectory=str(release),
            ProgramArguments=[str(release / ".venv/bin/python"), "-m", "ttakkari.deployment", "serve"],
            EnvironmentVariables=env)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(original))
        temporary.replace(path)
        write_json(self.root / "supervisor.json", {"revision": revision,
            "release": str(release), "source": str(source)})

    async def restore_legacy(self):
        # Called only before activation. A standby cannot own model jobs or Runs.
        ready = await self.slots.snapshot(0)
        if ready:
            if ready.get("state") != "standby" or ready.get("owned_runs") or ready.get("work") or ready.get("http_inflight"):
                raise RuntimeError("candidate may own work; refusing legacy rollback")
            await self.slots._launchctl("bootout", f"gui/{os.getuid()}/net.winterholic.ttakkari.generation.8791")
        if await self.slots.current_slot() is not None:
            await self.slots._launchctl("bootout", f"gui/{os.getuid()}/net.winterholic.ttakkari.proxy")
        if self.backup.exists():
            self.backup.replace(self.legacy)
        # Reload even if preparation failed before bootout: the interim backend
        # must leave its in-memory holding lifespan and start its normal queues.
        if await self.registered("net.winterholic.ttakkari.backend"):
            await self.slots._launchctl("bootout", f"gui/{os.getuid()}/net.winterholic.ttakkari.backend")
        await self.slots._launchctl("bootstrap", f"gui/{os.getuid()}", str(self.legacy))
        await self.controller.verify({"revision": self.expected_revision}, old_pid=None)


class Bootstrap:
    def __init__(self, controller, *, host=None):
        self.controller = controller
        self.store = controller.store
        self.host = host or Services(controller)

    async def process(self, job):
        if os.environ.get("TTAKKARI_RUN_ID"):
            raise RuntimeError("bootstrap belongs to the independent deployment controller")
        lease = self.controller.admission.acquire(exclusive=True)
        if lease is None:
            return
        try:
            self.host.expected_revision = job["revision"]
            self.controller.phase(job, "bootstrapping", bootstrap=True)
            release = Path(job["bootstrap_release"]) if job.get("bootstrap_release") else await self.host.prepare(job)
            self.controller.phase(job, "bootstrapping", bootstrap_release=str(release))
            if not job.get("bootstrap_generation"):
                await self.host.idle()
                candidate = await self.host.candidate(release, job["revision"])
                if candidate.get("protocol") != 1 or candidate.get("revision") != job["revision"] or not candidate.get("ready"):
                    raise RuntimeError("bootstrap candidate identity mismatch")
                self.controller.phase(job, "bootstrapping", bootstrap_generation=candidate["generation"])
            if not job.get("bootstrap_activation_started"):
                self.controller.phase(job, "bootstrapping", bootstrap_stop_started=True)
                await self.host.stop_legacy()
                await self.host.proxy()
                await self.host.verify(job["revision"])
                self.controller.phase(job, "bootstrapping", bootstrap_activation_started=True)
            await self.host.activate(job["bootstrap_generation"])
            self.controller.phase(job, "bootstrapping", bootstrap_activated=True)
            await self.host.verify(job["revision"])
            await self.host.pin_supervisor(release, job["revision"])
            write_json(self.store.root / "generations-enabled.json", {
                "strategy": "generations", "protocol": 1, "revision": job["revision"],
                "generation": job["bootstrap_generation"]})
            self.controller.succeed(job)
        except Exception as exc:  # noqa: BLE001 -- deployment boundary: persist failure and restore safely
            if job.get("bootstrap_activation_started"):
                self.controller.phase(job, "recovery_pending", error=str(exc))
            else:
                write_json(self.store.root / "bootstrap-failed.json", {"job": job["id"], "error": str(exc)})
                try:
                    await self.host.restore_legacy()
                except Exception as rollback:  # noqa: BLE001 -- preserve failed rollback in the journal
                    self.controller.phase(job, "recovery_pending", error=f"{exc}; restore: {rollback}")
                else:
                    self.controller.phase(job, "failed", error=str(exc))
        finally:
            lease.close()
        if job["status"] in {"succeeded", "failed", "recovery_pending"} and not job.get("bootstrap_notification"):
            from ttakkari.push.service import PushKind, notify
            success = job["status"] == "succeeded"
            try:
                counts = await notify(PushKind.deployment,
                    "무중단 배포 운영 활성화" if success else "최초 배포 전환 확인 필요",
                    "안전 전환과 공개 경로·인증 조회·이벤트 이어받기 검증을 마쳤습니다." if success
                    else "안전장치가 전환을 멈췄습니다. 기존 작업은 보존하며 배포 기록에 원인을 남겼습니다.",
                    tag="generation-bootstrap", target_id=job["id"])
            except Exception:
                log.exception("bootstrap outcome notification failed")
            else:
                self.store.update(job["id"], bootstrap_notification=counts)
