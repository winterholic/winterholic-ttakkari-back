"""pre-push 훅이 넘기는 GIT_* 가 테스트의 임시 저장소를 거쳐 공유 저장소를 건드리지 못하게 막는 회귀 테스트.

2026-10-21: worktree 안에서 push 하자 훅의 GIT_DIR(공유 저장소의 .git/worktrees/<이름>)이 테스트의 `git init`/
`git commit` 으로 새어 core.bare 가 true 로 바뀌고 worktree 브랜치가 테스트 커밋(init/contract/next)으로 덮였다.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import git
from git_isolation import LOCAL_GIT_ENV, git_argv, isolated_git_env

REPO_ROOT = Path(__file__).resolve().parents[1]

# 훅이 부르는 게이트 스크립트가 만드는 임시 디렉터리에서 테스트가 하는 일을 흉내 낸다:
# 저장소 위치를 알려 주지 않고 `git init` 과 커밋을 한다. GIT_DIR 이 새면 이 저장소가 공유 저장소로 바뀐다.
FAKE_PYTHON = """#!/usr/bin/env bash
env | grep -E '^GIT_(DIR|WORK_TREE|INDEX_FILE|COMMON_DIR|PREFIX)=' > "$FAKE_LOG_DIR/git-env.txt"
d="$(mktemp -d)"
cd "$d" || exit 1
git init -q -b main
git -c user.name=t -c user.email=t@t commit -q --allow-empty -m leaked
echo "1 passed"
"""
FAKE_UV = """#!/usr/bin/env bash
mkdir -p .venv/bin
cp "$FAKE_PYTHON_SRC" .venv/bin/python
chmod +x .venv/bin/python
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(git_argv(root, *args), env=env or isolated_git_env(), capture_output=True, text=True, check=False)


def test_local_env_list_matches_git():
    out = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True).stdout
    missing = set(out.split()) - LOCAL_GIT_ENV
    assert not missing, f"git 이 저장소 지정 변수로 보는 값이 LOCAL_GIT_ENV 에 없다: {sorted(missing)}"


def test_process_env_has_no_repository_variables():
    leaked = sorted(set(os.environ) & LOCAL_GIT_ENV)
    assert not leaked, f"conftest 가 지웠어야 할 GIT_* 가 남아 있다: {leaked}"


def test_git_helper_ignores_leaked_git_dir(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-q", "-b", "main")
    git(parent, "commit", "-q", "--allow-empty", "-m", "base")
    before = git(parent, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(parent / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(parent / ".git" / "index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(parent))

    child = tmp_path / "child"
    child.mkdir()
    git(child, "init", "-q", "-b", "main")
    git(child, "commit", "-q", "--allow-empty", "-m", "child")

    assert (child / ".git").is_dir()
    assert git(parent, "rev-parse", "HEAD") == before
    assert git(parent, "config", "--local", "--bool", "core.bare") == "false"
    assert git(child, "rev-parse", "--show-toplevel") == str(child.resolve())


def test_pre_push_gate_in_a_worktree_leaves_the_parent_repository_alone(tmp_path):
    main = tmp_path / "main"
    remote = tmp_path / "remote.git"
    work = tmp_path / "wt"
    fakebin = tmp_path / "fakebin"
    logdir = tmp_path / "log"
    for d in (main, fakebin, logdir):
        d.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], env=isolated_git_env(), check=True)

    (main / "scripts").mkdir()
    (main / "tests").mkdir()
    gate = main / "scripts" / "verify_deploy_gate.sh"
    gate.write_bytes((REPO_ROOT / "scripts" / "verify_deploy_gate.sh").read_bytes())
    gate.chmod(0o755)
    (main / "tests" / "test_migration_single_head.py").write_text("# placeholder\n")
    _run(main, "init", "-q", "-b", "main")
    _run(main, "add", "-A")
    assert _run(main, "commit", "-q", "-m", "base").returncode == 0
    _run(main, "remote", "add", "origin", str(remote))
    assert subprocess.run([str(gate), "--install-hook"], cwd=main, env=isolated_git_env(), capture_output=True, check=False).returncode == 0
    assert _run(main, "worktree", "add", "-q", "-b", "feature", str(work)).returncode == 0
    assert _run(work, "commit", "-q", "--allow-empty", "-m", "feature work").returncode == 0
    tip = _run(work, "rev-parse", "HEAD").stdout.strip()

    _write_exec(fakebin / "uv", FAKE_UV)
    _write_exec(tmp_path / "fake-python", FAKE_PYTHON)
    env = isolated_git_env() | {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "FAKE_LOG_DIR": str(logdir),
        "FAKE_PYTHON_SRC": str(tmp_path / "fake-python"),
        "TMPDIR": str(tmp_path),
    }
    env.pop("TTK_SKIP_GATE", None)
    push = _run(work, "push", "origin", "HEAD:refs/heads/main", env=env)

    assert push.returncode == 0, push.stdout + push.stderr
    assert "사전 검증 통과" in push.stderr + push.stdout, "훅이 게이트를 실행하지 않았다"
    assert (logdir / "git-env.txt").read_text() == "", "게이트가 테스트 프로세스에 저장소 지정 GIT_* 를 넘겼다"
    assert _run(main, "config", "--local", "--bool", "core.bare").stdout.strip() == "false"
    assert _run(work, "rev-parse", "HEAD").stdout.strip() == tip
    subjects = _run(main, "log", "--format=%s", "--all").stdout.splitlines()
    assert "leaked" not in subjects, "테스트용 커밋이 부모 저장소 이력에 들어갔다"


@pytest.mark.parametrize("name", ["GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"])
def test_isolated_env_drops_each_variable(name):
    assert name not in isolated_git_env({name: "/x", "PATH": "/bin"})
