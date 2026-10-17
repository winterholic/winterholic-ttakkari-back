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
