"""Experiment planner, search space expansion, budget guards, and method relation validation for Research Lab v2."""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import logging
import random
import uuid

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ttakkari.research.models import Method, MethodRelation, Study


@dataclass
class PlanValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    expanded_configurations: list[dict[str, Any]] = field(default_factory=list)
    resource_estimate: dict[str, Any] = field(default_factory=dict)


def canonical_json_hash(obj: Any) -> str:
    serialized = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def check_optuna_installed() -> bool:
    return importlib.util.find_spec("optuna") is not None


async def validate_method_combinations(
    db, method_ids: list[uuid.UUID]
) -> tuple[list[str], list[str]]:
    """Check conflicts, requirements, and combination warnings among selected methods."""
    errors: list[str] = []
    warnings: list[str] = []

    if len(method_ids) <= 1:
        return errors, warnings

    warnings.append(
        "복수 방법론이 조합되었습니다. 조합 효과는 개별 기법의 단순 합과 다를 수 있으므로 별도 Ablation/단일 실험과의 비교가 권장됩니다."
    )

    # Fetch relations between these methods
    relations = (
        await db.execute(
            select(MethodRelation).where(
                MethodRelation.from_method_id.in_(method_ids),
            )
        )
    ).scalars().all()

    methods_map = {}
    methods_res = (
        await db.execute(select(Method).where(Method.id.in_(method_ids)))
    ).scalars().all()
    for m in methods_res:
        methods_map[m.id] = m.name_ko or m.name

    selected_set = set(method_ids)

    for rel in relations:
        from_name = methods_map.get(rel.from_method_id, str(rel.from_method_id))
        to_name = methods_map.get(rel.to_method_id, str(rel.to_method_id))

        if rel.relation_type == "conflicts_with" and rel.to_method_id in selected_set:
            errors.append(
                f"방법론 충돌: '{from_name}'와(과) '{to_name}'은(는) 상호 충돌(conflicts_with) 관계입니다. {rel.note}"
            )
        elif rel.relation_type == "requires" and rel.to_method_id not in selected_set:
            warnings.append(
                f"선행 요구: '{from_name}'은(는) '{to_name}'을(를) 필요로 합니다(requires). 함께 선택하지 않으면 오동작할 수 있습니다. {rel.note}"
            )
        elif rel.relation_type == "precedes" and rel.to_method_id in selected_set:
            warnings.append(
                f"순서 주의: '{from_name}'은(는) '{to_name}'보다 먼저 적용되어야 합니다(precedes)."
            )

    return errors, warnings


