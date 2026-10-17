"""응답 크기 상한. 도구 결과가 크면 보호 영역에 저장돼 읽지 못하는 사고가 반복돼서, 만드는 쪽에서 막는다(ADR 0033)."""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_CHARS = 6000
HARD_MAX_CHARS = 16000
_STRING_STEPS = (1200, 600, 300, 160)


def clamp_chars(value: int | None) -> int:
    return max(500, min(int(value or DEFAULT_MAX_CHARS), HARD_MAX_CHARS))


def _size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _shrink_strings(node: Any, limit: int) -> Any:
    if isinstance(node, str):
        return node if len(node) <= limit else node[: limit - 1] + "…"
    if isinstance(node, dict):
        return {k: _shrink_strings(v, limit) for k, v in node.items()}
    if isinstance(node, list):
        return [_shrink_strings(v, limit) for v in node]
    return node


def _longest_list(node: Any, path: tuple = ()) -> tuple[int, tuple]:
    best = (0, ())
    if isinstance(node, list):
        best = (len(node), path)
        for i, v in enumerate(node):
            cand = _longest_list(v, (*path, i))
            if cand[0] > best[0]:
                best = cand
    elif isinstance(node, dict):
        for k, v in node.items():
            cand = _longest_list(v, (*path, k))
            if cand[0] > best[0]:
                best = cand
    return best


def _pop_last(node: Any, path: tuple) -> None:
    for step in path:
        node = node[step]
    node.pop()


def fit(payload: dict[str, Any], max_chars: int | None = None, *, hint: str = "") -> dict[str, Any]:
    """직렬화 길이가 상한 안이 되도록 문자열을 줄이고 마지막 항목부터 덜어 낸다. 잘리면 `truncated`와 `hint`를 붙인다."""
    limit = clamp_chars(max_chars)
    if _size(payload) <= limit:
        return payload
    out = json.loads(json.dumps(payload, ensure_ascii=False))
    out["truncated"] = True
    out["hint"] = hint or "응답이 상한(max_chars)을 넘어 줄였습니다. 범위를 좁히거나 cursor로 다음 쪽을 읽으세요."
    for step in _STRING_STEPS:
        out = _shrink_strings(out, step)
        if _size(out) <= limit:
            return out
    while _size(out) > limit:
        length, path = _longest_list(out)
        if length <= 1:
            break
        _pop_last(out, path)
    if _size(out) > limit:
        out = {"truncated": True, "hint": out["hint"], "note": "응답이 상한보다 커서 본문을 생략했습니다."}
    return out


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def cap_text(text: str, limit: int = HARD_MAX_CHARS) -> str:
    """MCP 서버의 마지막 방어선. JSON 문자열을 상한 안으로 자르고 잘렸음을 JSON 으로 알린다."""
    if len(text) <= limit:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return json.dumps({"truncated": True, "hint": "응답이 너무 커서 잘랐습니다.", "head": text[: limit - 200]}, ensure_ascii=False)
    if isinstance(data, dict):
        return dumps(fit(data, limit))
    return dumps(fit({"items": data}, limit))
