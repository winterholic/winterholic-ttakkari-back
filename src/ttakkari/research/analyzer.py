"""Statistical comparison, Pareto frontier calculation, ablation impacts, and chart series generator for Research Lab v2."""

from __future__ import annotations

import json
import math
import statistics
import uuid
from typing import Any

from sqlalchemy import select

from ttakkari.models import utcnow
from ttakkari.research.models import (
    Analysis,
    Configuration,
    Experiment,
    Measurement,
    Run,
)


def compute_distribution(values: list[float]) -> dict[str, Any]:
    """Compute mean, median, min, max, std for repeated measurements."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0, "std": None, "variability_status": "데이터 없음"}
    if n == 1:
        v = values[0]
        return {
            "n": 1,
            "mean": v,
            "median": v,
            "min": v,
            "max": v,
            "std": None,
            "variability_status": "변동성 미상 (n < 2)",
        }
    mean_val = statistics.mean(values)
    median_val = statistics.median(values)
    min_val = min(values)
    max_val = max(values)
    std_val = statistics.stdev(values) if n > 1 else None
    return {
        "n": n,
        "mean": mean_val,
        "median": median_val,
        "min": min_val,
        "max": max_val,
        "std": std_val,
        "variability_status": "측정 완료",
    }


def find_pareto_frontier(
    points: list[dict[str, Any]],
    x_metric: str,
    y_metric: str,
    x_higher_is_better: bool = False,
    y_higher_is_better: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute Pareto optimal frontier for 2 metrics."""
    valid_points = []
    for p in points:
        x_val = p.get("metrics", {}).get(x_metric)
        y_val = p.get("metrics", {}).get(y_metric)
        if x_val is not None and y_val is not None:
            valid_points.append(dict(p, x=float(x_val), y=float(y_val)))

    if not valid_points:
        return [], []

    frontier = []
    for candidate in valid_points:
        dominated = False
        for other in valid_points:
            if other["config_id"] == candidate["config_id"]:
                continue
            # Check if other dominates candidate
            other_x_better = (other["x"] >= candidate["x"]) if x_higher_is_better else (other["x"] <= candidate["x"])
            other_y_better = (other["y"] >= candidate["y"]) if y_higher_is_better else (other["y"] <= candidate["y"])
            other_strictly_better = ((other["x"] > candidate["x"]) if x_higher_is_better else (other["x"] < candidate["x"])) or \
                                    ((other["y"] > candidate["y"]) if y_higher_is_better else (other["y"] < candidate["y"]))

            if other_x_better and other_y_better and other_strictly_better:
                dominated = True
                break

        if not dominated:
            frontier.append(candidate)

    # Sort frontier by X for plotting
    frontier.sort(key=lambda p: p["x"], reverse=x_higher_is_better)
    frontier_ids = {p["config_id"] for p in frontier}

    annotated_points = []
    for p in valid_points:
        p_copy = dict(p)
        p_copy["is_pareto"] = p["config_id"] in frontier_ids
        annotated_points.append(p_copy)

    return annotated_points, frontier


