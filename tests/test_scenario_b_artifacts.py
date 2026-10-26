"""Narrow test for Scenario B: Adapter artifact persistence and ResearchArtifact linkage.

Verifies that:
1. AdapterRunResult.artifacts containing temporary files are properly registered into the permanent artifact store via register_file.
2. ResearchArtifact rows are created linking study, experiment, run, and artifact_id.
3. Registered audio artifacts can be retrieved via existing /api/artifacts endpoints (GET metadata & GET content) with valid WAV headers.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import Artifact
from ttakkari.research import runner
from ttakkari.research.adapters.base import AdapterRunResult, MeasurementRecord
from ttakkari.research.models import (
    Configuration,
    Experiment,
    ResearchArtifact,
    Run,
    Study,
)


class DummyAudioProducerAdapter:
    id = "dummy_audio_producer"

    async def execute_run(self, config, *, run_id, scope, cancel_event=None):
        tmp_dir = tempfile.mkdtemp(prefix=f"test_art_{run_id}_")
        wav_file = Path(tmp_dir) / "output.wav"
        # Standard 44-byte minimal WAV header
        header = (
            b"RIFF\x2c\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00data\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        wav_file.write_bytes(header)

        return AdapterRunResult(
            status="succeeded",
            raw_output={
                "mode": "synthesize",
                "audio_chunks": [{"chunk_index": 0, "path": str(wav_file), "filename": "output.wav"}],
            },
            artifacts=[{
                "kind": "audio",
                "role": "audio_sample",
                "category": "research_tts_sample",
                "path": str(wav_file),
                "filename": "output.wav",
                "duration_seconds": 0.5,
                "sample_rate": 22050,
                "bytes": len(header),
            }],
            environment_snapshot={"engine": "test"},
            wall_seconds=0.1,
        )

    def to_measurements(self, adapter_res, config_params):
        return [
            MeasurementRecord(
                metric_key="rtf",
                metric_label="RTF",
                value=0.2,
                unit="ratio",
                direction="lower_is_better",
            )
        ]


class MockRegistry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get_adapter(self, adapter_id):
        return self.adapter


@pytest.mark.asyncio
async def test_runner_persists_adapter_artifacts_and_serves_via_artifact_api(client, auth):
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()

    # 1. Setup DB records
    async with sessionmaker()() as db:
        study = Study(id=study_id, title="Artifact Test Study", goal="Verify artifact linkage", scope="personal")
        db.add(study)
        await db.flush()

        exp = Experiment(
            id=exp_id,
            study_id=study_id,
            title="Artifact Test Experiment",
            hypothesis="Artifacts should be stored and linked",
            rationale="Narrow test",
            adapter="dummy_audio_producer",
            adapter_id="dummy_audio_producer",
            primary_metric="rtf",
        )
        db.add(exp)
        await db.flush()

        cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="default", config_hash="h0", parameters={})
        db.add(cfg)
        await db.flush()

        run = Run(
            id=run_id,
            study_id=study_id,
            experiment_id=exp_id,
            configuration_id=cfg_id,
            adapter_id="dummy_audio_producer",
            status="queued",
        )
        db.add(run)
        await db.commit()

    # 2. Execute via runner
    adapter = DummyAudioProducerAdapter()
    reg = MockRegistry(adapter)
    executed_run = await runner.execute_run_instance(run_id, reg)
    assert executed_run.status == "succeeded"

    # 3. Verify DB ResearchArtifact and Artifact records
    async with sessionmaker()() as db:
        res_arts = (await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == run_id))).all()
        assert len(res_arts) == 1
        ra = res_arts[0]
        assert ra.role == "audio_sample"
        assert ra.label == "output.wav"

        file_art = await db.get(Artifact, ra.artifact_id)
        assert file_art is not None
        assert file_art.kind == "audio"
        assert file_art.category == "research_tts_sample"
        assert file_art.source == "research_run"
        assert Path(file_art.stored_path).is_file()

    # 4. Verify retrieval through HTTP API
    r_meta = await client.get(f"/api/artifacts/{ra.artifact_id}", headers=auth)
    assert r_meta.status_code == 200, r_meta.text
    meta = r_meta.json()
    assert meta["id"] == str(ra.artifact_id)
    assert meta["filename"] == "output.wav"
    assert meta["mime_type"] == "audio/wav"

    r_content = await client.get(f"/api/artifacts/{ra.artifact_id}/content", headers=auth)
    assert r_content.status_code == 200
    assert r_content.headers["content-type"] == "audio/wav"
    data = r_content.content
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"


@pytest.mark.asyncio
async def test_runner_fails_when_declared_artifact_file_is_missing():
    """RED test: A succeeded run declaring an artifact path that does NOT exist on disk

    must fail the run explicitly, preserving error code and message, instead of silently skipping it.
    """
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()

    async with sessionmaker()() as db:
        study = Study(id=study_id, title="Missing Artifact Test", goal="Verify missing file failure", scope="personal")
        db.add(study)
        await db.flush()

        exp = Experiment(
            id=exp_id,
            study_id=study_id,
            title="Missing File Exp",
            hypothesis="Missing file must fail run",
            rationale="Narrow defect test",
            adapter="missing_file_adapter",
            adapter_id="missing_file_adapter",
            primary_metric="rtf",
        )
        db.add(exp)
        await db.flush()

        cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="default", config_hash="h1", parameters={})
        db.add(cfg)
        await db.flush()

        run = Run(
            id=run_id,
            study_id=study_id,
            experiment_id=exp_id,
            configuration_id=cfg_id,
            adapter_id="missing_file_adapter",
            status="queued",
        )
        db.add(run)
        await db.commit()

    class MissingFileAdapter:
        id = "missing_file_adapter"

        async def execute_run(self, config, *, run_id, scope, cancel_event=None):
            return AdapterRunResult(
                status="succeeded",
                raw_output={"artifacts": [{"path": "/tmp/nonexistent_audio_file_12345.wav"}]},
                artifacts=[{"path": "/tmp/nonexistent_audio_file_12345.wav", "role": "audio_sample"}],
                environment_snapshot={"engine": "test"},
                wall_seconds=0.1,
            )

        def to_measurements(self, adapter_res, config_params):
            return []

    reg = MockRegistry(MissingFileAdapter())
    executed_run = await runner.execute_run_instance(run_id, reg)

    # Must NOT be succeeded! Must explicitly fail with error_code
    assert executed_run.status == "failed"
    assert executed_run.error_code == "artifact_file_missing"
    assert "nonexistent_audio_file_12345.wav" in (executed_run.error_message or "")

    async with sessionmaker()() as db:
        res_arts = (await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == run_id))).all()
        assert len(res_arts) == 0


@pytest.mark.asyncio
async def test_runner_deduplicates_artifacts_by_normalized_path():
    """RED test: Multiple artifact entries pointing to the same normalized file path

    (even with differing metadata dictionaries between artifacts and raw_output)
    must only register 1 permanent Artifact and link 1 ResearchArtifact row.
    """
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()

    async with sessionmaker()() as db:
        study = Study(id=study_id, title="Duplicate Artifact Test", goal="Verify dedup", scope="personal")
        db.add(study)
        await db.flush()

        exp = Experiment(
            id=exp_id,
            study_id=study_id,
            title="Dedup Exp",
            hypothesis="Duplicates must be removed",
            rationale="Narrow defect test",
            adapter="dup_file_adapter",
            adapter_id="dup_file_adapter",
            primary_metric="rtf",
        )
        db.add(exp)
        await db.flush()

        cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="default", config_hash="h2", parameters={})
        db.add(cfg)
        await db.flush()

        run = Run(
            id=run_id,
            study_id=study_id,
            experiment_id=exp_id,
            configuration_id=cfg_id,
            adapter_id="dup_file_adapter",
            status="queued",
        )
        db.add(run)
        await db.commit()

    tmp_dir = tempfile.mkdtemp(prefix="test_dup_art_")
    real_file = Path(tmp_dir) / "real_output.wav"
    header = b"RIFF\x2c\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00data\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    real_file.write_bytes(header)

    class DuplicateFileAdapter:
        id = "dup_file_adapter"

        async def execute_run(self, config, *, run_id, scope, cancel_event=None):
            # Same file, but differing metadata dicts
            return AdapterRunResult(
                status="succeeded",
                raw_output={
                    "artifacts": [
                        {"path": str(real_file), "role": "sample_primary", "extra": 123},
                    ]
                },
                artifacts=[
                    {"path": str(real_file.resolve()), "role": "audio_sample", "label": "real_output.wav"},
                    {"path": str(real_file), "role": "audio_sample_dup"},
                ],
                environment_snapshot={"engine": "test"},
                wall_seconds=0.1,
            )

        def to_measurements(self, adapter_res, config_params):
            return []

    reg = MockRegistry(DuplicateFileAdapter())
    executed_run = await runner.execute_run_instance(run_id, reg)
    assert executed_run.status == "succeeded"

    async with sessionmaker()() as db:
        res_arts = (await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == run_id))).all()
        # Must deduplicate by normalized path: exactly 1 ResearchArtifact row!
        assert len(res_arts) == 1


@pytest.mark.asyncio
async def test_runner_preserves_legacy_artifact_id_and_deduplicates():
    """Verifies that an existing artifact_id is linked to ResearchArtifact,

    and duplicate mentions of the same artifact_id are deduplicated.
    """
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()

    # Pre-create an Artifact in DB
    existing_art_id = uuid.uuid4()
    async with sessionmaker()() as db:
        study = Study(id=study_id, title="Legacy Artifact Test", goal="Verify legacy ID", scope="personal")
        db.add(study)
        await db.flush()

        exp = Experiment(
            id=exp_id,
            study_id=study_id,
            title="Legacy Exp",
            hypothesis="Legacy ID must link",
            rationale="Narrow defect test",
            adapter="legacy_art_adapter",
            adapter_id="legacy_art_adapter",
            primary_metric="rtf",
        )
        db.add(exp)
        await db.flush()

        cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="default", config_hash="h3", parameters={})
        db.add(cfg)
        await db.flush()

        run = Run(
            id=run_id,
            study_id=study_id,
            experiment_id=exp_id,
            configuration_id=cfg_id,
            adapter_id="legacy_art_adapter",
            status="queued",
        )
        db.add(run)

        art_row = Artifact(
            id=existing_art_id,
            filename="legacy.wav",
            mime_type="audio/wav",
            kind="audio",
            category="research_tts_sample",
            source="legacy",
            origin="agent",
            area="agent",
            original_path="/tmp/legacy.wav",
            rel_path="legacy.wav",
            storage_mode="copy",
            stored_path="/tmp/legacy.wav",
            size_bytes=100,
            sha256="abc",
        )
        db.add(art_row)
        await db.commit()

    class LegacyArtAdapter:
        id = "legacy_art_adapter"

        async def execute_run(self, config, *, run_id, scope, cancel_event=None):
            return AdapterRunResult(
                status="succeeded",
                raw_output={"artifacts": [{"artifact_id": str(existing_art_id)}]},
                artifacts=[
                    {"artifact_id": str(existing_art_id), "role": "legacy_audio"},
                    {"artifact_id": str(existing_art_id), "role": "legacy_audio_dup"},
                ],
                environment_snapshot={"engine": "test"},
                wall_seconds=0.1,
            )

        def to_measurements(self, adapter_res, config_params):
            return []

    reg = MockRegistry(LegacyArtAdapter())
    executed_run = await runner.execute_run_instance(run_id, reg)
    assert executed_run.status == "succeeded"

    async with sessionmaker()() as db:
        res_arts = (await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == run_id))).all()
        assert len(res_arts) == 1
        assert res_arts[0].artifact_id == existing_art_id



async def _seed_run(adapter_id: str) -> uuid.UUID:
    study_id, exp_id, cfg_id, run_id = (uuid.uuid4() for _ in range(4))
    async with sessionmaker()() as db:
        db.add(Study(id=study_id, title="Artifact failure", goal="evidence kept", scope="personal"))
        await db.flush()
        db.add(
            Experiment(
                id=exp_id,
                study_id=study_id,
                title="Artifact failure",
                hypothesis="h",
                rationale="r",
                adapter=adapter_id,
                adapter_id=adapter_id,
                primary_metric="rtf",
            )
        )
        await db.flush()
        db.add(Configuration(id=cfg_id, experiment_id=exp_id, label="default", config_hash=str(cfg_id), parameters={}))
        await db.flush()
        db.add(
            Run(
                id=run_id,
                study_id=study_id,
                experiment_id=exp_id,
                configuration_id=cfg_id,
                adapter_id=adapter_id,
                status="queued",
            )
        )
        await db.commit()
    return run_id


def _declaring_adapter(artifacts: list[dict]):
    class _Adapter:
        id = "declaring_adapter"

        async def execute_run(self, config, *, run_id, scope, cancel_event=None):
            return AdapterRunResult(
                status="succeeded",
                raw_output={"transcript": "kept"},
                artifacts=artifacts,
                environment_snapshot={"engine": "test"},
                wall_seconds=0.1,
            )

        def to_measurements(self, adapter_res, config_params):
            return [MeasurementRecord(metric_key="rtf", metric_label="RTF", value=0.2, unit="ratio", direction="lower_is_better")]

    return _Adapter()


async def _assert_failed_with_evidence(run_id: uuid.UUID, error_code: str) -> None:
    from ttakkari.research.models import Measurement

    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        assert run.status == "failed"
        assert run.error_code == error_code
        assert run.finished_at is not None
        assert run.raw_output.get("transcript") == "kept"
        _assert_environment_with_owner(run.environment_snapshot)
        ms = (await db.scalars(select(Measurement).where(Measurement.run_id == run_id))).all()
        assert [m.metric_key for m in ms] == ["rtf"]
        links = (await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == run_id))).all()
        assert links == []


def _assert_environment_with_owner(snapshot: dict) -> None:
    from datetime import datetime

    from ttakkari.research.ownership import OWNER_KEY, owner_token, probe

    owner = snapshot.get(OWNER_KEY)
    assert isinstance(owner, dict)
    assert snapshot == {"engine": "test", OWNER_KEY: owner}
    assert set(owner) == {"contract", "token", "generation_id", "accepted_at"}
    assert owner["contract"] == 1 and owner["generation_id"] is None
    token = owner_token(snapshot)
    assert token is not None
    assert datetime.fromisoformat(owner["accepted_at"]).tzinfo is not None
    handle = probe(token)
    assert handle is not None  # the finished/failed Run released execution ownership
    handle.close()


@pytest.mark.parametrize("bad_field", ["engine", "owner"])
def test_environment_assertion_rejects_lost_evidence(bad_field):
    from ttakkari.research.ownership import OWNER_KEY, ExecutionOwner

    owner = ExecutionOwner.acquire()
    snapshot = owner.snapshot({"engine": "test"})
    owner.close()
    _assert_environment_with_owner(snapshot)
    if bad_field == "engine":
        snapshot["engine"] = "lost"
    else:
        snapshot.pop(OWNER_KEY)
    with pytest.raises(AssertionError):
        _assert_environment_with_owner(snapshot)


@pytest.mark.asyncio
async def test_missing_second_file_keeps_evidence_and_copies_nothing(monkeypatch):
    from ttakkari.artifacts import service as art_service

    calls: list[str] = []
    real_register = art_service.register_file

    async def spy(*args, **kwargs):
        calls.append("register")
        return await real_register(*args, **kwargs)

    monkeypatch.setattr(art_service, "register_file", spy)
    present = Path(tempfile.mkdtemp(prefix="art_present_")) / "first.wav"
    present.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    run_id = await _seed_run("declaring_adapter")
    adapter = _declaring_adapter([{"path": str(present)}, {"path": "/tmp/missing_second_artifact_98765.wav"}])

    await runner.execute_run_instance(run_id, MockRegistry(adapter))

    await _assert_failed_with_evidence(run_id, "artifact_file_missing")
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_legacy_artifact_id_fails_run_instead_of_hanging():
    run_id = await _seed_run("declaring_adapter")
    adapter = _declaring_adapter([{"artifact_id": str(uuid.uuid4())}])

    await runner.execute_run_instance(run_id, MockRegistry(adapter))

    await _assert_failed_with_evidence(run_id, "artifact_not_found")


@pytest.mark.asyncio
async def test_malformed_legacy_artifact_id_fails_with_evidence():
    run_id = await _seed_run("declaring_adapter")
    adapter = _declaring_adapter([{"artifact_id": "not-a-uuid"}])

    await runner.execute_run_instance(run_id, MockRegistry(adapter))

    await _assert_failed_with_evidence(run_id, "artifact_invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["after_register", "during_copy", "during_flush", "during_commit", "cancelled"])
async def test_artifact_registration_failure_keeps_measurements_and_removes_only_new_copies(
    monkeypatch, tmp_path, failure_stage,
):
    import asyncio

    from ttakkari.artifacts import service as art_service
    from ttakkari.config import get_settings
    from ttakkari.research.models import Measurement

    store = get_settings().artifact_store
    marker = store / str(uuid.uuid4()) / "existing.wav"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"existing unrelated artifact")
    before = set(store.rglob("*"))
    async with sessionmaker()() as db:
        before_ids = set(await db.scalars(select(Artifact.id)))

    sources = [tmp_path / f"{i}.wav" for i in range(2)]
    for path in sources:
        path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    real_register = art_service.register_file
    real_copy = art_service.shutil.copy2
    calls = 0

    def fail_copy(src, dest, *args, **kwargs):
        if Path(src) == sources[1]:
            Path(dest).write_bytes(b"partial copy")
            raise OSError("copy failed after partial write")
        return real_copy(src, dest, *args, **kwargs)

    async def interrupted_register(db, *args, **kwargs):
        nonlocal calls
        calls += 1
        art = await real_register(db, *args, **kwargs)
        if calls == 2:
            if failure_stage == "after_register":
                raise RuntimeError("registration failed after copying")
            if failure_stage == "cancelled":
                raise asyncio.CancelledError()
            if failure_stage in {"during_flush", "during_commit"}:
                # A real PostgreSQL FK failure poisons the artifact transaction.
                db.add(ResearchArtifact(
                    id=uuid.uuid4(), study_id=uuid.uuid4(), artifact_id=art.id,
                    role="invalid_fk", label="invalid FK injection",
                ))
                if failure_stage == "during_flush":
                    await db.flush()
        return art

    monkeypatch.setattr(art_service, "register_file", interrupted_register)
    if failure_stage == "during_copy":
        monkeypatch.setattr(art_service.shutil, "copy2", fail_copy)
    run_id = await _seed_run("declaring_adapter")
    await runner.execute_run_instance(
        run_id, MockRegistry(_declaring_adapter([{"path": str(p)} for p in sources])),
    )

    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        assert run.status == ("cancelled" if failure_stage == "cancelled" else "failed")
        assert run.finished_at is not None
        assert run.raw_output == {"transcript": "kept"}
        _assert_environment_with_owner(run.environment_snapshot)
        rows = list(await db.scalars(select(Measurement).where(Measurement.run_id == run_id)))
        assert [m.value for m in rows] == [0.2]
        assert list(await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == run_id))) == []
        assert set(await db.scalars(select(Artifact.id))) == before_ids
    assert set(store.rglob("*")) == before
    assert marker.read_bytes() == b"existing unrelated artifact"
    assert all(p.is_file() for p in sources)
