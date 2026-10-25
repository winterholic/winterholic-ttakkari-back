"""KB v3: origin_scope/visibility 단일 정책 헬퍼와 L2 어댑터(설정 선택, 기본 비활성) 시험. DB 없이 돈다."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from ttakkari.kb_ontology.resolver import derive_scope
from ttakkari.kb_pipeline import derive_scope_and_visibility as pipeline_derive
from ttakkari.kb_pipeline import l2_adapters
from ttakkari.kb_pipeline.l2_adapters import (
    DeskQwenL2Adapter,
    DisabledL2Adapter,
    OpenAICompatL2Adapter,
    build_l2_adapter,
)
from ttakkari.kb_pipeline.orchestrator import (
    derive_scope_and_visibility as orchestrator_derive,
)
from ttakkari.kb_visibility import derive_derived_policy, derive_scope_and_visibility


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    yield


@pytest.fixture(autouse=True)
def _clean():
    yield


def _access(scope, visibility=None, origin=None):
    # 모델 생성자는 work+global 을 거부하므로(이중 방어) 정책 헬퍼에는 속성만 가진 객체로 모순 입력을 시험한다.
    return SimpleNamespace(scope=scope, visibility=visibility, origin_scope=origin)


POLICY_CASES = [
    (None, ("work", "work_only")),  # 접근 정보 없음
    (_access("work"), ("work", "work_only")),
    (_access("work", visibility="global", origin="personal"), ("work", "work_only")),  # work 는 global 로 올릴 수 없다
    (_access("personal"), ("personal", "global")),  # visibility 누락이어도 global
    (_access("personal", visibility="global", origin="personal"), ("personal", "global")),
    (_access("personal", visibility="work_only"), ("personal", "work_only")),  # 명시적으로 숨긴 것은 존중
    (_access("personal", visibility="garbage"), ("personal", "work_only")),  # 알 수 없는 값은 fail-closed
    (_access(None, visibility="global", origin="personal"), ("work", "work_only")),  # scope 미상
    (_access("mystery"), ("work", "work_only")),
]


@pytest.mark.parametrize(("access", "expected"), POLICY_CASES)
def test_one_policy_for_document_scope(access, expected):
    assert derive_scope_and_visibility(access) == expected


def test_pipeline_orchestrator_extractor_and_resolver_share_the_same_helper():
    assert pipeline_derive is derive_scope_and_visibility
    assert orchestrator_derive is derive_scope_and_visibility
    for access, expected in POLICY_CASES:
        resolved = derive_scope([access])
        assert (resolved.origin_scope, resolved.visibility) == derive_derived_policy([access])
        # 해소기의 단일 원천 결과 visibility 는 문서 정책의 visibility 와 같다
        assert resolved.visibility == expected[1]


def test_derived_objects_are_work_only_if_any_source_is():
    personal = _access("personal")
    work = _access("work")
    assert derive_derived_policy([personal, personal]) == ("personal", "global")
    assert derive_derived_policy([personal, work]) == ("work", "work_only")
    assert derive_derived_policy([("personal", "global"), ("personal", "work_only")]) == ("personal", "work_only")
    assert derive_derived_policy([]) == ("work", "work_only")
    assert derive_derived_policy([None]) == ("work", "work_only")
    assert derive_derived_policy(["personal", "work"]) == ("work", "work_only")
    assert derive_derived_policy(["mystery"]) == ("work", "work_only")
    assert derive_derived_policy([("personal", "bogus")]) == ("work", "work_only")


# ---------------------------------------------------------------------------
# L2 adapters
# ---------------------------------------------------------------------------
def _settings(**kw):
    base = {
        "kb_l2_adapter": "disabled", "kb_l2_base_url": "", "kb_l2_model": "", "kb_l2_timeout_seconds": 5.0,
        "kb_l2_concurrency": 1,
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def test_default_adapter_is_disabled_and_never_calls():
    adapter = build_l2_adapter(_settings())
    assert isinstance(adapter, DisabledL2Adapter)
    assert await adapter.availability() == (False, "l2_disabled")
    with pytest.raises(RuntimeError, match="l2_disabled"):
        await adapter("p", {})


def test_unknown_or_misconfigured_adapter_falls_back_to_disabled():
    assert isinstance(build_l2_adapter(_settings(kb_l2_adapter="nonsense")), DisabledL2Adapter)
    assert isinstance(build_l2_adapter(_settings(kb_l2_adapter="openai_compat")), DisabledL2Adapter)  # base_url 없음
    assert isinstance(build_l2_adapter(_settings(kb_l2_adapter="desk_qwen")), DeskQwenL2Adapter)
    configured = build_l2_adapter(_settings(kb_l2_adapter="openai_compat", kb_l2_base_url="http://127.0.0.1:19999/", kb_l2_model="m"))
    assert isinstance(configured, OpenAICompatL2Adapter) and configured.base_url == "http://127.0.0.1:19999"


async def test_desk_qwen_adapter_yields_when_the_single_slot_is_busy(monkeypatch):
    monkeypatch.setattr(l2_adapters, "check_qwen_availability", lambda: (False, "qwen_slot_busy"))
    adapter = DeskQwenL2Adapter()
    assert await adapter.availability() == (False, "qwen_slot_busy")
    with pytest.raises(RuntimeError, match="qwen_slot_busy"):
        await adapter("p", {})

    async def fake_call(prompt, schema):
        return {"entities": [], "assertions": [], "relations": []}

    monkeypatch.setattr(l2_adapters, "check_qwen_availability", lambda: (True, None))
    monkeypatch.setattr(l2_adapters, "default_qwen_llm_call", fake_call)
    assert await adapter("p", {}) == {"entities": [], "assertions": [], "relations": []}


async def test_openai_compat_adapter_posts_schema_request_and_limits_concurrency(monkeypatch):
    seen = []
    active = {"now": 0, "max": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        seen.append(request)
        active["now"] -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"entities": [], "assertions": [], "relations": []}'}}]})

    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    adapter = OpenAICompatL2Adapter("http://kb-l2.invalid", "model-x", timeout=3.0, concurrency=1)
    assert await adapter.availability() == (True, None)
    results = await asyncio.gather(*[adapter(f"p{i}", {"type": "object"}) for i in range(3)])
    assert results == [{"entities": [], "assertions": [], "relations": []}] * 3
    assert active["max"] == 1  # 동시 호출 상한
    body = seen[0].read().decode()
    assert '"json_schema"' in body and '"model-x"' in body and str(seen[0].url) == "http://kb-l2.invalid/v1/chat/completions"
    assert await OpenAICompatL2Adapter("", "m").availability() == (False, "l2_endpoint_unset")
