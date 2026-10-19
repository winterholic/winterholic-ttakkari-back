"""Google 계정 공통 계측(ADR 0051): 브라우저 실행 횟수, 로그인 시도·결과, 재인증 감지.

프로세스 안 카운터라 재시작하면 0 으로 돌아간다. 같은 수치를 구조화 로그(JSON 한 줄)로도 남긴다.
쿠키·토큰·본문은 어디에도 넣지 않는다. 기능별 요청 계측은 각 기능이 따로 둔다(NotebookLM 은 notebooklm.metrics).
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from typing import Any

log = logging.getLogger("ttakkari.google_account")
_lock = threading.Lock()
_counters: dict[str, int] = defaultdict(int)


def incr(name: str, n: int = 1) -> None:
    with _lock:
        _counters[name] += n


def event(name: str, **fields: Any) -> None:
    """구조화 로그 한 줄. 값은 숫자·짧은 코드 문자열만 받는다."""
    safe = {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:80]) for k, v in fields.items()}
    log.info("google_account %s", json.dumps({"event": name, **safe}, ensure_ascii=False, sort_keys=True))


def snapshot() -> dict[str, Any]:
    with _lock:
        return {"counters": dict(_counters), "scope": "process"}


def reset() -> None:
    with _lock:
        _counters.clear()
