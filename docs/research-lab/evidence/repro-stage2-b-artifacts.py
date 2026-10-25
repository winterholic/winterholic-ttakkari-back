"""Isolated reproduction for Stage 2 Scenario B artifact linkage defect.

Demonstrates that runner.execute_run_instance does not consume AdapterRunResult.artifacts
and expects adapter_res.raw_output['artifacts'] with pre-existing 'artifact_id',
resulting in 0 ResearchArtifact rows and unpersisted audio files.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from sqlalchemy import select
from ttakkari.db import init_engine, sessionmaker, dispose_engine
from ttakkari.models import Artifact
from ttakkari.research.models import Study, Experiment, Configuration, Run, ResearchArtifact
from ttakkari.research.runner import execute_run_instance
from ttakkari.research.adapters.base import AdapterRunResult, MeasurementRecord


class DummyAudioAdapter:
    """Adapter that emits audio artifacts in AdapterRunResult.artifacts, matching tts_synthesis."""
    id = "dummy_audio_adapter"

    async def execute_run(self, config, *, run_id, scope, cancel_event=None):
        tmp_dir = tempfile.mkdtemp(prefix=f"repro_audio_{run_id}_")
        wav_file = Path(tmp_dir) / "sample.wav"
        wav_file.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        
        return AdapterRunResult(
            status="succeeded",
            raw_output={
                "mode": "synthesize",
                "audio_chunks": [{"chunk_index": 0, "path": str(wav_file), "filename": "sample.wav"}],
            },
            artifacts=[{
                "kind": "audio",
                "category": "research_tts_sample",
                "path": str(wav_file),
                "filename": "sample.wav",
                "duration_seconds": 1.0,
                "sample_rate": 24000,
                "bytes": wav_file.stat().st_size,
            }],
            environment_snapshot={"engine": "mock"},
            wall_seconds=0.5,
        )

    def to_measurements(self, adapter_res, config_params):
        return [MeasurementRecord(metric_key="rtf", metric_label="RTF", value=0.15, unit="ratio", direction="lower_is_better")]


class MockRegistry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get_adapter(self, adapter_id):
        return self.adapter


async def reproduce():
    url = os.environ.get("TTAKKARI_TEST_DB")
    if not url or "ttakkari_test" not in url:
        print("SKIP: TTAKKARI_TEST_DB is not set or not ttakkari_test")
        return

    init_engine(url)
    sm = sessionmaker()
    
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()

    async with sm() as db:
        study = Study(id=study_id, title="Repro Artifact Linkage", goal="B 결함 재현", scope="personal")
        db.add(study)
        await db.flush()

        exp = Experiment(
            id=exp_id,
            study_id=study_id,
            title="B Repro Exp",
            hypothesis="B 결함 가설",
            rationale="재현 실험",
            adapter="dummy_audio_adapter",
            adapter_id="dummy_audio_adapter",
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
            adapter_id="dummy_audio_adapter",
            status="queued",
        )
        db.add(run)
        await db.commit()

    # Execute run with DummyAudioAdapter via MockRegistry
    adapter = DummyAudioAdapter()
    reg = MockRegistry(adapter)
    await execute_run_instance(run_id, reg)

    async with sm() as db:
        res_arts = (await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == run_id))).all()
        art_ids = [ra.artifact_id for ra in res_arts]
        file_arts = (await db.scalars(select(Artifact).where(Artifact.id.in_(art_ids)))).all() if art_ids else []
        executed_run = await db.get(Run, run_id)
        
        print("reproduction_result:")
        print("  run_status:", executed_run.status)
        print("  research_artifacts_count:", len(res_arts))
        print("  file_artifacts_count:", len(file_arts))
        if len(file_arts) > 0:
            print("  stored_file_exists:", Path(file_arts[0].stored_path).is_file())
            print("  stored_file_path:", file_arts[0].stored_path)
        print("  defect_reproduced:", len(res_arts) == 0 and len(file_arts) == 0)

    await dispose_engine()

if __name__ == "__main__":
    asyncio.run(reproduce())
