"""Persist requests; an external controller owns generation cutover or legacy idle restart."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import stat as stat_mod
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import asyncpg
import httpx
from sqlalchemy.engine import make_url

from ttakkari.config import get_settings
from ttakkari.db import dispose_engine
from ttakkari.deploy_gate import RestartGate, _active_run_count

log = logging.getLogger(__name__)
BACK = Path(__file__).resolve().parents[2]
TERMINAL = {"succeeded", "failed", "superseded"}
LABEL = "net.winterholic.ttakkari.deployer"


def state_dir() -> Path:
    return get_settings().home / "deploy"


def strategy() -> str:
    marker = state_dir() / "generations-enabled.json"
    if marker.exists():
        value = json.loads(marker.read_text())
        if value.get("protocol") != 1 or value.get("strategy") != "generations":
            raise RuntimeError("invalid generation activation marker; refusing an idle restart")
        return "generations"
    return get_settings().deploy_strategy


def revision() -> str:
    if sha := get_settings().release_revision:
        return sha
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=BACK, text=True
    ).strip()


def resolve_source_root() -> Path:
    """배포 원본 레포 경로. `.git` 없는 실행 릴리스 경로로의 덮어쓰기를 막는다.

    Git 작업 트리라는 것만으로는 부족하다. 식별 파일이나 원격 URL로 의도한
    프로젝트 저장소인지까지 확인하고, 아니면 저장 전에 거부한다.
    """
    source = Path(get_settings().deploy_source_root or BACK)
    try:
        top = Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=source, stderr=subprocess.DEVNULL, text=True,
        ).strip())
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError(
            "배포 소스 경로가 유효한 Git 작업 트리가 아닙니다: "
            f"{source}. TTAKKARI_DEPLOY_SOURCE_ROOT를 원본 레포 경로로 지정하세요."
        ) from exc
    if (top / "deploy/generation-contract.json").is_file():
        return top
    try:
        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=top, stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        remote = ""
    if "winterholic-ttakkari-back" in remote:
        return top
    raise RuntimeError(
        "배포 소스가 따까리 백엔드 저장소가 아닙니다: "
        f"{top}. TTAKKARI_DEPLOY_SOURCE_ROOT를 원본 레포 경로로 지정하세요."
    )


def ensure_revision(expected: str) -> None:
    source = resolve_source_root()
    if strategy() == "generations":
        # NOTE: 세대 배포는 `git archive <sha>`로 릴리스를 만든다. 공유 작업 트리의 미커밋 변경이나 다른 세션이 올린 HEAD 는
        # 배포 내용과 무관하다. 로컬 HEAD 가 다른 작업으로 갈라져도 origin/main 에 게시된 커밋은 허용한다.
        for ref in ("HEAD", "refs/remotes/origin/main"):
            if subprocess.run(["git", "merge-base", "--is-ancestor", expected, ref],
                              cwd=source, capture_output=True, check=False).returncode == 0:
                return
        raise ValueError("예약한 커밋이 없거나 현재 브랜치·origin/main 이력에 없습니다. 커밋을 확인하세요.")
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=source, text=True
    ).strip()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if head != expected or dirty:
        raise ValueError(
            "예약한 커밋과 작업 트리가 다릅니다. 변경을 커밋하고 다시 배포하세요."
        )


class Admission:
    def __init__(self, root: Path):
        self.path = root / "run-admission.lock"

    def acquire(self, *, exclusive: bool = False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.open("a")
        try:
            fcntl.flock(
                lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            lock.close()
            return None
        return lock


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.jobs = root / "jobs"
        self.jobs.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        with (self.root / "requests.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def get(self, job_id: str) -> dict:
        return json.loads((self.jobs / f"{uuid.UUID(job_id)}.json").read_text())

    def all(self) -> list[dict]:
        return sorted(
            (json.loads(p.read_text()) for p in self.jobs.glob("*.json")),
            key=lambda j: j["created_at"],
        )

    def _write(self, job: dict) -> dict:
        job["updated_at"] = datetime.now(UTC).isoformat()
        path = self.jobs / f"{uuid.UUID(job['id'])}.json"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(job, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        return job

    def update(self, job_id: str, **fields) -> dict:
        with self.locked():
            return self._write(self.get(job_id) | fields)

    def claim(self, job_id: str) -> dict:
        with self.locked():
            job = self.get(job_id)
            if job["status"] == "requested":
                return self._write(job | {"status": "preparing"})
            return job

    def request(self, sha: str, *, skip_tests: bool = False) -> dict:
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("40자리 커밋 SHA가 필요합니다.")
        with self.locked():
            for job in self.all():
                if job["status"] not in TERMINAL:
                    if job["revision"] == sha and job["skip_tests"] == skip_tests:
                        return job
                    if strategy() != "generations":
                        raise ValueError(
                            "다른 배포가 진행 중입니다. 상태를 확인한 뒤 다시 예약하세요."
                        )
            new_job = self._write(
                {
                    "id": str(uuid.uuid4()),
                    "revision": sha,
                    "skip_tests": skip_tests,
                    "status": "requested",
                    "created_at": datetime.now(UTC).isoformat(),
                    "error": None,
                    "strategy": strategy(),
                }
            )
            if strategy() == "generations":
                from ttakkari.generation_deployment import equivalent_contracts
                source = get_settings().deploy_source_root or BACK
                for old in self.all():
                    if old["id"] == new_job["id"] or old["status"] not in {"requested", "waiting_idle"} or old.get("strategy") != "generations":
                        continue
                    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", old["revision"], sha],
                        cwd=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    if ancestry.returncode == 0 and equivalent_contracts(source, old["revision"], sha):
                        self._write(old | {"status": "superseded", "superseded_by": new_job["id"]})
            return new_job


class Controller:
    def __init__(
        self, root: Path, *, poll_seconds: float = 5, verify_seconds: float = 60
    ):
        self.store = JobStore(root)
        self.admission = Admission(root)
        self.poll_seconds = poll_seconds
        self.verify_seconds = verify_seconds

    def phase(self, job: dict, status: str, **fields) -> None:
        job.update(self.store.update(job["id"], status=status, **fields))
        log.info("deployment %s: %s", job["id"], status)

    async def command(self, job: dict, *args: str, env: dict | None = None,
                      timeout: float | None = None) -> None:
        with (self.store.jobs / f"{job['id']}.log").open("a") as output:
            output.write(f"\n$ {' '.join(args)}\n")
            output.flush()
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=BACK, env=env, stdout=output, stderr=output,
                start_new_session=timeout is not None,
            )
            try:
                code = (
                    await asyncio.wait_for(proc.wait(), timeout)
                    if timeout is not None
                    else await proc.wait()
                )
            except TimeoutError:
                from ttakkari.generation_deployment import _terminate_group

                await _terminate_group(proc)
                raise RuntimeError(f"명령 시간 초과 ({timeout}s): {' '.join(args)}") from None
            if code:
                raise RuntimeError(
                    f"명령 실패: {' '.join(args)}. 로그: {job['id']}.log"
                )

    async def run_security_gate(self, job: dict, env: dict[str, str]) -> None:
        """Legacy idle/bootstrap path: bounded security gate before any restart."""
        from ttakkari.security.gate import build_security_gate_command

        command = build_security_gate_command(BACK / ".venv/bin/python", timeout=120)
        await self.command(job, *command, env=env, timeout=300)

    async def prepare(self, job: dict) -> None:
        ensure_revision(job["revision"])
        self.phase(job, "building")
        await self.command(job, "uv", "sync", "--frozen", "--quiet")
        # 배포별 DB를 써서 다른 세션의 pytest와 운영 데이터에 영향을 주지 않는다.
        name = "ttakkari_deploy_" + uuid.UUID(job["id"]).hex
        url = make_url(get_settings().database_url).set(drivername="postgresql")
        admin = await asyncpg.connect(
            url.set(database="postgres").render_as_string(hide_password=False)
        )
        created = False
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
            created = True
            test_url = url.set(
                drivername="postgresql+asyncpg", database=name
            ).render_as_string(hide_password=False)
            env = os.environ | {"TTAKKARI_TEST_DB": test_url}
            env.pop("TTAKKARI_BACKEND_DEPLOY_LOCK_HELD", None)
            self.phase(job, "security_gate")
            await self.run_security_gate(job, env)
            if not job["skip_tests"]:
                self.phase(job, "testing")
                await self.command(job, "uv", "run", "pytest", "-q", "-x", env=env)
        finally:
            try:
                if created:
                    await admin.execute(f'DROP DATABASE "{name}"')
            finally:
                await admin.close()
        ensure_revision(job["revision"])
        self.phase(job, "waiting", active_runs=None)

    async def wait_idle(self, job: dict):
        while True:
            count = await _active_run_count(None)
            self.phase(job, "waiting", active_runs=count)
            if count == 0:
                lease = self.admission.acquire(exclusive=True)
                if lease is not None:
                    try:
                        if await _active_run_count(None) == 0:
                            return lease
                    except BaseException:
                        lease.close()
                        raise
                    lease.close()
            await asyncio.sleep(self.poll_seconds)

    async def runtime(self) -> dict:
        async with httpx.AsyncClient(timeout=3) as client:
            base = get_settings().internal_api_url.rstrip("/")
            health = await client.get(base + "/api/health")
            health.raise_for_status()
            if health.json().get("ok") is not True:
                raise RuntimeError("health 응답이 정상 상태가 아닙니다.")
            r = await client.get(base + "/api/version")
            r.raise_for_status()
            return r.json()

    async def public_ready(self, job: dict) -> None:
        base = os.environ.get(
            "TTK_PUBLIC_URL", "https://ttakkari-api.winterholic.net"
        ).rstrip("/")
        for path in ("/api/health", "/api/version"):
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-fsS",
                "--max-time",
                "5",
                base + path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await proc.communicate()
            if proc.returncode:
                raise RuntimeError("공개 API 확인 실패")
            value = json.loads(out)
            if path.endswith("health") and value.get("ok") is not True:
                raise RuntimeError("공개 health 확인 실패")
            if path.endswith("version") and value.get("revision") != job["revision"]:
                raise RuntimeError("공개 API 버전 불일치")

    async def verify(self, job: dict, *, old_pid: int | None) -> None:
        deadline = time.monotonic() + self.verify_seconds
        error = "버전 또는 PID 확인 실패"
        while True:
            try:
                value = await self.runtime()
                if (
                    value.get("revision") == job["revision"]
                    and value.get("pid")
                    and value["pid"] != old_pid
                ):
                    await self.public_ready(job)
                    return
                error = "요청한 버전 또는 새 PID가 아닙니다."
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                error = str(exc)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"재시작 버전/PID 확인 실패: {error}")
            await asyncio.sleep(self.poll_seconds)

    def restart(self) -> None:
        subprocess.run(
            [
                "launchctl",
                "kickstart",
                "-k",
                f"gui/{os.getuid()}/net.winterholic.ttakkari.backend",
            ],
            check=True,
        )

    async def process(self, job: dict) -> None:
        job.update(self.store.claim(job["id"]))
        if job["status"] in TERMINAL:
            return
        if job.get("bootstrap"):
            if job["status"] != "recovery_pending":
                from ttakkari.generation_bootstrap import Bootstrap
                await Bootstrap(self).process(job)
            return
        if job.get("strategy") == "generations":
            from ttakkari.generation_deployment import GenerationController
            await GenerationController(self.store).process(job)
            return
        lease = None
        try:
            if job["status"] not in {"waiting", "restarting", "verifying"}:
                await self.prepare(job)
            elif job["status"] in {"restarting", "verifying"}:
                # kickstart 뒤 감독이 끊겼다면 이미 올라온 버전부터 확인해 중복 재시작을 피한다.
                try:
                    await self.verify(job, old_pid=job.get("old_pid"))
                except RuntimeError:
                    pass
                else:
                    self.succeed(job)
                    return
            lease = await self.wait_idle(job)
            ensure_revision(job["revision"])
            current = self.store.root / "current"
            previous_revision = current.read_text().strip() if current.exists() else None
            try:
                old_pid = (await self.runtime()).get("pid")
            except (httpx.HTTPError, ValueError, RuntimeError):
                old_pid = None
            self.phase(job, "migrating", previous_revision=previous_revision)
            await self.command(job, "uv", "run", "ttakkari", "migrate")
            ensure_revision(job["revision"])
            self.phase(job, "restarting", old_pid=old_pid)
            self.restart()
            self.phase(job, "verifying")
            await self.verify(job, old_pid=old_pid)
            self.succeed(job)
        except Exception as exc:
            log.exception("deployment %s failed", job["id"])
            self.phase(job, "failed", error=str(exc))
        finally:
            if lease is not None:
                lease.close()

    def succeed(self, job: dict) -> None:
        records = {"current": job["revision"]}
        if job.get("previous_revision"):
            records["previous"] = job["previous_revision"]
        for name, sha in records.items():
            path = self.store.root / name
            tmp = path.with_suffix(".tmp")
            tmp.write_text(sha + "\n")
            tmp.replace(path)
        self.phase(job, "succeeded", active_runs=0, error=None)

    def enter_pinned_supervisor(self) -> None:
        marker = self.store.root / "supervisor.json"
        if not marker.exists():
            return
        if os.environ.get("TTAKKARI_RUN_ID"):
            raise RuntimeError("supervisor belongs to the external deployer")
        value = json.loads(marker.read_text())
        release = Path(value["release"])
        if BACK.resolve() == release.resolve():
            return
        env = os.environ | {"TTAKKARI_RELEASE_REVISION": value["revision"],
            "TTAKKARI_DEPLOY_SOURCE_ROOT": value["source"], "TTAKKARI_DEPLOY_STRATEGY": "generations"}
        os.chdir(release)
        python = str(release / ".venv/bin/python")
        os.execve(python, [python, "-m", "ttakkari.deployment", "serve"], env)

    def bootstrap_job(self) -> dict | None:
        from ttakkari.generation_bootstrap import requested
        if not requested(BACK, self.store.root):
            return None
        # Only a verified, successful legacy deploy may initiate the transition.
        job = next((j for j in reversed(self.store.all()) if j["revision"] == revision()
                    and j["status"] == "succeeded" and j.get("strategy", "idle") == "idle"
                    and not j.get("bootstrap")), None)
        if job:
            return self.store.update(job["id"], status="bootstrapping", bootstrap=True)
        return None

    def pending_jobs(self) -> list[dict]:
        """Drain every old generation, then advance at most one unswitched deployment."""
        pending = [j for j in self.store.all() if j["status"] not in TERMINAL]
        drains, foreground = [], None
        for job in pending:
            if job.get("strategy") == "generations" and not job.get("bootstrap") and job["status"] in {"draining", "retiring"}:
                drains.append(job)
            elif foreground is None:
                foreground = job
        return drains + ([foreground] if foreground else [])

    async def serve(self) -> None:
        self.enter_pinned_supervisor()
        gate = RestartGate(self.store.root / "controller.lock")
        lock = gate._acquire()
        if lock is None:
            raise RuntimeError("배포 감독이 이미 실행 중입니다.")
        loaded_revision = revision()
        try:
            marker = self.store.root / "supervisor.json"
            if marker.exists():
                pinned = json.loads(marker.read_text())
                if pinned["revision"] != loaded_revision or Path(pinned["release"]).resolve() != BACK.resolve():
                    raise RuntimeError("supervisor runtime differs from its pinned release")
                from ttakkari.generation import PORTS
                from ttakkari.generation_bootstrap import write_json
                write_json(self.store.root / "supervisor-runtime.json", {
                    "revision": loaded_revision, "release": str(BACK), "pid": os.getpid(), "ports": list(PORTS)})
            while True:
                if strategy() == "generations":
                    from ttakkari.generation_deployment import GenerationController
                    try:
                        await GenerationController(self.store).reconcile_services()
                    except Exception:
                        log.exception("generation services need recovery; preserving existing work")
                jobs = self.pending_jobs()
                if not jobs and (bootstrap := self.bootstrap_job()):
                    jobs = [bootstrap]
                for job in jobs:
                    # A drain failure in this pass must fence any later cutover.
                    blocked = any(j["status"] in {"recovery_pending", "rolling_back"}
                                  and j["id"] != job["id"] for j in self.store.all())
                    if blocked and job["status"] not in {"draining", "retiring", "recovery_pending", "rolling_back"}:
                        break
                    deploy_gate = RestartGate(self.store.root / "backend-restart.lock")
                    deploy_lock = deploy_gate._acquire()
                    if deploy_lock is not None:
                        try:
                            await self.process(job)
                        finally:
                            deploy_gate._release(deploy_lock)
                        if job.get("bootstrap") and job["status"] == "succeeded":
                            await dispose_engine()
                            self.enter_pinned_supervisor()
                        if job["status"] in TERMINAL and revision() != loaded_revision:
                            # 자기 코드가 바뀐 감독도 작업 경계에서만 다시 읽는다. 예약은 파일에 남는다.
                            await dispose_engine()
                            os.execv(
                                sys.executable,
                                [sys.executable, "-m", "ttakkari.deployment", "serve"],
                            )
                await asyncio.sleep(self.poll_seconds)
        finally:
            gate._release(lock)
            await dispose_engine()


LockProbeState = Literal["held", "free", "unknown"]


def _probe_lock(path: Path) -> tuple[LockProbeState, str | None]:
    """잠금 상태를 held / free / unknown 으로 분리한다. 권한 거부 등 관측 실패는 절대 free로 취급하지 않는다."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return ("free", None)
    except OSError as exc:
        return ("unknown", type(exc).__name__)
    if not stat_mod.S_ISREG(st.st_mode):
        return ("unknown", "NotRegularFile")
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return ("free", None)
    except OSError as exc:
        return ("unknown", type(exc).__name__)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return ("held", None)
    except OSError as exc:
        return ("unknown", type(exc).__name__)
    else:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        return ("free", None)
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _lock_held(path: Path) -> bool | None:
    state, _reason = _probe_lock(path)
    if state == "held":
        return True
    if state == "free":
        return False
    return None