async def build_compare_series(
    db,
    experiment_id: uuid.UUID,
    baseline_config_id: uuid.UUID | None = None,
    primary_metric: str | None = None,
    secondary_metric: str | None = None,
    metrics_filter: list[str] | None = None,
) -> dict[str, Any]:
    """Build normalized chart-ready JSON structures for frontend SVG rendering."""
    exp = await db.get(Experiment, experiment_id)
    if not exp:
        raise ValueError(f"Experiment {experiment_id} not found")

    p_metric = primary_metric or exp.primary_metric or "recall_at_k"

    # Get all configurations
    configs = (
        await db.execute(
            select(Configuration)
            .where(Configuration.experiment_id == experiment_id)
            .order_by(Configuration.order_index)
        )
    ).scalars().all()

    # Get succeeded runs
    runs = (
        await db.execute(
            select(Run).where(
                Run.experiment_id == experiment_id,
                Run.status == "succeeded",
            )
        )
    ).scalars().all()

    run_ids = [r.id for r in runs]
    measurements = []
    if run_ids:
        measurements = (
            await db.execute(
                select(Measurement).where(Measurement.run_id.in_(run_ids))
            )
        ).scalars().all()

    # Group measurements by configuration_id -> metric_key -> list[value]
    cfg_metrics: dict[uuid.UUID, dict[str, list[float]]] = {}
    metric_meta: dict[str, dict[str, Any]] = {}

    for m in measurements:
        cfg_metrics.setdefault(m.configuration_id, {}).setdefault(m.metric_key, []).append(m.value)
        if m.metric_key not in metric_meta:
            metric_meta[m.metric_key] = {
                "label": m.metric_label,
                "unit": m.unit,
                "direction": m.direction,
                "is_proxy": m.is_proxy,
            }

    # Determine baseline config
    baseline_cfg = None
    if baseline_config_id:
        baseline_cfg = next((c for c in configs if c.id == baseline_config_id), None)
    if not baseline_cfg and exp.baseline_configuration_id:
        baseline_cfg = next((c for c in configs if c.id == exp.baseline_configuration_id), None)
    if not baseline_cfg:
        baseline_cfg = next((c for c in configs if c.role == "baseline"), None)
    if not baseline_cfg and configs:
        baseline_cfg = configs[0]

    baseline_id = baseline_cfg.id if baseline_cfg else None
    baseline_p_vals = cfg_metrics.get(baseline_id, {}).get(p_metric, []) if baseline_id else []
    baseline_p_mean = statistics.mean(baseline_p_vals) if baseline_p_vals else None

    direction = metric_meta.get(p_metric, {}).get("direction", "higher_is_better")
    higher_is_better = direction == "higher_is_better"

    # 1. Delta bars (relative change vs baseline on primary_metric)
    delta_bars: list[dict[str, Any]] = []
    # 2. Metric bars (grouped values across configurations)
    metric_bars: list[dict[str, Any]] = []
    # 3. Repeat distributions (mean/median/min/max/std/n)
    repeat_distributions: list[dict[str, Any]] = []
    # 4. Points for Pareto
    points_for_pareto: list[dict[str, Any]] = []

    best_cfg_id = None
    best_p_val = None

    for c in configs:
        c_vals_map = cfg_metrics.get(c.id, {})
        c_p_vals = c_vals_map.get(p_metric, [])
        dist = compute_distribution(c_p_vals)
        c_mean = dist["mean"] if dist["n"] > 0 else None

        # Determine best
        if c_mean is not None:
            if best_p_val is None:
                best_p_val = c_mean
                best_cfg_id = c.id
            elif higher_is_better and c_mean > best_p_val:
                best_p_val = c_mean
                best_cfg_id = c.id
            elif not higher_is_better and c_mean < best_p_val:
                best_p_val = c_mean
                best_cfg_id = c.id

        abs_delta = None
        rel_delta_pct = None
        is_improvement = None
        if c_mean is not None and baseline_p_mean is not None:
            abs_delta = c_mean - baseline_p_mean
            if abs(baseline_p_mean) > 1e-9:
                rel_delta_pct = (abs_delta / abs(baseline_p_mean)) * 100.0
            is_improvement = (abs_delta > 0) if higher_is_better else (abs_delta < 0)

        delta_bars.append({
            "config_id": str(c.id),
            "label": c.label,
            "role": c.role,
            "value": c_mean,
            "baseline_value": baseline_p_mean,
            "abs_delta": abs_delta,
            "rel_delta_pct": rel_delta_pct,
            "is_improvement": is_improvement,
            "unit": metric_meta.get(p_metric, {}).get("unit", ""),
        })

        # Metric bars summary
        metrics_summary = {}
        for m_key, vals in c_vals_map.items():
            if metrics_filter and m_key not in metrics_filter:
                continue
            metrics_summary[m_key] = statistics.mean(vals) if vals else None

        metric_bars.append({
            "config_id": str(c.id),
            "label": c.label,
            "role": c.role,
            "metrics": metrics_summary,
        })

        repeat_distributions.append({
            "config_id": str(c.id),
            "label": c.label,
            "metric_key": p_metric,
            "distribution": dist,
        })

        points_for_pareto.append({
            "config_id": str(c.id),
            "label": c.label,
            "role": c.role,
            "metrics": {k: statistics.mean(v) for k, v in c_vals_map.items() if v},
        })

    # Pareto calculation
    # Determine secondary metric
    sec_metrics = exp.secondary_metrics or []
    sec_metric = secondary_metric or (sec_metrics[0] if sec_metrics else None)

    # If the chosen sec_metric is identical to p_metric, do not use it
    if sec_metric == p_metric:
        sec_metric = None

    if not sec_metric:
        # Pick another metric if available in metric_meta (excluding p_metric)
        candidates = [m for m in metric_meta.keys() if m != p_metric]
        sec_metric = candidates[0] if candidates else None

    # Check if we have two distinct metrics with valid data
    pareto_points: list[dict[str, Any]] = []
    pareto_frontier: list[dict[str, Any]] = []
    pareto_status: str
    pareto_unavailable_reason: str | None

    if not sec_metric or sec_metric == p_metric:
        sec_metric = None
        sec_meta = {}
        pareto_status = "unavailable"
        pareto_unavailable_reason = (
            f"다목적 Pareto 분석에는 서로 다른 2개 이상의 지표가 필요합니다 "
            f"(현재 유효 지표: {p_metric} 1개). 단일 지표 순위(delta_bars/metric_bars)를 참고하세요."
        )
    else:
        sec_meta = metric_meta.get(sec_metric, {})
        sec_higher_is_better = sec_meta.get("direction", "lower_is_better") == "higher_is_better"
        pareto_points, pareto_frontier = find_pareto_frontier(
            points=points_for_pareto,
            x_metric=sec_metric,
            y_metric=p_metric,
            x_higher_is_better=sec_higher_is_better,
            y_higher_is_better=higher_is_better,
        )
        if not pareto_points:
            pareto_status = "unavailable"
            pareto_unavailable_reason = (
                f"선택된 보조 지표({sec_metric})의 유효한 측정 데이터가 부족하여 Pareto 분석을 생성할 수 없습니다."
            )
        else:
            pareto_status = "available"
            pareto_unavailable_reason = None

    # 5. Ablation impacts
    ablation_impacts: list[dict[str, Any]] = []
    for c in configs:
        if c.role == "ablation_drop" and c.ablation_factor:
            c_p_vals = cfg_metrics.get(c.id, {}).get(p_metric, [])
            drop_mean = statistics.mean(c_p_vals) if c_p_vals else None
            impact = None
            if baseline_p_mean is not None and drop_mean is not None:
                # If higher is better: baseline - drop represents contribution of that factor
                impact = (baseline_p_mean - drop_mean) if higher_is_better else (drop_mean - baseline_p_mean)
            ablation_impacts.append({
                "factor": c.ablation_factor,
                "config_id": str(c.id),
                "drop_label": c.label,
                "baseline_value": baseline_p_mean,
                "drop_value": drop_mean,
                "impact": impact,
                "unit": metric_meta.get(p_metric, {}).get("unit", ""),
            })

    # 6. Parameter difference table
    all_keys = set()
    for c in configs:
        all_keys.update(c.parameters.keys())

    varying_keys = []
    for k in sorted(all_keys):
        vals = [json.dumps(c.parameters.get(k), sort_keys=True) for c in configs]
        if len(set(vals)) > 1:
            varying_keys.append(k)

    config_diff_table = []
    for c in configs:
        diff_row = {
            "config_id": str(c.id),
            "label": c.label,
            "role": c.role,
            "varying_parameters": {k: c.parameters.get(k) for k in varying_keys},
        }
        config_diff_table.append(diff_row)

    # 7. Sweep lines (if grid/sweep on a single parameter)
    sweep_lines: list[dict[str, Any]] = []
    if len(varying_keys) == 1:
        v_key = varying_keys[0]
        line_points = []
        for c in configs:
            v_val = c.parameters.get(v_key)
            c_p_vals = cfg_metrics.get(c.id, {}).get(p_metric, [])
            c_mean = statistics.mean(c_p_vals) if c_p_vals else None
            line_points.append({
                "param_value": v_val,
                "metric_value": c_mean,
                "config_label": c.label,
            })
        sweep_lines.append({
            "param_key": v_key,
            "metric_key": p_metric,
            "points": line_points,
        })

    return {
        "experiment_id": str(experiment_id),
        "primary_metric": p_metric,
        "primary_metric_meta": metric_meta.get(p_metric, {}),
        "secondary_metric": sec_metric,
        "secondary_metric_meta": sec_meta,
        "baseline_configuration_id": str(baseline_id) if baseline_id else None,
        "best_configuration_id": str(best_cfg_id) if best_cfg_id else None,
        "delta_bars": delta_bars,
        "metric_bars": metric_bars,
        "repeat_distributions": repeat_distributions,
        "pareto_points": pareto_points,
        "pareto_frontier": pareto_frontier,
        "pareto_status": pareto_status,
        "pareto_unavailable_reason": pareto_unavailable_reason,
        "ablation_impacts": ablation_impacts,
        "config_diff_table": config_diff_table,
        "sweep_lines": sweep_lines,
        "metrics_catalog": metric_meta,
    }


