"""External generation deployer. Never invoke service control from an agent Run.

The database CAS and proxy reload cannot be atomic. Journal the intent before
either side effect; each subsequent pass reconciles observable state.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

import asyncpg
import httpx
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.generation import PORTS, PROTOCOL, _lock, activate
from ttakkari.models import RuntimeControl, RuntimeGeneration, RuntimeLease

log = logging.getLogger(__name__)
_LOG_TAIL_LINES = 12
_LOG_TAIL_CHARS = 4000
# 준비 단계별 상한(초). 감독 루프를 묶어 두었던 장시간 빌드·검사를 fencing한다.
STAGE_TIMEOUTS = {"uv sync": 600, "npm ci": 600, "pytest": 1800, "migrate": 600,
                  "command": 600}
PREPARE_TOTAL_TIMEOUT = 3600
# job id -> 실행 중인 준비 태스크. 감독 패스 사이에서 유지된다.
_PREPARE_TASKS: dict[str, asyncio.Task] = {}


def _utcnow() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


class StageError(RuntimeError):
    """단계·명령·종료코드를 보존한 배포 실패. 로그 문자열 추측 대신 이것을 기록한다."""

    def __init__(self, stage: str, command: tuple | list | None, exit_code: int | None,
                 summary: str):
        self.stage = stage
        self.command = " ".join(command) if command else None
        self.exit_code = exit_code
        self.summary = summary
        # 기존 "release stage {stage} failed (exit N)" 접두를 유지한다. oplog
        # 그룹핑이 이 형식에 의존한다.
        tail = f" (exit {exit_code})" if exit_code is not None else ""
        super().__init__(f"release stage {stage} failed{tail}; {summary}")


def test_environment(revision: str, test_url: str) -> dict:
    env = os.environ | {"TTAKKARI_TEST_DB": test_url, "TTAKKARI_RELEASE_REVISION": revision}
    for key in ("TTAKKARI_RUNTIME_GENERATION", "TTAKKARI_DEPLOY_STRATEGY",
                "TTAKKARI_DEPLOY_SOURCE_ROOT", "TTAKKARI_BACKEND_DEPLOY_LOCK_HELD"):
        env.pop(key, None)
    return env


def _safe_log_tail(path: Path, env: dict[str, str] | None) -> str:
    try:
        tail = "\n".join(path.read_text(errors="replace").splitlines()[-_LOG_TAIL_LINES:])
    except OSError:
        return "(log tail unavailable)"
    secrets = {
        value for key, value in (env or {}).items()
        if value and any(part in key.upper() for part in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "AUTH", "COOKIE"))
    }
    for secret in sorted(secrets, key=len, reverse=True):
        tail = tail.replace(secret, "[REDACTED]")
    tail = re.sub(r"(?i)(://[^:/\s@]+:)[^@\s]+@", r"\1[REDACTED]@", tail)
    tail = re.sub(
        r"(?i)\b(password|token|secret|api[_-]?key|authorization|cookie)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]", tail,
    )
    tail = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", tail)
    return tail[-_LOG_TAIL_CHARS:] or "(no output)"


def _sha(value: str) -> str:
    if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("full commit SHA required")
    return value


def equivalent_contracts(source: Path, old_revision: str, new_revision: str) -> bool:
    try:
        def contract(sha):
            raw = subprocess.check_output(["git", "show", f"{_sha(sha)}:deploy/generation-contract.json"],
                                          cwd=source, stderr=subprocess.DEVNULL)
            return json.loads(raw)
        before, after = contract(old_revision), contract(new_revision)
        return before == after and before.get("protocol") == PROTOCOL
    except (subprocess.CalledProcessError, ValueError, OSError):
        return False


class Releases:
    def __init__(self, source: Path, root: Path):
        self.source, self.root = source, root

    def archive(self, revision: str) -> Path:
        revision = _sha(revision)
        # git archive exit 128 대신 원인을 명시한다. 릴리스 경로 오지정이면 여기서 거부.
        try:
            subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
                cwd=self.source, stderr=subprocess.DEVNULL, text=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError(
                "배포 소스가 유효한 Git 작업 트리가 아닙니다: "
                f"{self.source}. TTAKKARI_DEPLOY_SOURCE_ROOT를 원본 레포 경로로 지정하세요."
            ) from exc
        target = self.root / "releases" / revision
        if target.exists():
            if json.loads((target / ".release.json").read_text())["revision"] != revision:
                raise ValueError("release marker mismatch")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        # git archive reads the requested object, never the mutable checkout.
        raw = subprocess.check_output(["git", "archive", revision], cwd=self.source)
        staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=target.parent))
        try:
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                archive.extractall(staging, filter="data")
            self.contract(staging)
            (staging / ".release.json").write_text(json.dumps({"revision": revision}))
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return target

    @staticmethod
    def contract(path: Path) -> dict:
        value = json.loads((path / "deploy/generation-contract.json").read_text())
        return validate_contract(value)


def validate_contract(value: dict) -> dict:
    if value.get("protocol") != PROTOCOL or not value.get("schema_revision"):
        raise ValueError("unsupported generation/schema contract; idle bootstrap required")
    if not isinstance(value.get("online_migration_from"), list):
        raise TypeError("online migration policy must be explicit")
    supervisor = value.get("supervisor")
    if supervisor is not None and (
        not isinstance(supervisor, dict)
        or not isinstance(supervisor.get("protocol"), int)
        or not isinstance(supervisor.get("slots"), int)
    ):
        raise TypeError("supervisor requirement must declare integer protocol/slots")
    return value


def contract_for_revision(source: Path, revision: str) -> dict:
    """작업 트리를 건드리지 않고 특정 커밋의 계약을 읽는다."""
    raw = subprocess.check_output(["git", "show", f"{_sha(revision)}:deploy/generation-contract.json"],
                                  cwd=source, stderr=subprocess.DEVNULL)
    return validate_contract(json.loads(raw))


def required_supervisor_capabilities(contract: dict) -> dict:
    """릴리스가 요구하는 감독 기능. 키가 없는 옛 계약은 2슬롯 감독을 가정한다."""
    required = contract.get("supervisor") or {}
    return {"protocol": required.get("protocol", 1), "slots": required.get("slots", 2)}


def supervisor_satisfies(contract: dict) -> bool:
    from ttakkari import generation
    current = generation.supervisor_capabilities()
    required = required_supervisor_capabilities(contract)
    return current["protocol"] == required["protocol"] and current["slots"] >= required["slots"]


def proxy_config(slot: int) -> dict:
    port = PORTS[slot]
    return {"admin": {"listen": "127.0.0.1:20191", "config": {"persist": False}}, "apps": {"http": {"servers": {"ttakkari": {
        "listen": ["127.0.0.1:8787"], "automatic_https": {"disable": True}, "routes": [
            {"match": [{"path": ["/api/ready", "/api/bootstrap-ready"]}], "handle": [{"handler": "static_response", "status_code": 404}]},
            {"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": f"127.0.0.1:{port}"}],
                         "flush_interval": -1, "stream_close_delay": 60_000_000_000}]}]}}}}}


class Slots:
    """launchd jobs live under deploy/services, not login-auto-loaded LaunchAgents."""
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def origin(slot: int) -> str:
        return f"http://127.0.0.1:{PORTS[slot]}"

    async def snapshot(self, slot: int) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(self.origin(slot) + "/api/ready")
                response.raise_for_status()
                value = response.json()
                if value.get("protocol") != PROTOCOL or not value.get("ready"):
                    return None
                return value
        except (httpx.HTTPError, ValueError):
            return None

    async def _launchctl(self, *args) -> None:
        if os.environ.get("TTAKKARI_RUN_ID"):
            raise RuntimeError("service control belongs to the external deployer")
        proc = await asyncio.create_subprocess_exec("launchctl", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _out, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError("launchd operation failed: " + err.decode(errors="replace")[-500:])

    async def start(self, slot: int, release: Path, revision: str) -> dict:
        existing = await self.snapshot(slot)
        if existing:
            if existing["revision"] != revision:
                raise RuntimeError("generation slot is occupied")
            return existing
        # Do not replace a possibly unhealthy live service: an occupied TCP port
        # or a registered launchd label must be investigated, never force-killed.
        services = self.root / "services"
        services.mkdir(parents=True, exist_ok=True)
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        label = f"net.winterholic.ttakkari.generation.{PORTS[slot]}"
        plist = services / f"{label}.plist"
        source = str(get_settings().deploy_source_root or Path(__file__).resolve().parents[2])
        env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", ""),
            "TTAKKARI_HOME": str(get_settings().home), "TTAKKARI_RUNTIME_GENERATION": "true",
            "TTAKKARI_RUNTIME_API_URL": self.origin(slot), "TTAKKARI_RELEASE_REVISION": revision,
            "TTAKKARI_DEPLOY_SOURCE_ROOT": source, "TTAKKARI_DEPLOY_STRATEGY": "generations"}
        plist.write_bytes(plistlib.dumps({"Label": label, "WorkingDirectory": str(release),
            "ProgramArguments": [str(release / ".venv/bin/python"), "-m", "ttakkari.cli", "serve",
                                  "--host", "127.0.0.1", "--port", str(PORTS[slot])],
            "EnvironmentVariables": env, "KeepAlive": True, "ThrottleInterval": 10, "RunAtLoad": True,
            "StandardOutPath": str(logs / f"{PORTS[slot]}.log"),
            "StandardErrorPath": str(logs / f"{PORTS[slot]}.log")}))
        await self._launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
        for _ in range(100):
            if ready := await self.snapshot(slot):
                if ready["revision"] != revision:
                    raise RuntimeError("unexpected slot revision")
                return ready
            await asyncio.sleep(0.3)
        raise RuntimeError("candidate readiness timed out; existing generation was preserved")

    async def stop(self, slot: int) -> None:
        ready = await self.snapshot(slot)
        if ready and not ready.get("drain_ready"):
            raise RuntimeError("refusing to stop a generation with unfinished work")
        # If it is unreachable, do not infer that its descendants are dead.
        if ready is None:
            raise RuntimeError("unreachable generation needs explicit recovery")
        await self._launchctl("bootout", f"gui/{os.getuid()}/net.winterholic.ttakkari.generation.{PORTS[slot]}")

    async def retired(self, slot: int, generation_id: str) -> bool:
        async with sessionmaker()() as db:
            row = await db.get(RuntimeGeneration, uuid.UUID(generation_id))
            if row is None or row.api_url != self.origin(slot):
                return False
            if await db.scalar(select(func.count()).select_from(RuntimeLease).where(RuntimeLease.generation_id == row.id)):
                return False
            probe = _lock(get_settings().home / "runtime" / f"{row.id}.lock")
            if probe is None:
                return False
            probe.close()
            return True

    @staticmethod
    def config_slot(config: dict) -> int:
        dial = config["apps"]["http"]["servers"]["ttakkari"]["routes"][-1]["handle"][0]["upstreams"][0]["dial"]
        return PORTS.index(int(dial.removeprefix("127.0.0.1:")))

    def persisted_slot(self) -> int | None:
        try:
            return self.config_slot(json.loads((self.root / "proxy.json").read_text()))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None

    async def current_slot(self) -> int | None:
        async with httpx.AsyncClient(timeout=3) as client:
            try:
                response = await client.get("http://127.0.0.1:20191/config/")
            except httpx.ConnectError:
                return None
            response.raise_for_status()
            # A responding but unexpected admin belongs to an operator to inspect.
            return self.config_slot(response.json())

    def persist_proxy(self, slot: int) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / "proxy.json"
        temp = target.with_suffix(".tmp")
        with temp.open("w") as output:
            json.dump(proxy_config(slot), output)
            output.flush()
            os.fsync(output.fileno())
        temp.replace(target)

    async def ensure_proxy(self, slot: int) -> None:
        if await self.current_slot() is not None:
            return
        caddy = shutil.which("caddy")
        if caddy is None:
            raise RuntimeError("Caddy installation required")
        self.persist_proxy(slot)
        services, logs = self.root / "services", self.root / "logs"
        services.mkdir(parents=True, exist_ok=True)
        logs.mkdir(exist_ok=True)
        label = "net.winterholic.ttakkari.proxy"
        plist = services / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({"Label": label,
            "ProgramArguments": [caddy, "run", "--config", str(self.root / "proxy.json")],
            "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "")},
            "KeepAlive": True, "ThrottleInterval": 10, "RunAtLoad": True,
            "StandardOutPath": str(logs / "proxy.log"), "StandardErrorPath": str(logs / "proxy.log")}))
        await self._launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
        for _ in range(50):
            if await self.current_slot() is not None:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError("proxy readiness timed out; generation was preserved")

    async def switch(self, slot: int) -> None:
        config = proxy_config(slot)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post("http://127.0.0.1:20191/load", json=config)
            response.raise_for_status()
            response = await client.get("http://127.0.0.1:20191/config/")
            response.raise_for_status()
            actual = response.json()["apps"]["http"]["servers"]["ttakkari"]["routes"][-1]["handle"][0]["upstreams"][0]["dial"]
            if actual != f"127.0.0.1:{PORTS[slot]}":
                raise RuntimeError("proxy did not retain the selected upstream")
        self.persist_proxy(slot)

    async def verify(self, revision: str) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            for origin in ("http://127.0.0.1:8787", "https://ttakkari-api.winterholic.net"):
                health = await client.get(origin + "/api/health")
                health.raise_for_status()
                version = await client.get(origin + "/api/version", headers={"Cache-Control": "no-cache"})
                version.raise_for_status()
                if health.json().get("ok") is not True or version.json().get("revision") != revision:
                    raise RuntimeError("public/local revision or health mismatch")


class GenerationController:
    def __init__(self, store, *, slots=None):
        self.store = store
        self.slots = slots or Slots(store.root)
        from ttakkari.deployment import BACK
        self.releases = Releases(get_settings().deploy_source_root or BACK, store.root)
        self.activate = activate

    def phase(self, job, status, **fields):
        job.update(self.store.update(job["id"], status=status, **fields))

    async def active(self):
        async with sessionmaker()() as db:
            control = await db.get(RuntimeControl, 1)
            return control.active_generation if control else None

    async def generation_info(self, generation_id):
        async with sessionmaker()() as db:
            row = await db.get(RuntimeGeneration, generation_id)
            if row is None:
                raise RuntimeError("active generation record is missing")
            return {"id": row.id, "revision": row.revision, "api_url": row.api_url}

    async def reconcile_services(self):
        generation_id = await self.active()
        if generation_id is None:
            return  # The first transition from legacy mode requires an idle bootstrap.
        info = await self.generation_info(generation_id)
        slot = next((i for i in range(len(PORTS)) if self.slots.origin(i) == info["api_url"]), None)
        if slot is None:
            raise RuntimeError("active generation has an unmanaged origin")
        ready = await self.slots.snapshot(slot)
        if ready is None:
            release = self.releases.root / "releases" / _sha(info["revision"])
            if Releases.contract(release)["protocol"] != PROTOCOL:
                raise RuntimeError("cannot restore an incompatible generation")
            ready = await self.slots.start(slot, release, info["revision"])
        # Registering a new process may replace a dead boot UUID for new work.
        # Existing Run leases stay with the previous process and need explicit recovery.
        current = await self.active()
        if (ready.get("protocol") != PROTOCOL or ready["revision"] != info["revision"]
                or uuid.UUID(ready["generation"]) != current):
            raise RuntimeError("restored service identity differs from the active owner")
        await self.slots.ensure_proxy(slot)
        if await self.slots.current_slot() != slot or self.slots.persisted_slot() != slot:
            await self.slots.switch(slot)

    def record_failure(self, job, status: str, exc: BaseException, *,
                       stage: str | None = None, command=None, exit_code: int | None = None) -> None:
        """배포 레코드에 단계·명령·종료코드·요약을 구조화해 남긴다.

        StageError면 그 안의 값을 우선한다. error(문자열)는 하위호환용으로 유지해
        backend_deploy_status·ops 조회가 그대로 읽는다.
        """
        if isinstance(exc, StageError):
            stage = stage or exc.stage
            command = command or exc.command
            exit_code = exit_code if exit_code is not None else exc.exit_code
            summary = exc.summary
        else:
            summary = str(exc)
        self.phase(job, status, error=str(exc), error_stage=stage,
                   error_command=command, error_exit=exit_code,
                   error_summary=(f"{stage}: {summary}" if stage else summary)[:2000])

    async def command(self, job, release, *args, stage="command", env=None, timeout=None):
        log_path = self.store.jobs / f"{job['id']}.log"
        limit = timeout if timeout is not None else STAGE_TIMEOUTS.get(stage, 600)
        # 단계 진입을 기록한다. 감독 루프는 이 heartbeat로 진척을 본다.
        self.phase(job, job["status"], prepare_stage=stage, prepare_heartbeat=_utcnow())
        with log_path.open("ab") as output:
            try:
                proc = await asyncio.create_subprocess_exec(*args, cwd=release, env=env,
                    stdout=output, stderr=output)
            except OSError as exc:
                raise StageError(stage, args, None,
                                 f"could not start ({type(exc).__name__}): {exc}") from exc
            try:
                returncode = await asyncio.wait_for(proc.wait(), limit)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    proc.kill()
                await proc.wait()
                raise StageError(stage, args, None,
                                 f"timed out after {limit}s; existing generation was preserved") from None
        if returncode:
            tail = _safe_log_tail(log_path, env if env is not None else os.environ)
            raise StageError(stage, args, returncode,
                             f"last {_LOG_TAIL_LINES} log lines:\n{tail}")

    async def environment(self, job, release):
        if not (release / ".environment-built").exists():
            await self.command(job, release, "uv", "sync", "--frozen", "--quiet", stage="uv sync")
            (release / ".environment-built").touch()
        if (release / ".browser-runtime/package-lock.json").is_file() and not (release / ".browser-built").exists():
            await self.command(job, release, "npm", "ci", "--prefix", ".browser-runtime",
                               "--ignore-scripts", "--no-audit", "--no-fund", stage="npm ci")
            (release / ".browser-built").touch()

    @staticmethod
    def contract_fingerprint(contract: dict) -> str:
        import hashlib
        return hashlib.sha1(json.dumps(contract, sort_keys=True).encode()).hexdigest()

    def verify_test_binding(self, job, release: Path) -> None:
        """검사 결과가 이 배포 SHA의 것인지 확인한다. 어긋나면 재검사를 요구한다."""
        try:
            marker = json.loads((release / ".release.json").read_text()).get("revision")
        except OSError as exc:
            raise StageError("verify", None, None, f"release marker unreadable: {exc}") from exc
        if marker != job["revision"]:
            raise StageError("verify", None, None,
                             f"release {str(marker)[:7]} != job {job['revision'][:7]}; re-preparation required")
        if job.get("tested_revision") and job["tested_revision"] != job["revision"]:
            raise StageError("verify", None, None,
                             f"tested {job['tested_revision'][:7]} != job {job['revision'][:7]}; retest required")
        if job.get("tested_contract"):
            current = self.contract_fingerprint(self.releases.contract(release))
            if current != job["tested_contract"]:
                raise StageError("verify", None, None,
                                 "release contract changed after testing; retest required")

    async def prepare_tracked(self, job):
        """준비 파이프라인을 백그라운드 태스크로 돌린다. 진행 중이면 None.

        빠른 준비(스텁·캐시 적중)는 한 패스에 끝낸다. 느린 준비는 heartbeat만
        남기고 None을 반환해 감독 루프가 drain·회복 주기를 계속 돌게 한다.
        """
        task = _PREPARE_TASKS.get(job["id"])
        if task is None:
            self.phase(job, job["status"], prepare_stage="queued", prepare_heartbeat=_utcnow())
            task = asyncio.create_task(self._prepare_supervised(self.store.get(job["id"])))
            _PREPARE_TASKS[job["id"]] = task
            await asyncio.sleep(0)
        if not task.done():
            self.phase(job, job["status"], prepare_heartbeat=_utcnow())
            return None
        del _PREPARE_TASKS[job["id"]]
        release = task.result()
        job.update(self.store.get(job["id"]))
        return release

    async def _prepare_supervised(self, job):
        try:
            return await asyncio.wait_for(self.prepare(job), PREPARE_TOTAL_TIMEOUT)
        except TimeoutError as exc:
            raise StageError("prepare", None, None,
                             f"prepare exceeded {PREPARE_TOTAL_TIMEOUT}s; "
                             "existing generation was preserved") from exc

    @staticmethod
    @contextlib.asynccontextmanager
    async def _operation(stage: str):
        try:
            yield
        except StageError:
            raise
        except Exception as exc:
            raise StageError(stage, None, None, str(exc)) from exc

    async def prepare(self, job):
        self.phase(job, "building")
        release = await asyncio.to_thread(self.releases.archive, job["revision"])
        await self.environment(job, release)
        if not job["skip_tests"]:
            self.phase(job, "testing")
            name = "ttakkari_gen_" + uuid.UUID(job["id"]).hex
            url = make_url(get_settings().database_url).set(drivername="postgresql")
            admin = await asyncpg.connect(url.set(database="postgres").render_as_string(hide_password=False))
            try:
                await admin.execute(f'CREATE DATABASE "{name}"')
                try:
                    test_url = url.set(drivername="postgresql+asyncpg", database=name).render_as_string(hide_password=False)
                    # Archives have no .git; use the candidate, not the supervisor's revision.
                    env = test_environment(job["revision"], test_url)
                    await self.command(
                        job, release, str(release / ".venv/bin/python"), "-m", "pytest", "-q", "-x",
                        "--timeout=120", stage="pytest", env=env,
                    )
                finally:
                    await admin.execute(f'DROP DATABASE "{name}"')
            finally:
                await admin.close()
        contract = self.releases.contract(release)
        async with sessionmaker()() as db:
            schema = await db.scalar(text("SELECT version_num FROM alembic_version"))
        if schema != contract["schema_revision"]:
            if schema not in contract["online_migration_from"]:
                raise RuntimeError("schema contract requires an idle migration/bootstrap; active work preserved")
            self.phase(job, "migrating")
            env = os.environ | {"TTAKKARI_MIGRATION_ONLINE": "true"}
            await self.command(
                job, release, str(release / ".venv/bin/python"), "-m", "ttakkari.cli", "migrate",
                stage="migrate", env=env,
            )
        self.phase(job, "prepared", release_path=str(release),
                   tested_revision=job["revision"],
                   tested_contract=self.contract_fingerprint(contract))
        return release

    async def rollback(self, job):
        old_slot, new_slot = job["previous_slot"], job["candidate_slot"]
        old = await self.slots.snapshot(old_slot)
        if old is None or old["generation"] != job["previous_generation"]:
            self.phase(job, "recovery_pending", error="previous generation unavailable; refusing speculative rollback")
            return
        old_id = uuid.UUID(old["generation"])
        active = await self.active()
        if active not in {old_id, uuid.UUID(job["candidate_generation"])}:
            self.phase(job, "recovery_pending", error="newer generation is active; refusing stale rollback")
            return
        if active != old_id:
            await self.activate(old_id, expected=active)
        await self.slots.switch(old_slot)
        self.phase(job, "rolling_back")
        candidate = await self.slots.snapshot(new_slot)
        if candidate and candidate.get("drain_ready"):
            await self.slots.stop(new_slot)
            self.phase(job, "failed")

    def record_active(self, job):
        for name, value in (("previous", job["previous_revision"]), ("current", job["revision"])):
            path = self.store.root / name
            temp = path.with_suffix(".tmp")
            temp.write_text(value + "\n")
            temp.replace(path)

    def succeed(self, job):
        self.phase(job, "succeeded", retired_generation=job["previous_generation"], error=None)

    async def retire(self, job):
        """Finish an older deployment without touching its now superseded candidate."""
        previous = await self.slots.snapshot(job["previous_slot"])
        if previous is None:
            if job["status"] == "retiring" and await self.slots.retired(
                    job["previous_slot"], job["previous_generation"]):
                if await self.active() == uuid.UUID(job["candidate_generation"]):
                    await self.slots.verify(job["revision"])
                self.succeed(job)
                return
            raise RuntimeError("draining generation unreachable; work survival is unknown")
        if previous["generation"] != job["previous_generation"]:
            raise RuntimeError("draining slot identity changed; refusing to stop its new owner")
        if await self.active() == uuid.UUID(previous["generation"]):
            raise RuntimeError("previous generation is active; refusing retirement")
        if previous.get("drain_ready"):
            self.phase(job, "retiring")
            await self.slots.stop(job["previous_slot"])
            self.succeed(job)

    async def ensure_supervisor_capable(self, job) -> bool:
        """릴리스 요구조건과 실행 중 감독을 비교한다. 진행 가능하면 True.

        불일치면 현재 활성 릴리스 코드로의 안전한 감독 갱신을 한 번 시도하고,
        여의치 않으면 supervisor_upgrade_required 로 보고한다. drain·복구
        경로는 이 검사를 거치지 않는다.
        """
        from ttakkari import generation
        try:
            contract = await asyncio.to_thread(
                contract_for_revision, self.releases.source, job["revision"])
        except (subprocess.CalledProcessError, ValueError, OSError) as exc:
            # 계약을 못 읽으면 게이트를 건너뛴다. prepare의 아카이브 검증이
            # 명확한 오류로 거부한다.
            log.warning("supervisor capability check skipped: %s", exc)
            return True
        if supervisor_satisfies(contract):
            return True
        required = required_supervisor_capabilities(contract)
        current = generation.supervisor_capabilities()
        detail = (f"release {job['revision'][:7]} requires supervisor {required}, "
                  f"running supervisor provides {current}")
        if await self.maybe_upgrade_supervisor(job, required):
            return False
        self.phase(job, "supervisor_upgrade_required", error_stage="supervisor",
                   error_summary="supervisor_upgrade_required: " + detail, error=detail)
        return False

    async def maybe_upgrade_supervisor(self, job, required) -> bool:
        """현재 활성 릴리스 코드로 감독을 갱신한다. 갱신을 걸었으면 True.

        같은 활성 리비전에 이미 시도했거나 영수증(receipts)이 없으면 False.
        갱신 후에는 이 프로세스가 옛 코드이므로 이번 패스에서는 진행하지 않는다.
        """
        try:
            active = await self.active()
            if active is None:
                return False
            info = await self.generation_info(active)
        except Exception:
            log.exception("supervisor capability check could not read the active generation")
            return False
        revision = info["revision"]
        if job.get("supervisor_upgrade_attempted_for") == revision:
            return False
        release = self.releases.root / "releases" / revision
        try:
            candidate_contract = Releases.contract(release)
        except (OSError, ValueError, TypeError):
            return False
        candidate = required_supervisor_capabilities(candidate_contract)
        if candidate["protocol"] != required["protocol"] or candidate["slots"] < required["slots"]:
            return False
        receipts = [j for j in self.store.all() if j["revision"] == revision
                    and j["status"] in {"draining", "succeeded"} and not j.get("skip_tests", True)
                    and j.get("strategy") == "generations" and j.get("release_path") == str(release)]
        if not receipts:
            return False
        try:
            await upgrade_supervisor(self, revision)
        except Exception:
            log.exception("automatic supervisor upgrade failed; reporting instead")
            return False
        self.phase(job, job["status"], supervisor_upgrade_attempted_for=revision,
                   error_stage="supervisor",
                   error_summary=f"supervisor upgrade to {revision[:7]} requested; retrying")
        return True

    async def process(self, job):
        if job["status"] == "recovery_pending":
            return
        if job["status"] in {"draining", "retiring"}:
            try:
                async with self._operation("drain"):
                    await self.retire(job)
            except Exception as exc:
                log.exception("generation retirement needs recovery: %s", job["id"])
                self.record_failure(job, "recovery_pending", exc, stage="drain")
            return
        try:
            if job["status"] == "rolling_back":
                await self.rollback(job)
                return
            if "candidate_slot" not in job:
                if not await self.ensure_supervisor_capable(job):
                    return
                snapshots = [await self.slots.snapshot(slot) for slot in range(len(PORTS))]
                active = await self.active()
                old_slot = next((i for i, row in enumerate(snapshots) if row and uuid.UUID(row["generation"]) == active), None)
                if old_slot is None:
                    raise RuntimeError("generation bootstrap required; active slot is missing")
                free = next((i for i, row in enumerate(snapshots) if row is None), None)
                if free is None:
                    self.phase(job, "waiting_slot")
                    return
                release = None
                if job.get("release_path"):
                    release = Path(job["release_path"])
                    try:
                        self.verify_test_binding(job, release)
                    except StageError as exc:
                        # 기록된 릴리스가 검사본과 어긋나면 실패가 아니라 재준비다.
                        log.warning("stale release binding; re-preparing: %s", exc)
                        self.phase(job, job["status"], release_path=None,
                                   tested_revision=None, tested_contract=None)
                        job.update(self.store.get(job["id"]))
                        release = None
                if release is None:
                    release = await self.prepare_tracked(job)
                    if release is None:
                        return
                self.phase(job, "starting", candidate_slot=free, previous_slot=old_slot,
                    previous_generation=str(active), previous_revision=snapshots[old_slot]["revision"],
                    release_path=str(release))
            async with self._operation("launch"):
                candidate = await self.slots.snapshot(job["candidate_slot"])
                if candidate is None:
                    candidate = await self.slots.start(job["candidate_slot"], Path(job["release_path"]), job["revision"])
            if candidate["revision"] != job["revision"] or candidate.get("protocol") != PROTOCOL:
                raise RuntimeError("candidate identity mismatch")
            cid = uuid.UUID(candidate["generation"])
            recorded = job.get("candidate_generation")
            if recorded and recorded != str(cid):
                raise RuntimeError("candidate restarted; reconcile ownership before activation")
            if not recorded:
                self.phase(job, "switching", candidate_generation=str(cid))
            current = await self.active()
            if current != cid:
                async with self._operation("activate"):
                    await self.activate(cid, expected=uuid.UUID(job["previous_generation"]))
            if job["status"] != "draining":
                async with self._operation("cutover"):
                    await self.slots.switch(job["candidate_slot"])
                async with self._operation("readiness"):
                    await self.slots.verify(job["revision"])
                self.record_active(job)
                self.phase(job, "draining", activated_revision=job["revision"])
            async with self._operation("drain"):
                await self.retire(job)
        except Exception as exc:
            log.exception("generation deployment failed: %s", job["id"])
            if job["status"] in {"draining", "retiring"}:
                self.record_failure(job, "recovery_pending", exc)
                return
            self.record_failure(job, "rolling_back" if job.get("candidate_generation") else "failed", exc)
            if job.get("candidate_generation"):
                try:
                    await self.rollback(job)
                except Exception:  # noqa: BLE001 -- persist failure; never discard it
                    self.phase(job, "recovery_pending", error="automatic reconciliation failed; inspect deployment journal")


async def upgrade_supervisor(controller, revision: str) -> dict:
    """External-host operation: change only the supervisor, never an app generation."""
    from ttakkari.deploy_gate import RestartGate
    from ttakkari.deployment import LABEL, strategy
    from ttakkari.generation_bootstrap import Services, write_json

    if os.environ.get("TTAKKARI_RUN_ID"):
        raise RuntimeError("supervisor upgrade belongs to an external host command, never an agent Run")
    revision = _sha(revision)
    if strategy() != "generations":
        raise RuntimeError("generation bootstrap is required before upgrading the supervisor")
    gate = RestartGate(controller.store.root / "backend-restart.lock")
    lock = gate._acquire()
    if lock is None:
        raise RuntimeError("deployment is changing state; retry the external upgrade after that operation")
    try:
        release = controller.store.root / "releases" / revision
        receipts = [j for j in controller.store.all() if j["revision"] == revision
                    and j["status"] in {"draining", "succeeded"} and not j.get("skip_tests", True)
                    and j.get("strategy") == "generations" and j.get("release_path") == str(release)]
        if not receipts:
            raise RuntimeError("a verified, activated generation deployment is required")
        if (json.loads((release / ".release.json").read_text()).get("revision") != revision
                or not (release / ".venv/bin/python").is_file()):
            raise RuntimeError("prepared release identity/runtime is invalid")
        Releases.contract(release)
        control = GenerationController(controller.store)
        active = await control.active()
        info = await control.generation_info(active)
        slot = next((i for i in range(len(PORTS)) if control.slots.origin(i) == info["api_url"]), None)
        ready = await control.slots.snapshot(slot) if slot is not None else None
        if (info["revision"] != revision or not ready or ready["generation"] != str(active)
                or ready["revision"] != revision):
            raise RuntimeError("verified release is not the current ready owner")
        await control.slots.verify(revision)
        await Services(controller).pin_supervisor(release, revision)
        result = {"revision": revision, "release": str(release), "status": "requested"}
        write_json(controller.store.root / "supervisor-upgrade.json", result)
    finally:
        gate._release(lock)
    # launchd may still have the old plist loaded. That entry point reads the new
    # supervisor.json before taking the controller lock and execs the pinned code.
    await control.slots._launchctl("kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
    return result
