"""GitHub account selection is available in either owner session group without token exposure."""

import pytest

from ttakkari.github.accounts import AccountRegistry, check_gh_args

pytestmark = [pytest.mark.security]


async def test_switch_registered_account_uses_fixed_host_and_checks_observed_active_state():
    calls, active = [], {"login": "winterholic"}
    async def runner(args):
        import json
        calls.append(list(args))
        if args[1:3] == ["auth", "switch"]:
            active["login"] = args[-1]
            return 0, b"switched"
        return 0, json.dumps({"hosts": {"github.com": [
            {"login": login, "state": "success", "active": active["login"] == login}
            for login in ("winterholic", "daeseon-dev")
        ]}}).encode()
    reg = AccountRegistry(runner=runner, gh_bin="gh")
    result = await reg.switch_account("daeseon-dev")
    assert result == {"account": "daeseon-dev", "active": True}
    assert ["gh", "auth", "switch", "--hostname", "github.com", "--user", "daeseon-dev"] in calls
    assert not any("token" in call for call in calls)


def test_switch_accepts_only_fixed_args_and_valid_registered_login_syntax():
    check_gh_args(["gh", "auth", "switch", "--hostname", "github.com", "--user", "daeseon-dev"])
    for tail in (["auth", "switch", "--hostname", "evil.test", "--user", "alice"],
                 ["auth", "switch", "--hostname", "github.com", "--user", "--help"]):
        with pytest.raises(ValueError):
            check_gh_args(["gh", *tail])


async def test_unknown_account_never_reaches_switch():
    from ttakkari.github.errors import GithubError
    calls = []
    async def runner(args):
        calls.append(args)
        return 0, b'{"hosts":{}}'
    reg = AccountRegistry(runner=runner, gh_bin="gh")
    with pytest.raises(GithubError):
        await reg.switch_account("unknown")
    assert not any("switch" in call for call in calls)


@pytest.mark.parametrize("pool", ["personal", "work"])
async def test_authenticated_session_can_list_and_switch_without_revealing_credentials(client, auth, make_dir, monkeypatch, pool):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from test_orchestrator import _session

    from ttakkari.github import service
    from ttakkari.orchestrator.runtime import issue_token

    sess = await _session(client, auth, make_dir, pool=pool)
    headers = {"Authorization": "Bearer " + issue_token(sess["id"])}
    registry = SimpleNamespace(
        list_accounts=AsyncMock(return_value=[SimpleNamespace(login="daeseon-dev", active=True, state="success")]),
        switch_account=AsyncMock(return_value={"account": "daeseon-dev", "active": True}))
    monkeypatch.setattr(service, "get_service", lambda: SimpleNamespace(registry=registry))
    response = await client.post("/api/agent/github/switch-account", headers=headers, json={"account": "daeseon-dev"})
    assert response.status_code == 200, response.text
    assert response.json() == {"account": "daeseon-dev", "active": True}
    listing = await client.get("/api/agent/github/accounts", headers=headers)
    assert listing.status_code == 200 and listing.json()["accounts"][0]["login"] == "daeseon-dev"
    assert (await client.post("/api/agent/github/switch-account", json={"account": "daeseon-dev"})).status_code == 401
