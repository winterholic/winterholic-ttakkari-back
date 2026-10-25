"""Runtime determination of Axis F (Execution Status) for Research Lab v2."""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Any

from ttakkari.research.models import Method

# Adapter registry accessor
def _get_registry():
    try:
        from ttakkari.research.adapters.registry import get_adapter_registry
        return get_adapter_registry()
    except ImportError:
        return None


@dataclass(frozen=True)
class ExecutionStatusResult:
    status: str
    status_label_ko: str
    is_executable: bool
    unavailable_reason: str | None = None
    unsupported_params: list[str] = field(default_factory=list)
    matched_adapter_id: str | None = None


STATUS_LABELS_KO = {
    "hypothesis_stage": "가설 단계",
    "adapter_required": "어댑터 필요",
    "environment_unavailable": "현재 환경 불가",
    "partially_supported": "일부 지원",
    "tool_integrated": "기존 도구 연동",
    "immediately_executable": "즉시 실행 가능",
}


_COMBINATION_STATUSES = {
    "local_executable", "upstream_supported", "different_mechanism",
    "external_only", "unsupported", "unverified",
}


def _combination_model(combination: dict[str, Any]) -> str | None:
    """Normalize the `model` / `model_id` spellings into one model condition."""
    for key in ("model_id", "model"):
        value = combination.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _valid_combination(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("hardware", "runtime", "status")):
        return False
    if entry["status"] not in _COMBINATION_STATUSES:
        return False
    declared = []
    for key in ("model", "model_id"):
        value = entry.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return False
        declared.append(value.strip())
    return len(set(declared)) <= 1


def _model_identity(details: dict[str, Any]) -> str | None:
    """Adapter model identity; conflicting spellings count as unidentified."""
    found = {
        value.strip() for key in ("model_id", "model")
        if isinstance(value := details.get(key), str) and value.strip()
    }
    return found.pop() if len(found) == 1 else None


