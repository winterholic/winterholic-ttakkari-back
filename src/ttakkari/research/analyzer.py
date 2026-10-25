"""Statistical comparison, Pareto frontier calculation, ablation impacts, and chart series generator for Research Lab v2."""

from __future__ import annotations

import json
import statistics
import uuid
from typing import Any

from fastapi import HTTPException
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


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _pareto_series(
    *,
    x_key: str | None,
    y_key: str,
    explicit_x: bool,
    metric_meta: dict[str, dict[str, Any]],
    points_for_pareto: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pareto points and frontier for two metrics, or an `unavailable` status that says why."""

    def unavailable(reason: str, x: str | None = None) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "unavailable_reason": reason,
            "x_metric": x,
            "y_metric": y_key,
            "points": [],
            "frontier": [],
        }

    measured = list(metric_meta)
    if explicit_x and x_key == y_key:
        return unavailable(
            f"가로축과 세로축이 같은 지표({y_key})입니다. 다목적 Pareto 분석에는 서로 다른 2개 지표가 필요합니다.",
            x_key,
        )
    if x_key is None or x_key == y_key:
        # No usable x requested (none, or the default equals y): any other measured metric will do.
        others = [m for m in measured if m != y_key]
        x_key = others[0] if others else None
    if x_key is None or x_key == y_key:
        return unavailable(
            f"다목적 Pareto 분석에는 서로 다른 2개 이상의 지표가 필요합니다 "
            f"(현재 유효 지표: {len(measured)}개{': ' + ', '.join(measured) if measured else ''}). "
            f"단일 지표 순위(delta_bars/metric_bars)를 참고하세요."
        )
    missing = [k for k in (x_key, y_key) if k not in metric_meta]
    if missing:
        return unavailable(
            f"지표({', '.join(missing)})의 유효한 측정 데이터가 부족하여 Pareto 분석을 생성할 수 없습니다.",
            x_key,
        )

    x_meta, y_meta = metric_meta[x_key], metric_meta[y_key]
    points, frontier = find_pareto_frontier(
        points=points_for_pareto,
        x_metric=x_key,
        y_metric=y_key,
        x_higher_is_better=x_meta.get("direction", "lower_is_better") == "higher_is_better",
        y_higher_is_better=y_meta.get("direction", "higher_is_better") == "higher_is_better",
    )
    if not points:
        return unavailable(
            f"두 지표({x_key}, {y_key})가 모두 측정된 설정이 없어 Pareto 분석을 생성할 수 없습니다.",
            x_key,
        )
    for p in points + frontier:
        p["configuration_id"] = p["config_id"]
        p["x_metric"] = x_key
        p["y_metric"] = y_key
        p["is_pareto_optimal"] = any(f["config_id"] == p["config_id"] for f in frontier)
    return {
        "status": "available",
        "unavailable_reason": None,
        "x_metric": x_key,
        "y_metric": y_key,
        "x_label": x_meta.get("label", x_key),
        "y_label": y_meta.get("label", y_key),
        "x_unit": x_meta.get("unit", ""),
        "y_unit": y_meta.get("unit", ""),
        "x_direction": x_meta.get("direction", "lower_is_better"),
        "y_direction": y_meta.get("direction", "higher_is_better"),
        "points": points,
        "frontier": frontier,
    }


async def build_compare_series(
    db,
    experiment_id: uuid.UUID,
    baseline_config_id: uuid.UUID | None = None,
    primary_metric: str | None = None,
    secondary_metric: str | None = None,
    metrics_filter: list[str] | None = None,
    pareto_x_metric: str | None = None,
    pareto_y_metric: str | None = None,
) -> dict[str, Any]:
    """Build normalized chart-ready JSON structures for frontend SVG rendering.

    Per-metric series (`delta_bars`, `metric_bars`, `repeat_distributions`, `sweep_lines`, `ablation_impacts`)
    cover every measured metric, primary first; `metrics_filter` narrows them. Only succeeded runs and
    the `overall` slice are counted, one value per run and metric.
    """
    exp = await db.get(Experiment, experiment_id)
    if not exp:
        raise HTTPException(404, "research_experiment_not_found")

    p_metric = primary_metric or exp.primary_metric or "recall_at_k"

    configs = (
        await db.execute(
            select(Configuration)
            .where(Configuration.experiment_id == experiment_id)
            .order_by(Configuration.order_index)
        )
    ).scalars().all()

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
                select(Measurement)
                .where(Measurement.run_id.in_(run_ids), Measurement.slice_key == "overall")
                .order_by(Measurement.created_at, Measurement.id)
            )
        ).scalars().all()

    # configuration_id -> metric_key -> one value per run
    cfg_metrics: dict[uuid.UUID, dict[str, list[float]]] = {}
    metric_meta: dict[str, dict[str, Any]] = {}
    seen_run_metric: set[tuple[uuid.UUID, str]] = set()
    for m in measurements:
        if (m.run_id, m.metric_key) in seen_run_metric:
            continue
        seen_run_metric.add((m.run_id, m.metric_key))
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

    # Measured metrics, primary first
    ordered = ([p_metric] if p_metric in metric_meta else []) + [k for k in metric_meta if k != p_metric]
    if metrics_filter:
        ordered = [k for k in ordered if k in metrics_filter]

    def values_of(cfg_id: uuid.UUID | None, key: str) -> list[float]:
        return cfg_metrics.get(cfg_id, {}).get(key, []) if cfg_id else []

    def meta_fields(key: str) -> dict[str, Any]:
        meta = metric_meta[key]
        return {
            "metric_key": key,
            "metric_label": meta["label"],
            "unit": meta["unit"],
            "direction": meta["direction"],
            "is_proxy": meta["is_proxy"],
        }

    delta_bars: list[dict[str, Any]] = []
    metric_bars: list[dict[str, Any]] = []
    repeat_distributions: list[dict[str, Any]] = []
    for key in ordered:
        higher_is_better = metric_meta[key]["direction"] == "higher_is_better"
        baseline_mean = _mean(values_of(baseline_id, key))
        bar_values = []
        for c in configs:
            dist = compute_distribution(values_of(c.id, key))
            c_mean = dist["mean"] if dist["n"] > 0 else None
            abs_delta = None
            rel_delta_pct = None
            is_improvement = None
            if c_mean is not None and baseline_mean is not None:
                abs_delta = c_mean - baseline_mean
                if abs(baseline_mean) > 1e-9:
                    rel_delta_pct = (abs_delta / abs(baseline_mean)) * 100.0
                if c.id != baseline_id:
                    is_improvement = (abs_delta > 0) if higher_is_better else (abs_delta < 0)
            delta_bars.append({
                **meta_fields(key),
                "configuration_id": str(c.id),
                "config_id": str(c.id),
                "label": c.label,
                "role": c.role,
                "is_baseline": c.id == baseline_id,
                "value": c_mean,
                "baseline_value": baseline_mean,
                "abs_delta": abs_delta,
                "rel_delta_pct": rel_delta_pct,
                "is_improvement": is_improvement,
                "n": dist["n"],
            })
            bar_values.append({
                "configuration_id": str(c.id),
                "config_id": str(c.id),
                "label": c.label,
                "role": c.role,
                "value": c_mean,
                "n": dist["n"],
            })
            if dist["n"] > 0:
                repeat_distributions.append({
                    **meta_fields(key),
                    "configuration_id": str(c.id),
                    "config_id": str(c.id),
                    "label": c.label,
                    "role": c.role,
                    "n": dist["n"],
                    "mean": dist["mean"],
                    "median": dist["median"],
                    "std": dist["std"],
                    "min": dist["min"],
                    "max": dist["max"],
                    "values": list(values_of(c.id, key)),
                    "distribution": dist,
                })
        metric_bars.append({**meta_fields(key), "values": bar_values})

    # Older per-configuration shape of `metric_bars`: {config, metrics: {key: mean}}
    metric_bars_by_config = [
        {
            "config_id": str(c.id),
            "label": c.label,
            "role": c.role,
            "metrics": {k: _mean(v) for k, v in cfg_metrics.get(c.id, {}).items() if k in ordered and v},
        }
        for c in configs
    ]

    # Best configuration on the primary metric
    p_higher = metric_meta.get(p_metric, {}).get("direction", "higher_is_better") == "higher_is_better"
    best_cfg_id = None
    best_p_val = None
    for c in configs:
        c_mean = _mean(values_of(c.id, p_metric))
        if c_mean is None:
            continue
        if best_p_val is None or (c_mean > best_p_val if p_higher else c_mean < best_p_val):
            best_p_val = c_mean
            best_cfg_id = c.id

    # Pareto: x = pareto_x (or the first secondary metric), y = pareto_y (or the primary metric)
    explicit_x = pareto_x_metric or secondary_metric
    x_request = explicit_x or ((exp.secondary_metrics or [None])[0])
    pareto = _pareto_series(
        x_key=x_request,
        y_key=pareto_y_metric or p_metric,
        explicit_x=bool(explicit_x),
        metric_meta=metric_meta,
        points_for_pareto=[
            {
                "config_id": str(c.id),
                "label": c.label,
                "role": c.role,
                "metrics": {k: _mean(v) for k, v in cfg_metrics.get(c.id, {}).items() if v},
            }
            for c in configs
        ],
    )
    pareto_frontier = pareto.pop("frontier")
    pareto_points = pareto["points"]
    sec_metric = pareto["x_metric"] if pareto["status"] == "available" else None
    sec_meta = metric_meta.get(sec_metric, {}) if sec_metric else {}

    # Ablation impacts: positive means removing the factor made the metric worse
    ablation_impacts: list[dict[str, Any]] = []
    for c in configs:
        if c.role != "ablation_drop":
            continue
        for key in ordered:
            higher_is_better = metric_meta[key]["direction"] == "higher_is_better"
            full_value = _mean(values_of(baseline_id, key))
            drop_value = _mean(values_of(c.id, key))
            if (full_value is None or drop_value is None) and key != p_metric:
                continue
            impact = None
            rel_impact_pct = None
            if full_value is not None and drop_value is not None:
                impact = (full_value - drop_value) if higher_is_better else (drop_value - full_value)
                if abs(full_value) > 1e-9:
                    rel_impact_pct = impact / abs(full_value) * 100.0
            ablation_impacts.append({
                **meta_fields(key),
                "factor": c.ablation_factor or c.label,
                "configuration_id": str(c.id),
                "config_id": str(c.id),
                "drop_label": c.label,
                "baseline_value": full_value,
                "full_value": full_value,
                "drop_value": drop_value,
                "impact": impact,
                "rel_impact_pct": rel_impact_pct,
            })

    # Parameter difference table
    all_keys = set()
    for c in configs:
        all_keys.update(c.parameters.keys())
    sorted_keys = sorted(all_keys)
    varying_keys = [
        k for k in sorted_keys
        if len({json.dumps(c.parameters.get(k), sort_keys=True) for c in configs}) > 1
    ]
    config_diff_rows = [
        {
            "configuration_id": str(c.id),
            "config_id": str(c.id),
            "label": c.label,
            "role": c.role,
            "values": c.parameters,
            "varying_parameters": {k: c.parameters.get(k) for k in varying_keys},
        }
        for c in configs
    ]

    # Sweep lines: a numeric parameter with 3+ distinct values, or the only parameter that varies
    sweep_lines: list[dict[str, Any]] = []
    for param in varying_keys:
        groups: dict[str, list[Configuration]] = {}
        for c in configs:
            groups.setdefault(json.dumps(c.parameters.get(param), sort_keys=True), []).append(c)
        first_values = [g[0].parameters.get(param) for g in groups.values()]
        numeric = all(_is_number(v) for v in first_values)
        if not ((numeric and len(groups) >= 3) or len(varying_keys) == 1):
            continue
        group_list = list(groups.values())
        if numeric:
            group_list.sort(key=lambda g: g[0].parameters.get(param))
        for key in ordered:
            points = []
            for g in group_list:
                vals = [v for c in g for v in values_of(c.id, key)]
                y = _mean(vals)
                points.append({
                    "x": g[0].parameters.get(param),
                    "y": y,
                    "n": len(vals),
                    "configuration_id": str(g[0].id) if len(g) == 1 else None,
                    "param_value": g[0].parameters.get(param),
                    "metric_value": y,
                    "config_label": g[0].label,
                })
            if any(p["y"] is not None for p in points) or key == p_metric:
                sweep_lines.append({
                    **meta_fields(key),
                    "parameter": param,
                    "param_key": param,
                    "points": points,
                })

    metrics_catalog = {k: dict(v) for k, v in metric_meta.items()}
    return {
        "experiment_id": str(experiment_id),
        "primary_metric": p_metric,
        "primary_metric_meta": metric_meta.get(p_metric, {}),
        "secondary_metric": sec_metric,
        "secondary_metric_meta": sec_meta,
        "baseline_configuration_id": str(baseline_id) if baseline_id else None,
        "best_configuration_id": str(best_cfg_id) if best_cfg_id else None,
        "metrics": [{"metric_key": k, **meta_fields(k)} for k in ordered],
        "delta_bars": delta_bars,
        "metric_bars": metric_bars,
        "metric_bars_by_config": metric_bars_by_config,
        "repeat_distributions": repeat_distributions,
        "pareto": pareto,
        "pareto_points": pareto_points,
        "pareto_frontier": pareto_frontier,
        "pareto_status": pareto["status"],
        "pareto_unavailable_reason": pareto["unavailable_reason"],
        "ablation_impacts": ablation_impacts,
        "config_diff_table": {
            "keys": sorted_keys,
            "changed_keys": varying_keys,
            "rows": config_diff_rows,
        },
        "config_diff_rows": config_diff_rows,
        "sweep_lines": sweep_lines,
        "metrics_catalog": metrics_catalog,
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
        pareto_x_metric=pareto_x_metric,
        pareto_y_metric=pareto_y_metric,
    )

    p_metric = series["primary_metric"]
    p_meta = series["primary_metric_meta"]
    best_id_str = series["best_configuration_id"]
    best_cfg_id = uuid.UUID(best_id_str) if best_id_str else None
    base_id_str = series["baseline_configuration_id"]
    base_cfg_id = uuid.UUID(base_id_str) if base_id_str else None

    # Generate recommendation text
    recommendation_ko = "측정 데이터가 충분하지 않습니다."
    best_delta = next(
        (
            d
            for d in series["delta_bars"]
            if d["config_id"] == best_id_str and d["metric_key"] == p_metric
        ),
        None,
    )
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
