"""A failed refresh or expired Muse window is not evidence of spare quota."""

import time
from uuid import uuid4

import pytest

from ttakkari.runs import account_status, profiles, usage


@pytest.mark.parametrize("failure", ["unavailable", "five_hour_reset"])
def test_muse_incomplete_current_quota_is_unknown_but_still_blocks_exhaustion(failure):
    aid = uuid4()
    now = time.time()
    observed = usage.Usage(
        windows=[
            usage.Window(
                "five_hour",
                "5h",
                0,
                int(now + (3600 if failure == "unavailable" else -1)),
            ),
            usage.Window("seven_day", "week", 20, int(now + 86400)),
        ],
        source="muse_cli",
        updated_at=now - 60,
        login="ok",
        error="Muse /usage unavailable" if failure == "unavailable" else None,
    )
    usage.seed(aid, observed)
    profile = profiles.Profile(aid, "Muse", "muse", model="muse-spark-1.3")
    try:
        assert profiles._usage_headroom(profile, now) is None
        view = account_status._usage(aid, now)
        assert view["min_remaining_percent"] is None
        assert account_status._cap_remaining(aid, 100, view) is None
        # A later failed read cannot release an observed weekly exhaustion.
        observed.windows[1].percent = 100
        assert profiles.usage_limit_until(profile, now) > now
    finally:
        usage.forget(aid)


def test_muse_fresh_complete_quota_still_has_measured_capacity():
    aid = uuid4()
    now = time.time()
    usage.seed(
        aid,
        usage.Usage(
            windows=[
                usage.Window("five_hour", "5h", 35, int(now + 3600)),
                usage.Window("seven_day", "week", 20, int(now + 86400)),
            ],
            source="muse_cli",
            updated_at=now,
            login="ok",
        ),
    )
    try:
        profile = profiles.Profile(aid, "Muse", "muse", model="muse-spark-1.3")
        assert profiles._usage_headroom(profile, now)[0] == 65
        assert (
            account_status._cap_remaining(aid, 100, account_status._usage(aid, now))
            == 65
        )
    finally:
        usage.forget(aid)


def test_muse_account_status_requires_muse_cli_even_when_cached_source_is_api():
    aid = uuid4()
    now = time.time()
    usage.seed(
        aid,
        usage.Usage(
            windows=[
                usage.Window("five_hour", "5h", 35, int(now + 3600)),
                usage.Window("seven_day", "week", 20, int(now + 86400)),
            ],
            source="api",
            updated_at=now,
        ),
    )
    try:
        view = account_status._usage(aid, now, engine="muse")
        assert view["stale"] is True
        assert view["min_remaining_percent"] is None
        assert account_status._cap_remaining(aid, 100, view, engine="muse") is None
    finally:
        usage.forget(aid)


@pytest.mark.parametrize(
    "source,error,windows",
    [
        (
            "muse_cli",
            "Muse /usage unavailable",
            [
                usage.Window("five_hour", "5h", 10, int(time.time() + 3600)),
                usage.Window("seven_day", "week", 20, int(time.time() + 86400)),
            ],
        ),
        (
            "muse_cli",
            None,
            [usage.Window("five_hour", "5h", 10, int(time.time() + 3600))],
        ),
        (
            "muse_cli",
            None,
            [
                usage.Window("five_hour", "5h", 10, None),
                usage.Window("seven_day", "week", 20, int(time.time() + 86400)),
            ],
        ),
    ],
)
def test_muse_accounts_api_never_promotes_incomplete_quota_to_headroom(
    monkeypatch, source, error, windows
):
    from ttakkari.api import accounts as api_accounts
    from ttakkari.models import EngineAccount

    aid = uuid4()
    account = EngineAccount(
        id=aid,
        name="Muse test",
        engine="muse",
        pool="personal",
        enabled=True,
        work_enabled=True,
        max_used_percent=95,
    )
    monkeypatch.setattr(api_accounts.profiles, "identity", lambda *_: ("ok", None))
    monkeypatch.setattr(api_accounts.profiles, "cooldown_remaining", lambda *_: 0)
    monkeypatch.setattr(api_accounts.profiles, "execution_ready", lambda *_: True)
    observed = usage.Usage(
        windows=windows, source=source, error=error, updated_at=time.time()
    )

    out = api_accounts._usage_out(0, account, {}, observed)

    assert out.remaining_to_limit_percent is None


def test_muse_account_status_marks_incomplete_quota_stale():
    aid = uuid4()
    now = time.time()
    usage.seed(
        aid,
        usage.Usage(
            windows=[usage.Window("five_hour", "5h", 20, int(now + 3600))],
            source="muse_cli",
            updated_at=now,
        ),
    )
    try:
        view = account_status._usage(aid, now, engine="muse")
        assert view["stale"] is True
        assert view["min_remaining_percent"] is None
    finally:
        usage.forget(aid)


