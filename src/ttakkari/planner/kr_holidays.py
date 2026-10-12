"""한국 공휴일. 키가 필요한 외부 API 대신 Python `holidays` 패키지(KR)의 내장 데이터를 쓴다(ADR 0008).

설날·추석(음력)·부처님오신날, 대체공휴일, 선거일, 과거 임시공휴일은 패키지가 계산하거나 하드코딩해 둔 값이다.
미래의 임시공휴일은 정부 공고 뒤 패키지가 업데이트되어야 들어온다(`uv lock --upgrade-package holidays` 후 배포).
음력 환산표가 2050년까지라 그 범위만 믿는다.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import TypedDict

import holidays

MIN_YEAR = 2000
MAX_YEAR = 2050
# 패키지 이름을 사용자에게 보이는 말로 다듬는다.
RENAME = {"신정연휴": "신정"}


class Holiday(TypedDict):
    date: str
    name: str
    substitute: bool


@lru_cache(maxsize=MAX_YEAR - MIN_YEAR + 1)
def year_holidays(year: int) -> tuple[Holiday, ...]:
    """한 해 공휴일. 해마다 한 번만 계산해 프로세스 동안 캐시한다."""
    if not MIN_YEAR <= year <= MAX_YEAR:
        return ()
    h = holidays.country_holidays("KR", years=year, language="ko")
    return tuple({"date": d.isoformat(), "name": RENAME.get(n, n), "substitute": "대체" in n}
                 for d, n in sorted(h.items()))


def holidays_between(start: date, end: date) -> list[Holiday]:
    """start 와 end(포함) 사이 공휴일."""
    s, e = start.isoformat(), end.isoformat()
    out: list[Holiday] = []
    for y in range(max(start.year, MIN_YEAR), min(end.year, MAX_YEAR) + 1):
        out.extend(x for x in year_holidays(y) if s <= x["date"] <= e)
    return out


def warm(center: int) -> None:
    """서버가 뜰 때 현재 연도 앞뒤를 미리 계산해 첫 요청을 빠르게 한다."""
    for y in range(center - 5, center + 6):
        year_holidays(y)
