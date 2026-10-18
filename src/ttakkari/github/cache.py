"""메모리 TTL·LRU 캐시. 키의 둘째 칸이 login 이라 계정 간에 절대 공유되지 않고, 계정이 사라지면 그 login 것만 비운다(ADR 0042 §5-6).
디스크·DB에는 아무것도 쓰지 않는다."""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_ENTRIES = 500
MAX_BYTES = 32 * 1024 * 1024


@dataclass(slots=True)
class Entry:
    payload: Any
    size: int
    expires: float
    login: str
    validated: float  # 상류에서 받았거나 304 로 다시 확인된 시각(clock). stale 최대 나이의 기준


class TTLCache:
    def __init__(self, *, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._data: OrderedDict[tuple, Entry] = OrderedDict()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = clock
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._data)

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def get(self, key: tuple) -> tuple[Entry | None, bool]:
        """(항목, 신선 여부). 만료된 항목도 돌려준다 — ETag 재검증과 stale-if-error 에 쓴다."""
        entry = self._data.get(key)
        if entry is None:
            return None, False
        self._data.move_to_end(key)
        return entry, self._clock() < entry.expires

    def put(self, key: tuple, payload: Any, *, size: int, ttl: float, login: str) -> None:
        if size > self._max_bytes:
            return
        self._drop(key)
        now = self._clock()
        self._data[key] = Entry(payload, size, now + ttl, login, now)
        self._bytes += size
        while len(self._data) > self._max_entries or self._bytes > self._max_bytes:
            self._drop(next(iter(self._data)))

    def touch(self, key: tuple, ttl: float) -> None:
        entry = self._data.get(key)
        if entry is not None:
            now = self._clock()
            entry.expires, entry.validated = now + ttl, now

    def age(self, entry: Entry) -> float:
        return self._clock() - entry.validated

    def drop(self, key: tuple) -> None:
        self._drop(key)

    def drop_where(self, match: Callable[[tuple], bool]) -> None:
        for key in [k for k in self._data if match(k)]:
            self._drop(key)

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    def purge_login(self, login: str) -> None:
        for key in [k for k, e in self._data.items() if e.login == login]:
            self._drop(key)

    def _drop(self, key: tuple) -> None:
        entry = self._data.pop(key, None)
        if entry is not None:
            self._bytes -= entry.size
