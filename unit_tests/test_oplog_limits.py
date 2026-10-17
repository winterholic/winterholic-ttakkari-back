"""응답 크기 상한(ADR 0033): 어떤 입력도 상한을 넘기지 않고, 잘렸으면 알린다."""
from __future__ import annotations

import json

from ttakkari.oplog.limits import HARD_MAX_CHARS, cap_text, clamp_chars, fit


def test_small_payload_is_untouched():
    payload = {"items": [{"a": 1}], "total": 1}
    assert fit(payload, 6000) == payload


def test_long_strings_and_lists_are_reduced_to_the_limit():
    payload = {"items": [{"id": i, "text": "x" * 3000} for i in range(60)], "total": 60, "next_cursor": 60}
    out = fit(payload, 2000)
    assert len(json.dumps(out, ensure_ascii=False)) <= 2000
    assert out["truncated"] is True and "hint" in out
    assert out["total"] == 60 and out["items"]  # 요약 필드는 남는다


def test_caller_cannot_raise_the_limit_above_hard_max():
    assert clamp_chars(10**9) == HARD_MAX_CHARS
    assert clamp_chars(None) == 6000
    assert clamp_chars(1) == 500
    out = fit({"text": "y" * 200_000}, 10**9)
    assert len(json.dumps(out, ensure_ascii=False)) <= HARD_MAX_CHARS


def test_one_giant_value_falls_back_to_a_minimal_notice():
    out = fit({"a": {"b": ["z" * 100_000] * 3}}, 500)
    assert len(json.dumps(out, ensure_ascii=False)) <= 500
    assert out["truncated"] is True


def test_mcp_guard_caps_even_non_json_and_oversized_json():
    huge = json.dumps({"items": [{"t": "q" * 5000} for _ in range(100)]})
    assert len(cap_text(huge)) <= HARD_MAX_CHARS
    assert json.loads(cap_text(huge))["truncated"] is True
    assert len(cap_text("plain " * 100_000)) <= HARD_MAX_CHARS
    small = json.dumps({"ok": True})
    assert cap_text(small) == small
