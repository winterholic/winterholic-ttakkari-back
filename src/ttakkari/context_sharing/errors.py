"""맥락 공유 API 오류. {code, message, ...extra} 로 내려간다(api/context_sharing.install)."""
from __future__ import annotations

from typing import Any


class ShareError(Exception):
    def __init__(self, status: int, code: str, message: str, *, extra: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.headers = headers or {}
