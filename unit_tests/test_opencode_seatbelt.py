"""Unit tests for Seatbelt isolation of OpenCode and Copilot CLI engines."""
from pathlib import Path

from ttakkari.security import seatbelt


def test_opencode_seatbelt_profile_blocks_ttakkari_and_secret_dirs():
    home = Path("/Users/winterholic")
    ttakkari_home = home / ".ttakkari"
    outbox = ttakkari_home / "data/outbox/00000000-0000-0000-0000-000000000001"
    workspace = Path("/Users/winterholic/development/workspace")
    account_home = ttakkari_home / "accounts/00000000-0000-0000-0000-000000000002/opencode"

    env = {
        "XDG_DATA_HOME": str(account_home / "data"),
        "XDG_CONFIG_HOME": str(account_home / "config"),
        "OPENCODE_CONFIG": "/tmp/ttakkari-opencode/sess/opencode.json",
    }

    policy = seatbelt.policy_for(
        home=home,
        ttakkari_home=ttakkari_home,
        outbox=outbox,
        write_roots=[workspace],
        env=env,
        engine="opencode",
        workspace=workspace,
    )

    rendered = seatbelt.render_profile(policy)

    # 1. DB network outbound is blocked
    for port in seatbelt.DB_PORTS:
        assert f'(remote ip "*:{port}")' in rendered

    # 2. .opencode and .ttakkari are protected
    assert ".opencode" in policy.secret_dirs
    assert ".copilot" in policy.secret_dirs
    assert f'(subpath "{home}/.opencode")' in rendered
    assert f'(subpath "{ttakkari_home}")' in rendered

    # 3. Only the designated account dirs are allowed
    assert account_home / "data" in policy.allowed
    assert account_home / "config" in policy.allowed
    assert Path("/tmp/ttakkari-opencode/sess/opencode.json").resolve() in policy.allowed_files

    # 4. Host ~/.opencode or other accounts are NOT allowed
    assert home / ".opencode" not in policy.allowed


def test_copilot_seatbelt_profile_isolation():
    home = Path("/Users/winterholic")
    ttakkari_home = home / ".ttakkari"
    outbox = ttakkari_home / "data/outbox/00000000-0000-0000-0000-000000000001"
    workspace = Path("/Users/winterholic/development/workspace")
    account_home = ttakkari_home / "accounts/00000000-0000-0000-0000-000000000003/copilot"

    # Isolated account
    env = {"COPILOT_HOME": str(account_home)}
    policy = seatbelt.policy_for(
        home=home,
        ttakkari_home=ttakkari_home,
        outbox=outbox,
        write_roots=[workspace],
        env=env,
        engine="copilot",
        workspace=workspace,
    )
    assert account_home in policy.allowed
    assert ".copilot" in policy.secret_dirs

    # System default account (no COPILOT_HOME)
    policy_sys = seatbelt.policy_for(
        home=home,
        ttakkari_home=ttakkari_home,
        outbox=outbox,
        write_roots=[workspace],
        env={},
        engine="copilot",
        workspace=workspace,
    )
    assert ".copilot" not in policy_sys.secret_dirs


def test_maybe_wrap_opencode_and_copilot():
    home = Path("/Users/winterholic")
    ttakkari_home = home / ".ttakkari"
    outbox = ttakkari_home / "data/outbox/00000000-0000-0000-0000-000000000001"
    workspace = Path("/Users/winterholic/development/workspace")

    cmd = ["opencode", "run", "hello"]
    wrapped = seatbelt.maybe_wrap(
        "opencode",
        cmd,
        enabled_engines=["opencode"],
        home=home,
        ttakkari_home=ttakkari_home,
        outbox=outbox,
        write_roots=[workspace],
        env={},
        workspace=workspace,
    )
    assert wrapped[0] == seatbelt.SANDBOX_EXEC
    assert wrapped[1] == "-p"
    assert wrapped[-3:] == cmd
