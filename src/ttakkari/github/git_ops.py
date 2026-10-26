"""Account-scoped GitHub checkout operations. No shell, arbitrary paths, or global gh state changes."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Protocol

from ttakkari.github import targets
from ttakkari.github.accounts import AccountRegistry

TIMEOUT = 180
WORKSPACE_DIR = ".ttakkari-github"


class GitOperationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Runner(Protocol):
    async def __call__(self, args: list[str], *, cwd: Path | None, env: dict[str, str],
                       timeout: int) -> tuple[int, bytes, bytes]: ...


async def _run_git(args: list[str], *, cwd: Path | None, env: dict[str, str],
                   timeout: int) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd) if cwd else None, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise GitOperationError("git_unavailable", "Mac Studio에서 git을 실행할 수 없습니다.") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitOperationError("git_timeout", "GitHub 저장소 작업이 제한 시간을 넘었습니다.") from None
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode if proc.returncode is not None else 1, out[:65536], err[:65536]


def _safe_env(token: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GIT_"))}
    env.update({
        "GIT_ASKPASS": str(Path(__file__).with_name("git_askpass.py")),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "TTAKKARI_GITHUB_TOKEN": token,
    })
    return env


def _target(root: Path, owner: str, repo: str) -> Path:
    try:
        base = root.expanduser().resolve(strict=True)
    except OSError:
        raise GitOperationError("workspace_unavailable", "현재 세션의 작업 공간을 확인할 수 없습니다.") from None
    if not base.is_dir():
        raise GitOperationError("workspace_unavailable", "현재 세션의 작업 공간이 디렉터리가 아닙니다.")
    path = base / WORKSPACE_DIR / owner / repo
    # Do not follow a pre-existing symlink at any newly created path component.
    cursor = base
    for part in (WORKSPACE_DIR, owner, repo):
        cursor = cursor / part
        if cursor.is_symlink():
            raise GitOperationError("unsafe_path", "저장소 경로에 심볼릭 링크가 있어 작업을 거부했습니다.")
    resolved = path.resolve(strict=False)
    if resolved != base and base not in resolved.parents:
        raise GitOperationError("unsafe_path", "저장소 경로가 작업 공간 밖입니다.")
    return path


def _repo(owner: str, repo: str) -> tuple[str, str]:
    try:
        return targets.owner(owner), targets.repo(repo)
    except Exception as exc:
        raise GitOperationError("invalid_target", "GitHub 저장소 owner/repo 형식이 올바르지 않습니다.") from exc


class GithubGitOperations:
    def __init__(self, registry: AccountRegistry, *, runner: Runner = _run_git) -> None:
        self.registry = registry
        self.runner = runner

    async def _git(self, args: list[str], *, cwd: Path | None, env: dict[str, str]) -> tuple[int, bytes, bytes]:
        return await self.runner(args, cwd=cwd, env=env, timeout=TIMEOUT)

    async def run(self, operation: str, account: str, owner: str, repo: str, workspace: Path) -> dict[str, Any]:
        if operation not in {"clone", "pull"}:
            raise GitOperationError("unsupported_operation", "지원 작업은 clone 또는 pull뿐입니다.")
        owner, repo = _repo(owner, repo)
        target = _target(workspace, owner, repo)
        token = await self.registry.token(account)
        env = _safe_env(token.reveal())
        url = f"https://github.com/{owner}/{repo}.git"

        if operation == "clone":
            if target.exists():
                raise GitOperationError("destination_exists", "대상 경로가 이미 있습니다. 기존 저장소를 덮어쓰지 않았습니다.")
            target.parent.mkdir(parents=True, exist_ok=True)
            code, _, _ = await self._git([
                "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull,
                "-c", "protocol.file.allow=never", "clone", "--depth=1", "--", url, str(target),
            ], cwd=workspace, env=env)
            if code != 0:
                raise GitOperationError("git_clone_failed", "GitHub 저장소를 복제하지 못했습니다. 계정 권한과 원격 상태를 확인하세요.")
            if not (target / ".git").is_dir():
                raise GitOperationError("git_clone_failed", "Git clone 결과에 저장소 메타데이터가 없습니다.")
        else:
            if not (target / ".git").is_dir():
                raise GitOperationError("repository_not_found", "이 세션의 GitHub 작업 공간에 해당 저장소가 없습니다.")
            code, remote, _ = await self._git(["-c", "credential.helper=", "remote", "get-url", "origin"], cwd=target, env=env)
            if code != 0 or remote.decode("utf-8", "replace").strip() not in {url, url.removesuffix(".git")}:
                raise GitOperationError("remote_mismatch", "저장소 origin이 요청한 github.com 저장소와 일치하지 않습니다.")
            code, dirty, _ = await self._git(["-c", "credential.helper=", "status", "--porcelain=v1", "--untracked-files=normal"], cwd=target, env=env)
            if code != 0 or dirty.strip():
                raise GitOperationError("working_tree_dirty", "로컬 변경이 있어 pull을 거부했습니다. 파일을 보존했습니다.")
            code, branch_out, _ = await self._git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=target, env=env)
            branch = branch_out.decode("utf-8", "replace").strip()
            if code != 0 or not branch or branch.startswith("-") or any(ord(c) < 32 for c in branch):
                raise GitOperationError("branch_unavailable", "현재 브랜치가 없어 pull할 수 없습니다.")
            code, upstream_out, _ = await self._git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd=target, env=env)
            if code != 0 or upstream_out.decode("utf-8", "replace").strip() != f"origin/{branch}":
                raise GitOperationError("upstream_mismatch", "현재 브랜치가 origin의 같은 이름 브랜치를 추적하지 않아 pull을 거부했습니다.")
            code, _, _ = await self._git([
                "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull,
                "-c", "protocol.file.allow=never", "pull", "--ff-only", "--no-recurse-submodules", "origin", branch,
            ], cwd=target, env=env)
            if code != 0:
                raise GitOperationError("git_pull_failed", "Fast-forward pull에 실패했습니다. 저장소 상태는 유지했습니다.")

        code, sha_out, _ = await self._git(["rev-parse", "HEAD"], cwd=target, env=env)
        sha = sha_out.decode("ascii", "ignore").strip() if code == 0 else ""
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            sha = "unknown"
        return {"operation": operation, "account": account, "repository": f"{owner}/{repo}",
                "path": str(target.relative_to(workspace)), "commit": sha}
