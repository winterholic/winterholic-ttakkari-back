"""파일 디스크립터 상한을 올린다.

launchd 가 띄운 프로세스는 soft 한도 256 을 물려받는다. 동시 Run 마다 엔진 자식·MCP 파이프가 몇 개씩 열려
한도에 닿으면 `OSError(24, 'Too many open files')` 로 무관한 Run 들이 한꺼번에 실패한다. 2026-10-29
"""
from __future__ import annotations

import logging
import resource

log = logging.getLogger(__name__)
# macOS 는 setrlimit 의 soft 값이 OPEN_MAX(10240) 를 넘으면 hard 가 무제한이어도 거절한다.
TARGET_NOFILE = 10240


def raise_nofile_limit(target: int = TARGET_NOFILE) -> int:
    """soft 한도를 target(또는 hard 한도) 까지 올리고 적용된 값을 돌려준다. 실패하면 기존 값을 유지한다."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft >= want:
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError):
        log.warning("파일 디스크립터 상한을 %d 로 올리지 못했다(현재 %d)", want, soft)
        return soft
    return want
