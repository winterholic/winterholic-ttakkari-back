"""Registry for research execution adapters."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ttakkari.research.adapters.base import ExecutionAdapter

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """Registry managing available ExecutionAdapter instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, ExecutionAdapter] = {}

    def register(self, adapter: ExecutionAdapter) -> None:
        """Register an adapter. Overwrites any existing adapter with same ID."""
        self._adapters[adapter.id] = adapter

    def get_adapter(self, adapter_id: str) -> ExecutionAdapter | None:
        """Retrieve an adapter by its unique ID."""
        return self._adapters.get(adapter_id)

    def get(self, adapter_id: str) -> ExecutionAdapter | None:
        """Alias for get_adapter."""
        return self.get_adapter(adapter_id)

    def list_adapters(self) -> list[ExecutionAdapter]:
        """Return all registered adapters."""
        return list(self._adapters.values())

    def all(self) -> list[ExecutionAdapter]:
        """Alias for list_adapters."""
        return self.list_adapters()

    def find_for_domain(self, domain: str) -> list[ExecutionAdapter]:
        """Find adapters supporting a specific domain."""
        return [ad for ad in self._adapters.values() if domain in ad.domains or "*" in ad.domains]

    def find_for_method(self, method_slug: str, domain: str | None = None) -> ExecutionAdapter | None:
        """Find the best matching adapter for a method slug and optional domain."""
        candidates = self.find_for_domain(domain) if domain else list(self._adapters.values())
        for adapter in candidates:
            for pattern in adapter.supported_methods:
                if pattern == "*":
                    return adapter
                if pattern.endswith("*") and method_slug.startswith(pattern[:-1]):
                    return adapter
                if pattern == method_slug:
                    return adapter
        return None


_GLOBAL_REGISTRY: AdapterRegistry | None = None


def get_adapter_registry(*, populate_builtins: bool = True) -> AdapterRegistry:
    """Get the global adapter registry, optionally populating default built-in adapters."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = AdapterRegistry()
        if populate_builtins:
            _register_builtins(_GLOBAL_REGISTRY)
    return _GLOBAL_REGISTRY


def _register_builtins(registry: AdapterRegistry) -> None:
    """Lazily import and register built-in adapters."""
    try:
        from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
        registry.register(LlmInferenceAdapter())
    except ImportError as e:
        logger.debug("llm_inference adapter not loaded: %s", e)

    try:
        from ttakkari.research.adapters.retrieval_eval import RetrievalEvalAdapter
        registry.register(RetrievalEvalAdapter())
    except ImportError as e:
        logger.debug("retrieval_eval adapter not loaded: %s", e)

    try:
        from ttakkari.research.adapters.tts_synthesis import TtsSynthesisAdapter
        registry.register(TtsSynthesisAdapter())
    except ImportError as e:
        logger.debug("tts_synthesis adapter not loaded: %s", e)

    try:
        from ttakkari.research.adapters.external_report import ExternalReportAdapter
        registry.register(ExternalReportAdapter())
    except ImportError as e:
        logger.debug("external_report adapter not loaded: %s", e)
