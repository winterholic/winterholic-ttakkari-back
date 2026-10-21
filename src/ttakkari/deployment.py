"""Persist requests; an external controller owns generation cutover or legacy idle restart."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

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
        # 배포 내용과 무관하므로, 커밋이 존재하고 현재 브랜치 이력에 있는지만 본다(409 병목 방지).
        if subprocess.run(["git", "merge-base", "--is-ancestor", expected, "HEAD"],
                          cwd=source, capture_output=True, check=False).returncode:
            raise ValueError("예약한 커밋이 없거나 현재 브랜치 이력에 없습니다. 커밋을 확인하세요.")
        return
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


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    upgrade = sub.add_parser("upgrade-supervisor")
    upgrade.add_argument("--revision", required=True)
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
