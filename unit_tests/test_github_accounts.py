"""gh 계정 목록·토큰 획득(전역 gh 상태를 바꾸지 않고 요청별 계정 선택). ADR 0042 §5 불변식 1~3."""
from __future__ import annotations

import pickle

import pytest
from github_fakes import FakeGh, fake_token

from ttakkari.github import accounts as acc
from ttakkari.github.errors import GithubError


def reg(gh: FakeGh, clock=None, **kw):
    return acc.AccountRegistry(runner=gh, gh_bin="gh", clock=clock or (lambda: 0.0), **kw)


async def code_of(coro) -> str:
    with pytest.raises(GithubError) as e:
        await coro
    return e.value.code


async def test_lists_github_com_accounts_without_touching_tokens():
    gh = FakeGh([("alice", "success"), ("bob", "error")], active="alice")
    rows = await reg(gh).list_accounts()
    assert [(a.login, a.active, a.state, a.scopes) for a in rows] == [
        ("alice", True, "success", ["gist", "read:org", "repo", "workflow"]),
        ("bob", False, "error", ["gist", "read:org", "repo", "workflow"])]
    assert gh.calls == [("auth", "status", "--json", "hosts")]  # 목록만으로는 토큰 명령이 불리지 않는다


async def test_only_two_gh_commands_are_ever_allowed():
    # 실행기 앞단 가드: 허용 형태 밖(switch·login·임의 login 문자열)은 실행 전에 거부한다.
    for bad in (["gh", "auth", "switch", "--user", "bob"], ["gh", "auth", "login"], ["gh", "auth", "refresh"],
                ["gh", "auth", "token", "--hostname", "github.com", "--user", "bob; rm -rf /"],
                ["gh", "auth", "token", "--hostname", "evil.com", "--user", "bob"],
                ["gh", "api", "user"], ["gh", "auth", "status", "--show-token"]):
        with pytest.raises(ValueError):
            acc.check_gh_args(bad)
    acc.check_gh_args(["gh", "auth", "status", "--json", "hosts"])
    acc.check_gh_args(["gh", "auth", "token", "--hostname", "github.com", "--user", "octo-cat"])


def test_subprocess_env_strips_token_overrides_and_prompts():
    env = acc.build_env({"PATH": "/bin", "HOME": "/h", "GH_TOKEN": "x", "GITHUB_TOKEN": "y", "GH_ENTERPRISE_TOKEN": "z",
                         "GITHUB_ENTERPRISE_TOKEN": "w", "GH_HOST": "ghe.example", "GH_CONFIG_DIR": "/c"})
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST"):
        assert k not in env
    assert env["GH_PROMPT_DISABLED"] == "1" and env["NO_COLOR"] == "1" and env["HOME"] == "/h" and env["GH_CONFIG_DIR"] == "/c"


async def test_require_validates_login_membership_and_state():
    gh = FakeGh([("alice", "success"), ("bob", "error")])
    r = reg(gh)
    assert (await r.require("alice")).login == "alice"
    assert (await r.require("ALICE")).login == "alice"  # 대소문자는 정규화
    assert await code_of(r.require("a/b")) == "invalid_target"
    assert await code_of(r.require("")) == "invalid_target"
    assert await code_of(r.require("carol")) == "account_unavailable"
    assert await code_of(r.require("bob")) == "gh_auth_expired"  # 목록에는 있으나 만료 상태
    assert await code_of(reg(FakeGh([])).require("alice")) == "no_account"