_default_lock_held = _lock_held


_ALLOWED_DIAG_STATUSES = frozenset({
    "requested",
    "preparing",
    "building",
    "security_gate",
    "testing",
    "waiting",
    "waiting_slot",
    "waiting_idle",
    "activating",
    "draining",
    "retiring",
    "recovery_pending",
    "rolling_back",
    "succeeded",
    "failed",
    "superseded",
    "cancelled",
})
_ALLOWED_DIAG_STRATEGIES = frozenset({"generations", "idle"})
_ALLOWED_DIAG_ERROR_STAGES = frozenset({
    "prepare",
    "build",
    "security_gate",
    "test",
    "cutover",
    "verify",
    "drain",
    "retire",
    "rollback",
    "reconcile",
    "bootstrap",
    "tts-drain",
})
_ALLOWED_UPGRADE_STATUSES = frozenset({"upgrading", "succeeded", "failed", "pending", "skipped"})


def _is_sha40(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _clean_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError):
        return None


def _clean_iso_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value)
        return value
    except ValueError:
        return None


def _classify_job_error_code(error: object) -> str | None:
    """원문 오류 문자열을 단 한 글자도 복사하지 않고 고정 allowlist 코드로만 분류한다."""
    if error is None or error == "":
        return None
    if not isinstance(error, str):
        return "redacted_error"
    lowered = error.lower()
    if "draining generation unreachable" in lowered:
        return "draining_generation_unreachable"
    if "draining slot identity changed" in lowered or "identity mismatch" in lowered:
        return "draining_slot_identity_changed"
    if "previous generation is active" in lowered:
        return "previous_generation_active"
    if "previous generation unavailable" in lowered:
        return "previous_generation_unavailable"
    if "newer generation became active" in lowered:
        return "newer_generation_active"
    if "automatic reconciliation failed" in lowered:
        return "automatic_reconciliation_failed"
    if "supervisor upgrade required" in lowered or "pinned release" in lowered:
        return "supervisor_upgrade_required"
    if "waiting for" in lowered and "idle" in lowered:
        return "waiting_idle"
    if "security_gate" in lowered or "security gate" in lowered:
        return "security_gate_failed"
    if "시간 초과" in error or "timeout" in lowered:
        return "command_timeout"
    if "명령 실패" in error:
        return "command_failed"
    return "redacted_error"


