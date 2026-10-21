"""한국어 TTS 정규화·문장 분할·청크 캐시 키 단위 테스트(순수 함수, DB 불필요)."""
from __future__ import annotations

import pytest

from ttakkari.tts.cache import chunk_key, relpath
from ttakkari.tts.normalize import (
    chunk_text,
    normalize_text,
    plain_text,
    sino,
    split_sentences,
)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, "영"), (1, "일"), (10, "십"), (11, "십일"), (100, "백"), (2026, "이천이십육"),
        (10_000, "만"), (11_000, "만 천"), (100_000, "십만"), (110_000, "십일만"),
        (100_000_000, "일억"), (123_456_789, "일억 이천삼백사십오만 육천칠백팔십구"),
    ],
)  # fmt: skip
def test_sino_numbers(number, expected):
    assert sino(number) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 한자어 음운 탈락: 십월이 아니라 시월, 육월이 아니라 유월
        ("2026-10-20", "이천이십육년 시월 이십일"),
        ("6월 6일", "유월 육일"),
        ("2026년 6월 1일", "이천이십육년 유월 일일"),
        ("10월 10일", "시월 십일"),
        ("2026.03.05", "이천이십육년 삼월 오일"),
        # 시간: 12시 이하는 고유어, 13시 이상(24시간제)은 한자어
        ("오후 3시 30분", "오후 세 시 삼십 분"),
        ("15:45", "십오 시 사십오 분"),
        ("9:00", "아홉 시"),
        # 금액·퍼센트·소수
        ("₩12,000", "만 이천 원"),
        ("12,000원", "만 이천 원"),
        ("$12.5", "십이점오 달러"),
        ("1,234,567원", "백이십삼만 사천오백육십칠 원"),
        ("45%", "사십오 퍼센트"),
        ("0.01%", "영점영일 퍼센트"),
        ("3.5배", "삼점오 배"),
        # 단위 앞 고유어 수와 한자어 수
        ("사과 3개", "사과 세 개"),
        ("학생 12명", "학생 열두 명"),
        ("20마리", "스무 마리"),
        ("100개", "백 개"),
        ("5분 3초", "오 분 삼 초"),
        ("3번째 줄", "세 번째 줄"),
        ("1번째", "첫 번째"),
        ("2시간", "두 시간"),
        ("3개월", "삼 개월"),
        ("3달", "세 달"),
        # 범위
        ("15℃ ~ 22℃", "십오 도에서 이십이 도"),
        ("10~20명", "열에서 스무 명"),
        ("30~40%", "삼십에서 사십 퍼센트"),
        ("1,000~2,000원", "천에서 이천 원"),
        # 식별자는 자릿수대로
        ("010-1234-5678", "공일공 일이삼사 오육칠팔"),
        ("v1.2.3", "일점이점삼"),
        ("00123", "공공일이삼"),
        ("-3도", "마이너스 삼 도"),
        # 영문 약어
        ("GPU/MPS 메모리", "지피유 엠피에스 메모리"),
        ("PR 리뷰와 CI/CD", "피알 리뷰와 씨아이 씨디"),
        ("R&D와 Q&A", "알앤디와 큐앤에이"),
        ("A/B 테스트", "에이 비 테스트"),
        ("16GB", "십육 기가바이트"),
        ("100Mbps", "백 메가비피에스"),
    ],
)  # fmt: skip
def test_korean_reading(raw, expected):
    assert normalize_text(raw) == expected


