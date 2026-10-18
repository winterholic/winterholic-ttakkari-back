"""GitHub 탐색 오류. 코드·HTTP 상태는 ADR 0042 §8 표와 같다.

상류 401 은 절대 401 로 내보내지 않는다. 프론트 client.ts 는 401 에서 앱 세션을 refresh 하고 실패하면 로그아웃시킨다.
상류 오류 본문은 그대로 전달하지 않는다(우리 문구만).
"""
from __future__ import annotations

from typing import Any

# code -> (HTTP status, 기본 문구)
CATALOG: dict[str, tuple[int, str]] = {
    "no_account": (503, "gh에 로그인된 GitHub 계정이 없습니다. Mac Studio에서 `gh auth login`이 필요합니다."),
    "account_unavailable": (404, "선택한 계정을 gh 계정 목록에서 찾을 수 없습니다."),
    "invalid_target": (422, "입력 형식이 올바르지 않습니다."),
    "unsupported_host": (422, "github.com 주소만 지원합니다."),
    "ref_not_found": (404, "브랜치·태그·커밋을 찾을 수 없습니다."),
    "readme_not_found": (404, "README가 없습니다."),
    "gh_not_found": (404, "없거나 이 계정에 권한이 없습니다."),
    "gh_forbidden": (403, "이 계정으로는 접근할 수 없습니다."),
    "gh_auth_expired": (502, "계정 인증이 만료되었습니다. Mac Studio에서 gh 재로그인이 필요합니다."),
    "gh_rate_limited": (429, "GitHub API 호출 한도에 도달했습니다."),
    "gh_gone": (410, "더 이상 제공되지 않는 항목입니다(예: 이슈 비활성)."),
    "gh_unavailable_legal": (451, "법적 사유로 접근이 차단된 항목입니다."),
    "gh_unprocessable": (422, "GitHub이 요청을 처리하지 못했습니다(예: 변경이 너무 큼). GitHub에서 직접 여세요."),
    "gh_empty_repository": (409, "비어 있는 저장소입니다."),
    "gh_too_large": (413, "응답이 표시 상한을 넘습니다. GitHub에서 직접 여세요."),
    "gh_timeout": (504, "GitHub 응답 시간이 예산을 넘었습니다."),
    "gh_upstream_error": (502, "GitHub 호출에 실패했습니다."),
    "gh_schema_changed": (502, "GitHub 응답 형식이 예상과 달라 표시할 수 없습니다."),
    "gh_logs_unavailable": (502, "로그를 안전하게 내려받을 수 없습니다."),
}


class GithubError(Exception):
    def __init__(self, code: str, message: str | None = None, *, reason: str | None = None,
                 reset: int | None = None, retry_after: int | None = None, extra: dict[str, Any] | None = None) -> None:
        status, default = CATALOG[code]
        self.code = code
        self.status = status
        self.message = message or default
        self.reason = reason
        self.reset = reset
        self.retry_after = retry_after
        self.extra = extra or {}
        super().__init__(f"{code}: {self.message}")

    def detail(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason:
            body["reason"] = self.reason
        if self.reset is not None:
            body["reset"] = self.reset
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        body.update(self.extra)
        return body

    def headers(self) -> dict[str, str] | None:
        return {"Retry-After": str(self.retry_after)} if self.retry_after is not None else None
