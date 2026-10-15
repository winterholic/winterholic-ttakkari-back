"""Local STT invariants: evidence is preserved; missing confidence stays missing."""

import math

import pytest


def core():
    from ttakkari.meetings.local_stt import core

    return core


def test_preserves_original_metrics_and_offsets():
    result = core().normalize_segments(
        [
            {
                "start": 0.1,
                "end": 1.2,
                "text": "  원본입니다. ",
                "avg_logprob": -0.8,
                "no_speech_prob": 0.05,
                "compression_ratio": 1.1,
                "words": [
                    {
                        "start": 0.2,
                        "end": 1.1,
                        "word": " 원본입니다.",
                        "probability": 0.7,
                    }
                ],
            }
        ],
        offset=12,
        duration=20,
    )
    assert result[0]["original_text"] == "  원본입니다. "
    assert result[0]["corrected_text"] is None
    assert result[0]["start"] == 12.1
    assert result[0]["words"][0]["start"] == 12.2
    assert result[0]["avg_logprob"] == -0.8
    assert result[0]["speaker_id"] is None
    assert result[0]["overlap"] is None
    assert "confidence" not in result[0]


def test_missing_metrics_are_not_fabricated():
    s = core().normalize_segments([{"start": 0, "end": 1, "text": "말"}], duration=1)[0]
    assert s["avg_logprob"] is None and s["no_speech_prob"] is None
    assert s["verification_status"] == "unverified"


@pytest.mark.parametrize(
    "start,end", [(-1, 1), (2, 1), (0, math.inf), (math.nan, 1), (0, 4)]
)
def test_rejects_invalid_timestamps(start, end):
    with pytest.raises(ValueError):
        core().normalize_segments(
            [{"start": start, "end": end, "text": "말"}], duration=3
        )


def test_flags_suspicion_without_deleting_speech():
    s = core().normalize_segments(
        [
            {
                "start": 0,
                "end": 3,
                "text": "감사합니다 " * 12,
                "avg_logprob": -1.2,
                "no_speech_prob": 0.9,
                "compression_ratio": 3,
            }
        ],
        duration=3,
    )[0]
    assert {"low_logprob", "possible_silence", "high_compression", "repetition"} <= set(
        s["flags"]
    )
    assert s["original_text"] == "감사합니다 " * 12


def test_prompt_bounds_deduplicates_and_never_treats_names_as_identity():
    prompt = core().term_prompt(["따까리", "따까리", "  김민수\n", "x" * 300])
    assert prompt.count("따까리") == 1 and "김민수" in prompt
    assert "\n" not in prompt and len(prompt) <= 400


def test_speaker_tie_and_overlap_are_explicit():
    turns = [
        {"start": 0, "end": 2, "speaker_id": "SPEAKER_00"},
        {"start": 1, "end": 3, "speaker_id": "SPEAKER_01"},
    ]
    s = core().normalize_segments([{"start": 1, "end": 2, "text": "둘"}], duration=3)
    aligned = core().align_speakers(s, turns)
    assert aligned[0]["speaker_id"] is None
    assert aligned[0]["overlap"] is True
    assert "speaker_ambiguous" in aligned[0]["flags"]


def test_two_sequential_speakers_are_not_overlap():
    s = core().normalize_segments([{"start": 0, "end": 2, "text": "교대"}], duration=2)
    a = core().align_speakers(
        s,
        [
            {"start": 0, "end": 1, "speaker_id": "A"},
            {"start": 1, "end": 2, "speaker_id": "B"},
        ],
    )[0]
    assert a["overlap"] is False and a["speaker_id"] is None


def test_reference_metrics_known_edits_and_empty_reference():
    m = core().error_rates("가 나 다", "가 라")
    assert m["wer"] == pytest.approx(2 / 3)
    assert m["cer"] == pytest.approx(2 / 3)
    assert core().error_rates("", "환각")["cer"] is None
    assert core().error_rates("가", "가 가 가")["cer"] == 2


def test_reprocess_never_automatically_adopts_without_reference():
    assert core().compare_candidate("원문", "수정", reference=None)["adopt"] is False
    assert (
        core().compare_candidate("가 라", "가 나", reference="가 나")["adopt"] is True
    )
    assert (
        core().compare_candidate("가 나", "가 라", reference="가 나")["adopt"] is False
    )


def test_candidate_equal_reference_score_does_not_win():
    assert (
        core().compare_candidate("가 다", "가 라", reference="가 나")["adopt"] is False
    )
