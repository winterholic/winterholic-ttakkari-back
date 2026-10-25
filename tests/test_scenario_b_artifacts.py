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
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.main import app
from ttakkari.models import Artifact
from ttakkari.research import runner
from ttakkari.research.adapters.base import AdapterRunResult, MeasurementRecord
from ttakkari.research.models import Configuration, Experiment, ResearchArtifact, Run, Study


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
