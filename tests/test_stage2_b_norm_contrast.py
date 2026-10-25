"""Negative contrast and regression tests for Scenario B text normalization & chunking pipeline.

Verifies:
1. Negative contrast test: Baseline (raw/disabled normalization) preserves raw abbreviations/numbers
   all the way to provider.synthesize input chunks, and differs from Candidate chunks.
2. Chunking limit & split regression with and without normalization.
3. Cancellation regression during synthesis.
4. Text override and STT speaker-prefix removal regression.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from ttakkari.research.adapters.tts_synthesis import TtsSynthesisAdapter
from ttakkari.tts.normalize import chunk_text
from ttakkari.tts.providers import ProviderAvailability, ProviderState, TtsChunkResult, TtsResult
from ttakkari.tts.supertonic import SupertonicProvider


def _fake_tts_result(count: int = 1) -> TtsResult:
    return TtsResult(
        chunks=[
            TtsChunkResult(
                index=i,
                path=f"/tmp/test_chunk_{i}.wav",
                duration_seconds=2.0,
                sample_rate=24000,
                bytes=48000,
            )
            for i in range(count)
        ],
        metrics={
            "synth_seconds": 0.4,
            "audio_seconds": 2.0,
            "load_seconds": 0.1,
            "peak_rss_bytes": 50000000,
        },
    )


@pytest.mark.asyncio
async def test_negative_contrast_baseline_raw_preserved_to_provider_chunks() -> None:
    """RED reproduction test:

    Baseline with convert_numbers=False and english_acronym_mode='raw' must pass raw
    abbreviations/numbers to provider.synthesize. Candidate with convert_numbers=True
    must pass normalized text. Baseline and Candidate chunks must NOT be equal.
    """
    adapter = TtsSynthesisAdapter()
    sample_text = "HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다."

    baseline_config = {
        "texts": [sample_text],
        "mode": "synthesize",
        "convert_numbers": False,
        "english_acronym_mode": "raw",
        "read_latin": False,
        "custom_pronunciation_dict": {},
    }

    candidate_config = {
        "texts": [sample_text],
        "mode": "synthesize",
        "convert_numbers": True,
        "english_acronym_mode": "korean_alphabet",
        "read_latin": True,
        "custom_pronunciation_dict": {"JSON": "제이슨", "HTTP": "에이치티티피"},
    }

    captured_chunks: dict[str, list[str]] = {}

    async def fake_synthesize_baseline(chunks, **kwargs):
        captured_chunks["baseline"] = list(chunks)
        return _fake_tts_result(len(chunks))

    async def fake_synthesize_candidate(chunks, **kwargs):
        captured_chunks["candidate"] = list(chunks)
        return _fake_tts_result(len(chunks))

    ready = ProviderAvailability(state=ProviderState.ready, reason="Ready")

    # 1. Run Baseline
    with patch.object(SupertonicProvider, "availability", return_value=ready), \
         patch.object(SupertonicProvider, "synthesize", side_effect=fake_synthesize_baseline):
        res_baseline = await adapter.execute_run(uuid4(), baseline_config, scope="personal")
        assert res_baseline.status == "succeeded"

    # 2. Run Candidate
    with patch.object(SupertonicProvider, "availability", return_value=ready), \
         patch.object(SupertonicProvider, "synthesize", side_effect=fake_synthesize_candidate):
        res_candidate = await adapter.execute_run(uuid4(), candidate_config, scope="personal")
        assert res_candidate.status == "succeeded"

    b_chunks = captured_chunks["baseline"]
    c_chunks = captured_chunks["candidate"]

    # Candidate should have normalized Korean pronunciation
    assert any("에이치티티피" in c for c in c_chunks), f"Candidate chunks missing acronym reading: {c_chunks}"
    assert any("제이슨" in c for c in c_chunks), f"Candidate chunks missing custom dict reading: {c_chunks}"

    # Baseline MUST preserve raw text into provider.synthesize chunks
    assert any("HTTP 200" in c for c in b_chunks), f"Baseline chunks did not preserve raw text: {b_chunks}"
    assert any("JSON" in c for c in b_chunks), f"Baseline chunks did not preserve raw acronym: {b_chunks}"

    # Negative contrast: Baseline and Candidate chunks must NOT be equal
    assert b_chunks != c_chunks, f"Baseline and Candidate chunks must differ, but both were: {b_chunks}"


def test_chunk_text_supports_normalize_false_and_preserves_limits() -> None:
    """chunk_text should support normalize=False to avoid re-normalizing preprocessed text."""
    long_raw = "HTTP 200 OK! " * 20  # long text to force splitting
    # With normalize=True (default), numbers/acronyms are normalized
    chunks_norm = chunk_text(long_raw, max_chars=60)
    for c in chunks_norm:
        assert len(c) <= 60
        assert "에이치티티피" in c

    # With normalize=False, raw text is kept intact while still respecting max_chars limit
    chunks_raw = chunk_text(long_raw, max_chars=60, normalize=False)
    for c in chunks_raw:
        assert len(c) <= 60
        assert "HTTP" in c
        assert "에이치티티피" not in c


@pytest.mark.asyncio
async def test_tts_cancellation_during_synthesis_returns_cancelled() -> None:
    """Cancellation via cancel_event should cleanly cancel synthesis."""
    adapter = TtsSynthesisAdapter()
    config = {
        "texts": ["취소 테스트 긴 문장입니다."],
        "mode": "synthesize",
    }
    cancel_event = asyncio.Event()

    async def slow_synthesize(chunks, **kwargs):
        cancel_event.set()
        await asyncio.sleep(2.0)
        return _fake_tts_result(1)

    ready = ProviderAvailability(state=ProviderState.ready, reason="Ready")
    with patch.object(SupertonicProvider, "availability", return_value=ready), \
         patch.object(SupertonicProvider, "synthesize", side_effect=slow_synthesize):
        res = await adapter.execute_run(uuid4(), config, scope="personal", cancel_event=cancel_event)
        assert res.status == "cancelled"
        assert res.raw_output.get("reason") == "cancelled_during_synthesis"


@pytest.mark.asyncio
async def test_stt_speaker_removal_and_text_override() -> None:
    """STT recognition result strips timestamp and speaker tag prefixes, and custom texts override presets."""
    from ttakkari.config import get_settings
    from ttakkari.meetings.local_stt import runner as stt_runner

    adapter = TtsSynthesisAdapter()
    custom_texts = ["오버라이드된 단일 테스트 문장입니다."]
    config = {
        "preset_id": "ko_mixed_pronunciation_v1",  # should be ignored due to texts
        "texts": custom_texts,
        "mode": "synthesize_and_stt",
        "voice": "M1",
    }

    async def fake_stt_worker(audio_path, params, settings, *, production=True):
        return {"text": "[00:01.20] 화자 1: 오버라이드된 단일 테스트 문장입니다."}

    settings = get_settings().model_copy(update={"local_stt_python": sys.executable})
    ready = ProviderAvailability(state=ProviderState.ready, reason="Ready")

    with patch("ttakkari.config.get_settings", return_value=settings), \
         patch.object(SupertonicProvider, "availability", return_value=ready), \
         patch.object(SupertonicProvider, "synthesize", return_value=_fake_tts_result(1)), \
         patch.object(stt_runner, "run_worker", side_effect=fake_stt_worker):
        res = await adapter.execute_run(uuid4(), config, scope="personal")
        assert res.status == "succeeded"
        stt_eval = res.raw_output.get("stt_eval", [])
        assert len(stt_eval) == 1
        # Speaker prefix [00:01.20] 화자 1: stripped
        assert stt_eval[0]["recognized_text"] == "오버라이드된 단일 테스트 문장입니다."
        assert stt_eval[0]["raw_reference"] == custom_texts[0]
