"""지식 도서관(KB v3) L2 subscription_cli 제품 기본 활성화 및 시험 격리 회귀 테스트.

작업 1 검증:
1. 제품 기본 설정이 채택된 subscription_cli 및 gpt-6-luna 로 활성화됨.
2. 기존 명시 disabled optout 이 완벽히 존중됨.
3. 일반 통합 테스트가 자동 cloud/CLI 호출을 하지 않도록 conftest 에서 명시적으로 disabled 로 격리됨.
4. 계정 없음 / quota budget / 실패 시 정직하게 degraded 상태를 유지함.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ttakkari.config import Settings
from ttakkari.kb_pipeline.l2_adapters import (
    DisabledL2Adapter,
    SubscriptionCliL2Adapter,
    build_l2_adapter,
)


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    """DB 연결 없는 순수 단위 테스트를 위해 conftest 의 세션 DB fixture 를 오버라이드한다."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """DB 연결 없는 순수 단위 테스트를 위해 conftest 의 per-test clean fixture 를 오버라이드한다."""
    yield


def test_default_settings_l2_activation(monkeypatch):
    """환경변수 오버라이드가 없을 때 제품 기본값은 subscription_cli 및 gpt-6-luna 이어야 한다."""
    monkeypatch.delenv("TTAKKARI_KB_L2_ADAPTER", raising=False)
    monkeypatch.delenv("TTAKKARI_KB_L2_SUBSCRIPTION_MODEL", raising=False)
    s = Settings(_env_file=None)
    assert s.kb_l2_adapter == "subscription_cli"
    assert s.kb_l2_subscription_model == "gpt-6-luna"
    assert s.kb_l2_subscription_engine == "codex"


def test_explicit_disabled_optout(monkeypatch):
    """명시적으로 disabled 로 설정하면 optout 되어 DisabledL2Adapter 가 생성되어야 한다."""
    s = Settings(kb_l2_adapter="disabled", _env_file=None)
    assert s.kb_l2_adapter == "disabled"
    adapter = build_l2_adapter(s)
    assert isinstance(adapter, DisabledL2Adapter)


def test_build_l2_adapter_default_activation(monkeypatch):
    """기본 설정과 session_factory 가 제공되면 SubscriptionCliL2Adapter 가 생성되어야 한다."""
    monkeypatch.delenv("TTAKKARI_KB_L2_ADAPTER", raising=False)
    monkeypatch.delenv("TTAKKARI_KB_L2_SUBSCRIPTION_MODEL", raising=False)
    s = Settings(_env_file=None)
    dummy_session_factory = lambda: None
    adapter = build_l2_adapter(s, session_factory=dummy_session_factory)
    assert isinstance(adapter, SubscriptionCliL2Adapter)
    assert adapter.session_factory is dummy_session_factory
    assert adapter.engine == "codex"
    assert adapter.model == "gpt-6-luna"


def test_build_l2_adapter_falls_back_to_disabled_without_session_factory(monkeypatch):
    """subscription_cli 설정이라도 session_factory 가 주어지지 않으면 안전하게 DisabledL2Adapter 로 폴백한다."""
    monkeypatch.delenv("TTAKKARI_KB_L2_ADAPTER", raising=False)
    s = Settings(_env_file=None)
    adapter = build_l2_adapter(s, session_factory=None)
    assert isinstance(adapter, DisabledL2Adapter)


@pytest.mark.asyncio
async def test_subscription_cli_degraded_when_capacity_unavailable():
    """계정이 없거나 quota budget/한도에 걸리면 availability 는 False 및 scope_capacity_unavailable 로 정직 유지된다."""
    dummy_db = AsyncMock()

    class DummySessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return dummy_db

        async def __aexit__(self, *args):
            pass

    adapter = SubscriptionCliL2Adapter(
        session_factory=DummySessionFactory(),
        engine="codex",
        model="gpt-6-luna",
        origin_scope="personal",
        visibility="global",
    )

    with patch(
        "ttakkari.runs.profiles.availability", new_callable=AsyncMock
    ) as mock_avail:
        # 가용 계정 없음 (free 빈 목록, blocked 에 1개)
        prof = SimpleNamespace(id="prof-1", pool="personal")
        mock_avail.return_value = ([], [(prof, 1700000000.0)])
        ready, reason = await adapter.availability()
        assert ready is False
        assert reason == "scope_capacity_unavailable"
        assert adapter.retry_after == 1700000000.0


def test_conftest_explicitly_disables_l2_for_isolation():
    """conftest 의 env 에 TTAKKARI_KB_L2_ADAPTER=disabled 가 명시되어 테스트 중 자동 cloud 호출을 막아야 한다."""
    import os

    assert os.environ.get("TTAKKARI_KB_L2_ADAPTER") == "disabled"
