import pytest


@pytest.mark.parametrize("owner_dead,groups,expected", [
    (None, [], "unknown_owner"),
    (True, [], "missing_process_group_evidence"),
    (False, [("absent", True)], "owner_alive"),
    (True, [("unknown", True)], "unknown_process_group"),
    (True, [("present", True)], "process_group_live"),
    (True, [("absent", True)], "safe_to_release"),
])
def test_reconcile_disposition_fails_closed_on_missing_or_unknown_evidence(owner_dead, groups, expected):
    from ttakkari.executor_reconcile import disposition

    assert disposition(owner_dead=owner_dead, groups=groups) == expected


def test_reconcile_command_exposes_dry_run_and_apply_without_database_access():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", "ttakkari.cli", "executor-reconcile", "--help"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout
    assert "--apply" in result.stdout
