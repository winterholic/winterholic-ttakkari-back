"""Quality recovery must change consumer input while retaining acoustic evidence."""

import pytest

from ttakkari.meetings.local_stt import core, worker


def segments(texts):
    return core.normalize_segments(
        [{"start": i * 2, "end": (i + 1) * 2, "text": t} for i, t in enumerate(texts)],
        duration=len(texts) * 2,
    )


def test_default_profile_is_bounded_vad_and_temperature_fallback():
    from ttakkari.config import Settings

    options = worker.decode_options(Settings(_env_file=None).local_stt_profile, [])
    assert options["vad_filter"] is True
    assert options["condition_on_previous_text"] is False
    assert list(options["temperature"]) == [0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert options["compression_ratio_threshold"] == 2.4
    assert options["log_prob_threshold"] == -1
    assert options["no_speech_threshold"] == 0.6
    assert options["vad_parameters"]["max_speech_duration_s"] <= 25


def test_punctuation_variants_of_repeated_sentences_are_detected():
    s = core.flag_repetitions(
        segments(["같은 문장입니다?", "같은 문장입니다.", "같은 문장입니다?"])
    )
    assert all("consecutive_duplicate" in x["flags"] for x in s)


def test_recovery_uses_short_audio_windows_and_preserves_originals():
    import numpy as np

    from ttakkari.meetings.local_stt.quality import recover_quality

    original = segments(["똑같은 문장입니다."] * 6)
    calls = []

    def recognize(audio, offset):
        calls.append((len(audio) / 16000, offset))
        raw = [{"start": 0, "end": len(audio) / 16000, "text": "실제 다른 발언입니다."}]
        return {
            "segments": core.normalize_segments(
                raw, duration=len(audio) / 16000, offset=offset
            ),
            "raw_segments": raw,
        }

    out = recover_quality(
        original, audio=np.zeros(12 * 16000), offset=0, recognize=recognize
    )
    assert calls and max(x[0] for x in calls) <= 8
    assert out["segments"][0]["original_text"] == original[0]["original_text"]
    assert "실제 다른 발언" in "".join(
        s["original_text"] for s in out["presentation_segments"]
    )
    assert out["quality_retries"][0]["raw_segments"]
    assert original[0]["verification_status"] == "unverified"


def test_persistent_loop_is_folded_and_marked_for_review():
    from ttakkari.meetings.local_stt.quality import recover_quality

    original = segments(["의미가 있는 문장입니다."] * 4)

    def recognize(audio, offset):
        return {
            "segments": core.normalize_segments(
                [{"start": 0, "end": 8, "text": "의미가 있는 문장입니다." * 10}],
                duration=8,
                offset=offset,
            ),
            "raw_segments": [{"text": "raw retry"}],
        }

    out = recover_quality(
        original, audio=[0] * (8 * 16000), offset=0, recognize=recognize
    )
    text = "".join(s["original_text"] for s in out["presentation_segments"])
    assert text.count("의미가 있는 문장입니다.") == 1
    assert all(
        s["verification_status"] == "needs_review" for s in out["presentation_segments"]
    )
    assert out["quality"]["folded_characters"] > 0
    assert out["segments"][0]["original_text"] == original[0]["original_text"]


def test_legitimate_two_repeats_are_kept():
    from ttakkari.meetings.local_stt.quality import recover_quality

    original = segments(["그렇습니다.", "그렇습니다."])
    out = recover_quality(original, audio=[], offset=0, recognize=None)
    assert [s["original_text"] for s in out["presentation_segments"]] == [
        "그렇습니다."
    ] * 2


def test_empty_retry_window_preserves_its_original_speech():
    import numpy as np

    from ttakkari.meetings.local_stt.quality import recover_quality

    original = segments(["길게 반복되는 원본 문장입니다."] * 6)

    def recognize(audio, offset):
        raw = [] if offset == 0 else [{"start": 0, "end": 4, "text": "후반 발언"}]
        return {
            "segments": core.normalize_segments(raw, duration=4, offset=offset),
            "raw_segments": raw,
        }

    result = recover_quality(
        original, audio=np.zeros(12 * 16000), offset=0, recognize=recognize
    )
    rows = result["presentation_segments"]
    assert any(s["start"] == 0 and "원본" in s["original_text"] for s in rows)
    assert any(s["start"] >= 8 and "후반" in s["original_text"] for s in rows)


def test_retry_failure_keeps_raw_speech_and_records_explicit_failure():
    from ttakkari.meetings.local_stt.quality import recover_quality

    original = segments(["계속되는 반복 문장입니다."] * 4)

    def recognize(audio, offset):
        raise RuntimeError("decoder unavailable")

    result = recover_quality(
        original, audio=[0] * (8 * 16000), offset=0, recognize=recognize
    )
    assert result["segments"][0]["original_text"] == original[0]["original_text"]
    assert result["presentation_segments"][0]["verification_status"] == "needs_review"
    assert result["quality_retries"][0]["status"] == "failed_preserved"
    assert result["quality_retries"][0]["failure_type"] == "RuntimeError"


def test_speaker_turns_split_on_word_boundaries_and_keep_unknown_overlap():
    from ttakkari.meetings.local_stt.utterances import build_utterances

    s = segments(["첫째 둘째"])
    s[0]["words"] = [
        {"start": 0, "end": 1, "word": "첫째 "},
        {"start": 1, "end": 2, "word": "둘째"},
    ]
    turns = [
        {"start": 0, "end": 1, "speaker_id": "A"},
        {"start": 1, "end": 2, "speaker_id": "B"},
    ]
    out = build_utterances(s, turns, overlap_known=False)
    assert [(x["speaker_label"], x["text"]) for x in out] == [
        ("화자 1", "첫째"),
        ("화자 2", "둘째"),
    ]
    assert all(x["overlap"] is None for x in out)
    assert all(x["verification_status"] == "unverified" for x in out)


def test_ambiguous_word_is_not_assigned_to_both_speakers():
    from ttakkari.meetings.local_stt.utterances import build_utterances

    s = segments(["겹침"])
    turns = [
        {"start": 0, "end": 2, "speaker_id": "A"},
        {"start": 0, "end": 2, "speaker_id": "B"},
    ]
    out = build_utterances(s, turns, overlap_known=True)
    assert len(out) == 1 and out[0]["speaker_id"] is None and out[0]["overlap"] is True
    assert out[0]["verification_status"] == "needs_review"


def test_embedding_clusters_use_acoustic_vectors_and_first_seen_labels():
    from ttakkari.meetings.local_stt.embedding_diarization import cluster_embeddings

    labels = cluster_embeddings([[1.0, 0.0], [0.99, 0.01], [0.0, 1.0], [0.01, 0.99]])
    assert labels[0] == labels[1] and labels[2] == labels[3] and labels[0] != labels[2]
    assert cluster_embeddings([[1.0, 0.0]]) == [0]


def test_missing_public_embedding_is_explicit(tmp_path):
    from ttakkari.meetings.local_stt.embedding_diarization import diarize_embeddings

    with pytest.raises(ValueError, match="missing"):
        diarize_embeddings([], model_path=tmp_path, offset=0)


def test_vad_chunks_decode_independently_and_restore_absolute_word_times():
    from ttakkari.meetings.local_stt.worker import decode_vad_chunks

    calls = []

    def decode(samples):
        calls.append(len(samples))
        return [
            {
                "start": 0,
                "end": 1,
                "text": " 발언",
                "words": [{"start": 0.1, "end": 0.9, "word": "발언"}],
            }
        ]

    raw, seconds = decode_vad_chunks(
        list(range(6 * 16000)),
        [{"start": 16000, "end": 32000}, {"start": 4 * 16000, "end": 5 * 16000}],
        decode,
    )
    assert calls == [16000, 16000] and seconds == 2
    assert [s["start"] for s in raw] == [1, 4]
    assert raw[1]["words"][0]["start"] == pytest.approx(4.1)


def test_folded_metrics_measure_text_not_stale_warning_flags():
    from ttakkari.meetings.local_stt.quality import repetition_metrics

    s = segments(["한 번만 나오는 문장."])
    s[0]["flags"] = ["phrase_repetition", "repetition_folded"]
    assert repetition_metrics(s)["repeated_segments"] == 0


def test_unknown_overlap_is_preserved_through_public_embedding_adapter(monkeypatch):
    from ttakkari.meetings.local_stt import embedding_diarization, enhance

    monkeypatch.setattr(
        embedding_diarization,
        "diarize_embeddings",
        lambda *a, **kw: {
            "turns": [{"start": 0, "end": 2, "speaker_id": "A"}],
            "exclusive_turns": [],
            "overlap_supported": False,
        },
    )
    out = enhance.enhance_result(
        {"duration": 2, "segments": segments(["문장"])},
        audio=[],
        offset=0,
        request={"speaker_embedding_model_path": "/model"},
        recognize=None,
    )
    assert out["diarization_status"] == "succeeded"
    assert out["overlap_detection_status"] == "unsupported"
    assert out["segments"][0]["overlap"] is None


def test_identical_embeddings_estimate_one_cluster_without_fragmenting():
    from ttakkari.meetings.local_stt.embedding_diarization import cluster_embeddings

    assert cluster_embeddings([[1.0, 0.0]] * 8) == [0] * 8


def test_spectral_count_does_not_turn_short_window_variation_into_extra_people():
    from ttakkari.meetings.local_stt.embedding_diarization import cluster_embeddings

    labels = cluster_embeddings(
        [
            [1, 0, 0],
            [0.8, 0.6, 0],
            [0.8, -0.6, 0],
            [0, 0, 1],
            [0, 0.6, 0.8],
            [0, -0.6, 0.8],
        ]
    )
    assert labels[:3] == [labels[0]] * 3
    assert labels[3:] == [labels[3]] * 3
    assert labels[0] != labels[3]


def test_adjacent_vad_spans_pack_without_exceeding_window_or_crossing_long_silence():
    from ttakkari.meetings.local_stt.worker import pack_vad_chunks

    spans = [
        {"start": 0, "end": 2 * 16000},
        {"start": 3 * 16000, "end": 6 * 16000},
        {"start": 20 * 16000, "end": 23 * 16000},
        {"start": 24 * 16000, "end": 27 * 16000},
    ]
    assert pack_vad_chunks(spans) == [
        {"start": 0, "end": 6 * 16000},
        {"start": 20 * 16000, "end": 27 * 16000},
    ]
    assert spans[0]["end"] == 2 * 16000
    assert all(
        c["end"] - c["start"] <= 25 * 16000
        for c in pack_vad_chunks(
            [{"start": 0, "end": 24 * 16000}, {"start": 24 * 16000, "end": 30 * 16000}]
        )
    )


@pytest.mark.parametrize("legacy", ["baseline", "improved"])
def test_legacy_operational_profile_cannot_reenable_unsafe_decoding(legacy):
    from ttakkari.config import Settings

    assert (
        Settings(_env_file=None, local_stt_profile=legacy).local_stt_profile == "robust"
    )


def test_repetition_metrics_distinguish_span_from_nonoverlapping_audio_duration():
    from ttakkari.meetings.local_stt.quality import repetition_metrics

    s = segments(["연속된 반복 발언입니다."] * 3)
    s[1]["start"] = 3
    s[1]["end"] = 4
    s[2]["start"] = 5
    s[2]["end"] = 6
    m = repetition_metrics(s)
    assert m["repetition_runs"] == 1 and m["longest_run_segments"] == 3
    assert m["longest_run_span_seconds"] == 6 and m["repeated_seconds"] == 4


def test_retry_failure_is_evidence_and_preserves_original_speech():
    from ttakkari.meetings.local_stt.quality import recover_quality

    original = segments(["반복된 발언 원문입니다."] * 3)

    def fail(*args):
        raise RuntimeError("private engine details")

    result = recover_quality(original, audio=[0] * 6 * 16000, offset=0, recognize=fail)
    assert result["segments"][0]["original_text"] == original[0]["original_text"]
    assert result["presentation_segments"][0]["verification_status"] == "needs_review"
    assert result["quality_retries"][0]["status"] == "failed_preserved"
    assert result["quality_retries"][0]["failure_type"] == "RuntimeError"
    assert "private engine details" not in str(result)