def test_muse_quota_current_rejects_non_muse_source_and_preserves_other_engine_api(
    monkeypatch,
):
    now = time.time()
    windows = [
        usage.Window("five_hour", "5h", 35, int(now + 3600)),
        usage.Window("seven_day", "week", 20, int(now + 86400)),
    ]
    assert not usage.muse_quota_current(usage.Usage(windows=windows, source="api"), now)

    from ttakkari.api import accounts as api_accounts
    from ttakkari.models import EngineAccount

    aid = uuid4()
    account = EngineAccount(
        id=aid,
        name="Codex test",
        engine="codex",
        pool="personal",
        enabled=True,
        work_enabled=True,
        max_used_percent=95,
    )
    monkeypatch.setattr(api_accounts.profiles, "identity", lambda *_: ("ok", None))
    monkeypatch.setattr(api_accounts.profiles, "cooldown_remaining", lambda *_: 0)
    monkeypatch.setattr(api_accounts.profiles, "execution_ready", lambda *_: True)
    out = api_accounts._usage_out(
        0, account, {}, usage.Usage(windows=windows, source="api", updated_at=now)
    )
    assert out.remaining_to_limit_percent == 60

    account.engine = "muse"
    out = api_accounts._usage_out(
        0,
        account,
        {},
        usage.Usage(
            windows=windows,
            source="muse_cli",
            updated_at=now,
        ),
    )
    assert out.stale is False
    assert out.remaining_to_limit_percent == 60


@pytest.mark.parametrize(
    "source,percent,reset_offset",
    [
        ("api", 100, -60),
        ("muse_cli", 80, -60),
    ],
)
def test_muse_accounts_api_marks_expired_observations_unknown(
    source, percent, reset_offset, monkeypatch
):
    from ttakkari.api import accounts as api_accounts
    from ttakkari.models import EngineAccount

    now = time.time()
    aid = uuid4()
    account = EngineAccount(
        id=aid,
        name="Muse test",
        engine="muse",
        pool="personal",
        enabled=True,
        work_enabled=True,
        max_used_percent=95,
    )
    monkeypatch.setattr(api_accounts.profiles, "identity", lambda *_: ("ok", None))
    monkeypatch.setattr(api_accounts.profiles, "cooldown_remaining", lambda *_: 0)
    monkeypatch.setattr(api_accounts.profiles, "execution_ready", lambda *_: True)
    observed = usage.Usage(
        windows=[
            usage.Window("five_hour", "5h", 20, int(now + 3600)),
            usage.Window("seven_day", "week", percent, int(now + reset_offset)),
        ],
        source=source,
        updated_at=now,
    )

    out = api_accounts._usage_out(0, account, {}, observed)

    assert out.stale is True
    assert out.remaining_to_limit_percent is None
    assert all(window.percent < 100 for window in out.windows)


def test_codex_session_log_expired_100_is_removed_but_live_api_is_preserved(
    monkeypatch,
):
    from ttakkari.api import accounts as api_accounts
    from ttakkari.models import EngineAccount

    now = time.time()
    windows = [usage.Window("five_hour", "5h", 100, int(now - 60))]
    monkeypatch.setattr(api_accounts.profiles, "identity", lambda *_: ("ok", None))
    monkeypatch.setattr(api_accounts.profiles, "cooldown_remaining", lambda *_: 0)
    monkeypatch.setattr(api_accounts.profiles, "execution_ready", lambda *_: True)

    session_log = EngineAccount(
        id=uuid4(),
        name="Codex log",
        engine="codex",
        pool="personal",
        enabled=True,
        work_enabled=True,
        max_used_percent=100,
    )
    expired = api_accounts._usage_out(
        0,
        session_log,
        {},
        usage.Usage(
            windows=windows,
            source="session_log",
            updated_at=now,
        ),
    )
    assert expired.stale is True
    assert expired.windows == []
    assert expired.remaining_to_limit_percent is None

    api_account = EngineAccount(
        id=uuid4(),
        name="Claude API",
        engine="claude",
        pool="personal",
        enabled=True,
        work_enabled=True,
        max_used_percent=100,
    )
    live_api = api_accounts._usage_out(
        0,
        api_account,
        {},
        usage.Usage(
            windows=[usage.Window("five_hour", "5h", 35, int(now + 3600))],
            source="api",
            updated_at=now,
        ),
    )
    assert live_api.stale is False
    assert live_api.remaining_to_limit_percent == 65
