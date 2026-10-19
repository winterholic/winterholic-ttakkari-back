"""NotebookLM 오류. 공통 계층(ttakkari.google_account.errors)을 그대로 쓰고 NotebookLM 고유 오류만 더한다(ADR 0045, 0051).

NotebookLMError 는 공통 뿌리의 별칭이라 기존 except NotebookLMError 와 REST 오류 핸들러가 그대로 동작한다.
"""

from __future__ import annotations

from ttakkari.google_account.errors import (
    AuthRequired,
    Busy,
    Cooldown,
    Disabled,
    GoogleAccountError,
    IntegrationError,
    Invalid,
    NotFound,
    ProtectionSignal,
    RateLimited,
    Timeout,
    TooSoon,
    Transient,
    Unsupported,
    Upstream,
    UpstreamRejected,
)

NotebookLMError = GoogleAccountError


class Duplicate(NotebookLMError):
    status = 409
    code = "duplicate"


__all__ = [
    "AuthRequired", "Busy", "Cooldown", "Disabled", "Duplicate", "GoogleAccountError", "IntegrationError", "Invalid", "NotFound",
    "NotebookLMError", "ProtectionSignal", "RateLimited", "Timeout", "TooSoon", "Transient", "Unsupported", "Upstream",
    "UpstreamRejected",
]
