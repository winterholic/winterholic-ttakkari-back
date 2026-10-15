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