def test_measured_error_sources_become_korean_reading():
    # 실측에서 가장 많이 틀린 문장들. 원문 그대로 엔진에 주지 않고 읽는 말로 바꿔서 준다.
    assert normalize_text("2026년 10월 20일 현재 모델 처리 속도가 기존 대비 3.5배 향상되었습니다.") == (
        "이천이십육년 시월 이십일 현재 모델 처리 속도가 기존 대비 삼점오 배 향상되었습니다."
    )
    assert normalize_text("서버 GPU/MPS 메모리 사용량은 45%이며, 라이선스 비용은 ₩12,000입니다.") == (
        "서버 지피유 엠피에스 메모리 사용량은 사십오 퍼센트이며, 라이선스 비용은 만 이천 원입니다."
    )
    assert normalize_text("작업실 온도가 15℃ ~ 22℃ 범위로 유지되며 에러율은 0.01% 이하입니다.") == (
        "작업실 온도가 십오 도에서 이십이 도 범위로 유지되며 에러율은 영점영일 퍼센트 이하입니다."
    )


def test_unit_is_not_matched_inside_longer_word():
    # "3분기"의 '분'을 시간 단위로 읽지 않는다.
    assert normalize_text("3분기 실적") == "삼분기 실적"


def test_plain_text_strips_markdown_but_not_content():
    text = "# 제목\n- 항목 **굵게** 와 `code` 와 [링크](http://a.com)\n```py\nprint(1)\n```\n끝. 😀 <b>태그</b> https://x.io/a"
    out = normalize_text(text)
    assert "print" not in out and "http" not in out and "😀" not in out and "<b>" not in out
    assert "굵게" in out and "링크" in out and "제목" in out and "끝" in out
    assert "**" not in out and "`" not in out and "#" not in out


def test_table_rows_become_sentences():
    out = plain_text("| 이름 | 값 |\n|---|---|\n| a | 1 |")
    assert "---" not in out and "이름, 값." in out


def test_idempotent_and_nfc():
    raw = "2026-10-20 회의, 45% 달성, GPU 사용."
    once = normalize_text(raw)
    assert normalize_text(once) == once
    assert normalize_text("한글") == "한글"  # NFC 로 끝난다


def test_split_sentences_boundaries():
    assert split_sentences("첫째입니다. 둘째예요! 셋째인가요? 넷째\n다섯째") == [
        "첫째입니다.", "둘째예요!", "셋째인가요?", "넷째", "다섯째"
    ]  # fmt: skip
    assert split_sentences("...  ,,") == []


def test_chunks_respect_limit_and_prefer_clause_boundaries():
    text = "짧은 문장입니다. " + "긴 문장은 쉼표에서, 먼저 나뉘고, 그래도 길면 공백에서 나뉘어야 합니다. " * 3
    chunks = chunk_text(text, 30)
    assert all(len(c) <= 30 for c in chunks)
    assert chunks[0] == "짧은 문장입니다."
    assert any(c.endswith(",") for c in chunks)
    assert " ".join(chunks).replace(",", "").split() == normalize_text(text).replace(",", "").split()


def test_token_longer_than_limit_is_hard_split():
    chunks = chunk_text("가" * 50, 20)
    assert [len(c) for c in chunks] == [20, 20, 10]


def test_chunk_text_rejects_nonpositive_limit():
    with pytest.raises(ValueError, match="positive"):
        chunk_text("x", 0)


def _key(**override):
    base = {"engine": "supertonic", "model_revision": "r1", "voice": "M1", "speed": 1.0, "lang": "ko", "text": "안녕."}
    return chunk_key(**{**base, **override})


def test_chunk_key_is_deterministic_and_sensitive_to_every_field():
    assert _key() == _key()
    assert len(_key()) == 32 and relpath(_key()) == f"{_key()[:2]}/{_key()}.wav"
    variants = [
        {"engine": "other"}, {"model_revision": "r2"}, {"voice": "F1"}, {"speed": 1.05}, {"lang": "en"}, {"text": "안녕"},
    ]  # fmt: skip
    keys = {_key(**v) for v in variants}
    assert len(keys) == len(variants) and _key() not in keys


def test_chunk_key_field_boundaries_cannot_collide():
    # 이어붙이면 같아지는 서로 다른 필드 조합이 같은 키가 되면 안 된다.
    assert _key(voice="M1", lang="ko") != _key(voice="M", lang="1ko")
    assert _key(engine="ab", model_revision="c") != _key(engine="a", model_revision="bc")
