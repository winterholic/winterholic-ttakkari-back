"""Legacy restart guards and external deployment reservation compatibility helpers."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import inspect
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

log = logging.getLogger(__name__)


class RestartGate:
    def __init__(self, lock_path: Path, *, poll_seconds: float = 30, max_wait_seconds: float = 6 * 60 * 60):
        self.lock_path = lock_path
        self.poll_seconds = poll_seconds
        self.max_wait_seconds = max_wait_seconds

    def _acquire(self):
        try:
            # flock은 파일 내용을 바꾸지 않는다. 기존 잠금에는 쓰기 권한을 요구하지 않는다.
            lock = self.lock_path.open("r")
        except FileNotFoundError:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = self.lock_path.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return None
        return lock

    @staticmethod
    def _release(lock) -> None:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    async def restart_backend(
        self,
        active_runs: Callable[[], int | object],
        restart: Callable[[], None],
        *,
        own_run_id: str | None = None,
    ) -> bool:
        """Wait boundedly, recheck immediately before one restart, and serialize callers."""
        lock = None
        owns_deploy_lock = os.environ.get("TTAKKARI_BACKEND_DEPLOY_LOCK_HELD") == "1"
        if not owns_deploy_lock:
            lock = self._acquire()
            if lock is None:
                log.info("backend restart already owned by another deploy")
                return False
        try:
            deadline = time.monotonic() + self.max_wait_seconds
            async def count() -> int:
                value = active_runs()
                return int(await value) if inspect.isawaitable(value) else int(value)

            while time.monotonic() < deadline:
                # Recheck after the idle observation; a Run may have started meanwhile.
                if await count() == 0 and await count() == 0:
                    log.info("restarting backend; excluded deploy Run=%s", own_run_id or "none")
                    restart()
                    return True
                if time.monotonic() >= deadline:
                    log.error("backend restart deferred: active Runs remained for the full wait window")
                    return False
                await asyncio.sleep(self.poll_seconds)
            log.error("backend restart deferred: active Runs remained for the full wait window")
            return False
        finally:
            if lock is not None:
                self._release(lock)

    @staticmethod
    def deploy_frontend(deploy: Callable[[], None]) -> None:
        """Frontend deployment does not share the backend Run gate."""
        deploy()


def run_deployment_script(script: Path, args: list[str]) -> int:
    """Hold one process lock across pull, migrations, tests and the final restart gate."""
    lock_path = Path.home() / ".ttakkari" / "deploy" / "backend-restart.lock"
    gate = RestartGate(lock_path)
    lock = gate._acquire()
    if lock is None:
        log.info("backend deployment already running; duplicate invocation skipped")
        return 75
    try:
        env = os.environ | {"TTAKKARI_BACKEND_DEPLOY_LOCK_HELD": "1"}
        result = subprocess.run(["bash", str(script), *args], env=env, check=False)
        return result.returncode
    finally:
        gate._release(lock)

async def _active_run_count(own_run_id: str | None) -> int:
    from sqlalchemy import func, select

    from ttakkari.db import sessionmaker
    from ttakkari.models import Run, RunStatus

    async with sessionmaker()() as db:
        # Queued Runs have no live engine process and are restored by server startup; only running work blocks.
        stmt = select(func.count()).select_from(Run).where(Run.status == RunStatus.running)
        if own_run_id:
            stmt = stmt.where(Run.id != UUID(own_run_id))
        return int(await db.scalar(stmt) or 0)


async def deploy_backend(own_run_id: str | None) -> bool:
    # 구형 호출도 직접 kickstart하지 않는다. 외부 감독이 활성 배포 전략을 선택한다.
    from ttakkari.deployment import JobStore, ensure_revision, revision, state_dir

    if own_run_id:
        raise RuntimeError("작업 안에서는 request_backend_deploy MCP 도구로 예약하세요.")
    sha = revision()
    ensure_revision(sha)
    job = JobStore(state_dir()).request(sha)
    print(f"배포 예약: {job['id']} (완료 상태는 deploy/deploy.sh status에서 확인)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=("backend", "deploy-script"))
    parser.add_argument("--own-run-id", default=os.environ.get("TTAKKARI_DEPLOY_RUN_ID") or os.environ.get("TTAKKARI_RUN_ID") or None)
    parser.add_argument("script", nargs="?")
    parser.add_argument("script_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.component == "deploy-script":
        if not args.script:
            parser.error("deploy-script requires a script path")
        raise SystemExit(run_deployment_script(Path(args.script), args.script_args))
    restarted = asyncio.run(deploy_backend(args.own_run_id))
    if not restarted:
        raise SystemExit("backend restart skipped or timed out; rerun deployment when idle")


if __name__ == "__main__":
    main()
