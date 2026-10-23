"""External generation deployer. Never invoke service control from an agent Run.

The database CAS and proxy reload cannot be atomic. Journal the intent before
either side effect; each subsequent pass reconciles observable state.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import psutil
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.generation import PORTS, PROTOCOL, _lock, activate
from ttakkari.models import (
    RuntimeCommand,
    RuntimeControl,
    RuntimeGeneration,
    RuntimeLease,
    utcnow,
)

log = logging.getLogger(__name__)
_LOG_TAIL_LINES = 12
_LOG_TAIL_CHARS = 4000
# 준비 단계별 상한(초). 감독 루프를 묶어 두었던 장시간 빌드·검사를 fencing한다.
STAGE_TIMEOUTS = {"uv sync": 600, "npm ci": 600, "pytest": 1800, "security_gate": 300,
                  "migrate": 600, "command": 600}
PREPARE_TOTAL_TIMEOUT = 3600
# job id -> 실행 중인 준비 태스크. 감독 패스 사이에서 유지된다.
_PREPARE_TASKS: dict[str, asyncio.Task] = {}


def _utcnow() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _group_alive(pgid: int) -> bool:
    """killpg(pgid, 0)은 아무것도 보내지 않고 존재만 확인한다."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def _terminate_group(proc, *, grace_seconds: float = 5) -> None:
    """프로세스 그룹 전체를 TERM 후 KILL한다.

    자식이 먼저 죽어도 손자가 그룹에 남을 수 있어, 유예 뒤에는 그룹 존재를
    직접 확인하고 남아 있으면 무조건 KILL한다. 한계: 손자가 setsid/setpgid로
    그룹을 빠져나갔으면 닿지 않는다. 그런 탈주는 허용하지 않는 명령만 쓴다.
    """
    import signal
    pgid = proc.pid
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), grace_seconds)
    except TimeoutError:
        pass
    if _group_alive(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
    await proc.wait()


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


class IdleRequired(Exception):
    """스키마 상태가 온라인 마이그레이션으로 건너갈 수 없어 유휴 구간이 필요하다. 실패가 아니라 대기다."""


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
            # Verify the immutable artifact again on reuse; the revision marker alone
            # does not detect an edited manifest or a damaged extracted archive.
            self.contract(target)
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
        value = validate_contract(value)
        executor = value.get("run_executor")
        if executor:
            if (executor.get("protocol") != 2 or executor.get("activation") not in {"disabled", "enabled"}
                    or executor.get("manifest") != "deploy/run-executor-manifest.json"):
                raise ValueError("invalid run executor contract")
            manifest_path = path / "deploy/run-executor-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            expected_digest = executor.get("manifest_sha256")
            if (not isinstance(expected_digest, str) or len(expected_digest) != 64
                    or any(char not in "0123456789abcdef" for char in expected_digest)
                    or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_digest):
                raise ValueError("executor manifest digest is missing or mismatched")
            if (manifest.get("schema") != 1 or manifest.get("role") != "run-executor"
                    or manifest.get("activation") != executor.get("activation")
                    or manifest.get("command") != ["{release}/.venv/bin/python", "-m",
                                                    "ttakkari.cli", "executor", "--activate"]
                    or manifest.get("readiness") != ["executor_generation", "revision", "pid",
                                                       "process_started_at", "heartbeat_at"]
                    or not {"TTAKKARI_RUNTIME_GENERATION", "TTAKKARI_RELEASE_REVISION",
                            "TTAKKARI_DEPLOY_STRATEGY", "TTAKKARI_DEPLOY_SOURCE_ROOT"}
                    <= set(manifest.get("environment", {}))
                    or not manifest.get("activation_gates")):
                raise ValueError("invalid or mismatched run executor manifest")
        return value


def validate_contract(value: dict) -> dict:
    if value.get("protocol") != PROTOCOL or not value.get("schema_revision"):
        raise ValueError("unsupported generation/schema contract; idle bootstrap required")
    if not isinstance(value.get("online_migration_from"), list):
        raise TypeError("online migration policy must be explicit")
    aliases = value.get("schema_aliases", {})
    known = {value["schema_revision"], *value["online_migration_from"]}
    if not isinstance(aliases, dict) or any(
        not isinstance(old, str) or new not in known or old == new for old, new in aliases.items()
    ):
        # 별칭은 "물리 스키마가 대상 리비전과 같다"는 보증이다. 알려진 리비전으로만 되돌릴 수 있다.
        raise TypeError("schema_aliases must map retired revision ids onto known contract revisions")
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

    @staticmethod
    def executor_spec(release: Path, revision: str) -> tuple[list[str], dict[str, str]]:
        contract = Releases.contract(release)
        executor = contract.get("run_executor")
        if not executor or executor.get("activation") != "enabled":
            raise RuntimeError("executor activation is disabled by the pinned release contract")
        manifest = json.loads((release / executor["manifest"]).read_text())
        source_root = str(get_settings().deploy_source_root or Path(__file__).resolve().parents[2])
        command = [value.replace("{release}", str(release)) for value in manifest["command"]]
        env = {key: value.replace("{revision}", revision).replace("{source_root}", source_root)
               for key, value in manifest["environment"].items()}
        env.update({"HOME": str(Path.home()), "PATH": os.environ.get("PATH", ""),
                    "TTAKKARI_HOME": str(get_settings().home)})
        return command, env

    async def executor_snapshot(self, *, revision: str) -> dict | None:
        """Fail closed unless pointer, pinned artifact, heartbeat and OS identity agree."""
        async with sessionmaker()() as db:
            control = await db.get(RuntimeControl, 1)
            row = await db.get(RuntimeGeneration, control.executor_generation) if (
                control and control.executor_generation) else None
            if row is None or row.role != "executor" or row.state != "active" or row.revision != revision:
                return None
            if row.heartbeat_at is None or (utcnow() - row.heartbeat_at).total_seconds() > 3 * get_settings().runtime_poll_seconds:
                return None
            pid, started_at = row.pid, row.process_started_at
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - started_at) > 1.0:
                return None
            command = process.cmdline()
            if "ttakkari.cli" not in command or "executor" not in command or "--activate" not in command:
                return None
        except (psutil.Error, OSError, PermissionError):
            return None
        return {"ready": True, "generation": str(row.id), "revision": row.revision,
                "pid": pid, "process_started_at": started_at, "heartbeat_at": row.heartbeat_at}

    async def ensure_executor(self, release: Path, revision: str) -> dict:
        contract = Releases.contract(release)
        executor = contract.get("run_executor")
        if not executor or executor.get("activation") != "enabled":
            return {"ready": False, "activation": "disabled"}
        command, env = self.executor_spec(release, revision)
        if not Path(command[0]).is_file() or json.loads((release / ".release.json").read_text()).get("revision") != revision:
            raise RuntimeError("pinned executor interpreter or release marker is invalid")
        if ready := await self.executor_snapshot(revision=revision):
            return ready
        async with sessionmaker()() as db:
            apis = list((await db.execute(select(RuntimeGeneration.protocol, RuntimeGeneration.execution_mode)
                .where(RuntimeGeneration.role == "api",
                       RuntimeGeneration.state.in_(("standby", "active", "draining"))))).all())
            leases = await db.scalar(select(func.count()).select_from(RuntimeLease))
            pending = list(await db.scalars(select(RuntimeCommand.kind).where(
                RuntimeCommand.acknowledged_at.is_(None))))
        if not apis or any(protocol != 2 or mode != "remote" for protocol, mode in apis):
            raise RuntimeError("executor activation requires every live API to be protocol2 remote")
        if leases:
            raise RuntimeError("executor activation requires a drained Run lease boundary")
        if set(pending) - {"cancel", "reset", "release_waiting"}:
            raise RuntimeError("executor activation refuses an unknown pending command")
        services = self.root / "services"
        logs = self.root / "logs"
        services.mkdir(parents=True, exist_ok=True)
        logs.mkdir(exist_ok=True)
        label = "net.winterholic.ttakkari.executor"
        plist = services / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({"Label": label, "WorkingDirectory": str(release),
            "ProgramArguments": command, "EnvironmentVariables": env, "KeepAlive": False,
            "RunAtLoad": False, "StandardOutPath": str(logs / "executor.log"),
            "StandardErrorPath": str(logs / "executor.log")}))
        await self._launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
        for _ in range(100):
            if ready := await self.executor_snapshot(revision=revision):
                return ready
            await asyncio.sleep(0.3)
        raise RuntimeError("pinned executor readiness timed out; API generation remains available")

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
        release = self.releases.root / "releases" / _sha(info["revision"])
        slot = next((i for i in range(len(PORTS)) if self.slots.origin(i) == info["api_url"]), None)
        if slot is None:
            raise RuntimeError("active generation has an unmanaged origin")
        ready = await self.slots.snapshot(slot)
        if ready is None:
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
        await self.slots.ensure_executor(release, info["revision"])

    # error_command·error_exit 미지정 표식. 명시적 null(해당 없음)과 구분한다.
    _UNSET: object = object()

    def record_failure(self, job, status: str, exc: BaseException, *,
                       stage: str | None = None, command=_UNSET, exit_code=_UNSET) -> None:
        """배포 레코드에 단계·명령·종료코드·요약을 구조화해 남긴다.

        stage는 반드시 채운다(명시 → StageError → unknown). 마지막 준비 단계로
        추측하지 않는다. command·exit code는 해당할 때만 채우고, 아니면 null과
        이유를 남긴다. error(문자열)는 하위호환용으로 유지해
        backend_deploy_status·ops 조회가 그대로 읽는다.
        """
        reasons: list[str] = []
        if isinstance(exc, StageError):
            stage = stage or exc.stage
            if command is self._UNSET:
                command = exc.command
            if exit_code is self._UNSET:
                exit_code = exc.exit_code
            summary = exc.summary
        else:
            summary = str(exc)
        if stage is None:
            stage = "unknown"
            reasons.append("stage unknown: no stage context recorded this failure")
        if command is self._UNSET:
            command = None
        if exit_code is self._UNSET:
            exit_code = None
        if command is None:
            reasons.append("command is null: no command ran for this failure")
        if exit_code is None:
            reasons.append("exit code is null: no process exit was observed")
        detail = f"{stage}: {summary}"
        if reasons:
            detail += " [" + "; ".join(reasons) + "]"
        self.phase(job, status, error=str(exc), error_stage=stage,
                   error_command=command, error_exit=exit_code,
                   error_summary=detail[:2000])

    async def command(self, job, release, *args, stage="command", env=None, timeout=None):
        log_path = self.store.jobs / f"{job['id']}.log"
        limit = timeout if timeout is not None else STAGE_TIMEOUTS.get(stage, 600)
        # 단계 진입을 기록한다. 감독 루프는 이 heartbeat로 진척을 본다.
        self.phase(job, job["status"], prepare_stage=stage, prepare_heartbeat=_utcnow())
        with log_path.open("ab") as output:
            try:
                # 새 세션(프로세스 그룹)으로 띄운다. 타임아웃 때 손자까지 함께 정리한다.
                proc = await asyncio.create_subprocess_exec(*args, cwd=release, env=env,
                    stdout=output, stderr=output, start_new_session=True)
            except OSError as exc:
                raise StageError(stage, args, None,
                                 f"could not start ({type(exc).__name__}): {exc}") from exc
            try:
                returncode = await asyncio.wait_for(proc.wait(), limit)
            except TimeoutError:
                await _terminate_group(proc, grace_seconds=5)
                raise StageError(stage, args, None,
                                 f"timed out after {limit}s; existing generation was preserved") from None
        if returncode:
            tail = _safe_log_tail(log_path, env if env is not None else os.environ)
            raise StageError(stage, args, returncode,
                             f"last {_LOG_TAIL_LINES} log lines:\n{tail}")

    # 한 게이트가 pytest 를 띄우는 최대 횟수(첫 실행 + 재시도). 서로 다른 흔들린 시험이 있어도 시험마다 한 번만 재시도한다.
    _PYTEST_MAX_RUNS = 4
    _PRIOR_FAILURE_LIMIT = 10

    def _gate_cache_dir(self, job) -> Path:
        # NOTE: stepwise 상태는 pytest cache_dir 에 남는다. 릴리스 폴더(.pytest_cache)를 쓰면 같은 SHA 를 다시 준비하거나 다른 후보가
        # 폴더를 재사용할 때 이전 실행의 중단 지점이 섞여, 앞부분 시험을 건너뛴 채 통과할 수 있다. 배포 작업(job id)마다
        # 따로 두고 시작 전에 비운다.
        return self.store.jobs / f"{job['id']}.pytest-cache"

    @staticmethod
    def _stepwise_last_failed(cache_dir: Path) -> str | None:
        """pytest --sw 가 남긴 마지막 실패 시험 id. 읽지 못하면 None(호출자는 재시도하지 않고 실패시킨다)."""
        try:
            info = json.loads((cache_dir / "v/cache/stepwise").read_text())
            failed = info["last_failed"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return failed if isinstance(failed, str) and failed else None

    def _prior_failures_path(self) -> Path:
        return self.store.root / "gate-last-failed.json"

    def _load_prior_failures(self) -> list[str]:
        try:
            ids = json.loads(self._prior_failures_path().read_text())["nodeids"]
        except (OSError, ValueError, KeyError, TypeError):
            return []
        # argv 로 그대로 넘기므로 옵션처럼 보이는 값과 시험 id 모양이 아닌 값은 버린다.
        return [i for i in ids if isinstance(i, str) and not i.startswith("-") and ".py" in i][:self._PRIOR_FAILURE_LIMIT]

    def _save_prior_failures(self, job, nodeids: list[str]) -> None:
        path = self._prior_failures_path()
        try:
            if nodeids:
                path.write_text(json.dumps({"revision": job.get("revision"), "job": job["id"], "at": _utcnow(),
                                            "nodeids": nodeids[:self._PRIOR_FAILURE_LIMIT]}))
            else:
                path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not update %s", path, exc_info=True)

    async def run_pytest(self, job, release, env):
        """전체 시험을 stepwise(--sw)로 돈다: 첫 실패에서 멈추고, 재시도는 그 시험부터 끝까지 이어 간다.

        NOTE: 예전에는 첫 실행을 -x 로 멈추고 --lf 로 실패한 시험만 다시 돌았다. 재시도가 통과하면 중단 지점 뒤의
        시험은 한 번도 실행되지 않은 채 게이트가 통과했다. --sw 재시도는 실패했던 시험을 먼저 다시 돌고 통과하면 뒤
        시험을 이어서 돈다. 같은 시험이 다시 실패하면 진짜 결함이라 게이트가 실패하고, 뒤에서 처음 실패한 시험은
        한 번 더 재시도한다(시험마다 한 번). 시험 실패만 재시도하고 수집 오류·타임아웃은 그대로 실패시킨다.
        (여러 세션이 한 Mac 에서 동시에 돌아 부하가 높으면 시간에 민감한 시험 한 건이 배포 전체를 막았다. 2026-10-19)
        """
        python = str(release / ".venv/bin/python")
        pytest = (python, "-m", "pytest", "-q", "--timeout=120")
        cache_dir = self._gate_cache_dir(job)
        log_path = self.store.jobs / f"{job['id']}.log"
        shutil.rmtree(cache_dir, ignore_errors=True)
        try:
            await self._run_prior_failures(job, release, env, pytest, cache_dir, log_path)
            # 보안 회귀 묶음은 앞선 security_gate 단계가 이미 실행했다. 같은 시험을 전체 실행에서 또 돌지 않는다.
            main = (*pytest, "-m", "not security", "-o", f"cache_dir={cache_dir / 'main'}", "--sw")
            started_at: str | None = None
            for attempt in range(1, self._PYTEST_MAX_RUNS + 1):
                try:
                    await self.command(job, release, *main, stage="pytest", env=env)
                    self._save_prior_failures(job, [])
                    return
                except StageError as exc:
                    # NOTE: --sw 는 시험 실패를 exit 2(Interrupted)로 끝낸다. 수집 오류도 exit 2 이므로 stepwise 가 실패
                    # 시험을 기록했을 때만 시험 실패로 본다. 그 밖의 종료(수집 오류·내부 오류·타임아웃)는 재시도하지 않는다.
                    failed = self._stepwise_last_failed(cache_dir / "main")
                    if exc.exit_code not in (1, 2) or failed is None:
                        raise
                    if failed == started_at or attempt == self._PYTEST_MAX_RUNS:
                        self._save_prior_failures(job, [failed])
                        raise
                    started_at = failed
                    with log_path.open("ab") as log_file:
                        log_file.write(f"\n== pytest failed at {failed}; retrying from it once and continuing "
                                       f"to the end (--sw, run {attempt + 1}/{self._PYTEST_MAX_RUNS}) ==\n".encode())
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

    async def _run_prior_failures(self, job, release, env, pytest, cache_dir, log_path) -> None:
        """직전 배포 게이트에서 실패한 시험을 전체 실행 앞에서 먼저 돌려, 같은 결함이면 몇 분 안에 실패를 알린다.

        전체 실행에도 그 시험이 원래 자리에서 다시 포함되므로 시험 범위는 줄지 않는다. 이 단계는 속도용이라 시험이
        사라졌거나 수집되지 않으면(exit 4/5 등) 그냥 넘어가고, 시험 실패(exit 1)만 한 번 재시도해 두 번 실패하면 중단한다.
        """
        ids = self._load_prior_failures()
        if not ids:
            return
        command = (*pytest, "-o", f"cache_dir={cache_dir / 'prior'}", *ids)
        with log_path.open("ab") as log_file:
            log_file.write(f"\n== running the previous gate's failures first: {' '.join(ids)} ==\n".encode())
        for attempt in (1, 2):
            try:
                await self.command(job, release, *command, stage="pytest", env=env)
                return
            except StageError as exc:
                if exc.exit_code != 1:
                    return
                if attempt == 2:
                    self._save_prior_failures(job, ids)
                    raise

    async def run_security_gate(self, job, release, env):
        """SEC2-GATE-01: 보안 회귀 묶음을 배포 게이트에서 필수 실행한다."""
        from ttakkari.security.gate import build_security_gate_command

        budget = STAGE_TIMEOUTS["security_gate"]
        deadline = time.monotonic() + budget
        python_bin = release / ".venv/bin/python"
        cmd = build_security_gate_command(python_bin, timeout=120)
        try:
            await self.command(job, release, *cmd, stage="security_gate", env=env,
                               timeout=max(0.001, deadline - time.monotonic()))
        except StageError as exc:
            if exc.exit_code != 1:
                raise
            with (self.store.jobs / f"{job['id']}.log").open("ab") as log_file:
                log_file.write(b"\n== security gate failed; rerunning only the failed tests once (--lf) ==\n")
            retry_cmd = build_security_gate_command(python_bin, timeout=120, last_failed=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StageError("security_gate", retry_cmd, None,
                                 f"timed out after {budget}s; existing generation was preserved") from exc
            await self.command(job, release, *retry_cmd, stage="security_gate", env=env,
                               timeout=remaining)


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
        except (StageError, IdleRequired):
            raise
        except Exception as exc:
            raise StageError(stage, None, None, str(exc)) from exc

    async def prepare(self, job):
        self.phase(job, "building")
        async with self._operation("fetch"):
            release = await asyncio.to_thread(self.releases.archive, job["revision"])
        await self.environment(job, release)
        name = "ttakkari_gen_" + uuid.UUID(job["id"]).hex
        url = make_url(get_settings().database_url).set(drivername="postgresql")
        async with self._operation("test-db"):
            admin = await asyncpg.connect(url.set(database="postgres").render_as_string(hide_password=False))
        try:
            async with self._operation("db_create"):
                await admin.execute(f'CREATE DATABASE "{name}"')
            try:
                test_url = url.set(drivername="postgresql+asyncpg", database=name).render_as_string(hide_password=False)
                # Archives have no .git; use the candidate, not the supervisor's revision.
                env = test_environment(job["revision"], test_url)
                # SEC2-GATE-01: 보안 회귀 테스트 묶음을 배포 게이트에 연결. 실패 시 후보 실패 처리.
                self.phase(job, "security_gate")
                await self.run_security_gate(job, release, env)
                if not job["skip_tests"]:
                    self.phase(job, "testing")
                    await self.run_pytest(job, release, env)
            finally:
                await admin.execute(f'DROP DATABASE "{name}"')
        finally:
            await admin.close()
        async with self._operation("schema_contract"):
            contract = self.releases.contract(release)
            mode, schema = await self.schema_plan(contract)
            if mode == "idle":
                # 검사는 이미 통과했다. 릴리스·검사 바인딩을 남겨 다음 패스가 재검사 없이 유휴만 기다린다.
                self.phase(job, "waiting_idle", release_path=str(release),
                           tested_revision=job["revision"],
                           tested_contract=self.contract_fingerprint(contract),
                           idle_required=True, idle_reason=self.idle_reason(contract, schema),
                           idle_since=job.get("idle_since") or _utcnow(),
                           error_stage="schema_contract", active_runs=None,
                           error_summary="waiting_idle: " + self.idle_reason(contract, schema))
                raise IdleRequired(self.idle_reason(contract, schema))
        if mode == "online" and schema != contract["schema_revision"]:
            # 별칭 스택은 릴리스의 alembic/env.py 가 정렬한다. 감독 코드가 릴리스보다 오래돼도 통하도록 여기서 고쳐 쓰지 않는다.
            await self.migrate(job, release, online=True)
        self.phase(job, "prepared", release_path=str(release),
                   tested_revision=job["revision"],
                   tested_contract=self.contract_fingerprint(contract))
        return release

    @staticmethod
    def idle_reason(contract: dict, schema: str | None) -> str:
        return (f"database schema {schema!r} is outside the online migration range of release "
                f"{contract['schema_revision']!r}; waiting for an idle moment, no active Run is interrupted")

    async def schema_plan(self, contract: dict) -> tuple[str, str | None]:
        """현재 DB 스키마와 릴리스 계약을 비교한다. ("current"|"online"|"idle", 버전)."""
        async with sessionmaker()() as db:
            schema = await db.scalar(text("SELECT version_num FROM alembic_version"))
        resolved = contract.get("schema_aliases", {}).get(schema, schema)
        if resolved == contract["schema_revision"] and resolved == schema:
            return "current", schema
        if resolved == contract["schema_revision"] or resolved in contract["online_migration_from"]:
            return "online", schema
        return "idle", schema

    async def migrate(self, job, release, *, online: bool) -> None:
        self.phase(job, "migrating")
        env = os.environ | ({"TTAKKARI_MIGRATION_ONLINE": "true"} if online else {})
        await self.command(
            job, release, str(release / ".venv/bin/python"), "-m", "ttakkari.cli", "migrate",
            stage="migrate", env=env,
        )

    async def acquire_idle(self, job, release):
        """유휴 구간을 기다린다. 실행 중 Run 이 없고 접수 배타 잠금을 얻을 때만 잠금을 돌려준다.
        그 사이 유휴가 필요 없어졌다면 "online"/"current", 아직 기다려야 하면 None.

        기다리는 동안은 새 Run 접수를 막지 않는다. 대기 상태는 job 에 노출하되 값이 바뀔 때만 쓴다.
        """
        from ttakkari.deploy_gate import _active_run_count
        from ttakkari.deployment import Admission
        contract = self.releases.contract(release)
        mode, schema = await self.schema_plan(contract)
        if mode != "idle":
            # 그 사이 스키마가 온라인 범위로 들어왔다면 유휴가 필요 없다. 다음 패스가 온라인으로 이행한다.
            self.phase(job, job["status"], idle_required=False, idle_reason=None,
                       error_stage=None, error_summary=None, active_runs=None)
            return mode
        count = await _active_run_count(None)
        lease = None
        if count == 0:
            lease = Admission(self.store.root).acquire(exclusive=True)
            if lease is not None and await _active_run_count(None) != 0:
                lease.close()
                lease, count = None, await _active_run_count(None)
        if lease is not None:
            return lease
        if job["status"] != "waiting_idle" or job.get("active_runs") != count:
            self.phase(job, "waiting_idle", active_runs=count,
                       idle_reason=self.idle_reason(contract, schema),
                       idle_since=job.get("idle_since") or _utcnow(),
                       error_stage="schema_contract",
                       error_summary=f"waiting_idle: {count} active Run(s); {self.idle_reason(contract, schema)}")
        return None

    async def rollback(self, job):
        # 거부 분기는 자체 기록하고, 그 밖의 예외는 rollback 단계로 묶는다.
        async with self._operation("rollback"):
            old_slot, new_slot = job["previous_slot"], job["candidate_slot"]
            old = await self.slots.snapshot(old_slot)
            if old is None or old["generation"] != job["previous_generation"]:
                self.record_failure(job, "recovery_pending",
                    RuntimeError("previous generation unavailable; refusing speculative rollback"),
                    stage="rollback")
                return
            old_id = uuid.UUID(old["generation"])
            active = await self.active()
            if active not in {old_id, uuid.UUID(job["candidate_generation"])}:
                self.record_failure(job, "recovery_pending",
                    RuntimeError("newer generation is active; refusing stale rollback"),
                    stage="rollback")
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
        from datetime import UTC, datetime
        revision = info["revision"]
        # 정책: 같은 revision에는 1회만 시도한다. 실패해도 지우지 않아 패스마다
        # 다시 시도하지 않고 supervisor_upgrade_required로 멈춘다. 재평가는
        # 새 작업·새 활성 revision에서만 일어난다.
        if job.get("supervisor_upgrade_attempted_for") == revision:
            return False
        now = datetime.now(UTC).isoformat()
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
        attempts = list(job.get("supervisor_upgrade_attempts") or [])
        attempt = {"revision": revision, "at": now, "ok": False, "error": None}
        # kickstart가 이 프로세스를 죽여도 기록이 남도록 시도부터 영속화한다.
        # 갱신 후 다시 떴는데도 capability가 부족하면 같은 대상을 다시 시도하지
        # 않고 supervisor_upgrade_required로 멈춘다.
        self.phase(job, job["status"], supervisor_upgrade_attempted_for=revision,
                   supervisor_upgrade_attempts=[*attempts, attempt],
                   error_stage="supervisor",
                   error_summary=f"supervisor upgrade to {revision[:7]} requested; retrying")
        try:
            await upgrade_supervisor(self, revision)
        except Exception as exc:
            attempt["error"] = str(exc)[:500]
            self.phase(job, job["status"], supervisor_upgrade_attempts=[*attempts, attempt])
            log.exception("automatic supervisor upgrade failed; reporting instead")
            return False
        attempt["ok"] = True
        self.phase(job, job["status"], supervisor_upgrade_attempts=[*attempts, attempt])
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
        idle_lease = None
        try:
            if job["status"] == "rolling_back":
                await self.rollback(job)
                return
            if "candidate_slot" not in job:
                if not await self.ensure_supervisor_capable(job):
                    return
                async with self._operation("prepare"):
                    snapshots = [await self.slots.snapshot(slot) for slot in range(len(PORTS))]
                    active = await self.active()
                old_slot = next((i for i, row in enumerate(snapshots) if row and uuid.UUID(row["generation"]) == active), None)
                if old_slot is None:
                    raise StageError("prepare", None, None,
                                     "generation bootstrap required; active slot is missing")
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
                    try:
                        release = await self.prepare_tracked(job)
                    except IdleRequired:
                        job.update(self.store.get(job["id"]))
                        return
                    if release is None:
                        return
                if job.get("idle_required"):
                    # 온라인으로 건너갈 수 없는 스키마는 실패가 아니라 대기다. 유휴 때 잠금을 쥐고 이행한 뒤 컷오버까지 유지한다.
                    gate = await self.acquire_idle(job, release)
                    if gate is None:
                        return
                    if gate == "online":
                        await self.migrate(job, release, online=True)
                    elif gate != "current":
                        idle_lease = gate
                        await self.migrate(job, release, online=False)
                        self.phase(job, job["status"], idle_required=False, idle_reason=None,
                                   error_stage=None, error_summary=None, active_runs=None)
                self.phase(job, "starting", candidate_slot=free, previous_slot=old_slot,
                    previous_generation=str(active), previous_revision=snapshots[old_slot]["revision"],
                    release_path=str(release))
            async with self._operation("launch"):
                candidate = await self.slots.snapshot(job["candidate_slot"])
                if candidate is None:
                    candidate = await self.slots.start(job["candidate_slot"], Path(job["release_path"]), job["revision"])
                if candidate["revision"] != job["revision"] or candidate.get("protocol") != PROTOCOL:
                    raise StageError("launch", None, None, "candidate identity mismatch")
                cid = uuid.UUID(candidate["generation"])
                recorded = job.get("candidate_generation")
                if recorded and recorded != str(cid):
                    raise StageError("launch", None, None,
                                     "candidate restarted; reconcile ownership before activation")
            if not recorded:
                self.phase(job, "switching", candidate_generation=str(cid))
            async with self._operation("activate"):
                current = await self.active()
                if current != cid:
                    await self.activate(cid, expected=uuid.UUID(job["previous_generation"]))
            if job["status"] != "draining":
                async with self._operation("cutover"):
                    await self.slots.switch(job["candidate_slot"])
                async with self._operation("readiness"):
                    await self.slots.verify(job["revision"])
                async with self._operation("record"):
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
                    self.record_failure(job, "recovery_pending",
                        RuntimeError("automatic reconciliation failed; inspect deployment journal"),
                        stage="rollback")
        finally:
            if idle_lease is not None:
                idle_lease.close()


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
