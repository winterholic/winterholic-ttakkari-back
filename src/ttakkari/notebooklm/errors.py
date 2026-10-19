"""NotebookLM 도메인 오류. 상류 라이브러리 예외는 어댑터에서 이 계층으로 바꿔 밖으로 새지 않게 한다(ADR 0045).

메시지에는 쿠키·토큰·응답 원문을 싣지 않는다. 사용자에게 그대로 보여 줄 수 있는 문장만 쓴다.
"""

from __future__ import annotations


class NotebookLMError(Exception):
    """모든 NotebookLM 오류의 뿌리. status 는 REST 상태, code 는 프론트·MCP 가 분기하는 안정 코드다."""

    status = 502
    code = "upstream_error"
    # 읽기 요청에 한해 같은 요청을 다시 보내도 안전한 오류인가(재시도 정책이 본다).
    retryable = False

    def __init__(self, message: str, *, retry_after: int | None = None, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after
        # 쓰기 요청이 Google 에 도달했는지 알 수 없는 상태. 이 경우 같은 쓰기를 다시 보내지 않는다.
        self.outcome_unknown = outcome_unknown


class Disabled(NotebookLMError):
    status = 503
    code = "disabled"


class AuthRequired(NotebookLMError):
    """로그인 세션이 없거나 만료됐다. 사용자가 로그인 창에서 다시 로그인해야 한다."""

    status = 409
    code = "auth_required"


class RateLimited(NotebookLMError):
    """Google 이 사용 제한(429/RESOURCE_EXHAUSTED)을 알렸다. 쿨다운 동안 요청하지 않는다."""

    status = 429
    code = "rate_limited"


class Cooldown(RateLimited):
    """이전 제한으로 쿨다운 중이라 Google 에 요청하지 않았다."""

    code = "cooldown"


class ProtectionSignal(NotebookLMError):
    """CAPTCHA·보안 확인·접근 제한 신호. 자동화를 멈추고 사람이 확인할 때까지 요청하지 않는다(fail closed)."""

    status = 423
    code = "protection_signal"


class NotFound(NotebookLMError):
    status = 404
    code = "not_found"


class Invalid(NotebookLMError):
    status = 422
    code = "invalid"


class Unsupported(NotebookLMError):
    status = 501
    code = "unsupported"


class Busy(NotebookLMError):
    status = 409
    code = "busy"


class Duplicate(NotebookLMError):
    status = 409
    code = "duplicate"


class Transient(NotebookLMError):
    """연결 끊김 등 일시 오류."""

    status = 503
    code = "transient"
    retryable = True


class Timeout(NotebookLMError):
    status = 504
    code = "timeout"
    retryable = True


class Upstream(NotebookLMError):
    """Google/상류가 5xx 또는 해석할 수 없는 응답을 줬다."""

    status = 502
    code = "upstream_error"
    retryable = True


class UpstreamRejected(Upstream):
    """상류가 4xx 로 거절했다. 같은 요청을 다시 보내도 같은 결과다."""

    code = "upstream_rejected"
    retryable = False
