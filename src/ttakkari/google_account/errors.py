"""Google 계정을 쓰는 기능들이 함께 쓰는 오류 계층(ADR 0045, 0051). 상류 라이브러리 예외는 어댑터에서 이 계층으로 바꿔 밖으로 새지 않게 한다.

메시지에는 쿠키·토큰·응답 원문을 싣지 않는다. 사용자에게 그대로 보여 줄 수 있는 문장만 쓴다.
기능 고유 오류(예: NotebookLM 의 소스 중복)는 각 기능 패키지가 이 뿌리를 상속해 정의한다.
"""

from __future__ import annotations


class GoogleAccountError(Exception):
    """Google 계정 기반 기능 오류의 뿌리. status 는 REST 상태, code 는 프론트·MCP 가 분기하는 안정 코드다."""

    status = 502
    code = "upstream_error"
    # 읽기 요청에 한해 같은 요청을 다시 보내도 안전한 오류인가(재시도 정책이 본다).
    retryable = False

    def __init__(self, message: str, *, retry_after: int | None = None, outcome_unknown: bool = False,
                 detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after
        # 원인 요약 한 줄(예외 클래스 + 마스킹한 메시지). 쿠키·토큰·응답 원문은 담지 않는다. 화면에 보여 줘도 된다.
        self.detail = detail
        # 쓰기 요청이 Google 에 도달했는지 알 수 없는 상태. 이 경우 같은 쓰기를 다시 보내지 않는다.
        self.outcome_unknown = outcome_unknown


class Disabled(GoogleAccountError):
    status = 503
    code = "disabled"


class AuthRequired(GoogleAccountError):
    """로그인 세션이 없거나 만료됐다. 사용자가 로그인 창에서 다시 로그인해야 한다. 계정 수준 상태라 기능과 무관하게 공유된다."""

    status = 409
    code = "auth_required"


class RateLimited(GoogleAccountError):
    """Google 이 사용 제한(429/RESOURCE_EXHAUSTED)을 알렸다. 그 기능만 쿨다운 동안 요청하지 않는다."""

    status = 429
    code = "rate_limited"


class TooSoon(GoogleAccountError):
    """우리 쪽 최소 간격 제한(예: 접속 확인 연타 방지). Google 사용 제한이 아니고 Google 에 요청하지도 않았다."""

    status = 429
    code = "too_soon"


class Cooldown(RateLimited):
    """이전 제한으로 쿨다운 중이라 Google 에 요청하지 않았다."""

    code = "cooldown"


class ProtectionSignal(GoogleAccountError):
    """CAPTCHA·보안 확인·접근 제한 신호. 그 기능의 자동 요청을 멈추고 사람이 확인할 때까지 요청하지 않는다(fail closed)."""

    status = 423
    code = "protection_signal"


class NotFound(GoogleAccountError):
    status = 404
    code = "not_found"


class Invalid(GoogleAccountError):
    status = 422
    code = "invalid"


class Unsupported(GoogleAccountError):
    status = 501
    code = "unsupported"


class Busy(GoogleAccountError):
    status = 409
    code = "busy"


class Transient(GoogleAccountError):
    """연결 끊김 등 일시 오류."""

    status = 503
    code = "transient"
    retryable = True


class Timeout(GoogleAccountError):
    status = 504
    code = "timeout"
    retryable = True


class Upstream(GoogleAccountError):
    """Google/상류가 5xx 또는 해석할 수 없는 응답을 줬다."""

    status = 502
    code = "upstream_error"
    retryable = True


class UpstreamRejected(Upstream):
    """상류가 4xx 로 거절했다. 같은 요청을 다시 보내도 같은 결과다."""

    code = "upstream_rejected"
    retryable = False


class IntegrationError(GoogleAccountError):
    """따까리 연동 코드·설정의 결함(Google/상류가 낸 오류가 아님). 같은 요청을 다시 보내도 같은 결과다."""

    status = 500
    code = "integration_error"
