import pytest

from ttakkari.runs.profiles import is_auth_error


@pytest.mark.parametrize("text", [
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "Failed to authenticate: [REDACTED] session expired and could not be refreshed",
    "authentication failed",
])
def test_expired_login_is_auth_error(text):
    assert is_auth_error(text)


def test_permission_error_is_not_auth_error():
    assert not is_auth_error("permission denied: session expired lock file")


def test_auth_failed_marker_round_trip():
    import uuid

    from ttakkari.runs import profiles

    aid = uuid.uuid4()
    profiles.mark_auth_failed(aid)
    profiles.cool_down(aid, 600)
    assert profiles.auth_failed(aid) and profiles.cooldown_until(aid)
    profiles.clear_auth_failed(aid)
    assert not profiles.auth_failed(aid) and not profiles.cooldown_until(aid)
