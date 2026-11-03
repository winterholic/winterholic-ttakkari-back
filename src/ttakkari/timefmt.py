"""사용자와 에이전트에게 보이는 문구의 시각 표기. 저장·API 는 UTC 이고, 문구는 한국 시간(KST)으로 바꿔 쓴다."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def to_kst(value: datetime) -> datetime:
    """시간대 정보가 없는 값은 UTC 로 본다(DB 에서 naive 로 읽히는 경우)."""
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(KST)


def kst_label(value: datetime) -> str:
    """'11/04 17:40' 형태. 사용자 문구에 시각을 넣을 때 쓴다(UTC 원시 값을 그대로 찍지 않는다)."""
    return to_kst(value).strftime("%m/%d %H:%M")
