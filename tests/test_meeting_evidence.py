"""Conservative quote-to-time lookup; no models, fuzzy guesses, or extra LLM calls."""

import pytest

from ttakkari.meetings.evidence import locate_evidence


def utterance(uid, start, text, end=None, flags=None):
    return {
        "id": uid,
        "start": start,
        "end": start + 2 if end is None else end,
        "text": text,
        "flags": flags or [],
    }


def test_exact_quote_ignores_spacing_case_and_sentence_punctuation():
    rows = [utterance("a", 0, "API 배포는 목요일로 확정합니다.")]
    assert locate_evidence("“api 배포는  목요일로 확정합니다!”", rows) == (0, "a")


def test_quote_can_span_nearby_utterances_and_points_to_the_first():
    rows = [utterance("a", 2, "배포는 목요일로"), utterance("b", 4, "확정합니다.")]
    assert locate_evidence("배포는 목요일로 확정합니다.", rows) == (2, "a")


@pytest.mark.parametrize(
    "quote", [None, "", "네", "단어만 조금 유사한 다른 발언입니다."]
)
def test_missing_short_and_unmatched_quotes_have_no_location(quote):
    assert locate_evidence(
        quote, [utterance("a", 1, "이번 주는 모바일 작업을 진행합니다.")]
    ) == (None, None)


def test_same_quote_at_different_times_is_ambiguous():
    rows = [
        utterance("a", 1, "모바일 먼저 진행합니다."),
        utterance("b", 8, "모바일 먼저 진행합니다."),
    ]
    assert locate_evidence("모바일 먼저 진행합니다.", rows) == (None, None)


def test_quotes_do_not_bridge_long_silence_or_folded_repetition():
    rows = [utterance("a", 1, "배포는 목요일로"), utterance("b", 40, "확정합니다.")]
    assert locate_evidence("배포는 목요일로 확정합니다.", rows) == (None, None)
    assert locate_evidence(
        "배포는 목요일로 확정합니다.",
        [utterance("a", 1, "배포는 목요일로 확정합니다.", flags=["repetition_folded"])],
    ) == (None, None)


def test_rendered_time_and_speaker_prefix_are_not_quote_content():
    rows = [utterance("a", 12.5, "배포는 목요일로 확정합니다.")]
    assert locate_evidence(
        "[12.50–14.50] 화자 1 [검증 필요]: 배포는 목요일로 확정합니다.", rows
    ) == (12.5, "a")


def test_numeric_punctuation_cannot_change_the_matched_meaning():
    rows = [utterance("a", 2, "비용은 1.5억원으로 확정합니다.")]
    assert locate_evidence("비용은 15억원으로 확정합니다.", rows) == (None, None)
    assert locate_evidence("비용은 1.5억원으로 확정합니다.", rows) == (2, "a")
