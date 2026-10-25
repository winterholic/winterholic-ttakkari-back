"""Unit tests for account auth failure detection and fallback logic."""
import uuid

from ttakkari.runs.profiles import Profile, is_auth_error


def test_auth_failure_removes_account_from_remaining_and_allows_fallback():
    account1 = Profile(id=uuid.uuid4(), name="copilot-1", engine="copilot")
    account2 = Profile(id=uuid.uuid4(), name="copilot-2", engine="copilot")
    remaining = [account1, account2]

    # Account 1 fails with an auth error
    error_output = "Error: unauthorized. Token expired or not authenticated. Please run opencode auth login"
    assert is_auth_error(error_output) is True

    # Simulation of manager fallback handling
    auth_failed = is_auth_error(error_output)
    if auth_failed:
        remaining = [p for p in remaining if p.id != account1.id]

    assert len(remaining) == 1
    assert remaining[0].id == account2.id
    assert account1 not in remaining
