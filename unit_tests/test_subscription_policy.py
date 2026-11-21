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
    # An hour earlier than observation + 7 days: the window already started, so a rounded zero may hide real use.
    d = policy().plan({"reset_at": 1000}, sample(reset=602210, percent=0, at=1010), now=1010)
    assert not d.activate and d.state["last_check_outcome"] == "already_active_or_unverifiable"
    assert d.state["handled_reset_at"] == 1000 and d.state["reset_at"] == 602210


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


# 2026-11-21 production check (KST): the weekly boundary arrived at 22:30:04, the check ran 10 s later and the API
# already reported a new window whose resetsAt is the observation time + 7 days (less than a second later).
OLD_RESET, OBSERVED_AT, NEW_RESET = 1795267804, 1795267814.485, 1795872615
WEEK = 7 * 24 * 3600


def reset_check(reset=NEW_RESET, percent=0, at=OBSERVED_AT, user_used=False, **state):
    return policy().plan({"reset_at": OLD_RESET, **state}, sample(reset=reset, percent=percent, at=at),
                         now=at, user_used=user_used)


def test_unused_new_window_after_reset_requests_activation():
    d = reset_check()
    assert d.activate and d.cycle == OLD_RESET
    assert d.state["last_check_outcome"] == "reset_confirmed"
    assert "handled_reset_at" not in d.state, "the durable service owns the dispatch marker"
    assert d.state["reset_at"] == NEW_RESET


def test_observed_2026_11_21_windows_activate_and_the_user_run_account_stays_already_used():
    # (updated_at, resets_at) measured that day: personal 22:30:14.485, daddy 22:31:33.074, company ai2 18:24:59.749.
    # Only the 7-day relation was measured for daddy and ai2; their old reset is derived as observation - 10 s.
    seen = {"personal": (1795267814.485, 1795872615), "daddy": (1795267893.074, 1795872694),
            "ai2": (1795253099.749, 1795857900)}
    for name, (at, reset) in seen.items():
        d = policy().plan({"reset_at": int(at) - 10}, sample(reset=reset, percent=0, at=at), now=at,
                          user_used=name == "ai2")
        assert d.activate is (name != "ai2"), name
        assert d.state["last_check_outcome"] == ("already_used" if name == "ai2" else "reset_confirmed"), name


def test_new_window_with_any_usage_is_still_already_used():
    d = reset_check(percent=1)
    assert not d.activate and d.state["last_check_outcome"] == "already_used"
    assert d.state["handled_reset_at"] == OLD_RESET


def test_window_that_started_before_observation_is_never_activated():
    d = reset_check(reset=NEW_RESET - 3600)
    assert not d.activate and d.state["last_check_outcome"] == "already_active_or_unverifiable"
    assert d.state["handled_reset_at"] == OLD_RESET and d.state["reset_at"] == NEW_RESET - 3600


def test_unused_window_tolerance_is_symmetric_and_exclusive():
    tol = policy().UNUSED_WINDOW_TOLERANCE_SECONDS
    at = OLD_RESET + 10
    for drift, activates in [(0, True), (1, True), (-1, True), (tol - 1, True), (1 - tol, True),
                             (tol, False), (-tol, False), (3600, False), (-3600, False)]:
        d = policy().plan({"reset_at": OLD_RESET}, sample(reset=at + WEEK + drift, percent=0, at=at), now=at)
        assert d.activate is activates, drift


def test_new_window_still_obeys_auto_off_user_use_prior_dispatch_and_unconfirmed_usage():
    off = reset_check(auto_enabled=False)
    assert not off.activate and off.state["last_check_outcome"] == "auto_disabled"
    assert off.state["handled_outcome"] == "auto_disabled" and off.state["next_activation_at"] is None
    for state, user_used, expected in [({}, True, "already_used"),
                                       ({"dispatched_cycle": OLD_RESET}, False, "already_dispatched")]:
        d = reset_check(user_used=user_used, **state)
        assert not d.activate and d.state["last_check_outcome"] == expected
    for allowed in (None, False):
        u = sample(reset=NEW_RESET, percent=0, at=OBSERVED_AT)
        u.ordinary_usage_allowed = allowed
        d = policy().plan({"reset_at": OLD_RESET}, u, now=OBSERVED_AT)
        assert not d.activate and d.state["last_check_outcome"] == "already_active_or_unverifiable"


def test_activation_of_an_idle_window_is_not_repeated_and_the_next_week_activates_again():
    first = reset_check()
    # The durable service commits this marker before the model request.
    state = {**first.state, "dispatched_cycle": OLD_RESET, "handled_reset_at": OLD_RESET,
             "handled_outcome": "dispatched"}
    # The request started the window 5 s after the check, so it reports its own start + 7 days: within the
    # idle-window tolerance of the observation, yet it must never be treated as another unused window.
    pinned = int(OBSERVED_AT + 5) + WEEK + 1
    d = policy().plan(state, sample(reset=pinned, percent=0, at=OBSERVED_AT + 9), now=OBSERVED_AT + 9)
    assert not d.activate and d.state["reset_at"] == pinned
    boundary = d.state["reset_at"] + 10
    nxt = policy().plan(d.state, sample(reset=boundary + WEEK, percent=0, at=boundary), now=boundary)
    assert nxt.activate and nxt.cycle == pinned
