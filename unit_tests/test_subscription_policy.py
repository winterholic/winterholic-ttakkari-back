import importlib

from ttakkari.runs.usage import Usage, Window


def policy():
    name = "ttakkari.runs.subscription_policy"
    assert importlib.util.find_spec(name) is not None, "subscription scheduling policy is not implemented"
    return importlib.import_module(name)


def sample(reset=1000, percent=20, at=500):
    u = Usage(windows=[Window("seven_day", "주간", percent, reset)], source="api", updated_at=at, login="ok")
    u.ordinary_usage_allowed = True
    return u


def test_bootstrap_never_activates_existing_cycle_and_queries_in_twelve_hours():
    d = policy().plan({}, sample(reset=100000), now=500)
    assert not d.activate and d.state["reset_at"] == 100000
    assert d.state["next_check_at"] == 500 + 12 * 3600
    assert d.state["next_activation_at"] == 100010


def test_reset_is_a_single_exact_reservation_without_pre_reset_polling():
    d = policy().plan({}, sample(), now=500)
    assert not d.activate and d.state["next_check_at"] == 1010
    early = policy().plan(d.state, sample(at=1005), now=1005)
    assert early.state["next_check_at"] == 1010 and not early.activate


def test_reset_timestamp_change_replaces_the_old_reservation():
    d = policy().plan({"reset_at": 1000}, sample(reset=2000, at=800), now=800)
    assert d.state["reset_at"] == 2000 and d.state["next_check_at"] == 2010


def test_fresh_zero_after_expired_inactive_window_requests_activation():
    d = policy().plan({"reset_at": 1000, "auto_enabled": True}, sample(percent=0, at=1010), now=1010)
    assert d.activate and d.cycle == 1000
    assert d.state["last_check_outcome"] == "reset_confirmed"


def test_rounded_zero_with_advanced_window_is_ambiguous_and_never_duplicated():
    d = policy().plan({"reset_at": 1000}, sample(reset=605810, percent=0, at=1010), now=1010)
    assert not d.activate and d.state["last_check_outcome"] == "already_active_or_unverifiable"
    assert d.state["handled_reset_at"] == 1000 and d.state["reset_at"] == 605810


def test_user_use_and_prior_dispatch_both_suppress_activation():
    for state, user_used, expected in [({"reset_at": 1000}, True, "already_used"),
                                       ({"reset_at": 1000, "dispatched_cycle": 1000}, False, "already_dispatched")]:
        d = policy().plan(state, sample(percent=0, at=1010), now=1010, user_used=user_used)
        assert not d.activate and d.state["last_check_outcome"] == expected


def test_missing_or_failed_snapshot_cannot_confirm_reset():
    for u in [Usage(error="auth_error"), Usage(), sample(percent=0, at=999)]:
        d = policy().plan({"reset_at": 1000}, u, now=1010)
        assert not d.activate and d.state["next_check_at"] == 1040


def test_reconfirmation_backoff_is_bounded_by_count_and_elapsed_time():
    state, now, times = {"reset_at": 1000}, 1010, []
    for _ in range(6):
        d = policy().plan(state, sample(percent=99, at=now), now=now)
        assert not d.activate
        state = d.state
        times.append(state["next_check_at"] - now)
        now = state["next_check_at"]
    assert times == [30, 60, 120, 240, 450, 43200]
    assert state["last_check_outcome"] == "verification_exhausted"


def test_disabled_auto_keeps_monitoring_but_never_dispatches():
    d = policy().plan({"reset_at": 1000, "auto_enabled": False}, sample(percent=0, at=1010), now=1010)
    assert not d.activate and d.state["next_activation_at"] is None


def test_terminal_cycle_does_not_rearm_a_past_due_timer():
    d = policy().plan({"reset_at": 1000, "handled_reset_at": 1000}, sample(percent=0, at=1100), now=1100)
    assert not d.activate and d.state["next_check_at"] == 44300


def test_percent_and_timestamp_never_override_unknown_or_denied_ordinary_usage():
    for allowed in (None, False):
        u = sample(percent=0, at=1010)
        u.ordinary_usage_allowed = allowed
        d = policy().plan({"reset_at": 1000}, u, now=1010)
        assert not d.activate and d.state["next_check_at"] == 1040