def _job_summary(job: dict) -> dict:
    summary: dict[str, object] = {
        "id": _clean_uuid(job.get("id")),
        "revision": job.get("revision") if _is_sha40(job.get("revision")) else None,
        "status": job.get("status") if job.get("status") in _ALLOWED_DIAG_STATUSES else "unknown",
        "strategy": job.get("strategy") if job.get("strategy") in _ALLOWED_DIAG_STRATEGIES else None,
        "created_at": _clean_iso_timestamp(job.get("created_at")),
    }
    if "updated_at" in job:
        summary["updated_at"] = _clean_iso_timestamp(job.get("updated_at"))
    for slot_key in ("previous_slot", "candidate_slot"):
        if slot_key in job:
            val = job.get(slot_key)
            summary[slot_key] = val if isinstance(val, int) and not isinstance(val, bool) else None
    for uuid_key in ("previous_generation", "candidate_generation", "retired_generation"):
        if uuid_key in job:
            summary[uuid_key] = _clean_uuid(job.get(uuid_key))
    for sha_key in ("previous_revision", "activated_revision"):
        if sha_key in job:
            val = job.get(sha_key)
            summary[sha_key] = val if _is_sha40(val) else None
    if "error_stage" in job:
        raw_stage = job.get("error_stage")
        if raw_stage is None:
            summary["error_stage"] = None
        elif isinstance(raw_stage, str) and raw_stage in _ALLOWED_DIAG_ERROR_STAGES:
            summary["error_stage"] = raw_stage
        else:
            summary["error_stage"] = "unknown"
    raw_err = job.get("error")
    summary["has_error"] = bool(raw_err)
    summary["error_code"] = _classify_job_error_code(raw_err)
    summary["recovery_intent_pending"] = isinstance(job.get("recovery_intent"), dict)
    return summary


