from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from ttakkari.generation_deployment import Releases


def test_deployed_library_and_v2_revisions_converge_without_rewriting_their_parents():
    source = Path(__file__).resolve().parents[1]
    config = Config(str(source / "alembic.ini"))
    config.set_main_option("script_location", str(source / "alembic"))
    directory = ScriptDirectory.from_config(config)
    assert len(directory.get_heads()) == 1, "deployed main and V2 must converge before release"
    ancestry = {revision.revision for revision in directory.walk_revisions()}
    assert {"0068_library_intent", "0070_background_consumer_queue"} <= ancestry
    # Both branches have already been published. Preserve their historical identity.
    assert directory.get_revision("0068_library_intent").down_revision == "0067_collector_outbox_retry"
    assert directory.get_revision("0068_tts_payload_chunks").down_revision == "0067_collector_outbox_retry"


def test_deployed_library_and_previous_v2_schemas_are_declared_upgrade_sources():
    source = Path(__file__).resolve().parents[1]
    contract = Releases.contract(source)
    assert {"0068_library_intent", "0070_background_consumer_queue"} <= set(contract["online_migration_from"])


def test_deployed_library_upgrade_and_rollback_sql_do_not_reapply_its_data_migration(monkeypatch):
    from io import StringIO
    from types import SimpleNamespace

    from alembic import command
    from ttakkari import config as settings

    source = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(settings, "get_settings", lambda: SimpleNamespace(
        database_url="postgresql+asyncpg://localhost/ttakkari_test"))

    def sql(direction, revision):
        output = StringIO()
        config = Config(output_buffer=output)
        config.set_main_option("script_location", str(source / "alembic"))
        getattr(command, direction)(config, revision, sql=True)
        return output.getvalue()

    upgrade = sql("upgrade", "0068_library_intent:head")
    rollback = sql("downgrade", "0071_library_platform_v2_merge:0068_library_intent")
    for column in ("chunks_json", "queue_state", "claimed_at", "spec", "index_requested_at", "index_started_at"):
        assert f"ADD COLUMN {column}" in upgrade
        # Downgrading a merge to one parent preserves the other branch. It is
        # not a physical rollback of V2; keep this limitation visible.
        assert f"DROP COLUMN {column}" not in rollback
    assert "VALUES ('0070_background_consumer_queue')" in rollback
    assert "version_num='0068_library_intent'" in rollback
    for plan in (upgrade, rollback):
        assert "UPDATE artifacts" not in plan
        assert "ALTER TABLE artifacts" not in plan
        assert "artifact_area_backup_0068" not in plan


def test_executor_migration_follows_tts_and_uses_reserved_integration_revision():
    source = Path(__file__).resolve().parents[1]
    config = Config(str(source / "alembic.ini"))
    config.set_main_option("script_location", str(source / "alembic"))
    directory = ScriptDirectory.from_config(config)
    executor = directory.get_revision("0064_run_executor")
    assert executor.down_revision == "0063_collector_stage_items"
    from ttakkari.models import RuntimeProcessGroup
    assert {"run_id", "generation_id", "lease_token", "attempt", "pid", "process_started_at", "pgid"} <= set(
        RuntimeProcessGroup.__table__.columns.keys())
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


def test_executor_manifest_digest_is_required_and_checked_against_content(tmp_path):
    import hashlib
    import json

    import pytest

    source = tmp_path
    (source / "deploy").mkdir()
    manifest_path = source / "deploy/run-executor-manifest.json"
    manifest = {
        "schema": 1, "role": "run-executor", "activation": "disabled",
        "command": ["{release}/.venv/bin/python", "-m", "ttakkari.cli", "executor", "--activate"],
        "readiness": ["executor_generation", "revision", "pid", "process_started_at", "heartbeat_at"],
        "environment": {"TTAKKARI_RUNTIME_GENERATION": "true", "TTAKKARI_RELEASE_REVISION": "{revision}",
                        "TTAKKARI_DEPLOY_STRATEGY": "generations", "TTAKKARI_DEPLOY_SOURCE_ROOT": "{source_root}"},
        "activation_gates": ["release_marker_matches_revision"],
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    manifest_path.write_bytes(manifest_bytes)
    contract = {"protocol": 1, "schema_revision": "0061_local_tts", "online_migration_from": [],
                "run_executor": {"protocol": 2, "activation": "disabled",
                                 "manifest": "deploy/run-executor-manifest.json",
                                 "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}}
    contract_path = source / "deploy/generation-contract.json"
    contract_path.write_text(json.dumps(contract))

    assert Releases.contract(source)["run_executor"]["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()

    contract["run_executor"].pop("manifest_sha256")
    contract_path.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match="manifest digest"):
        Releases.contract(source)

    contract["run_executor"]["manifest_sha256"] = "0" * 64
    contract_path.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match="manifest digest"):
        Releases.contract(source)

    contract["run_executor"]["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    contract_path.write_text(json.dumps(contract))
    manifest_path.unlink()
    with pytest.raises(FileNotFoundError):
        Releases.contract(source)
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.write_bytes(manifest_bytes + b" ")
    with pytest.raises(ValueError, match="manifest digest"):
        Releases.contract(source)


def test_release_archive_binds_manifest_digest_to_archived_git_revision(tmp_path):
    import hashlib
    import json
    import subprocess

    import pytest

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    deploy = source / "deploy"
    deploy.mkdir()
    manifest_bytes = (Path(__file__).resolve().parents[1] / "deploy/run-executor-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    contract = json.loads((Path(__file__).resolve().parents[1] / "deploy/generation-contract.json").read_text())
    contract["run_executor"]["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    contract["run_executor"]["activation"] = manifest["activation"]
    (deploy / "run-executor-manifest.json").write_bytes(manifest_bytes)
    (deploy / "generation-contract.json").write_text(json.dumps(contract))
    subprocess.run(["git", "-C", str(source), "add", "deploy"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "release"], check=True)
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()

    release = Releases(source, tmp_path / "releases").archive(revision)
    assert (release / "deploy/run-executor-manifest.json").read_bytes() == manifest_bytes
    Releases.contract(release)

    # A later checkout edit is outside this immutable artifact; a new archive with stale digest fails.
    (source / "deploy/run-executor-manifest.json").write_bytes(manifest_bytes + b"\n")
    assert Releases(source, tmp_path / "releases").archive(revision) == release
    subprocess.run(["git", "-C", str(source), "add", "deploy/run-executor-manifest.json"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "tampered manifest"], check=True)
    tampered_revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    with pytest.raises(ValueError, match="manifest digest"):
        Releases(source, tmp_path / "releases").archive(tampered_revision)

    (release / "deploy/run-executor-manifest.json").write_bytes(manifest_bytes + b" ")
    with pytest.raises(ValueError, match="manifest digest"):
        Releases(source, tmp_path / "releases").archive(revision)
