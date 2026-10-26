from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.errors import GithubError
from ttakkari.github.git_ops import GithubGitOperations, GitOperationError


@pytest.fixture
async def registry():
    async def gh(args):
        if args[1:4] == ["auth", "status", "--json"]:
            return 0, b'{"hosts":{"github.com":[{"login":"winterholic","state":"success"},{"login":"daeseon-dev","state":"success"}]}}'
        login = args[-1]
        return 0, f"secret-{login}-token".encode()

    return AccountRegistry(runner=gh, gh_bin="gh")


@pytest.mark.asyncio
async def test_clone_uses_selected_account_without_global_switch(tmp_path, registry):
    calls = []

    async def runner(args, *, cwd, env, timeout):
        calls.append((list(args), cwd, dict(env)))
        if "clone" in args:
            Path(args[-1], ".git").mkdir(parents=True)
        else:
            return 0, b"a" * 40, b""
        return 0, b"Cloning into 'repo'...\n", b""

    ops = GithubGitOperations(registry, runner=runner)
    result = await ops.run("clone", "daeseon-dev", "amamov", "regna-ios", tmp_path)

    assert result["account"] == "daeseon-dev"
    assert result["operation"] == "clone"
    assert "clone" in calls[0][0]
    assert calls[0][0][-3] == "--"
    assert not any("recurse-submodules" in arg for arg in calls[0][0])
    assert calls[0][0][-1] == str(tmp_path / ".ttakkari-github" / "amamov" / "regna-ios")
    assert calls[0][2]["TTAKKARI_GITHUB_TOKEN"] == "secret-daeseon-dev-token"
    assert calls[0][2]["GIT_TERMINAL_PROMPT"] == "0"
    assert "gh" not in " ".join(calls[0][0])
    assert "secret-daeseon-dev-token" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("login", ["winterholic", "daeseon-dev"])
async def test_parallel_accounts_keep_credentials_isolated(tmp_path, registry, login):
    seen = []

    async def runner(args, *, cwd, env, timeout):
        await asyncio.sleep(0)
        if "clone" in args:
            Path(args[-1], ".git").mkdir(parents=True)
            seen.append((args[-2], env["TTAKKARI_GITHUB_TOKEN"]))
            return 0, b"ok", b""
        return 0, b"a" * 40, b""

    ops = GithubGitOperations(registry, runner=runner)
    await asyncio.gather(*(ops.run("clone", user, "amamov", f"repo-{user}", tmp_path)
                           for user in ("winterholic", "daeseon-dev")))

    assert set(seen) == {
        ("https://github.com/amamov/repo-winterholic.git", "secret-winterholic-token"),
        ("https://github.com/amamov/repo-daeseon-dev.git", "secret-daeseon-dev-token"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "repo"), [("../evil", "repo"), ("github.com.evil", "repo"),
                                               ("-c", "credential.helper=evil"), ("owner", "../repo")])
async def test_rejects_untrusted_repo_parts_before_credentials(tmp_path, registry, owner, repo):
    async def runner(*args, **kwargs):
        raise AssertionError("git must not run for invalid repository parts")

    ops = GithubGitOperations(registry, runner=runner)
    with pytest.raises(GitOperationError) as invalid:
        await ops.run("clone", "daeseon-dev", owner, repo, tmp_path)
    assert invalid.value.code == "invalid_target"


@pytest.mark.asyncio
async def test_unknown_account_and_operation_are_rejected(tmp_path, registry):
    async def runner(*args, **kwargs):
        raise AssertionError("git must not run")

    ops = GithubGitOperations(registry, runner=runner)
    with pytest.raises(GithubError):
        await ops.run("clone", "unknown-user", "owner", "repo", tmp_path)
    with pytest.raises(GitOperationError) as unsupported:
        await ops.run("shell", "winterholic", "owner", "repo", tmp_path)
    assert unsupported.value.code == "unsupported_operation"


@pytest.mark.asyncio
@pytest.mark.parametrize(("origin", "dirty"), [
    (b"https://github.com/other/repo.git", b""),
    (b"https://github.com/acme/private.git", b" M tracked.txt"),
])
async def test_pull_refuses_wrong_origin_or_dirty_tree(tmp_path, registry, origin, dirty):
    calls = []
    target = tmp_path / ".ttakkari-github" / "acme" / "private"
    (target / ".git").mkdir(parents=True)

    async def runner(args, *, cwd, env, timeout):
        calls.append(list(args))
        if "remote" in args:
            return (0, origin, b"")
        if "status" in args:
            return (0, dirty, b"")
        raise AssertionError("pull must not start after a failed precondition")

    ops = GithubGitOperations(registry, runner=runner)
    with pytest.raises(GitOperationError):
        await ops.run("pull", "daeseon-dev", "acme", "private", tmp_path)
    assert not any("pull" in call for call in calls)


@pytest.mark.asyncio
async def test_clone_refuses_existing_destination_without_deleting_it(tmp_path, registry):
    async def runner(*args, **kwargs):
        raise AssertionError("git must not run when destination exists")

    target = tmp_path / ".ttakkari-github" / "acme" / "repo"
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("preserve")
    ops = GithubGitOperations(registry, runner=runner)
    with pytest.raises(GitOperationError) as existing:
        await ops.run("clone", "daeseon-dev", "acme", "repo", tmp_path)
    assert existing.value.code == "destination_exists"
    assert marker.read_text() == "preserve"


def test_askpass_rejects_other_hosts_and_answers_github_without_logging(monkeypatch, capsys):
    from ttakkari.github import git_askpass

    monkeypatch.setenv("TTAKKARI_GITHUB_TOKEN", "do-not-print-token")
    monkeypatch.setattr("sys.argv", ["git_askpass.py", "Password for 'https://github.com.evil.com': "])
    assert git_askpass.main() == 1
    assert capsys.readouterr().out == ""

    monkeypatch.setattr("sys.argv", ["git_askpass.py", "Password for 'https://github.com': "])
    assert git_askpass.main() == 0
    assert capsys.readouterr().out == "do-not-print-token\n"

    monkeypatch.setattr("sys.argv", ["git_askpass.py", "Password for 'https://x-access-token@github.com': "])
    assert git_askpass.main() == 0
    assert capsys.readouterr().out == "do-not-print-token\n"


def test_github_git_is_mutating_auto_approved_tool_not_read_only():
    from ttakkari.orchestrator.tool_policy import AUTO_APPROVED_TOOLS, READ_ONLY_TOOLS

    assert "github_git" in AUTO_APPROVED_TOOLS
    assert "github_git" not in READ_ONLY_TOOLS