async def analyze_experiment(
    db,
    experiment_id: uuid.UUID,
    baseline_configuration_id: uuid.UUID | None = None,
    pareto_x_metric: str | None = None,
    pareto_y_metric: str | None = None,
) -> Analysis:
    """Run full comparative analysis, generate deterministic Korean recommendation, and upsert research_analyses row."""
    series = await build_compare_series(
        db,
        experiment_id=experiment_id,
        baseline_config_id=baseline_configuration_id,
        primary_metric=pareto_y_metric,
        secondary_metric=pareto_x_metric,
    )

    p_metric = series["primary_metric"]
    p_meta = series["primary_metric_meta"]
    best_id_str = series["best_configuration_id"]
    best_cfg_id = uuid.UUID(best_id_str) if best_id_str else None
    base_id_str = series["baseline_configuration_id"]
    base_cfg_id = uuid.UUID(base_id_str) if base_id_str else None

    # Generate recommendation text
    recommendation_ko = "측정 데이터가 충분하지 않습니다."
    best_delta = next((d for d in series["delta_bars"] if d["config_id"] == best_id_str), None)
    if best_delta and best_delta["value"] is not None:
        val_str = f"{best_delta['value']:.3f} {p_meta.get('unit', '')}".strip()
        label = best_delta["label"]
        rel_str = f"{best_delta['rel_delta_pct']:+.1f}%" if best_delta["rel_delta_pct"] is not None else "기준"
        recommendation_ko = (
            f"주요 지표({p_meta.get('label', p_metric)}) 기준, 최적 설정은 '{label}'({val_str})입니다. "
            f"Baseline 대비 상대 변화율은 {rel_str}입니다."
        )
        if series["pareto_frontier"]:
            frontier_labels = [p["label"] for p in series["pareto_frontier"]]
            recommendation_ko += f" (파레토 최적 후보: {', '.join(frontier_labels[:3])})"

    analysis_row = (
        await db.execute(
            select(Analysis).where(Analysis.experiment_id == experiment_id)
        )
    ).scalar_one_or_none()

    if analysis_row:
        analysis_row.baseline_configuration_id = base_cfg_id
        analysis_row.best_configuration_id = best_cfg_id
        analysis_row.primary_metric = p_metric
        analysis_row.summary = series
        analysis_row.recommendation_ko = recommendation_ko
        analysis_row.created_at = utcnow()
    else:
        analysis_row = Analysis(
            id=uuid.uuid4(),
            experiment_id=experiment_id,
            baseline_configuration_id=base_cfg_id,
            best_configuration_id=best_cfg_id,
            primary_metric=p_metric,
            summary=series,
            recommendation_ko=recommendation_ko,
            created_at=utcnow(),
        )
        db.add(analysis_row)

    await db.flush()
    return analysis_row