def supervisor_diagnostics(root: Path | None = None) -> dict:
    """감독 핀(supervisor.json)과 실행 기록(supervisor-runtime.json)·잠금을 경로/비밀 노출 없이 읽는다."""
    from ttakkari.generation_deployment import Slots, _pid_alive

    root = root or state_dir()
    pinned_rev = None
    pinned_rel: Path | None = None
    pinned_exists = False
    pinned_matches = False
    source_configured = False
    marker = root / "supervisor.json"
    if marker.is_file():
        try:
            pinned = json.loads(marker.read_text())
            raw_pinned_rev = pinned.get("revision")
            pinned_rev = raw_pinned_rev if _is_sha40(raw_pinned_rev) else None
            if pinned.get("release"):
                pinned_rel = Path(pinned["release"])
                pinned_exists = pinned_rel.is_dir()
                rel_marker = pinned_rel / ".release.json"
                if rel_marker.is_file() and pinned_rev is not None:
                    pinned_matches = json.loads(rel_marker.read_text()).get("revision") == pinned_rev
            source_configured = bool(pinned.get("source"))
        except (OSError, ValueError, TypeError):
            pass

    runtime_rev = None
    runtime_rel: Path | None = None
    pid: int | None = None
    pid_alive: bool | None = False
    ports = None
    updated_at = None
    age_s = None
    rt_path = root / "supervisor-runtime.json"
    if rt_path.is_file():
        try:
            rt = json.loads(rt_path.read_text())
            raw_rt_rev = rt.get("revision")
            runtime_rev = raw_rt_rev if _is_sha40(raw_rt_rev) else None
            if rt.get("release"):
                runtime_rel = Path(rt["release"])
            raw_pid = rt.get("pid")
            if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
                pid = raw_pid
                pid_alive = _pid_alive(pid)
            else:
                pid_alive = None if raw_pid is not None else False
            raw_ports = rt.get("ports")
            if isinstance(raw_ports, list) and all(isinstance(p, int) and not isinstance(p, bool) for p in raw_ports):
                ports = raw_ports
            stat = rt_path.stat()
            updated_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
            age_s = round(max(0.0, time.time() - stat.st_mtime), 1)
        except (OSError, ValueError, TypeError):
            pass

    matches_rev = (runtime_rev == pinned_rev) if (pinned_rev and runtime_rev) else None
    matches_rel = None
    if pinned_rel is not None and runtime_rel is not None:
        try:
            matches_rel = runtime_rel.resolve() == pinned_rel.resolve()
        except OSError:
            matches_rel = False

    last_upgrade = None
    upg_path = root / "supervisor-upgrade.json"
    if upg_path.is_file():
        try:
            upg = json.loads(upg_path.read_text())
            raw_upg_rev = upg.get("revision")
            raw_upg_status = upg.get("status")
            last_upgrade = {
                "revision": raw_upg_rev if _is_sha40(raw_upg_rev) else None,
                "status": raw_upg_status if raw_upg_status in _ALLOWED_UPGRADE_STATUSES else "unknown",
            }
        except (OSError, ValueError, TypeError):
            pass

    ctrl_lock_state, _ = _probe_lock(root / "controller.lock")
    restart_lock_state, _ = _probe_lock(root / "backend-restart.lock")
    return {
        "pinned_revision": pinned_rev,
        "pinned_release_exists": pinned_exists,
        "pinned_release_matches_revision": pinned_matches,
        "source_configured": source_configured,
        "runtime_revision": runtime_rev,
        "pid": pid,
        "pid_alive": pid_alive,
        "ports": ports,
        "updated_at": updated_at,
        "age_s": age_s,
        "matches_pinned_revision": matches_rev,
        "matches_pinned_release": matches_rel,
        "controller_lock_held": True if ctrl_lock_state == "held" else (False if ctrl_lock_state == "free" else None),
        "controller_lock_state": ctrl_lock_state,
        "restart_lock_held": True if restart_lock_state == "held" else (False if restart_lock_state == "free" else None),
        "restart_lock_state": restart_lock_state,
        "persisted_proxy_slot": Slots(root).persisted_slot(),
        "last_upgrade": last_upgrade,
    }


