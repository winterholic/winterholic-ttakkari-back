"""Conservative offline enhancement: intervals, evidence, and immutable originals."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ttakkari.meetings.local_stt import core


def turn(a, b, speaker):
    return {"start": a, "end": b, "speaker_id": speaker}


def test_overlap_sweep_unions_duplicate_labels_and_preserves_changes():
    turns = [turn(0, 4, "A"), turn(1, 3, "A"), turn(2, 5, "B"), turn(3, 4, "C")]
    assert core.overlap_regions(turns, duration=6) == [
        {"start": 2.0, "end": 3.0, "speaker_ids": ["A", "B"]},
        {"start": 3.0, "end": 4.0, "speaker_ids": ["A", "B", "C"]},
    ]
    assert core.overlap_regions([turn(0, 1, "A"), turn(1, 2, "B")], duration=2) == []


@pytest.mark.parametrize(
    "turns", [[turn(0, float("nan"), "A")], [turn(0, 3, "A")], [turn(1, 1, "A")]]
)
def test_invalid_diarization_is_rejected(turns):
    with pytest.raises(ValueError):
        core.overlap_regions(turns, duration=2)


def test_diarization_aligns_words_and_marks_overlap_and_uncovered():
    segments = core.normalize_segments(
        [
            {
                "start": 0,
                "end": 4,
                "text": "하나 둘",
                "words": [
                    {"start": 0, "end": 1, "word": "하나"},
                    {"start": 3, "end": 4, "word": "둘"},
                ],
            },
            {"start": 5, "end": 6, "text": "셋"},
        ],
        duration=6,
    )
    before = deepcopy(segments)
    result = core.annotate_diarization(
        segments, [turn(0, 2.5, "A"), turn(2, 4, "B")], duration=6
    )
    assert segments == before
    assert result["speaker_count"] == 2
    assert result["identity_status"] == "not_run"
    assert result["segments"][0]["overlap"] is True
    assert "overlapping_speech" in result["segments"][0]["flags"]
    assert [w["speaker_id"] for w in result["segments"][0]["words"]] == ["A", "B"]
    assert "speaker_uncovered" in result["segments"][1]["flags"]
    assert result["speaker_identities"] == {}


def test_overlap_comparison_handles_permutation_and_requires_all_sources():
    compare = core.compare_overlap_candidate
    refs = ["안녕하세요", "오늘 회의"]
    good = compare("안녕", ["오늘 회의", "안녕하세요"], references=refs)
    assert good["adopt"] is True and good["permutation"] == [1, 0]
    assert (
        compare("안녕하세요 오늘 회의", ["오늘 회의", "안녕하세요"], references=refs)[
            "adopt"
        ]
        is False
    )
    assert (
        compare("안녕", ["안녕하세요"], references=refs)["reason"]
        == "source_count_mismatch"
    )
    assert (
        compare("안녕", ["안녕하세요", "오늘 회의"], references=None)["reason"]
        == "no_reference"
    )


def test_only_overlaps_are_separated_with_budgets_and_no_reference_no_adoption():
    from ttakkari.meetings.local_stt.enhance import reprocess_overlaps

    calls = []

    def separate(a, b):
        calls.append((a, b))
        return ["audio-0", "audio-1"]

    def transcribe(audio, offset):
        return {"text": str(audio), "segments": [], "raw_segments": []}

    regions = [
        {"start": 2, "end": 4, "speaker_ids": ["A", "B"]},
        {"start": 6, "end": 9, "speaker_ids": ["A", "B", "C"]},
        {"start": 10, "end": 13, "speaker_ids": ["A", "B"]},
    ]
    result = reprocess_overlaps(
        regions,
        separate=separate,
        recognize=transcribe,
        original=lambda a, b: "원문",
        max_region_seconds=2,
        max_total_seconds=2,
    )
    assert calls == [(2, 4)]
    assert result[0]["decision"]["adopt"] is False
    assert result[0]["selected_tracks"] is None
    assert result[1]["status"] == "unsupported_speaker_count"
    assert result[2]["status"] == "duration_budget_exceeded"


def test_failed_separation_is_explicit_and_keeps_next_candidate():
    from ttakkari.meetings.local_stt.enhance import reprocess_overlaps

    def fail(a, b):
        raise RuntimeError("private path must not be exposed")

    regions = [{"start": 0, "end": 1, "speaker_ids": ["A", "B"]}]
    result = reprocess_overlaps(regions, separate=fail, recognize=None, original=None)
    assert result == [
        {
            "start": 0,
            "end": 1,
            "speaker_ids": ["A", "B"],
            "status": "failed",
            "failure_type": "RuntimeError",
        }
    ]


def test_diarizer_requires_local_model_before_import(tmp_path):
    from ttakkari.meetings.local_stt.diarization import diarize

    with pytest.raises(ValueError, match="local"):
        diarize([], model_path=tmp_path / "missing", offset=0, device="cpu")


def test_pyannote_output_keeps_regular_and_exclusive_separate():
    from ttakkari.meetings.local_stt.diarization import serialize_output

    class Annotation:
        def __init__(self, entries):
            self.entries = entries

        def itertracks(self, yield_label):
            for a, b, label in self.entries:
                yield SimpleNamespace(start=a, end=b), 0, label

    output = SimpleNamespace(
        speaker_diarization=Annotation([(0, 2, "S0"), (1, 3, "S1")]),
        exclusive_speaker_diarization=Annotation([(0, 1, "S0"), (1, 3, "S1")]),
    )
    result = serialize_output(output, offset=10, duration=3)
    assert result["turns"][0] == turn(10, 12, "S0")
    assert result["exclusive_turns"][0] == turn(10, 11, "S0")
    assert len(core.overlap_regions(result["turns"], duration=13)) == 1
    assert core.overlap_regions(result["exclusive_turns"], duration=13) == []


def test_separator_requires_complete_local_weights(tmp_path):
    from ttakkari.meetings.local_stt.separation import load_separator

    with pytest.raises(ValueError, match="local"):
        load_separator(tmp_path, device="cpu")


def test_optional_enhancement_preserves_stt_and_reports_unavailable(monkeypatch):
    from ttakkari.meetings.local_stt import enhance

    result = {
        "segments": core.normalize_segments(
            [{"start": 0, "end": 1, "text": "원문"}], duration=2
        ),
        "raw_segments": [{"text": "원문"}],
        "text": "원문",
    }
    original = deepcopy(result)
    report = enhance.enhance_result(
        result,
        audio=None,
        offset=0,
        request={"diarization_model_path": "/missing/local-model"},
        recognize=None,
    )
    assert result == original
    assert report["diarization_status"] == "unavailable"
    assert report["overlap_detection_status"] == "not_run"
    assert report["segments"] == result["segments"]


def test_optional_enhancement_runs_regular_turns_and_preserves_text(
    monkeypatch, tmp_path
):
    from ttakkari.meetings.local_stt import diarization, enhance

    monkeypatch.setattr(
        diarization,
        "diarize",
        lambda *a, **kw: {
            "turns": [turn(0, 2, "A"), turn(1, 3, "B")],
            "exclusive_turns": [turn(0, 1, "A"), turn(1, 3, "B")],
        },
    )
    segments = core.normalize_segments(
        [{"start": 0, "end": 3, "text": "원문"}], duration=3
    )
    original = {
        "segments": segments,
        "raw_segments": [{"text": "원문"}],
        "text": "원문",
        "duration": 3,
    }
    (tmp_path / "config.yaml").write_text("test")
    report = enhance.enhance_result(
        original,
        audio=None,
        offset=0,
        request={"diarization_model_path": str(tmp_path)},
        recognize=None,
    )
    assert report["diarization_status"] == "succeeded"
    assert report["overlap_detection_status"] == "succeeded"
    assert report["speaker_count"] == 2
    assert report["overlap_regions"] == [
        {"start": 1.0, "end": 2.0, "speaker_ids": ["A", "B"]}
    ]
    assert original["segments"][0]["overlap"] is None
    assert report["text"] == "원문" and report["segments"][0]["original_text"] == "원문"
    assert report["separation_status"] == "not_configured"


def test_text_correction_is_explicit_non_cascading_and_preserves_original():
    source = core.normalize_segments(
        [{"start": 0, "end": 1, "text": "따까리 티티까리"}], duration=1
    )
    result = core.correct_terms(source, {"티티까리": "따까리", "따까리": "Ttakkari"})
    assert source[0]["corrected_text"] is None
    assert result[0]["original_text"] == source[0]["original_text"]
    assert result[0]["corrected_text"] == "Ttakkari 따까리"
    assert result[0]["verification_status"] == "unverified"
    assert "term_correction_candidate" in result[0]["flags"]


def test_korean_phrase_and_cross_segment_repetition_flags_do_not_delete():
    source = core.normalize_segments(
        [
            {"start": 0, "end": 1, "text": "감사합니다감사합니다감사합니다감사합니다"},
            {"start": 2, "end": 3, "text": "마무리하겠습니다"},
            {"start": 4, "end": 5, "text": "마무리하겠습니다"},
            {"start": 6, "end": 7, "text": "마무리하겠습니다"},
        ],
        duration=7,
    )
    out = core.flag_repetitions(source)
    assert "phrase_repetition" in out[0]["flags"]
    assert all("consecutive_duplicate" in s["flags"] for s in out[1:])
    assert [s["original_text"] for s in out] == [s["original_text"] for s in source]
    assert source[0]["flags"] == []


def test_sequential_speaker_change_is_marked_even_with_majority():
    s = core.normalize_segments([{"start": 0, "end": 4, "text": "교대"}], duration=4)
    out = core.annotate_diarization(s, [turn(0, 3, "A"), turn(3, 4, "B")], duration=4)
    assert out["segments"][0]["speaker_id"] == "A"
    assert "speaker_change" in out["segments"][0]["flags"]


def test_no_empty_separated_track_can_be_adopted():
    assert (
        core.compare_overlap_candidate(
            "", ["오늘 회의", ""], references=["오늘 회의", "예"]
        )["adopt"]
        is False
    )


def test_partial_diarization_failure_is_explicit(monkeypatch, tmp_path, caplog):
    from ttakkari.meetings.local_stt import diarization, enhance

    (tmp_path / "config.yaml").write_text("test")

    def fail(*args, **kwargs):
        raise RuntimeError("no device private path")

    monkeypatch.setattr(diarization, "diarize", fail)
    original = {"text": "원문", "segments": []}
    result = enhance.enhance_result(
        original,
        audio=None,
        offset=0,
        request={"diarization_model_path": str(tmp_path)},
        recognize=None,
    )
    assert result["diarization_status"] == "failed"
    assert result["diarization_failure_type"] == "RuntimeError"
    assert result["text"] == "원문" and result["overlap_detection_status"] == "not_run"
    assert "Diarization failed: RuntimeError" in caplog.text


def test_enhancement_separates_only_detected_region_and_keeps_original(
    monkeypatch, tmp_path
):
    import numpy as np

    from ttakkari.meetings.local_stt import diarization, enhance, separation

    (tmp_path / "config.yaml").write_text("test")
    monkeypatch.setattr(
        diarization,
        "diarize",
        lambda *a, **kw: {
            "turns": [turn(10, 12, "A"), turn(11, 13, "B")],
            "exclusive_turns": [],
        },
    )
    clips = []

    def loader(*args, **kwargs):
        def separate(audio):
            clips.append(audio.copy())
            return [audio, audio]

        return separate

    monkeypatch.setattr(separation, "load_separator", loader)
    original = {
        "text": "원文",
        "duration": 20,
        "segments": core.normalize_segments(
            [{"start": 0, "end": 3, "text": "원文"}], duration=3, offset=10
        ),
    }
    audio = np.arange(3 * 16000, dtype=np.float32)
    result = enhance.enhance_result(
        original,
        audio=audio,
        offset=10,
        request={
            "diarization_model_path": str(tmp_path),
            "separation_model_path": str(tmp_path),
        },
        recognize=lambda a, offset: {
            "text": "후보",
            "segments": [],
            "raw_segments": [],
        },
    )
    assert np.array_equal(clips[0], audio[16000:32000])
    assert result["text"] == "원文" and result["segments"][0]["original_text"] == "원文"
    assert result["overlap_candidates"][0]["selected_tracks"] is None
    assert original["segments"][0]["overlap"] is None


def test_all_skipped_regions_are_not_success(monkeypatch, tmp_path):
    from ttakkari.meetings.local_stt import diarization, enhance, separation

    (tmp_path / "config.yaml").write_text("test")
    monkeypatch.setattr(
        diarization,
        "diarize",
        lambda *a, **kw: {
            "turns": [turn(0, 20, "A"), turn(0, 20, "B")],
            "exclusive_turns": [],
        },
    )
    # Loading does not imply separation ran; no eligible region may look successful.
    monkeypatch.setattr(separation, "load_separator", lambda *a, **kw: lambda audio: [])
    result = enhance.enhance_result(
        {"duration": 20, "segments": [], "text": ""},
        audio=None,
        offset=0,
        request={
            "diarization_model_path": str(tmp_path),
            "separation_model_path": str(tmp_path),
        },
        recognize=None,
    )
    assert result["separation_status"] == "skipped"
    assert result["overlap_candidates"][0]["status"] == "duration_budget_exceeded"
