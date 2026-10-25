"""KB v3 L2(깊은 추출) 호출 어댑터. 설정(kb_l2_adapter)으로 고르고 기본은 꺼짐이다.

어댑터는 (가용성 확인, 호출) 두 가지만 약속한다. 가용하지 않으면 추출 단계는 L0/L1 결과를 보존한 채
degraded(사유 포함)로 끝나며, 호출 실패도 같은 방식으로 격리된다. 어댑터 생성은 모델 서버를 띄우지 않는다.

- disabled: 호출하지 않는다(기본). 추출 단계는 degraded('l2_disabled').
- desk_qwen: 접수용 로컬 Qwen(runs/desk_qwen) 재사용. 접수와 같은 단일 슬롯이라 Qwen 슬롯이 바쁘거나 서버가 없으면 건너뛴다.
- openai_compat: 전용 OpenAI 호환 서버(kb_l2_base_url, 예: 별도 llama-server). 접수 슬롯과 경합하지 않는다.
- subscription_cli: 기존 Codex/Claude 구독 CLI를 tool-free JSON 호출로 사용하며 문서 provenance에 맞는 계정만 선택한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import Settings, get_settings
from ttakkari.kb_pipeline.extract_l2 import (
    L2_SYSTEM_PROMPT,
    check_qwen_availability,
    default_qwen_llm_call,
)
from ttakkari.kb_pipeline.subscription_transport import (
    DIAG_TIMEOUT,
    L2TransportError,
    ScopeCapacityUnavailableError,
    SubscriptionCallConfig,
    execute_subscription_l2_call,
    resolve_target_pool,
)
from ttakkari.runs import profiles

log = logging.getLogger(__name__)

L2_DISABLED_REASON = "l2_disabled"


class L2Adapter(Protocol):
    name: str

    async def availability(self) -> tuple[bool, str | None]: ...

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


class _ConcurrencyGate:
    """이벤트 루프마다 하나씩 만드는 세마포어(프로세스 내 L2 동시 호출 상한)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sem: asyncio.Semaphore | None = None

    def semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._loop = loop
            self._sem = asyncio.Semaphore(self.limit)
        return self._sem


_SUBSCRIPTION_GATES: dict[str, _ConcurrencyGate] = {}


class DisabledL2Adapter:
    name = "disabled"

    async def availability(self) -> tuple[bool, str | None]:
        return False, L2_DISABLED_REASON

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(L2_DISABLED_REASON)


class DeskQwenL2Adapter:
    """접수용 단일 슬롯 Qwen. 접수 응답과 슬롯을 다투므로 가용성은 매 호출 직전에 다시 확인한다."""

    name = "desk_qwen"

    def __init__(self, concurrency: int = 1) -> None:
        self._gate = _ConcurrencyGate(concurrency)

    async def availability(self) -> tuple[bool, str | None]:
        return check_qwen_availability()

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        async with self._gate.semaphore():
            ready, reason = check_qwen_availability()
            if not ready:
                raise RuntimeError(reason or "desk_qwen_unavailable")
            return await default_qwen_llm_call(prompt, schema)


class OpenAICompatL2Adapter:
    """OpenAI 호환 /v1/chat/completions 서버(llama-server 등) 하나. 모델 서버 기동은 이 어댑터의 일이 아니다."""

    name = "openai_compat"

    def __init__(
        self, base_url: str, model: str, timeout: float = 60.0, concurrency: int = 1
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._gate = _ConcurrencyGate(concurrency)

    async def availability(self) -> tuple[bool, str | None]:
        if not self.base_url:
            return False, "l2_endpoint_unset"
        return True, None

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        import httpx

        if not self.base_url:
            raise RuntimeError("l2_endpoint_unset")
        payload = {
            "model": self.model or "default",
            "messages": [
                {"role": "system", "content": L2_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "seed": 20261025,
            "max_tokens": 1500,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "kb_l2_extraction", "strict": True, "schema": schema},
            },
        }
        async with self._gate.semaphore(), httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=3.0), trust_env=False
        ) as client:
            resp = await client.post(
                self.base_url + "/v1/chat/completions", json=payload
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)