def expand_configurations(
    strategy: str,
    search_space: dict[str, Any],
    fixed_inputs: dict[str, Any],
    max_runs: int = 20,
    seed: int = 42,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Expand search space into concrete configurations according to strategy."""
    errors: list[str] = []
    warnings: list[str] = []
    configs: list[dict[str, Any]] = []

    # Combine fixed_inputs and baseline search_space
    base_params = dict(fixed_inputs)

    if strategy in ("single", "baseline"):
        params = dict(base_params)
        for k, v in search_space.items():
            params[k] = v[0] if isinstance(v, list) and v else v
        configs.append(
            {
                "label": "Baseline",
                "role": "baseline",
                "order_index": 0,
                "parameters": params,
                "config_hash": canonical_json_hash(params),
                "ablation_factor": None,
            }
        )

    elif strategy == "ab":
        # First config is baseline
        baseline_params = dict(base_params)
        treatment_keys = []
        for k, v in search_space.items():
            if isinstance(v, list) and len(v) > 1:
                baseline_params[k] = v[0]
                treatment_keys.append((k, v[1:]))
            elif isinstance(v, list) and len(v) == 1:
                baseline_params[k] = v[0]
            else:
                baseline_params[k] = v

        configs.append(
            {
                "label": "Control (A)",
                "role": "baseline",
                "order_index": 0,
                "parameters": baseline_params,
                "config_hash": canonical_json_hash(baseline_params),
                "ablation_factor": None,
            }
        )

        treatment_idx = 1
        for k, vals in treatment_keys[:3]:  # Max 3 treatment variations
            for val in vals[:3]:
                t_params = dict(baseline_params)
                t_params[k] = val
                configs.append(
                    {
                        "label": f"Treatment (B: {k}={val})",
                        "role": "treatment",
                        "order_index": treatment_idx,
                        "parameters": t_params,
                        "config_hash": canonical_json_hash(t_params),
                        "ablation_factor": None,
                    }
                )
                treatment_idx += 1

    elif strategy == "repeat":
        # Single configuration to be repeated N times
        params = dict(base_params)
        for k, v in search_space.items():
            params[k] = v[0] if isinstance(v, list) and v else v
        configs.append(
            {
                "label": "Repeated Baseline",
                "role": "baseline",
                "order_index": 0,
                "parameters": params,
                "config_hash": canonical_json_hash(params),
                "ablation_factor": None,
            }
        )

    elif strategy == "grid":
        # Cartesian product of list parameters
        sweep_keys = []
        sweep_values = []
        fixed_part = dict(base_params)

        for k, v in search_space.items():
            if isinstance(v, list) and len(v) > 1:
                sweep_keys.append(k)
                sweep_values.append(v)
            elif isinstance(v, list) and len(v) == 1:
                fixed_part[k] = v[0]
            else:
                fixed_part[k] = v

        if not sweep_keys:
            configs.append(
                {
                    "label": "Grid #1 (Default)",
                    "role": "candidate",
                    "order_index": 0,
                    "parameters": fixed_part,
                    "config_hash": canonical_json_hash(fixed_part),
                    "ablation_factor": None,
                }
            )
        else:
            total_combinations = 1
            for vals in sweep_values:
                total_combinations *= len(vals)

            if total_combinations > max_runs:
                errors.append(
                    f"Grid 탐색 공간 조합 수({total_combinations})가 최대 허용 예산({max_runs})을 초과합니다. 파라미터 후보를 축소하세요."
                )
                return configs, errors, warnings

            idx = 0
            for combo in itertools.product(*sweep_values):
                params = dict(fixed_part)
                label_parts = []
                for k, val in zip(sweep_keys, combo):
                    params[k] = val
                    label_parts.append(f"{k}={val}")
                role = "baseline" if idx == 0 else "candidate"
                configs.append(
                    {
                        "label": f"#{idx + 1} ({', '.join(label_parts)})",
                        "role": role,
                        "order_index": idx,
                        "parameters": params,
                        "config_hash": canonical_json_hash(params),
                        "ablation_factor": None,
                    }
                )
                idx += 1

    elif strategy == "random":
        sweep_keys = []
        sweep_values = []
        fixed_part = dict(base_params)

        for k, v in search_space.items():
            if isinstance(v, list) and len(v) > 1:
                sweep_keys.append(k)
                sweep_values.append(v)
            elif isinstance(v, list) and len(v) == 1:
                fixed_part[k] = v[0]
            else:
                fixed_part[k] = v

        if not sweep_keys:
            configs.append(
                {
                    "label": "Random Sample #1",
                    "role": "candidate",
                    "order_index": 0,
                    "parameters": fixed_part,
                    "config_hash": canonical_json_hash(fixed_part),
                    "ablation_factor": None,
                }
            )
        else:
            rng = random.Random(seed)
            all_combos = list(itertools.product(*sweep_values))
            sample_size = min(len(all_combos), max_runs)
            sampled = rng.sample(all_combos, sample_size)

            for idx, combo in enumerate(sampled):
                params = dict(fixed_part)
                label_parts = []
                for k, val in zip(sweep_keys, combo):
                    params[k] = val
                    label_parts.append(f"{k}={val}")
                role = "baseline" if idx == 0 else "candidate"
                configs.append(
                    {
                        "label": f"Random #{idx + 1} ({', '.join(label_parts)})",
                        "role": role,
                        "order_index": idx,
                        "parameters": params,
                        "config_hash": canonical_json_hash(params),
                        "ablation_factor": None,
                    }
                )

    elif strategy == "bayesian":
        if not check_optuna_installed():
            errors.append(
                "베이지안 최적화(strategy='bayesian')는 현재 optuna 패키지가 설치되어 있지 않아 미지원됩니다. 'grid' 또는 'random' 전략을 선택하세요."
            )
            return configs, errors, warnings
        warnings.append("Optuna 베이지안 탐색 공간이 활성화되었습니다.")
        # Fallback expansion for preview
        params = dict(base_params)
        for k, v in search_space.items():
            params[k] = v[0] if isinstance(v, list) and v else v
        configs.append(
            {
                "label": "Bayesian Trial #1 (Initial)",
                "role": "candidate",
                "order_index": 0,
                "parameters": params,
                "config_hash": canonical_json_hash(params),
                "ablation_factor": None,
            }
        )

    elif strategy == "ablation":
        # Full configuration (all features active)
        full_params = dict(base_params)
        ablation_candidates: list[tuple[str, Any, Any]] = []  # (key, active_val, drop_val)

        for k, v in search_space.items():
            if isinstance(v, list) and len(v) == 2:
                # e.g. [True, False]
                full_params[k] = v[0]
                ablation_candidates.append((k, v[0], v[1]))
            elif isinstance(v, bool):
                full_params[k] = True
                ablation_candidates.append((k, True, False))
            elif isinstance(v, list) and v:
                full_params[k] = v[0]
                if len(v) > 1:
                    ablation_candidates.append((k, v[0], v[1]))
            else:
                full_params[k] = v

        configs.append(
            {
                "label": "Full Configuration (Baseline)",
                "role": "baseline",
                "order_index": 0,
                "parameters": full_params,
                "config_hash": canonical_json_hash(full_params),
                "ablation_factor": None,
            }
        )

        for idx, (k, active_val, drop_val) in enumerate(ablation_candidates):
            if len(configs) >= max_runs:
                warnings.append(
                    f"Ablation 요소가 많아 상한({max_runs})에서 잘렸습니다."
                )
                break
            drop_params = dict(full_params)
            drop_params[k] = drop_val
            configs.append(
                {
                    "label": f"Ablation: Drop {k}",
                    "role": "ablation_drop",
                    "order_index": idx + 1,
                    "parameters": drop_params,
                    "config_hash": canonical_json_hash(drop_params),
                    "ablation_factor": k,
                }
            )

    else:
        errors.append(f"알 수 없는 실험 탐색 전략: '{strategy}'")

    return configs, errors, warnings


async def unknown_catalog_method_ids(db, method_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Ids in `method_ids` with no catalog Method row, in first-seen order."""
    wanted = list(dict.fromkeys(method_ids))
    if not wanted:
        return []
    found = set((await db.scalars(select(Method.id).where(Method.id.in_(wanted)))).all())
    return [mid for mid in wanted if mid not in found]


def unknown_method_message(ids: list[uuid.UUID]) -> str:
    return "research_catalog_method_not_found: " + ", ".join(str(i) for i in ids)


async def local_readiness_issues(
    db,
    method_ids: list[uuid.UUID],
    *,
    adapter_id: str | None,
    scope: str,
) -> list[str]:
    """Messages for catalog methods that cannot run locally (on `adapter_id` when given)."""
    from ttakkari.research.readiness import compute_execution_status

    issues: list[str] = []
    for mid in method_ids:
        m_obj = await db.get(Method, mid)
        if m_obj is None:
            continue
        status = compute_execution_status(m_obj, scope=scope, adapter_id=adapter_id)
        if not status.is_executable:
            label = m_obj.name_ko or m_obj.name or str(mid)
            issues.append(
                f"방법론 '{label}'은(는) 로컬 자동 실행을 지원하지 않습니다: {status.unavailable_reason}"
            )
    return issues


async def validate_experiment_plan(
    db,
    *,
    study_id: uuid.UUID,
    method_id: uuid.UUID | None,
    compared_method_ids: list[uuid.UUID],
    adapter_id: str,
    strategy: str,
    search_space: dict[str, Any],
    fixed_inputs: dict[str, Any],
    budget: dict[str, Any],
    seed: int = 42,
    scope: str = "personal",
) -> PlanValidationResult:
    """Validate full experiment definition against budget, adapter constraints, and combination relations."""
    errors: list[str] = []
    warnings: list[str] = []

    # Budget limits
    max_runs_limit = 12 if adapter_id == "llm_inference" else 50
    requested_max_runs = budget.get("max_runs", 20)
    if requested_max_runs > max_runs_limit:
        errors.append(
            f"요청된 max_runs({requested_max_runs})가 어댑터 '{adapter_id}'의 하드 상한({max_runs_limit})을 초과합니다."
        )

    requested_seconds = budget.get("max_total_seconds", 300)
    if requested_seconds > 3600:
        errors.append(
            f"요청된 max_total_seconds({requested_seconds})가 최대 허용 시간(3600초)을 초과합니다."
        )

    # Check method relations
    all_methods = []
    if method_id:
        all_methods.append(method_id)
    all_methods.extend(compared_method_ids)

    # A missing catalog row would otherwise skip the readiness check and dispatch.
    missing = await unknown_catalog_method_ids(db, all_methods)
    if missing:
        errors.append(unknown_method_message(missing))

    rel_errors, rel_warnings = await validate_method_combinations(db, all_methods)
    errors.extend(rel_errors)
    warnings.extend(rel_warnings)

    study = await db.get(Study, study_id)
    if study is not None:
        scope = study.scope

    # Design validity and local execution readiness are separate. Manual external
    # reports can carry hypotheses and methods without a local adapter, but every
    # method a local adapter would run (primary and compared) must be executable.
    local_dispatch = adapter_id != "external_report"
    for message in await local_readiness_issues(
        db, all_methods, adapter_id=adapter_id if local_dispatch else None, scope=scope
    ):
        (errors if local_dispatch else warnings).append(message)

    # Scope validation
    if scope == "work" and adapter_id == "llm_inference":
        errors.append(
            "회사(work) 범위에서는 호스트 GPU(llm_inference) 실행기를 사용할 수 없습니다."
        )

    # Expand configurations
    expanded_configs, exp_errors, exp_warnings = expand_configurations(
        strategy=strategy,
        search_space=search_space,
        fixed_inputs=fixed_inputs,
        max_runs=requested_max_runs,
        seed=seed,
    )
    errors.extend(exp_errors)
    warnings.extend(exp_warnings)

    # Validate expanded configurations against adapter constraints
    if local_dispatch:
        if not adapter_id:
            errors.append("실행 어댑터 ID가 지정되지 않았습니다.")
        else:
            from ttakkari.research.adapters import get_adapter_registry

            adapter = None
            try:
                adapter = get_adapter_registry().get_adapter(adapter_id)
            except Exception as exc:
                logger.warning(
                    "Adapter registry lookup for '%s' failed: %s",
                    adapter_id,
                    type(exc).__name__,
                )
                errors.append(
                    f"어댑터 '{adapter_id}' 조회 중 오류가 발생했습니다: {type(exc).__name__}"
                )

            if adapter is None:
                if not any(f"'{adapter_id}'" in e for e in errors):
                    errors.append(f"알 수 없는 실행 어댑터입니다: '{adapter_id}'")
            else:
                for cfg in expanded_configs:
                    params_dict = cfg.get("parameters", cfg)
                    try:
                        cfg_errors = adapter.validate_config(params_dict, scope=scope)
                        for ce in cfg_errors:
                            if ce not in errors:
                                errors.append(ce)
                    except Exception as exc:
                        logger.warning(
                            "Adapter '%s' validate_config failed: %s",
                            adapter_id,
                            type(exc).__name__,
                        )
                        err_msg = f"어댑터 '{adapter_id}' 설정 검증 중 오류가 발생했습니다: {type(exc).__name__}"
                        if err_msg not in errors:
                            errors.append(err_msg)

    # Resource estimate
    runs_count = len(expanded_configs)
    if strategy == "repeat":
        runs_count *= budget.get("repetitions", 2)

    est_sec_per_run = 15 if adapter_id == "llm_inference" else 3 if adapter_id == "retrieval_eval" else 5
    resource_estimate = {
        "total_runs": runs_count,
        # `estimated_seconds` is the contract name (adapters.base.ResourceEstimate); `estimated_total_seconds` stays for older callers.
        "estimated_seconds": runs_count * est_sec_per_run,
        "estimated_total_seconds": runs_count * est_sec_per_run,
        "estimated_memory_mb": 4096 if adapter_id == "llm_inference" else 512,
        "requires_gpu_lease": adapter_id == "llm_inference",
        "requires_tts_lock": adapter_id == "tts_synthesis",
        "requires_cpu_lock": adapter_id == "retrieval_eval",
    }

    return PlanValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        expanded_configurations=expanded_configs,
        resource_estimate=resource_estimate,
    )
