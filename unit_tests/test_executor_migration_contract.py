from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from ttakkari.generation_deployment import Releases


def test_executor_migration_follows_tts_and_uses_reserved_integration_revision():
    source = Path(__file__).resolve().parents[1]
    config = Config(str(source / "alembic.ini"))
    config.set_main_option("script_location", str(source / "alembic"))
    directory = ScriptDirectory.from_config(config)
    executor = directory.get_revision("0063_run_executor")
    assert executor.down_revision == "0061_local_tts"
    assert Releases.contract(source)["schema_revision"] == directory.get_current_head()


def test_executor_flag_is_a_declared_setting_and_preserves_local_voice_defaults():
    import pytest
    from pydantic import ValidationError

    from ttakkari.config import Settings

    local = Settings(_env_file=None)
    remote = Settings(_env_file=None, runtime_generation=True, run_executor_mode="remote")
    assert local.run_executor_mode == "local"
    assert remote.run_executor_mode == "remote"
    assert (remote.local_tts_enabled, remote.local_tts_engine, remote.local_tts_default_voice) == (
        local.local_tts_enabled, local.local_tts_engine, local.local_tts_default_voice)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, run_executor_mode="remote")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runtime_generation=True, run_executor_mode="unknown")


def test_executor_command_manifest_pins_interpreter_revision_and_disabled_activation():
    import json

    source = Path(__file__).resolve().parents[1]
    manifest = json.loads((source / "deploy/run-executor-manifest.json").read_text())
    assert manifest["activation"] == "disabled"
    assert manifest["command"] == [
        "{release}/.venv/bin/python", "-m", "ttakkari.cli", "executor", "--activate"]
    assert manifest["readiness"] == ["executor_generation", "revision", "pid", "process_started_at", "heartbeat_at"]
