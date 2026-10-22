"""테스트가 만드는 임시 git 저장소를 호출 환경(특히 git 훅)으로부터 격리한다.

pre-push 훅은 GIT_DIR 등을 자식 프로세스에 넘긴다. 테스트가 `git init`/`git commit` 을 cwd 만 지정해 부르면
그 값이 임시 디렉터리보다 우선해 공유 저장소의 core.bare 와 worktree 브랜치를 바꿔 버린다.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

# `git rev-parse --local-env-vars` 와 같은 목록. 어긋나면 tests/test_git_env_isolation.py 가 잡는다.
LOCAL_GIT_ENV = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
})
_INDEXED_CONFIG = re.compile(r"GIT_CONFIG_(KEY|VALUE)_\d+")


def _is_local(name: str) -> bool:
    return name in LOCAL_GIT_ENV or bool(_INDEXED_CONFIG.fullmatch(name))


def isolated_git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """저장소를 가리키는 GIT_* 를 지우고 사용자·시스템 git 설정도 끈 환경을 돌려준다."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not _is_local(k)}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def scrub_process_env() -> None:
    """이 프로세스(와 앞으로 만들 모든 subprocess)의 환경에서 저장소 지정 GIT_* 를 지운다."""
    for name in [k for k in os.environ if _is_local(k)]:
        del os.environ[name]


def git_argv(root: Path, *args: str) -> list[str]:
    """항상 명시적 -C 와 고정 작성자로 git 명령줄을 만든다."""
    return ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args]
