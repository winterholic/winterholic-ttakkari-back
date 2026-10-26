"""Opt-in actual CPU model acceptance; explicitly invoke this file, never default CI.

TTAKKARI_RESEARCH_LIVE_MEMORY=1 and RESEARCH_MEMORY_LIVE_REPORT (current Run
outbox path) are required. Uses conftest's isolated PostgreSQL and temporary HOME.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_research_runner_lifecycle import seed_runs

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.model_process import (
    WorkerMemoryLimitError,
    worker_memory_limit,
    worker_observations,
)
from ttakkari.research import runner
from ttakkari.research.adapters.registry import AdapterRegistry
from ttakkari.research.adapters.tts_synthesis import TtsSynthesisAdapter
from ttakkari.research.models import Configuration, Experiment, Run

pytestmark = pytest.mark.skipif(os.getenv('TTAKKARI_RESEARCH_LIVE_MEMORY') != '1', reason='actual ML opt-in')
ROOT = Path('/Users/winterholic/development/projects/winterholic-ttakkari/back')


def runtime_settings(monkeypatch):
    s = get_settings()
    values = {
        'local_tts_python': ROOT / '.tts-runtime/supertonic/bin/python',
        'local_tts_model_path': ROOT / '.tts-runtime/supertonic/assets',
        'local_stt_python': ROOT / '.stt-runtime/venv/bin/python',
        'local_stt_model_path': ROOT / '.stt-runtime/faster-whisper-large-v3',
        'local_tts_cpu_threads': 2, 'local_stt_cpu_threads': 2,
        'local_tts_timeout_seconds': 90, 'local_stt_timeout_seconds': 120,
    }
    for key, value in values.items():
        monkeypatch.setattr(s, key, value)
    assert all(values[k].exists() for k in values if isinstance(values[k], Path))
    adapter = TtsSynthesisAdapter()
    assert adapter.check_environment(scope='personal').ready
    registry = AdapterRegistry()
    registry.register(adapter)
    return s, registry


async def actual_run(registry, memory):
    exp_id, ids = await seed_runs()
    async with sessionmaker()() as db:
        exp = await db.get(Experiment, exp_id)
        exp.adapter_id = 'tts_synthesis'
        exp.budget = {'max_memory_mb': memory, 'max_total_seconds': 240}
        row = await db.get(Run, ids[0])
        row.adapter_id = 'tts_synthesis'
        (await db.get(Configuration, row.configuration_id)).parameters = {
            'mode': 'synthesize_and_stt', 'texts': ['안녕하세요. 메모리 감시를 확인합니다.'],
            'voice': 'M1',
        }
        await db.commit()
    await runner.run_experiment_batch(exp_id, ids, registry)
    return ids[0]


async def fresh_receipt(client, auth, rid):
    response = await client.get(f'/api/research/runs/{rid}', headers=auth)
    assert response.status_code == 200
    body = response.json()
    async with sessionmaker()() as db:
        row = await db.get(Run, rid)
        assert body['raw_output'] == row.raw_output
        assert body['environment_snapshot'] == row.environment_snapshot
    return body


async def test_actual_models_memory_guard(client, auth, monkeypatch):
    _s, registry = runtime_settings(monkeypatch)
    rid = await actual_run(registry, 8192)
    normal = await fresh_receipt(client, auth, rid)
    report = {'environment': 'lead Codex command sandbox; actual CPU Supertonic/Whisper; isolated Run PostgreSQL',
              'normal': normal}
    path = Path(os.environ['RESEARCH_MEMORY_LIVE_REPORT'])
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    validate_normal_receipt(normal)


def validate_normal_receipt(normal):
    assert normal['status'] == 'succeeded', (normal.get('error_code'), normal.get('raw_output'))
    env = normal['environment_snapshot']
    assert env['research_memory_guard']['samples'] > 0
    assert env['research_memory_guard']['error_code'] is None
    workers = env['research_model_workers']
    assert workers['state'] == 'released'
    assert {w['module'] for w in workers['workers'].values()} == {
        'ttakkari.tts.worker_supertonic', 'ttakkari.meetings.local_stt.worker'}
    assert all(w['state'] == 'exited' and w['returncode'] == 0 for w in workers['workers'].values())
    assert len(normal['raw_output']['stt_eval']) == normal['raw_output']['chunk_count'] > 0



async def test_actual_low_model_thresholds(client, auth, monkeypatch):
    s, registry = runtime_settings(monkeypatch)
    path = Path(os.environ['RESEARCH_MEMORY_LIVE_REPORT'])
    report = json.loads(path.read_text(encoding='utf-8'))
    normal = report['normal']
    assert normal['status'] == 'succeeded'
    assert len(normal['raw_output']['stt_eval']) == normal['raw_output']['chunk_count'] > 0
    low_id = await actual_run(registry, 128)
    low = await fresh_receipt(client, auth, low_id)
    report['low_tts'] = low
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assert low['status'] == 'cancelled'
    assert low['error_code'] == 'memory_budget_exceeded'
    assert low['environment_snapshot']['research_model_workers']['state'] == 'released'

    # The combined Run's low threshold stops at TTS. Exercise STT separately
    # with the exact production runner and the already generated audio, no TTS stub.
    from ttakkari.meetings.local_stt import runner as stt_runner
    audio = Path(normal['raw_output']['audio_chunks'][0]['path'])
    assert audio.is_file()
    observations = []

    async def observe(value):
        observations.append(value)

    with (
        worker_memory_limit(512) as guard,
        worker_observations(observe),
        pytest.raises(WorkerMemoryLimitError, match='memory_budget_exceeded'),
    ):
        await stt_runner.run_worker(audio, {'language': 'ko'}, s)
    report['low_stt_direct'] = {'guard': guard.snapshot(), 'observations': observations,
                                'persisted_run': False}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assert len(observations) == 2
    assert observations[-1]['state'] == 'exited'
    assert observations[-1]['returncode'] != 0
    print('actual CPU models: normal combined Run + low TTS Run fresh HTTP/PG; direct low STT kill/wait')


def test_captured_normal_receipt_and_negative_controls():
    from copy import deepcopy

    normal = json.loads(Path(os.environ['RESEARCH_MEMORY_LIVE_REPORT']).read_text(encoding='utf-8'))['normal']
    validate_normal_receipt(normal)
    corrupt = deepcopy(normal)
    child = next(iter(corrupt['environment_snapshot']['research_model_workers']['workers'].values()))
    child['returncode'] = 17
    with pytest.raises(AssertionError):
        validate_normal_receipt(corrupt)
    corrupt = deepcopy(normal)
    corrupt['raw_output']['stt_eval'] = []
    with pytest.raises(AssertionError):
        validate_normal_receipt(corrupt)
