"""요청 수·지연·재시도·브라우저 실행 횟수 계측(ADR 0045 §성능). 성능 실측은 로그인 후 단계라 구조만 둔다.

프로세스 안 카운터라 재시작하면 0 으로 돌아간다. 같은 수치를 구조화 로그(JSON 한 줄)로도 남겨
나중에 로그에서 P50/P95 를 계산할 수 있게 한다. 쿠키·토큰·본문은 어디에도 넣지 않는다.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from typing import Any

log = logging.getLogger("ttakkari.notebooklm")
_lock = threading.Lock()
_counters: dict[str, int] = defaultdict(int)
_latency: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "sum_ms": 0.0, "max_ms": 0.0})


def incr(name: str, n: int = 1) -> None:
    with _lock:
        _counters[name] += n


def observe(op: str, elapsed_ms: float) -> None:
    with _lock:
        s = _latency[op]
        s["count"] += 1
        s["sum_ms"] += elapsed_ms
        s["max_ms"] = max(s["max_ms"], elapsed_ms)


def event(name: str, **fields: Any) -> None:
    """구조화 로그 한 줄. 값은 숫자·짧은 코드 문자열만 받는다."""
    safe = {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:80]) for k, v in fields.items()}
    log.info("notebooklm %s", json.dumps({"event": name, **safe}, ensure_ascii=False, sort_keys=True))


def snapshot() -> dict[str, Any]:
    with _lock:
        lat = {
            op: {"count": int(s["count"]), "avg_ms": round(s["sum_ms"] / s["count"], 1) if s["count"] else 0.0,
                 "max_ms": round(s["max_ms"], 1)}
            for op, s in _latency.items()
        }
        return {"counters": dict(_counters), "latency": lat, "scope": "process"}


def reset() -> None:
    with _lock:
        _counters.clear()
        _latency.clear()
