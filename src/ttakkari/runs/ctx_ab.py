"""워커 컨텍스트 개선 A/B 공용 배정.

같은 세션은 항상 같은 쪽이어야 후속 Run 에서 주입물이 바뀌지 않는다. 그래서 세션 ID 해시로 정한다.
기능마다 해시 솔트를 다르게 해 배정이 서로 독립이다. percent 가 0이면 배정하지 않는다(기본).
"""

from __future__ import annotations

import hashlib


def arm(feature: str, session_id: str | None, percent: int) -> str:
    """on | control | off. percent<=0이거나 세션이 없으면 off."""
    if percent <= 0 or not session_id:
        return "off"
    bucket = int(hashlib.sha256(f"{feature}:{session_id}".encode()).hexdigest()[:8], 16) % 100
    return "on" if bucket < percent else "control"


def percent_of(settings: object, name: str) -> int:
    """설정에서 A/B 비율을 읽는다. 시험용 가짜 설정 객체에는 속성이 없어 0으로 본다."""
    try:
        return int(getattr(settings, name, 0) or 0)
    except (TypeError, ValueError):
        return 0