class SubscriptionCliL2Adapter:
    """Tool-free bounded JSON calls through an enabled, scope-compatible CLI account."""

    name = "subscription_cli"

    def __init__(self, session_factory: Callable[[], AsyncSession], *, engine: str,
                 model: str = "", effort: str = "", timeout: float = 90.0,
                 concurrency: int = 1, origin_scope: str, visibility: str) -> None:
        self.session_factory = session_factory
        self.engine = engine
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.origin_scope = origin_scope
        self.visibility = visibility
        self.target_pool = resolve_target_pool(origin_scope, visibility)
        self.retry_after: float | None = None
        self._gate = _SUBSCRIPTION_GATES.setdefault(engine, _ConcurrencyGate(concurrency))
        if self._gate.limit != concurrency:
            log.warning("KB L2 concurrency setting differs from existing process gate; using existing limit")

    async def availability(self) -> tuple[bool, str | None]:
        if self.target_pool is None:
            return False, "scope_capacity_unavailable"
        async with self.session_factory() as db:
            free, blocked = await profiles.availability(db, self.engine, self.target_pool, model=self.model or None)
        if free:
            return True, None
        self.retry_after = min((until for _, until in blocked), default=None)
        return False, "scope_capacity_unavailable"

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if self.target_pool is None:
            raise ScopeCapacityUnavailableError("document provenance is not permitted for subscription cloud")
        semaphore = self._gate.semaphore()
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=self.timeout)
        except TimeoutError as exc:
            raise L2TransportError(DIAG_TIMEOUT, "subscription concurrency wait timed out", consumed=False) from exc
        try:
            async with self.session_factory() as db:
                candidates, blocked = await profiles.availability(
                    db, self.engine, self.target_pool, model=self.model or None
                )
                if not candidates:
                    retry_after = min((until for _, until in blocked), default=None)
                    self.retry_after = retry_after
                    raise ScopeCapacityUnavailableError("no enabled subscription profile for document scope", retry_after)
                run_id = uuid.uuid4()
                profile = await profiles.choose_async(candidates, run_id)
            try:
                return await execute_subscription_l2_call(
                    profile,
                    prompt,
                    schema,
                    SubscriptionCallConfig(self.engine, self.model, self.effort, self.timeout),
                    run_id=run_id,
                )
            except Exception as exc:
                if getattr(exc, "diagnosis", None) == "usage_limit":
                    self.retry_after = getattr(exc, "retry_after", None)
                raise
            finally:
                profiles.release(run_id)
        finally:
            semaphore.release()


def build_l2_adapter(settings: Settings | None = None, *, session_factory: Callable[[], AsyncSession] | None = None,
                     origin_scope: str = "work", visibility: str = "work_only") -> L2Adapter:
    """kb_l2_adapter 설정으로 어댑터를 만든다. 알 수 없는 값이나 설정 누락은 꺼진 어댑터가 된다."""
    s = settings or get_settings()
    kind = getattr(s, "kb_l2_adapter", "disabled")
    if kind == "desk_qwen":
        return DeskQwenL2Adapter(concurrency=s.kb_l2_concurrency)
    if kind == "openai_compat":
        if not s.kb_l2_base_url:
            log.warning("kb_l2_adapter=openai_compat but kb_l2_base_url is empty; L2 disabled")
            return DisabledL2Adapter()
        return OpenAICompatL2Adapter(
            s.kb_l2_base_url,
            s.kb_l2_model,
            timeout=s.kb_l2_timeout_seconds,
            concurrency=s.kb_l2_concurrency,
        )
    if kind == "subscription_cli" and session_factory is not None:
        return SubscriptionCliL2Adapter(
            session_factory,
            engine=s.kb_l2_subscription_engine,
            model=s.kb_l2_subscription_model,
            effort=s.kb_l2_subscription_effort,
            timeout=s.kb_l2_subscription_timeout_seconds,
            concurrency=s.kb_l2_subscription_concurrency,
            origin_scope=origin_scope,
            visibility=visibility,
        )
    return DisabledL2Adapter()
