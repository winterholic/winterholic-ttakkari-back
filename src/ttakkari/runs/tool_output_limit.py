"""Codex 도구 출력 상한 A/B 배정.

같은 세션은 항상 같은 쪽이어야 이어받기(resume) 중에 상한이 바뀌지 않는다. 그래서 세션 ID 해시로 정한다.
percent 가 0이면 아무것도 바꾸지 않는다(기본).
"""

from __future__ import annotations

import hashlib


def arm(session_id: str | None, settings: object) -> tuple[str, int | None]:
    """(배정, 토큰 상한). 배정은 off | limit | control."""
    # 시험에서 설정 객체를 가짜로 바꿔 끼우는 곳이 있어 속성이 없으면 끈 것으로 본다.
    percent = getattr(settings, "codex_tool_output_ab_percent", 0)
    if percent <= 0 or not session_id:
        return "off", None
    bucket = int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % 100
    if bucket < percent:
        return "limit", getattr(settings, "codex_tool_output_ab_tokens", 5000)
    return "control", None