def compute_execution_status(
    method: Method | dict[str, Any],
    adapter_registry: Any = None,
    scope: str = "personal",
    adapter_id: str | None = None,
) -> ExecutionStatusResult:
    """Compute Axis F status deterministically in order of criteria."""
    reg = adapter_registry or _get_registry()

    # Extract method attributes (handles both ORM model and dict)
    if isinstance(method, dict):
        slug = method.get("slug", "")
        lifecycle = method.get("lifecycle_status", "validated")
        params = method.get("parameters", [])
        metrics = method.get("metrics", [])
        adapter_hints = method.get("adapter_hints", [])
        compatibility = method.get("compatibility", {})
    else:
        slug = getattr(method, "slug", "")
        lifecycle = getattr(method, "lifecycle_status", "validated")
        params = getattr(method, "parameters", []) or []
        metrics = getattr(method, "metrics", []) or []
        adapter_hints = getattr(method, "adapter_hints", []) or []
        compatibility = getattr(method, "compatibility", {}) or {}

    # 1. hypothesis_stage
    if lifecycle == "hypothesis" or (not params and not metrics):
        return ExecutionStatusResult(
            status="hypothesis_stage",
            status_label_ko=STATUS_LABELS_KO["hypothesis_stage"],
            is_executable=False,
            unavailable_reason="실험 프로토콜 구체화 필요 (가설 편집 또는 외부 결과 기록만 가능)",
        )

    # If registry is missing or has no get_adapter function, treat as adapter_required unless adapter hints empty
    if not adapter_hints:
        return ExecutionStatusResult(
            status="adapter_required",
            status_label_ko=STATUS_LABELS_KO["adapter_required"],
            is_executable=False,
            unavailable_reason="연결 가능한 어댑터 힌트가 지정되지 않았습니다.",
        )

    matched_adapter = None
    if reg is not None and hasattr(reg, "get_adapter"):
        hints = [adapter_id] if adapter_id else adapter_hints
        for hint in hints:
            ad = reg.get_adapter(hint)
            if ad is not None and (not adapter_id or adapter_id in adapter_hints):
                matched_adapter = ad
                break

    # 2. adapter_required
    if matched_adapter is None:
        return ExecutionStatusResult(
            status="adapter_required",
            status_label_ko=STATUS_LABELS_KO["adapter_required"],
            is_executable=False,
            unavailable_reason=f"매칭되는 실행 어댑터가 아직 구현되지 않았습니다 (힌트: {', '.join(adapter_hints)})",
        )

    # 3. environment_unavailable check
    # Check hardware compatibility (e.g., CUDA-only on macOS Apple Silicon)
    hw_reqs = compatibility.get("hardware", [])
    if isinstance(hw_reqs, list) and hw_reqs:
        # If hardware is exclusively cuda and we are on darwin (Apple Silicon / CPU)
        if all(h.lower() == "cuda" for h in hw_reqs) and platform.system() == "Darwin":
            return ExecutionStatusResult(
                status="environment_unavailable",
                status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
                is_executable=False,
                unavailable_reason="CUDA 전용 하드웨어를 요구하는 기법으로 현재 Mac 환경에서는 실행 불가합니다.",
                matched_adapter_id=matched_adapter.id,
            )

    # Scope constraint: host_gpu / llm_inference is personal-only
    if scope == "work" and matched_adapter.id == "llm_inference":
        return ExecutionStatusResult(
            status="environment_unavailable",
            status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
            is_executable=False,
            unavailable_reason="회사(work) 범위에서는 개인 전용 호스트 GPU 실행기를 사용할 수 없습니다.",
            matched_adapter_id=matched_adapter.id,
        )

    # Check adapter's check_environment
    env_status = None
    try:
        env_status = matched_adapter.check_environment(scope=scope)
    except Exception as e:
        return ExecutionStatusResult(
            status="environment_unavailable",
            status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
            is_executable=False,
            unavailable_reason=f"환경 확인 실패: {e}",
            matched_adapter_id=matched_adapter.id,
        )

    if env_status and not env_status.ready:
        return ExecutionStatusResult(
            status="environment_unavailable",
            status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
            is_executable=False,
            unavailable_reason=env_status.reason or "어댑터 실행 환경이 준비되지 않았습니다.",
            matched_adapter_id=matched_adapter.id,
        )

    # Check hardware×runtime combinations if defined (combinations is authoritative over raw facet lists)
    has_combinations = "combinations" in compatibility
    combos = compatibility.get("combinations")
    if has_combinations and (
        not isinstance(combos, list) or not combos or any(not _valid_combination(c) for c in combos)
    ):
        return ExecutionStatusResult(
            status="environment_unavailable",
            status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
            is_executable=False,
            unavailable_reason="호환성 조합 선언이 비어 있거나 잘못되어 fail-closed 처리되었습니다.",
            matched_adapter_id=matched_adapter.id,
        )

    if has_combinations:
        # If matched adapter is external_report, it can never locally execute.
        if matched_adapter.id == "external_report":
            has_local_exec = any(c.get("status") == "local_executable" for c in combos)
            if not has_local_exec:
                return ExecutionStatusResult(
                    status="environment_unavailable",
                    status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
                    is_executable=False,
                    unavailable_reason="로컬 실행 가능한 하드웨어/런타임 조합이 정의되지 않았습니다 (외부 결과 등록 전용).",
                    matched_adapter_id=matched_adapter.id,
                )

        # For execution adapters, inspect adapter's runtime identity from check_environment details
        details = getattr(env_status, "details", {}) or {}
        adapter_hw = details.get("hardware")
        adapter_rt = details.get("runtime")

        # Fail-closed if runtime identity is missing or ambiguous
        if not adapter_hw or not adapter_rt:
            return ExecutionStatusResult(
                status="environment_unavailable",
                status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
                is_executable=False,
                unavailable_reason="어댑터 환경의 하드웨어 또는 런타임 식별 정보(runtime/hardware identity)가 모호하여 fail-closed 처리되었습니다.",
                matched_adapter_id=matched_adapter.id,
            )

        # Match against authoritative combinations
        matching_tuples = [
            c for c in combos
            if c.get("hardware") == adapter_hw and c.get("runtime") == adapter_rt
        ]
        # A tuple with a model condition is a more specific declaration than the
        # generic hardware/runtime tuple. The adapter's own model decides which
        # level applies; an unidentified model leaves model conditions uncertain.
        adapter_model = _model_identity(details)
        generic = [c for c in matching_tuples if _combination_model(c) is None]
        specific = [c for c in matching_tuples if _combination_model(c) is not None]
        if specific and adapter_model is None:
            return ExecutionStatusResult(
                status="environment_unavailable",
                status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
                is_executable=False,
                unavailable_reason=f"조합 ({adapter_hw} × {adapter_rt})에 모델 조건이 있으나 어댑터가 사용 모델을 식별하지 못해 fail-closed 처리되었습니다.",
                matched_adapter_id=matched_adapter.id,
            )
        model_level = [c for c in specific if _combination_model(c) == adapter_model]
        applicable = model_level or generic
        if len({c["status"] for c in applicable}) > 1:
            return ExecutionStatusResult(
                status="environment_unavailable",
                status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
                is_executable=False,
                unavailable_reason=f"조합 ({adapter_hw} × {adapter_rt})에 동일 조건의 상충하는 status 선언이 있어 fail-closed 처리되었습니다.",
                matched_adapter_id=matched_adapter.id,
            )
        matching_tuple = applicable[0] if applicable else None

        if matching_tuple is None or matching_tuple.get("status") != "local_executable":
            t_status = matching_tuple.get("status") if matching_tuple else "not_listed"
            t_note = matching_tuple.get("note", "") if matching_tuple else ""
            reason = f"조합 ({adapter_hw} × {adapter_rt})은 현재 환경에서 로컬 실행 불가 상태입니다 (상태: {t_status})."
            if t_note:
                reason = f"{reason} {t_note}".strip()
            return ExecutionStatusResult(
                status="environment_unavailable",
                status_label_ko=STATUS_LABELS_KO["environment_unavailable"],
                is_executable=False,
                unavailable_reason=reason,
                matched_adapter_id=matched_adapter.id,
            )

    # 4. Check method whitelist support (fail-closed: missing or empty supported_methods denies Full Support)
    supported_methods = getattr(matched_adapter, "supported_methods", None)
    is_method_supported = False
    if supported_methods:
        if "*" in supported_methods:
            is_method_supported = True
        elif slug in supported_methods:
            is_method_supported = True
        else:
            for pattern in supported_methods:
                if pattern.endswith("*") and slug.startswith(pattern[:-1]):
                    is_method_supported = True
                    break

    # Check parameter support (partially_supported vs fully supported)
    supported_params = set(getattr(matched_adapter, "supported_parameters", []))
    unsupported: list[str] = []
    for p in params:
        pkey = p.get("key") if isinstance(p, dict) else getattr(p, "key", None)
        if pkey and pkey not in supported_params:
            unsupported.append(pkey)

    if not is_method_supported:
        reason = (
            f"어댑터 '{matched_adapter.id}'에 지원 기법 목록(supported_methods)이 선언되지 않았거나 비어 있어 Full Support를 부여할 수 없습니다."
            if not supported_methods
            else f"기법 '{slug}'는 어댑터 '{matched_adapter.id}'의 직접 지원 목록에 없어 대리 실행(proxy execution) 모드로 분류됩니다."
        )
        return ExecutionStatusResult(
            status="partially_supported",
            status_label_ko=STATUS_LABELS_KO["partially_supported"],
            is_executable=True,
            unavailable_reason=reason,
            unsupported_params=unsupported,
            matched_adapter_id=matched_adapter.id,
        )

    if unsupported:
        return ExecutionStatusResult(
            status="partially_supported",
            status_label_ko=STATUS_LABELS_KO["partially_supported"],
            is_executable=True,
            unavailable_reason=f"일부 파라미터 미지원 ({', '.join(unsupported)})",
            unsupported_params=unsupported,
            matched_adapter_id=matched_adapter.id,
        )

    # 5. tool_integrated vs immediately_executable
    exec_mode = getattr(env_status, "execution_mode", "in_process") if env_status else "in_process"
    if exec_mode == "host_supervisor" or matched_adapter.id in ("llm_inference", "qwen_local"):
        return ExecutionStatusResult(
            status="tool_integrated",
            status_label_ko=STATUS_LABELS_KO["tool_integrated"],
            is_executable=True,
            matched_adapter_id=matched_adapter.id,
        )

    return ExecutionStatusResult(
        status="immediately_executable",
        status_label_ko=STATUS_LABELS_KO["immediately_executable"],
        is_executable=True,
        matched_adapter_id=matched_adapter.id,
    )
