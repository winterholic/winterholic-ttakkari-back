"""SEC2-AUTH-02: 운영 설정 안전값 확인과 기동 시 fail-fast (RED/GREEN).

수정 전에는 잘못된 조합도 기동에 성공해 이 테스트가 실패하고(문제 있음),
수정 후에는 기동 실패(RuntimeError)로 테스트가 통과한다.
정상 운영 설정 대조군은 기동에 성공하고 경고를 남기지 않아야 한다.
"""
from __future__ import annotations

import logging

import pytest

from ttakkari.config import DEV_INSECURE_JWT_SECRET, Settings, get_settings

GOOD_PROD_SECRET = "0123456789abcdef0123456789abcdef"  # 32 ASCII 문자 = 32바이트
LOGGER = "ttakkari.config"


def _settings(**kw) -> Settings:
    base: dict = {
        "_env_file": None,
        "env": "prod",
        "jwt_secret": GOOD_PROD_SECRET,
        "public_share_origin": "https://share.example.com",
        "cors_origins": ["https://app.example.com"],
        "trust_proxy_ip_header": None,
    }
    base.update(kw)
    return Settings(**base)


def test_prod_dev_constant_secret_fails_startup() -> None:
    with pytest.raises(RuntimeError, match="개발용 기본 서명키"):
        _settings(jwt_secret=DEV_INSECURE_JWT_SECRET)


def test_prod_short_secret_fails_startup() -> None:
    with pytest.raises(RuntimeError, match="짧습니다"):
        _settings(jwt_secret="too-short")


def test_prod_multibyte_secret_counts_bytes_not_chars() -> None:
    # 8글자지만 UTF-8 24바이트라 실패해야 한다.
    with pytest.raises(RuntimeError, match="짧습니다"):
        _settings(jwt_secret="가나다라마바사아")
    # 11글자 = 33바이트라 통과한다.
    _settings(jwt_secret="가나다라마바사아자차카")


def test_prod_empty_secret_fails_startup() -> None:
    with pytest.raises(RuntimeError, match="비어 있습니다"):
        _settings(jwt_secret="")


def test_prod_control_starts_without_warnings(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _settings()
    assert [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING] == []


def test_nonprod_with_public_origin_warns_but_starts(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        s = _settings(env="dev", jwt_secret="", public_share_origin="https://ttakkari.winterholic.net")
    assert not s.is_prod
    assert any("공개 도메인" in r.message for r in caplog.records if r.name == LOGGER)


def test_localhost_only_cors_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _settings(env="dev", jwt_secret="", public_share_origin="http://localhost:3000",
                   cors_origins=["http://localhost:5173", "http://127.0.0.1:5173"])
    assert any("cors_origins" in r.message for r in caplog.records if r.name == LOGGER)


def test_trust_proxy_header_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _settings(trust_proxy_ip_header="X-Forwarded-For")
    assert any("trust_proxy_ip_header" in r.message for r in caplog.records if r.name == LOGGER)


def test_tunnel_proxy_header_passes_quietly(caplog: pytest.LogCaptureFixture) -> None:
    # 운영 검증 구성(Cloudflare Tunnel + CF-Connecting-IP)은 경고 없이 통과한다.
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _settings(trust_proxy_ip_header="CF-Connecting-IP")
    assert [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING] == []


def test_get_settings_entrypoint_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTAKKARI_NO_ENV_FILE", "1")
    monkeypatch.setenv("TTAKKARI_ENV", "prod")
    monkeypatch.setenv("TTAKKARI_JWT_SECRET", "too-short")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_health_response_exposes_no_validation_result() -> None:
    import inspect

    from ttakkari.api import system

    src = inspect.getsource(system.health)
    assert "validate_operational" not in src
    assert "jwt_secret" not in src