def queue_diagnostics(store: JobStore | None = None) -> dict:
    """pending_jobs() 선택 결과와 recovery_pending 차단 관계를 요약한다."""
    store = store or JobStore(state_dir())
    pending = [j for j in store.all() if j["status"] not in TERMINAL]
    drains, foreground = [], None
    for job in pending:
        if job.get("strategy") == "generations" and not job.get("bootstrap") and job["status"] in {"draining", "retiring"}:
            drains.append(job)
        elif foreground is None:
            foreground = job
    blocking = [j for j in pending if j["status"] in {"recovery_pending", "rolling_back"}]
    blocked = (
        [j for j in pending if j["status"] not in {"draining", "retiring", "recovery_pending", "rolling_back"}]
        if blocking
        else []
    )
    return {
        "pending_count": len(pending),
        "cutover_blocked": bool(blocking),
        "draining_jobs": [_job_summary(j) for j in drains],
        "foreground_job": _job_summary(foreground) if foreground else None,
        "blocking_jobs": [_job_summary(j) for j in blocking],
        "blocked_jobs": [_job_summary(j) for j in blocked],
    }


def slot_ownership_diagnostics(
    root: Path,
    generations: list[dict],
    *,
    persisted_proxy_slot: int | None = None,
) -> list[dict]:
    from ttakkari.generation import PORTS
    from ttakkari.generation_deployment import Slots

    if persisted_proxy_slot is None:
        persisted_proxy_slot = Slots(root).persisted_slot()
    out = []
    for slot, port in enumerate(PORTS):
        origin = Slots.origin(slot)
        slot_gens = [g for g in generations if g.get("slot") == slot]
        active_owner = next((g["id"] for g in slot_gens if g.get("state") == "active"), None)
        live = [
            {
                "id": g["id"],
                "revision": g.get("revision"),
                "state": g.get("state"),
                "pid": g.get("pid"),
                "pid_alive": g.get("pid_alive"),
                "lock_held": g.get("lock_held"),
                "leases": g.get("leases", 0),
                "process_groups": g.get("process_groups", 0),
            }
            for g in slot_gens
            if (
                g.get("state") != "offline"
                or g.get("lock_held") is not False
                or g.get("pid_alive") is True
                or (g.get("pid_alive") is None and g.get("pid") is not None)
                or g.get("leases", 0) > 0
            )
        ]
        out.append({
            "slot": slot,
            "port": port,
            "origin": origin,
            "persisted_proxy": persisted_proxy_slot == slot,
            "active_owner": active_owner,
            "live_generations": live,
        })
    return out


