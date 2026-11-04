"""배포 릴리스 디렉터리 자동 정리.

배포마다 ~/.ttakkari/deploy/releases/<40자 SHA>/ 가 약 600MB(.venv 포함)씩 쌓이고 배포 감독에는 정리 기능이 없다(2026-11-04 260개, 120GB).
감독은 한 번 설치되면 앱 배포로 갱신되지 않으므로 정리는 최신 코드로 도는 세대 런타임의 주기 작업(generation_service.maintenance_jobs)이 맡는다.

확신이 없으면 지우지 않는다(fail-closed). 다음은 모두 보호한다.
- 실행 중인 프로세스가 쓰는 릴리스(`ps` 의 deploy/releases/<sha>), 이 프로세스 자신의 릴리스
- 상태 파일(current·previous·supervisor*.json·proxy.json·bootstrap-*.json·generations-enabled.json)과 services/ 가 가리키는 SHA
- ~/Library/LaunchAgents 의 ttakkari plist 가 가리키는 SHA
- 끝나지 않은 배포 작업(jobs/*.json)의 revision
- 가장 최근 keep_recent 개와, 수정된 지 min_age 초가 안 된 것(방금 만든 빌드)
`ps` 를 읽지 못하거나 상태 파일을 읽다 오류가 나면 이번 정리는 통째로 건너뛴다."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ttakkari.config import get_settings

log = logging.getLogger("ttakkari.release_gc")
SHA = re.compile(r"[0-9a-f]{40}")
STATE_FILES = ("current", "previous", "supervisor.json", "supervisor-runtime.json", "supervisor-upgrade.json",
               "proxy.json", "bootstrap-proxy.json", "bootstrap-smoke.json", "generations-enabled.json")
ACTIVE_JOB_STATUSES = frozenset({"requested", "testing", "building", "waiting_slot", "waiting_idle", "switching",
                                 "draining", "running"})


@dataclass
class PruneResult:
    skipped: str | None = None
    kept: int = 0
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    freed_bytes: int = 0


class ProtectionUnreadable(RuntimeError):
    """보호 목록을 믿을 수 없어 정리를 건너뛴다."""


def _shas(text: str) -> set[str]:
    return set(SHA.findall(text))


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except FileNotFoundError:
        return ""
    except OSError as e:  # 디렉터리·권한 문제는 보호 목록이 불완전하다는 뜻이다
        raise ProtectionUnreadable(f"{path.name}: {type(e).__name__}") from e


def running_releases(ps_text: str) -> set[str]:
    return set(re.findall(r"deploy/releases/([0-9a-f]{40})", ps_text))


def _ps() -> str:
    out = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True, timeout=20, check=False)
    if out.returncode != 0 or not out.stdout:
        raise ProtectionUnreadable("ps")
    return out.stdout


def protected_revisions(root: Path, *, ps_text: str, launch_agents: Path | None, own_revision: str | None) -> dict[str, set[str]]:
    """보호할 SHA 와 그 이유."""
    why: dict[str, set[str]] = {}

    def add(shas: set[str], reason: str) -> None:
        for sha in shas:
            why.setdefault(sha, set()).add(reason)

    add(running_releases(ps_text), "실행 중 프로세스")
    if own_revision:
        add(_shas(own_revision), "이 프로세스")
    for name in STATE_FILES:
        add(_shas(_read(root / name)), name)
    services = root / "services"
    if services.is_dir():
        for path in sorted(services.iterdir()):
            if path.is_file():
                add(_shas(_read(path)), f"services/{path.name}")
    if launch_agents is not None and launch_agents.is_dir():
        for path in sorted(launch_agents.glob("*ttakkari*.plist")):
            add(_shas(_read(path)), f"launchd {path.name}")
    jobs = root / "jobs"
    if jobs.is_dir():
        for path in jobs.glob("*.json"):
            try:
                data = json.loads(_read(path) or "{}")
            except ValueError as e:
                raise ProtectionUnreadable(f"jobs/{path.name}") from e
            if isinstance(data, dict) and data.get("status") in ACTIVE_JOB_STATUSES:
                add(_shas(str(data.get("revision", ""))), "진행 중 배포 작업")
    return why


def _size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if not p.is_symlink() and p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def prune(root: Path, *, keep_recent: int = 8, min_age_seconds: int = 3600, own_revision: str | None = None,
          ps_text: str | None = None, launch_agents: Path | None = None, now: float | None = None,
          dry_run: bool = False) -> PruneResult:
    """root 는 ~/.ttakkari/deploy. 지운 릴리스와 남긴 개수를 돌려준다."""
    result = PruneResult()
    releases = root / "releases"
    if not releases.is_dir():
        result.skipped = "releases 없음"
        return result
    try:
        why = protected_revisions(root, ps_text=ps_text if ps_text is not None else _ps(), launch_agents=launch_agents,
                                  own_revision=own_revision)
    except (ProtectionUnreadable, subprocess.SubprocessError, OSError) as e:
        result.skipped = f"보호 목록을 읽지 못함: {e}"
        return result
    now = time.time() if now is None else now
    entries = [p for p in releases.iterdir() if SHA.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    protect = set(why) | {p.name for p in entries[:max(keep_recent, 0)]}
    for path in entries:
        if path.name in protect or now - path.stat().st_mtime < min_age_seconds:
            result.kept += 1
            continue
        if dry_run:
            result.deleted.append(path.name)
            result.freed_bytes += _size(path)
            continue
        size = _size(path)
        try:
            shutil.rmtree(path)
        except OSError:
            result.failed.append(path.name)
            result.kept += 1
            continue
        result.deleted.append(path.name)
        result.freed_bytes += size
    return result


async def run_once() -> None:
    """세대 런타임의 주기 작업. 파일 삭제는 이벤트 루프를 막지 않게 스레드에서 한다."""
    s = get_settings()
    root = s.home / "deploy"
    res = await asyncio.to_thread(
        prune, root, keep_recent=s.release_gc_keep_recent, min_age_seconds=s.release_gc_min_age_seconds,
        own_revision=s.release_revision, launch_agents=Path.home() / "Library" / "LaunchAgents")
    if res.skipped:
        log.info("release gc skipped: %s", res.skipped)
    elif res.deleted or res.failed:
        log.warning("release gc: deleted %d releases (%.1f GB), kept %d, failed %d",
                    len(res.deleted), res.freed_bytes / 1e9, res.kept, len(res.failed))
