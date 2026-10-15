import pytest

from ttakkari.meetings.local_stt import benchmark


def test_aggregate_uses_micro_average_and_does_not_invent_meeting_metrics():
    rows = [
        {"reference": "가 나", "hypothesis": "가 라"},
        {"reference": "다", "hypothesis": "다"},
    ]
    result = benchmark.aggregate(rows)
    assert result["cer"] == pytest.approx(1 / 3)
    assert result["wer"] == pytest.approx(1 / 3)
    assert result["der"] is None
    assert result["hallucination_rate"] is None


def test_empty_benchmark_fails_instead_of_reporting_zero_error():
    with pytest.raises(ValueError):
        benchmark.aggregate([])


def test_unlabelled_samples_do_not_count_as_zero_error():
    result = benchmark.aggregate([{"reference": None, "hypothesis": "말"}])
    assert result["cer"] is None and result["labelled_samples"] == 0


async def test_standalone_benchmark_keeps_requested_historical_profile(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from ttakkari.meetings.local_stt import runner

    calls = []

    async def record(**kw):
        calls.append(kw["request"])
        return {}

    monkeypatch.setattr(runner, "run_json_worker", record)
    await benchmark.run_worker(
        tmp_path / "audio.wav",
        {"profile": "baseline"},
        SimpleNamespace(
            local_stt_python=tmp_path / "python",
            local_stt_model_path=tmp_path / "model",
            local_stt_timeout_seconds=1,
            local_stt_max_audio_seconds=10,
            local_stt_cpu_threads=1,
        ),
    )
    assert calls[0]["profile"] == "baseline"