async def diagnose_deployment(root: Path | None = None, *, include_db: bool = True) -> dict:
    root = root or state_dir()
    store = JobStore(root)
    sup = supervisor_diagnostics(root)
    queue = queue_diagnostics(store)
    result: dict = {
        "deploy_strategy": strategy(),
        "supervisor": sup,
        "deploy_queue": queue,
    }
    if include_db:
        try:
            from ttakkari.db import sessionmaker
            from ttakkari.operations.inline import runtime_report

            async with sessionmaker()() as db:
                report = await runtime_report(db)
            result["control"] = report.get("control")
            result["generations"] = report.get("generations")
            result["slots"] = report.get("slots")
        except Exception as exc:  # noqa: BLE001
            result["db_error"] = type(exc).__name__
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    upgrade = sub.add_parser("upgrade-supervisor")
    upgrade.add_argument("--revision", required=True)
    diagnose = sub.add_parser("diagnose")
    diagnose.add_argument("--no-db", action="store_true")
    recover = sub.add_parser("recover-drain")
    recover.add_argument("--job-id", required=True)
    recover.add_argument("--expected-revision", required=True)
    recover.add_argument("--expected-previous-generation", required=True)
    recover.add_argument("--expected-previous-revision", required=True)
    recover.add_argument("--expected-previous-slot", type=int, required=True)
    recover.add_argument("--expected-candidate-generation", required=True)
    recover.add_argument("--expected-active-generation", required=True)
    recover.add_argument("--expected-active-revision", required=True)
    recover.add_argument("--expected-epoch", type=int, required=True)
    recover.add_argument("--apply", action="store_true")
    request = sub.add_parser("request")
    request.add_argument("--no-pull", action="store_true")
    request.add_argument("--skip-tests", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("job_id", nargs="?")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.command == "serve":
        asyncio.run(Controller(state_dir()).serve())
        return
    if args.command == "upgrade-supervisor":
        from ttakkari.generation_deployment import upgrade_supervisor
        print(json.dumps(asyncio.run(upgrade_supervisor(Controller(state_dir()), args.revision))))
        return
    if args.command == "diagnose":
        print(
            json.dumps(
                asyncio.run(diagnose_deployment(state_dir(), include_db=not args.no_db)),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "recover-drain":
        from ttakkari.generation_deployment import GenerationController

        ctrl = GenerationController(JobStore(state_dir()))
        out = asyncio.run(
            ctrl.recover_drain(
                job_id=args.job_id,
                expected_revision=args.expected_revision,
                expected_previous_generation=args.expected_previous_generation,
                expected_previous_revision=args.expected_previous_revision,
                expected_previous_slot=args.expected_previous_slot,
                expected_candidate_generation=args.expected_candidate_generation,
                expected_active_generation=args.expected_active_generation,
                expected_active_revision=args.expected_active_revision,
                expected_epoch=args.expected_epoch,
                apply=args.apply,
            )
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if args.command == "status":
        store = JobStore(state_dir())
        print(
            json.dumps(
                store.get(args.job_id) if args.job_id else store.all(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if os.environ.get("TTAKKARI_RUN_ID"):
        sys.exit(
            "따까리 작업에서는 request_backend_deploy MCP 도구로 예약하세요. 직접 잠금·재시작 권한은 필요 없습니다."
        )
    ensure_revision(revision())
    if not args.no_pull:
        subprocess.run(["git", "pull", "--ff-only"], cwd=BACK, check=True)
    sha = revision()
    ensure_revision(sha)
    job = JobStore(state_dir()).request(sha, skip_tests=args.skip_tests)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    print("배포 예약 접수. 완료 상태는 deploy/deploy.sh status <id>로 확인하세요.")


if __name__ == "__main__":
    main()
