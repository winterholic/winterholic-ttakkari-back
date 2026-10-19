"""notebooklm-py 어댑터가 상류 공개 API 를 실제로 올바르게 부르는지(가짜 백엔드로는 잡히지 않는 부분)."""

from __future__ import annotations

import pytest
from notebooklm import NotebookLMClient

from ttakkari.google_account import redact
from ttakkari.notebooklm import errors
from ttakkari.notebooklm.adapters import notebooklm_py as adapter


def test_client_config_is_accepted_by_upstream_from_storage():
    # 회귀: config= 에 낱개 조정 인자(chat_timeout=120 등)를 섞어 넘기면 상류가 TypeError 를 내
    # 로그인 직후 접속 확인이 매번 upstream_error 로 실패했다(네트워크에 나가기 전 로컬 예외).
    cfg = adapter.build_client_config(timeout=45.0, chat_timeout=120.0)
    ctx = NotebookLMClient.from_storage(path="/nonexistent/storage_state.json", config=cfg)
    assert ctx is not None
    assert cfg.retry.rate_limit_max_retries == 0 and cfg.retry.server_error_max_retries == 0
    assert cfg.features.chat_timeout == 120.0
    assert cfg.backend.transport.read_timeout == 45.0 and cfg.backend.transport.pool_timeout == 45.0


def test_legacy_mix_is_what_upstream_rejects():
    # 위 회귀의 원인을 고정해 둔다. 상류가 이 조합을 허용하게 바뀌면 이 테스트가 알려 준다.
    with pytest.raises(TypeError, match="cannot be combined"):
        NotebookLMClient.from_storage(
            path="/nonexistent/storage_state.json", chat_timeout=120.0, config=adapter.build_client_config(30.0, 120.0))


def test_unexpected_exception_is_integration_error_with_masked_detail():
    redact.register_secrets(["sekrit-cookie-value"])
    err = adapter.map_exception(TypeError("bad arg sekrit-cookie-value"))
    assert isinstance(err, errors.IntegrationError) and err.code == "integration_error" and err.status == 500
    assert err.detail and err.detail.startswith("TypeError") and "sekrit-cookie-value" not in err.detail
    assert not err.retryable


def test_upstream_family_exception_keeps_upstream_error_code_with_detail():
    import notebooklm as up

    err = adapter.map_exception(up.NotebookLMError("boom"))
    assert err.code == "upstream_error" and err.detail and err.detail.startswith("NotebookLMError")
