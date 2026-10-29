from types import SimpleNamespace

from ttakkari.deploy_test_receipt import (
    DB4_NODEIDS,
    database_name,
    format_receipt_lines,
    register_receipt_plugin,
    terminal_outcome,
)


def test_database_name_discards_dsn_credentials_and_endpoint():
    value = database_name("postgresql+asyncpg://test-user:secret@127.0.0.1:55432/ttakkari_gen_abc")
    assert value == "ttakkari_gen_abc"
    assert "secret" not in value
    assert "127.0.0.1" not in value


def test_terminal_outcome_preserves_skip_reason_and_xfail_state():
    report = SimpleNamespace(
        outcome="skipped", when="call", wasxfail="requires a local GPU", longrepr=None,
    )
    assert terminal_outcome([report]) == ("xfailed", "requires a local GPU")


def test_terminal_outcome_preserves_a_normal_skip_reason_and_reports_fixture_error():
    skipped = SimpleNamespace(
        outcome="skipped", when="setup", wasxfail=None, longrepr=("tests/test_db4.py", 12, "catalog fixture unavailable"),
    )
    failed = SimpleNamespace(outcome="failed", when="setup", longrepr="fixture error")
    assert terminal_outcome([skipped]) == ("skipped", "catalog fixture unavailable")
    assert terminal_outcome([failed]) == ("failed", "pytest setup phase failed")


def test_terminal_outcome_marks_selected_but_unreported_case_as_not_run():
    assert terminal_outcome([], interrupted=True) == (
        "not_run", "not reached before pytest stepwise interruption",
    )


def test_plugin_registers_only_for_full_revision_and_per_job_isolated_database():
    class _Plugins:
        def __init__(self):
            self.registered = []

        def register(self, plugin, name):
            self.registered.append((plugin, name))

    config = SimpleNamespace(pluginmanager=_Plugins(), option=SimpleNamespace(markexpr=""))
    valid = {
        "TTAKKARI_RELEASE_REVISION": "c" * 40,
        "TTAKKARI_TEST_DB": "postgresql+asyncpg://user:secret@127.0.0.1:55432/ttakkari_gen_abc",
    }
    assert register_receipt_plugin(config, valid) is True
    assert len(config.pluginmanager.registered) == 1
    assert config.pluginmanager.registered[0][1] == "deploy-db4-receipt"
    assert config.pluginmanager.registered[0][0].db == "ttakkari_gen_abc"

    unrelated = SimpleNamespace(pluginmanager=_Plugins(), option=SimpleNamespace(markexpr=""))
    assert register_receipt_plugin(unrelated, {**valid, "TTAKKARI_TEST_DB": "postgresql://localhost/local"}) is False
    assert unrelated.pluginmanager.registered == []


def test_plugin_reads_process_environment_when_no_environment_is_injected(monkeypatch):
    class _Plugins:
        def __init__(self):
            self.registered = []

        def register(self, plugin, name):
            self.registered.append((plugin, name))

    monkeypatch.setenv("TTAKKARI_RELEASE_REVISION", "d" * 40)
    monkeypatch.setenv(
        "TTAKKARI_TEST_DB",
        "postgresql+asyncpg://user:secret@127.0.0.1:55432/ttakkari_deploy_abc",
    )
    config = SimpleNamespace(pluginmanager=_Plugins(), option=SimpleNamespace(markexpr=""))

    assert register_receipt_plugin(config) is True
    assert len(config.pluginmanager.registered) == 1
    assert config.pluginmanager.registered[0][0].db == "ttakkari_deploy_abc"


def test_receipt_names_revision_argv_marker_skip_reason_db_and_each_named_case():
    outcomes = {nodeid: ("not_run", "not selected by this invocation") for nodeid in DB4_NODEIDS}
    outcomes[DB4_NODEIDS[0]] = ("skipped", "fixture prerequisite unavailable")
    lines = format_receipt_lines(
        revision="a" * 40,
        argv=["/release/.venv/bin/python", "-m", "pytest", "-q", "--timeout=120", "-m", "not security", "--sw"],
        marker="not security",
        skip_tests=False,
        skip_reason="pytest stage executed",
        database="ttakkari_gen_0123456789abcdef",
        outcomes=outcomes,
    )

    joined = "\n".join(lines)
    assert "revision=" + "a" * 40 in joined
    assert 'argv[0]="/release/.venv/bin/python"' in joined
    assert 'argv[1]="-m"' in joined and 'argv[2]="pytest"' in joined
    assert "marker=not security" in joined
    assert "skip_tests=false" in joined and "skip_reason=pytest stage executed" in joined
    assert "db=ttakkari_gen_0123456789abcdef" in joined
    assert all(nodeid in joined for nodeid in DB4_NODEIDS)
    assert "outcome=skipped" in joined and "reason=fixture prerequisite unavailable" in joined
    assert sum(line.startswith("db4 case ") for line in lines) == 4


def test_receipt_redacts_credential_like_values_from_argv_and_bounds_skip_reason():
    outcomes = {nodeid: ("skipped", "token=topsecret " + ("x" * 400)) for nodeid in DB4_NODEIDS}
    lines = format_receipt_lines(
        revision="b" * 40, argv=["pytest", "--password=topsecret"], marker="",
        skip_tests=False, skip_reason="pytest invoked", database="isolated",
        outcomes=outcomes,
    )
    joined = "\n".join(lines)
    assert "topsecret" not in joined
    assert all(len(line) <= 900 for line in lines)
    assert all(nodeid in joined for nodeid in DB4_NODEIDS)
