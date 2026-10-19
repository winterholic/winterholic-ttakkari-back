"""마스킹은 Google 계정 공통 모듈로 옮겼다(ADR 0051). 기존 import 경로를 위한 재노출이다."""

from ttakkari.google_account.redact import (  # noqa: F401
    MASK,
    MAX_ERROR_CHARS,
    clear_secrets,
    mask,
    register_secrets,
    safe_error,
)