async def test_token_is_fetched_per_account_without_switch_and_cached_in_memory():
    gh = FakeGh()
    now = [0.0]
    r = reg(gh, clock=lambda: now[0], token_ttl=300)
    a = await r.token("alice")
    b = await r.token("bob")
    assert a.reveal() == fake_token("alice") and b.reveal() == fake_token("bob")
    token_calls = [c for c in gh.calls if c[:2] == ("auth", "token")]
    assert token_calls == [("auth", "token", "--hostname", "github.com", "--user", "alice"),
                           ("auth", "token", "--hostname", "github.com", "--user", "bob")]
    assert all(c[:2] != ("auth", "switch") for c in gh.calls)
    await r.token("alice")
    assert len([c for c in gh.calls if c[:2] == ("auth", "token")]) == 2  # 300초 안에는 재호출 없음
    now[0] = 301.0
    await r.token("alice")
    assert len([c for c in gh.calls if c[:2] == ("auth", "token")]) == 3


async def test_token_refresh_after_invalidate_picks_up_new_token():
    gh = FakeGh()
    r = reg(gh)
    first = (await r.token("alice")).reveal()
    gh.gen["alice"] = 1  # gh auth refresh 로 토큰이 바뀐 상황
    assert (await r.token("alice")).reveal() == first  # 캐시
    r.invalidate("alice")
    assert (await r.token("alice")).reveal() == fake_token("alice", 1)


async def test_token_command_failure_or_garbage_is_auth_expired_never_leaks():
    gh = FakeGh()
    gh.token_rc["alice"] = 1
    assert await code_of(reg(gh).token("alice")) == "gh_auth_expired"
    gh2 = FakeGh()
    gh2.bad_stdout = True  # 공백·개행이 섞인 출력은 헤더 주입 위험이라 거부
    with pytest.raises(GithubError) as e:
        await reg(gh2).token("alice")
    assert e.value.code == "gh_auth_expired" and "tok en" not in str(e.value) and "tok en" not in repr(e.value)


def test_secret_never_prints_or_pickles():
    s = acc.Secret(fake_token("alice"))
    assert fake_token("alice") not in repr(s) and fake_token("alice") not in str(s) and fake_token("alice") not in f"{s}"
    assert fake_token("alice") not in repr({"k": s}) and fake_token("alice") not in repr([s])
    with pytest.raises(TypeError):
        pickle.dumps(s)


async def test_list_cache_ttl_and_removed_account_callback_and_token_purge():
    gh = FakeGh()
    now = [0.0]
    r = reg(gh, clock=lambda: now[0], list_ttl=60)
    removed: list[str] = []
    r.on_removed(removed.append)
    await r.list_accounts()
    await r.list_accounts()
    assert len([c for c in gh.calls if c[:2] == ("auth", "status")]) == 1
    await r.token("bob")
    gh.accounts = [("alice", "success")]  # bob 이 gh 에서 로그아웃됨
    now[0] = 61.0
    assert [a.login for a in await r.list_accounts()] == ["alice"]
    assert removed == ["bob"]
    assert await code_of(r.require("bob")) == "account_unavailable"
    n = len(gh.calls)
    assert await code_of(r.token("bob")) == "account_unavailable"
    assert not any(c[:2] == ("auth", "token") and c[-1] == "bob" for c in gh.calls[n:])  # 목록에 없으면 토큰 명령도 안 부른다


async def test_unknown_login_forces_one_refresh_but_not_a_storm():
    gh = FakeGh([("alice", "success")])
    now = [0.0]
    r = reg(gh, clock=lambda: now[0])
    await r.list_accounts()
    gh.accounts.append(("carol", "success"))  # 방금 gh auth login 으로 추가
    now[0] = 10.0
    assert (await r.require("carol")).login == "carol"  # 미스 시 강제 갱신(최소 5초 간격)
    n = len([c for c in gh.calls if c[:2] == ("auth", "status")])
    for _ in range(5):
        await code_of(r.require("zed"))
    assert len([c for c in gh.calls if c[:2] == ("auth", "status")]) <= n + 1


async def test_probe_returns_only_a_boolean():
    gh = FakeGh()
    gh.token_rc["bob"] = 1
    r = reg(gh)
    assert await r.probe("alice") is True and await r.probe("bob") is False
