"""kb_visibility 의 재노출. origin_scope/visibility 정책 구현은 ttakkari.kb_visibility 한 곳에만 있다."""

from ttakkari.kb_visibility import (
    FAIL_CLOSED,
    SCOPE_PERSONAL,
    SCOPE_WORK,
    VISIBILITY_GLOBAL,
    VISIBILITY_WORK_ONLY,
    derive_derived_policy,
    derive_scope_and_visibility,
    resolve_document_policy,
)

__all__ = [
    "FAIL_CLOSED",
    "SCOPE_PERSONAL",
    "SCOPE_WORK",
    "VISIBILITY_GLOBAL",
    "VISIBILITY_WORK_ONLY",
    "derive_derived_policy",
    "derive_scope_and_visibility",
    "resolve_document_policy",
]
