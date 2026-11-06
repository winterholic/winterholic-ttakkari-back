"""Neutral diagnosis contract for bounded subscription transports."""

from __future__ import annotations

DIAG_NONZERO_EXIT = "nonzero_exit"
DIAG_TIMEOUT = "timeout"
DIAG_USAGE_LIMIT = "usage_limit"
DIAG_INVALID_JSON = "invalid_json"
DIAG_SCHEMA_VIOLATION = "schema_violation"
DIAG_SCOPE_CAPACITY_UNAVAILABLE = "scope_capacity_unavailable"


class L2TransportError(RuntimeError):
    """Base error for L2 transport failures with structured diagnosis."""

    def __init__(self, diagnosis: str, message: str, retry_after: float | None = None,
                 *, consumed: bool = True, engine: str | None = None,
                 account: str | None = None, model: str | None = None) -> None:
        super().__init__(f"[{diagnosis}] {message}")
        self.diagnosis = diagnosis
        self.retry_after = retry_after
        self.consumed = consumed
        self.engine = engine
        self.account = account
        self.model = model


class ScopeCapacityUnavailableError(L2TransportError):
    """Raised when no profile is available for the required scope."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(DIAG_SCOPE_CAPACITY_UNAVAILABLE, message, retry_after, consumed=False)
