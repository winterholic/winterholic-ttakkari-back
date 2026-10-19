"""NotebookLM 을 Google 계정 공통 모듈(google_account)의 첫 consumer 로 등록한다(ADR 0051).

NotebookLM 이 정의하는 것은 이것뿐이다. 로그인 창·세션 저장·재인증 알림은 공통 모듈이 하고,
쿨다운(429)·보호 신호(blocked)·접속 확인 이력은 NotebookLM 이 자기 표(notebooklm_accounts)에 둔다.
"""

from __future__ import annotations

from ttakkari.config import get_settings
from ttakkari.google_account import registry
from ttakkari.notebooklm.service import FEATURE_ID, service

CONSUMER = registry.Consumer(
    id=FEATURE_ID,
    label="NotebookLM",
    description="노트북·소스 관리와 출처 기반 질의",
    path="/notebooklm",
    login_url="https://notebooklm.google.com/",
    app_hosts=("notebooklm.google.com", "notebook.google.com"),
    enabled=lambda: get_settings().notebooklm_enabled,
    describe=service.feature_state,
    verify=service.verify,
    resume=service.resume,
    on_session_saved=service.on_session_saved,
    on_session_removed=service.on_session_removed,
    on_reauth=service.invalidate,
)

registry.register(CONSUMER)
