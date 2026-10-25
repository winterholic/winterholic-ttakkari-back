"""API contract tests for Research Lab v2 (docs/research-lab/refactor-v2.md §7.1).

Each test pins a response shape the frontend relies on. Data goes in through the real endpoints
(methods, experiments, external reports); no result is injected by mocking.
"""

import uuid

import pytest

from ttakkari.db import sessionmaker
from ttakkari.research.models import Run

API = "/api/research"


async def make_study(client, auth, scope="personal"):
    r = await client.post(
        f"{API}/studies",
        headers=auth,
        json={"title": f"Contract {uuid.uuid4().hex[:6]}", "goal": "계약 검증", "scope": scope, "criteria": []},
    )
    assert r.status_code == 201
    return r.json()["id"]


async def make_method(client, auth, *, targets, layers=(), goals=(), slug_prefix="ct"):
    r = await client.post(
        f"{API}/methods",
        headers=auth,
        json={
            "slug": f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
            "name": "Contract method",
            "name_ko": "계약 검증 방법",
            "targets": list(targets),
            "layers": list(layers),
            "goals": list(goals),
            "lifecycle_status": "hypothesis",
        },
    )
    assert r.status_code == 201
    return r.json()


async def make_experiment(client, auth, study_id, **body):
    payload = {"title": "Contract experiment", "primary_metric": "decode_tps", **body}
    r = await client.post(f"{API}/studies/{study_id}/experiments", headers=auth, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def report(client, auth, exp_id, cfg_id, metrics, provenance="external_benchmark"):
    r = await client.post(
        f"{API}/experiments/{exp_id}/external-report",
        headers=auth,
        json={
            "configuration_id": cfg_id,
            "provenance": provenance,
            # Synthetic fixture identifiers exercise provenance transport, not a paper claim.
            "source_title": "Synthetic API contract fixture",
            "source_url": "https://example.invalid/research-contract-fixture",
            "metrics": [
                {"metric_key": k, "metric_label": k, "value": v, "unit": u, "direction": d}
                for k, v, u, d in metrics
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_external_benchmark_requires_source_and_persists_it_across_requests(client, auth):
    """Exercise the real ASGI endpoint and PostgreSQL, beyond the StubDb regression."""
    from sqlalchemy import func, select

    from ttakkari.research.models import Measurement

    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report", strategy="single")
    body = {
        "configuration_id": exp["configurations"][0]["id"],
        "provenance": "external_benchmark",
        "metrics": [{"metric_key": "decode_tps", "value": 12.0}],
    }
    for fields in ({}, {"source_title": "  ", "source_url": "https://example.invalid/fixture"}):
        rejected = await client.post(f"{API}/experiments/{exp['id']}/external-report", headers=auth, json={**body, **fields})
        assert rejected.status_code == 422, rejected.text
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(Run).where(Run.experiment_id == uuid.UUID(exp["id"]))) == 0

    fields = {"source_title": "Synthetic roundtrip fixture", "source_url": "https://example.invalid/roundtrip"}
    saved = await client.post(f"{API}/experiments/{exp['id']}/external-report", headers=auth, json={**body, **fields})
    assert saved.status_code == 201, saved.text
    run_id = uuid.UUID(saved.json()["id"])
    async with sessionmaker()() as db:
        stored = await db.get(Run, run_id)
        assert stored is not None and stored.execution_origin == "external_report"
        assert stored.raw_output["source_title"] == fields["source_title"]
        assert stored.environment_snapshot["source_url"] == fields["source_url"]
        measurements = (await db.scalars(select(Measurement).where(Measurement.run_id == run_id))).all()
        assert len(measurements) == 1
        assert measurements[0].meta["provenance"] == "external_benchmark"
        assert measurements[0].meta["source_title"] == fields["source_title"]

    reloaded = await client.get(f"{API}/experiments/{exp['id']}", headers=auth)
    assert reloaded.status_code == 200, reloaded.text
    stored_run = next(r for r in reloaded.json()["runs"] if r["id"] == str(run_id))
    assert stored_run["source_title"] == fields["source_title"]
    assert stored_run["source_url"] == fields["source_url"]
    assert stored_run["provenance"] == "external_benchmark"


@pytest.mark.parametrize("location,field,value", [
    ("raw_output", "source_title", "Another paper"),
    ("environment_snapshot", "source_url", "https://example.invalid/another"),
    ("environment_snapshot", "source_locator", "doi:10.1234/another"),
    ("meta", "source_title", "Another metric source"),
    ("meta", "source_year", 2023),
    ("raw_output", "provenance", "manual_evaluation"),
    ("environment_snapshot", "provenance", "manual_evaluation"),
    ("environment_snapshot", "execution_origin", "local_execution"),
])
async def test_external_report_rejects_conflicting_source_before_persistence(client, auth, location, field, value):
    from sqlalchemy import func, select

    from ttakkari.research.models import Measurement

    sid = await make_study(client, auth)
    exp = await make_experiment(client, auth, sid, adapter_id="external_report", strategy="single")
    body = {
        "configuration_id": exp["configurations"][0]["id"],
        "provenance": "external_benchmark",
        "source_title": "Synthetic canonical source",
        "source_url": "https://example.invalid/canonical",
        "source_year": 2026,
        "metrics": [{"metric_key": "decode_tps", "value": 12.0}],
    }
    if location == "meta":
        body["metrics"][0]["meta"] = {field: value}
    else:
        body[location] = {field: value}
    response = await client.post(f"{API}/experiments/{exp['id']}/external-report", headers=auth, json=body)
    assert response.status_code == 422, response.text
    assert "conflict" in response.text and field in response.text
    async with sessionmaker()() as db:
        eid = uuid.UUID(exp["id"])
        assert await db.scalar(select(func.count()).select_from(Run).where(Run.experiment_id == eid)) == 0
        assert await db.scalar(select(func.count()).select_from(Measurement).where(Measurement.experiment_id == eid)) == 0


async def test_external_report_canonical_source_survives_fresh_read(client, auth):
    from sqlalchemy import select

    from ttakkari.research.models import Measurement

    sid = await make_study(client, auth)
    exp = await make_experiment(client, auth, sid, adapter_id="external_report", strategy="single")
    source = {"source_title": "Synthetic canonical source", "source_url": "doi:10.1234/synthetic", "source_year": 2026}
    body = {
        "configuration_id": exp["configurations"][0]["id"],
        "provenance": "external_benchmark",
        "raw_output": {"source_title": source["source_title"], "source_locator": source["source_url"], "source_year": 2026},
        "environment_snapshot": {"source_title": "", "hardware": "synthetic"},
        "metrics": [{"metric_key": "decode_tps", "value": 12.0, "meta": {"slice_note": "preserve me"}}],
    }
    saved = await client.post(f"{API}/experiments/{exp['id']}/external-report", headers=auth, json=body)
    assert saved.status_code == 201, saved.text
    rid = uuid.UUID(saved.json()["id"])
    reloaded = await client.get(f"{API}/runs/{rid}", headers=auth)
    assert reloaded.status_code == 200, reloaded.text
    async with sessionmaker()() as db:
        run = await db.get(Run, rid)
        measurement = await db.scalar(select(Measurement).where(Measurement.run_id == rid))
        for stored in (reloaded.json(), run.raw_output, run.environment_snapshot, measurement.meta):
            for key, value in source.items():
                assert stored[key] == value
            assert stored["provenance"] == "external_benchmark"
        assert measurement.meta["slice_note"] == "preserve me"
        assert run.environment_snapshot["hardware"] == "synthetic"


SPEED = ("decode_tps", "tok/s", "higher_is_better")
LATENCY = ("latency_ms", "ms", "lower_is_better")
MEMORY = ("vram_mb", "MB", "lower_is_better")


def point(spec, value):
    key, unit, direction = spec
    return (key, value, unit, direction)


# ---- 1. 방법 검색: 축 안 OR, 축 사이 AND -------------------------------------------------


async def test_method_search_ors_values_within_an_axis_and_ands_across_axes(client, auth):
    tag = uuid.uuid4().hex[:6]
    a, b, c = f"zt_a_{tag}", f"zt_b_{tag}", f"zt_c_{tag}"
    la, lb = f"zl_a_{tag}", f"zl_b_{tag}"
    m_a = await make_method(client, auth, targets=[a], layers=[la])
    m_b = await make_method(client, auth, targets=[b], layers=[lb])
    m_c = await make_method(client, auth, targets=[c], layers=[la])

    async def ids(query: str):
        r = await client.get(f"{API}/methods?{query}&limit=100", headers=auth)
        assert r.status_code == 200
        return {m["id"] for m in r.json()["items"]}, r.json()

    one, _ = await ids(f"target={a}")
    assert one == {m_a["id"]}

    # 쉼표 OR 과 반복 파라미터 OR 은 같은 결과
    comma, body = await ids(f"target={a},{b}")
    repeated, _ = await ids(f"target={a}&target={b}")
    assert comma == repeated == {m_a["id"], m_b["id"]}
    assert body["total"] == 2

    # 축 사이는 AND: target(a|b) 이면서 layer(la) → m_a 만
    both, _ = await ids(f"target={a},{b},{c}&layer={la}")
    assert both == {m_a["id"], m_c["id"]}
    narrowed, _ = await ids(f"target={a},{b}&layer={la}")
    assert narrowed == {m_a["id"]}

    # 값 하나가 없는 코드여도 나머지 값은 걸러내지 않는다
    partial, _ = await ids(f"target={a},nonexistent_{tag}")
    assert partial == {m_a["id"]}

    # 수명 주기·실행 가능 상태도 여러 값
    both_status, _ = await ids(f"target={a},{b}&lifecycle_status=hypothesis,validated")
    assert both_status == {m_a["id"], m_b["id"]}
    none_status, _ = await ids(f"target={a},{b}&lifecycle_status=validated,deprecated")
    assert none_status == set()
    by_exec, _ = await ids(f"target={a},{b}&execution_status=hypothesis_stage,tool_integrated")
    assert by_exec == {m_a["id"], m_b["id"]}
    no_exec, _ = await ids(f"target={a},{b}&execution_status=immediately_executable")
    assert no_exec == set()


async def test_catalog_multi_target_equals_union_of_single_targets(client, auth):
    """실제 카탈로그에서도 llm,tts 는 llm 과 tts 의 합집합이다."""

    async def ids(query: str):
        got: set[str] = set()
        offset = 0
        while True:
            r = await client.get(f"{API}/methods?{query}&limit=100&offset={offset}", headers=auth)
            assert r.status_code == 200
            body = r.json()
            got |= {m["id"] for m in body["items"]}
            offset += 100
            if offset >= body["total"]:
                return got

    llm, tts, both = await ids("target=llm"), await ids("target=tts"), await ids("target=llm,tts")
    assert llm and tts
    assert both == llm | tts


async def test_catalog_method_ids_are_stable_across_read_requests(client, auth):
    """읽기 요청의 지연 시드 적재가 커밋되지 않으면 요청마다 새 id 가 생겨, 목록에서 고른 방법으로 실험을 만들 수 없다."""
    first = (await client.get(f"{API}/methods?target=llm&limit=100", headers=auth)).json()["items"]
    second = (await client.get(f"{API}/methods?target=llm&limit=100", headers=auth)).json()["items"]
    assert first and {m["id"] for m in first} == {m["id"] for m in second}
    # /taxonomy 가 먼저 불려도 사전이 비지 않는다
    assert (await client.get(f"{API}/taxonomy", headers=auth)).json()["axes"]["target"]

    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, method_id=first[0]["id"], adapter_id="external_report")
    assert exp["experiment"]["method_id"] == first[0]["id"]


# ---- 4. facets 키: 단수형이 계약, 복수형은 호환 --------------------------------------------


async def test_facets_use_singular_axis_keys_and_keep_plural_aliases(client, auth):
    tag = uuid.uuid4().hex[:6]
    code = f"zf_{tag}"
    await make_method(client, auth, targets=[code], layers=[f"zfl_{tag}"], goals=[f"zfg_{tag}"])
    body = (await client.get(f"{API}/methods?target={code}", headers=auth)).json()
    facets = body["facets"]

    for singular, plural in (("target", "targets"), ("layer", "layers"), ("goal", "goals"), ("intervention", "interventions"), ("research_method", "research_methods")):
        assert singular in facets, f"{singular} facet missing"
        assert facets[singular] == facets[plural]
    assert facets["target"] == {code: 1}
    assert facets["layer"] == {f"zfl_{tag}": 1}
    assert facets["execution_status"] == {"hypothesis_stage": 1}
    assert facets["lifecycle_status"] == {"hypothesis": 1}
    # /taxonomy 의 축 이름과 같은 키를 쓴다
    taxonomy_axes = (await client.get(f"{API}/taxonomy", headers=auth)).json()["axes"]
    assert {"target", "layer", "goal", "intervention", "research_method"} <= set(taxonomy_axes)
    assert {"target", "layer", "goal", "intervention", "research_method"} <= set(facets)


async def test_method_list_items_carry_local_measurements_and_detail_resolves_relations(client, auth):
    tag = uuid.uuid4().hex[:6]
    src = await make_method(client, auth, targets=[f"zr_{tag}"])
    dst = await make_method(client, auth, targets=[f"zr_{tag}"])
    rel = await client.post(
        f"{API}/methods/{src['id']}/relations",
        headers=auth,
        json={"to_method_id": dst["id"], "relation_type": "combines_with", "note": "함께 쓴다"},
    )
    assert rel.status_code == 201

    items = (await client.get(f"{API}/methods?target=zr_{tag}", headers=auth)).json()["items"]
    assert items and all(
        set(i["local_measurements"]) >= {"total_runs", "succeeded_runs", "failed_runs", "measured_run_count", "locally_measured"}
        for i in items
    )
    assert all(i["local_measurements"]["measured_run_count"] == 0 for i in items)

    detail = (await client.get(f"{API}/methods/{src['slug']}", headers=auth)).json()
    (relation,) = detail["relations"]
    assert relation["from_method_id"] == src["id"]
    assert relation["to_method"] == {
        "id": dst["id"],
        "slug": dst["slug"],
        "name": dst["name"],
        "name_ko": dst["name_ko"],
        "execution_status": "hypothesis_stage",
    }
    # 평평한 필드는 이전 호출자를 위해 남는다
    assert relation["to_method_slug"] == dst["slug"]
    assert detail["local_measurements"]["measured_run_count"] == 0


async def test_local_measurements_count_only_locally_measured_runs(client, auth):
    """외부 보고는 측정 횟수에 넣지 않는다."""
    study_id = await make_study(client, auth)
    method = await make_method(client, auth, targets=[f"zm_{uuid.uuid4().hex[:6]}"])
    exp = await make_experiment(client, auth, study_id, method_id=method["id"], adapter_id="external_report")
    await report(client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 10.0)])

    detail = (await client.get(f"{API}/methods/{method['slug']}", headers=auth)).json()
    lm = detail["local_measurements"]
    assert lm["total_runs"] == 1 and lm["succeeded_runs"] == 1
    assert lm["measured_run_count"] == 0 and lm["locally_measured"] is False

    listed = (await client.get(f"{API}/methods?q={method['slug']}", headers=auth)).json()["items"]
    assert listed[0]["local_measurements"] == lm


# ---- 2. 실험 상세: experiment 래퍼와 run별 measurements ---------------------------------------


async def test_experiment_detail_wraps_experiment_and_includes_measurements(client, auth):
    study_id = await make_study(client, auth)
    created = await make_experiment(
        client, auth, study_id, strategy="ab", search_space={"threads": [4, 8]}, adapter_id="external_report", secondary_metrics=["latency_ms"]
    )
    # 생성 응답도 같은 래퍼를 가진다
    assert created["experiment"]["id"] == created["id"]
    assert created["experiment"]["name"] == "Contract experiment"

    base, treat = created["configurations"][0], created["configurations"][1]
    r1 = await report(client, auth, created["id"], base["id"], [point(SPEED, 30.0), point(LATENCY, 50.0)])
    r2 = await report(client, auth, created["id"], treat["id"], [point(SPEED, 45.0), point(LATENCY, 40.0)])

    detail = (await client.get(f"{API}/experiments/{created['id']}", headers=auth)).json()

    exp = detail["experiment"]
    assert exp["id"] == created["id"]
    assert exp["name"] == exp["title"] == "Contract experiment"
    assert exp["adapter_id"] == "external_report" and exp["strategy"] == "ab"
    assert exp["primary_metric"] == "decode_tps" and exp["secondary_metrics"] == ["latency_ms"]
    # 이전 호출자를 위한 평평한 필드
    assert detail["id"] == created["id"] and detail["title"] == "Contract experiment"

    assert len(detail["configurations"]) == 2
    assert {r["id"] for r in detail["runs"]} == {r1["id"], r2["id"]}

    # 최상위 measurements: 모든 run 의 측정 행. 행마다 run_id 와 configuration_id 가 있다
    flat = detail["measurements"]
    assert len(flat) == 4
    assert {(m["run_id"], m["metric_key"]): m["value"] for m in flat} == {
        (r1["id"], "decode_tps"): 30.0,
        (r1["id"], "latency_ms"): 50.0,
        (r2["id"], "decode_tps"): 45.0,
        (r2["id"], "latency_ms"): 40.0,
    }
    assert {m["configuration_id"] for m in flat} == {base["id"], treat["id"]}

    # run 안에도 자기 측정이 들어 있어 GET /runs/{id} 를 따로 부를 필요가 없다
    by_run = {r["id"]: r for r in detail["runs"]}
    assert {m["metric_key"] for m in by_run[r1["id"]]["measurements"]} == {"decode_tps", "latency_ms"}
    single = (await client.get(f"{API}/runs/{r1['id']}", headers=auth)).json()
    assert sorted(m["id"] for m in single["measurements"]) == sorted(m["id"] for m in by_run[r1["id"]]["measurements"])

    assert detail["latest_analysis"] is None and detail["artifacts"] == []


async def test_experiment_detail_without_runs_has_empty_measurements(client, auth):
    study_id = await make_study(client, auth)
    created = await make_experiment(client, auth, study_id)
    detail = (await client.get(f"{API}/experiments/{created['id']}", headers=auth)).json()
    assert detail["measurements"] == [] and detail["runs"] == []
    assert detail["experiment"]["name"] == "Contract experiment"
    missing = await client.get(f"{API}/experiments/{uuid.uuid4()}", headers=auth)
    assert missing.status_code == 404


# ---- 3. compare-series: 다중 지표 ----------------------------------------------------------


async def seeded_compare(client, auth):
    """ab 실험: 설정 2개, 지표 3개(속도·지연·메모리), 처리군은 속도·지연이 좋고 메모리가 나쁘다."""
    study_id = await make_study(client, auth)
    exp = await make_experiment(
        client, auth, study_id, strategy="ab", search_space={"threads": [4, 8]}, adapter_id="external_report", secondary_metrics=["latency_ms"]
    )
    base, treat = exp["configurations"]
    await report(client, auth, exp["id"], base["id"], [point(SPEED, 30.0), point(LATENCY, 50.0), point(MEMORY, 1000.0)])
    await report(client, auth, exp["id"], treat["id"], [point(SPEED, 45.0), point(LATENCY, 40.0), point(MEMORY, 1500.0)])
    return exp, base, treat


async def get_series(client, auth, exp_id, query=""):
    r = await client.get(f"{API}/experiments/{exp_id}/compare-series{query}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def test_compare_series_covers_every_measured_metric_primary_first(client, auth):
    exp, base, treat = await seeded_compare(client, auth)
    series = await get_series(client, auth, exp["id"], f"?baseline_config_id={base['id']}")

    assert [m["metric_key"] for m in series["metrics"]] == ["decode_tps", "latency_ms", "vram_mb"]
    assert series["primary_metric"] == "decode_tps"

    # delta_bars: (설정, 지표)마다 한 행, 주요 지표가 앞
    rows = {(d["configuration_id"], d["metric_key"]): d for d in series["delta_bars"]}
    assert len(series["delta_bars"]) == 6
    assert series["delta_bars"][0]["metric_key"] == "decode_tps"
    speed = rows[(treat["id"], "decode_tps")]
    assert speed["rel_delta_pct"] == pytest.approx(50.0)
    assert speed["abs_delta"] == pytest.approx(15.0)
    assert speed["direction"] == "higher_is_better" and speed["unit"] == "tok/s"
    assert speed["metric_label"] == "decode_tps" and speed["is_baseline"] is False
    assert speed["is_improvement"] is True
    # 낮을수록 좋은 지표: 값이 줄면 개선
    assert rows[(treat["id"], "latency_ms")]["rel_delta_pct"] == pytest.approx(-20.0)
    assert rows[(treat["id"], "latency_ms")]["is_improvement"] is True
    # 메모리는 늘었으므로 퇴보
    assert rows[(treat["id"], "vram_mb")]["rel_delta_pct"] == pytest.approx(50.0)
    assert rows[(treat["id"], "vram_mb")]["is_improvement"] is False
    assert rows[(base["id"], "decode_tps")]["is_baseline"] is True
    # 이전 필드는 유지
    assert speed["config_id"] == treat["id"]

    # metric_bars: 설정별 dict 가 아니라 지표별 묶음
    bars = {g["metric_key"]: g for g in series["metric_bars"]}
    assert set(bars) == {"decode_tps", "latency_ms", "vram_mb"}
    group = bars["vram_mb"]
    assert group["unit"] == "MB" and group["direction"] == "lower_is_better" and group["is_proxy"] is False
    assert {(v["configuration_id"], v["value"], v["n"]) for v in group["values"]} == {(base["id"], 1000.0, 1), (treat["id"], 1500.0, 1)}
    # 설정별 dict 모양은 별도 키로 남는다
    by_config = {c["config_id"]: c for c in series["metric_bars_by_config"]}
    assert by_config[treat["id"]]["metrics"] == {"decode_tps": 45.0, "latency_ms": 40.0, "vram_mb": 1500.0}

    # repeat_distributions: (설정, 지표)마다 평평한 통계
    dist = {(d["configuration_id"], d["metric_key"]): d for d in series["repeat_distributions"]}
    assert len(series["repeat_distributions"]) == 6
    one = dist[(treat["id"], "decode_tps")]
    assert one["n"] == 1 and one["mean"] == 45.0 and one["std"] is None and one["values"] == [45.0]
    assert one["distribution"]["n"] == 1

    # config_diff_table: 달라진 키만 changed_keys 에
    diff = series["config_diff_table"]
    assert "threads" in diff["keys"] and diff["changed_keys"] == ["threads"]
    assert {r["configuration_id"] for r in diff["rows"]} == {base["id"], treat["id"]}
    assert {r["values"]["threads"] for r in diff["rows"]} == {4, 8}
    assert series["config_diff_rows"] == diff["rows"]


async def test_compare_series_metrics_filter_narrows_every_per_metric_series(client, auth):
    exp, base, _ = await seeded_compare(client, auth)
    series = await get_series(client, auth, exp["id"], f"?baseline_config_id={base['id']}&metrics=decode_tps,vram_mb")
    assert [m["metric_key"] for m in series["metrics"]] == ["decode_tps", "vram_mb"]
    for key in ("delta_bars", "metric_bars", "repeat_distributions"):
        assert {d["metric_key"] for d in series[key]} == {"decode_tps", "vram_mb"}, key
    # 필터는 보이는 지표만 줄인다. 채택 기준(주요 지표)은 그대로
    assert series["primary_metric"] == "decode_tps"


async def test_compare_series_pareto_axes_and_unavailable_reason(client, auth):
    exp, base, treat = await seeded_compare(client, auth)

    series = await get_series(client, auth, exp["id"], f"?baseline_config_id={base['id']}&pareto_x_metric=vram_mb&pareto_y_metric=decode_tps")
    assert series["pareto_status"] == "available" and series["pareto_unavailable_reason"] is None
    pareto = series["pareto"]
    assert pareto["status"] == "available"
    assert (pareto["x_metric"], pareto["y_metric"]) == ("vram_mb", "decode_tps")
    assert (pareto["x_label"], pareto["y_label"]) == ("vram_mb", "decode_tps")
    assert (pareto["x_unit"], pareto["y_unit"]) == ("MB", "tok/s")
    assert (pareto["x_direction"], pareto["y_direction"]) == ("lower_is_better", "higher_is_better")
    by_cfg = {p["configuration_id"]: p for p in pareto["points"]}
    assert (by_cfg[base["id"]]["x"], by_cfg[base["id"]]["y"]) == (1000.0, 30.0)
    assert (by_cfg[treat["id"]]["x"], by_cfg[treat["id"]]["y"]) == (1500.0, 45.0)
    # 메모리는 처리군이 더 쓰고 속도는 더 빨라 서로 밀리지 않는다 → 둘 다 파레토 최적
    assert by_cfg[base["id"]]["is_pareto_optimal"] and by_cfg[treat["id"]]["is_pareto_optimal"]
    assert series["pareto_points"] == pareto["points"]
    assert len(series["pareto_frontier"]) == 2

    # 같은 지표를 두 축에 놓으면 그리지 않고 이유를 준다
    same = await get_series(client, auth, exp["id"], "?pareto_x_metric=decode_tps&pareto_y_metric=decode_tps")
    assert same["pareto_status"] == "unavailable" and same["pareto"]["points"] == []
    assert "같은 지표" in same["pareto_unavailable_reason"]

    # 측정되지 않은 지표를 고르면 이유를 준다
    unknown = await get_series(client, auth, exp["id"], "?pareto_x_metric=nope&pareto_y_metric=decode_tps")
    assert unknown["pareto_status"] == "unavailable" and "nope" in unknown["pareto_unavailable_reason"]


async def test_compare_series_single_metric_reports_pareto_unavailable_instead_of_empty(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    await report(client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 30.0)])
    series = await get_series(client, auth, exp["id"])

    assert series["pareto_status"] == "unavailable"
    assert "2개" in series["pareto_unavailable_reason"]
    assert series["pareto"] == {
        "status": "unavailable",
        "unavailable_reason": series["pareto_unavailable_reason"],
        "x_metric": None,
        "y_metric": "decode_tps",
        "points": [],
    }
    assert series["pareto_points"] == [] and series["pareto_frontier"] == []
    # 단일 지표 순위는 그대로 나온다
    assert [d["metric_key"] for d in series["delta_bars"]] == ["decode_tps"]


async def test_compare_series_repeat_distribution_and_sweep_lines(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(
        client, auth, study_id, strategy="grid", search_space={"threads": [2, 4, 8]}, adapter_id="external_report", secondary_metrics=["latency_ms"]
    )
    cfgs = exp["configurations"]
    assert len(cfgs) == 3
    speeds = {2: [10.0, 14.0], 4: [20.0], 8: [32.0]}
    for cfg in cfgs:
        for v in speeds[cfg["parameters"]["threads"]]:
            await report(client, auth, exp["id"], cfg["id"], [point(SPEED, v), point(LATENCY, 100.0 / v)])

    series = await get_series(client, auth, exp["id"])
    dist = {d["configuration_id"]: d for d in series["repeat_distributions"] if d["metric_key"] == "decode_tps"}
    two = dist[next(c["id"] for c in cfgs if c["parameters"]["threads"] == 2)]
    assert two["n"] == 2 and two["mean"] == 12.0 and two["median"] == 12.0
    assert two["min"] == 10.0 and two["max"] == 14.0 and two["std"] == pytest.approx(2.8284, rel=1e-3)
    assert two["values"] == [10.0, 14.0]

    lines = {(s["parameter"], s["metric_key"]): s for s in series["sweep_lines"]}
    assert set(lines) == {("threads", "decode_tps"), ("threads", "latency_ms")}
    pts = lines[("threads", "decode_tps")]["points"]
    assert [(p["x"], p["y"], p["n"]) for p in pts] == [(2, 12.0, 2), (4, 20.0, 1), (8, 32.0, 1)]
    assert lines[("threads", "decode_tps")]["unit"] == "tok/s"


async def test_compare_series_ablation_impacts_for_each_metric(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(
        client, auth, study_id, strategy="ablation", search_space={"flash_attn": [True, False]}, adapter_id="external_report", secondary_metrics=["latency_ms"]
    )
    full, drop = exp["configurations"]
    assert drop["role"] == "ablation_drop" and drop["ablation_factor"] == "flash_attn"
    await report(client, auth, exp["id"], full["id"], [point(SPEED, 40.0), point(LATENCY, 25.0)])
    await report(client, auth, exp["id"], drop["id"], [point(SPEED, 30.0), point(LATENCY, 35.0)])

    series = await get_series(client, auth, exp["id"], f"?baseline_config_id={full['id']}")
    impacts = {a["metric_key"]: a for a in series["ablation_impacts"]}
    assert set(impacts) == {"decode_tps", "latency_ms"}
    speed = impacts["decode_tps"]
    assert speed["factor"] == "flash_attn"
    assert (speed["full_value"], speed["drop_value"], speed["impact"]) == (40.0, 30.0, 10.0)
    assert speed["rel_impact_pct"] == pytest.approx(25.0)
    # 낮을수록 좋은 지표: 요소를 빼면 지연이 늘어 나빠졌으므로 기여는 양수
    latency = impacts["latency_ms"]
    assert (latency["full_value"], latency["drop_value"]) == (25.0, 35.0)
    assert latency["impact"] == 10.0 and latency["unit"] == "ms"


async def test_compare_series_ignores_failed_runs_and_non_overall_slices(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    cfg = exp["configurations"][0]
    await report(client, auth, exp["id"], cfg["id"], [point(SPEED, 30.0)])
    sliced = await client.post(
        f"{API}/experiments/{exp['id']}/external-report",
        headers=auth,
        json={
            "configuration_id": cfg["id"],
            "provenance": "manual_evaluation",
            "metrics": [
                {"metric_key": "decode_tps", "value": 999.0, "slice_key": "query:7", "direction": "higher_is_better"},
                {"metric_key": "decode_tps", "value": 50.0, "direction": "higher_is_better"},
            ],
        },
    )
    assert sliced.status_code == 201
    # 실패한 run 의 값은 섞이지 않는다
    sm = sessionmaker()
    async with sm() as db:
        from ttakkari.research.models import Measurement

        failed = Run(
            id=uuid.uuid4(), experiment_id=uuid.UUID(exp["id"]), configuration_id=uuid.UUID(cfg["id"]),
            study_id=uuid.UUID(study_id), adapter_id="external_report", status="failed", error_code="oom",
        )
        db.add(failed)
        await db.flush()
        db.add(Measurement(
            run_id=failed.id, experiment_id=failed.experiment_id, configuration_id=failed.configuration_id,
            metric_key="decode_tps", metric_label="decode_tps", value=1.0, unit="tok/s", direction="higher_is_better",
        ))
        await db.commit()

    series = await get_series(client, auth, exp["id"])
    (dist,) = [d for d in series["repeat_distributions"] if d["metric_key"] == "decode_tps"]
    assert dist["values"] == [30.0, 50.0] and dist["mean"] == 40.0


async def test_compare_series_unknown_experiment_is_404(client, auth):
    r = await client.get(f"{API}/experiments/{uuid.uuid4()}/compare-series", headers=auth)
    assert r.status_code == 404


# ---- 5. 대시보드 ---------------------------------------------------------------------------


async def test_dashboard_items_match_run_and_best_result_shapes(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    cfg = exp["configurations"][0]
    run = await report(client, auth, exp["id"], cfg["id"], [point(SPEED, 30.0)])
    best = (
        await client.post(
            f"{API}/studies/{study_id}/best-results",
            headers=auth,
            json={
                "experiment_id": exp["id"], "configuration_id": cfg["id"], "run_id": run["id"],
                "objective": "속도", "primary_metric": "decode_tps", "rationale": "가장 빠름", "status": "adopted",
            },
        )
    ).json()
    rejected = (
        await client.post(
            f"{API}/studies/{study_id}/best-results",
            headers=auth,
            json={
                "experiment_id": exp["id"], "configuration_id": cfg["id"], "run_id": run["id"],
                "objective": "메모리", "primary_metric": "vram_mb", "rationale": "기준 미달", "status": "rejected",
            },
        )
    ).json()

    dash = (await client.get(f"{API}/dashboard?study_id={study_id}", headers=auth)).json()

    (recent,) = dash["recent_runs"]
    assert recent["id"] == recent["run_id"] == run["id"]
    assert recent["study_id"] == study_id and recent["experiment_id"] == exp["id"]
    assert recent["configuration_id"] == cfg["id"]
    assert recent["execution_origin"] == "external_report" and recent["status"] == "succeeded"
    assert recent["experiment_name"] == recent["experiment_title"] == "Contract experiment"
    assert recent["configuration_label"] == cfg["label"]
    assert {"repeat_index", "progress", "error_code", "error_message", "started_at", "finished_at"} <= set(recent)
    assert "raw_output" not in recent and "environment_snapshot" not in recent

    by_status = {b["status"]: b for b in dash["best_results"]}
    assert set(by_status) == {"adopted", "rejected"}
    adopted = by_status["adopted"]
    assert adopted["id"] == adopted["best_result_id"] == best["id"]
    assert (adopted["experiment_id"], adopted["configuration_id"], adopted["run_id"]) == (exp["id"], cfg["id"], run["id"])
    assert adopted["decision_node_id"] == best["decision_node"]["id"]
    assert adopted["objective"] == "속도" and adopted["experiment_name"] == "Contract experiment"
    assert by_status["rejected"]["id"] == rejected["id"] and by_status["rejected"]["rationale"] == "기준 미달"

    assert dash["counts"]["completed_runs"] >= 1
    assert isinstance(dash["counts"]["executable_methods"], int)


async def test_dashboard_host_readiness_comes_from_adapters_not_constants(client, auth):
    from ttakkari.research.adapters.registry import get_adapter_registry

    registered = {a.id for a in get_adapter_registry().list_adapters()}
    dash = (await client.get(f"{API}/dashboard", headers=auth)).json()
    ready = dash["host_readiness"]
    assert set(ready) == registered and {"llm_inference", "retrieval_eval", "tts_synthesis", "external_report"} <= registered
    for adapter_id, info in ready.items():
        assert info["adapter_id"] == adapter_id
        assert isinstance(info["ready"], bool)
        assert {"name_ko", "version", "execution_mode", "reason", "available_models", "details", "supported_parameters", "required_inputs"} <= set(info)
    assert ready["external_report"]["ready"] is True and ready["external_report"]["execution_mode"] == "manual"
    # 회사 범위에서는 호스트 GPU 어댑터가 어댑터 자신의 판단으로 막힌다
    work = (await client.get(f"{API}/dashboard?scope=work", headers=auth)).json()["host_readiness"]
    assert work["llm_inference"]["ready"] is False and "personal" in work["llm_inference"]["reason"]


async def test_dashboard_executable_methods_counts_runnable_catalog_entries(client, auth):
    methods = (await client.get(f"{API}/methods?limit=100", headers=auth)).json()
    dash = (await client.get(f"{API}/dashboard", headers=auth)).json()
    counts = dash["counts"]
    assert counts["methods"] >= methods["total"] > 0
    by_status = methods["facets"]["execution_status"]
    full = sum(by_status.get(s, 0) for s in ("immediately_executable", "tool_integrated"))
    # 일부 지원도 실행은 허용되지만(§3) 완전 지원과는 따로 센다
    assert counts["fully_executable_methods"] == full
    assert counts["executable_methods"] == full + by_status.get("partially_supported", 0)


# ---- 실행 목록·취소·채택·보관 응답 ---------------------------------------------------------


async def test_runs_list_names_runs_and_reports_gpu_state_and_locks(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    run = await report(client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 1.0)])

    body = (await client.get(f"{API}/runs?experiment_id={exp['id']}", headers=auth)).json()
    (item,) = body["runs"]
    assert item["id"] == run["id"]
    assert item["experiment_name"] == "Contract experiment"
    assert item["configuration_label"] == exp["configurations"][0]["label"]
    assert item["study_title"].startswith("Contract ")
    assert isinstance(body["active_locks"], list)
    gpu = body["host_gpu_status"]
    assert gpu is None or {"ready", "reason", "available_models", "lease_held"} <= set(gpu)


async def test_runs_list_active_locks_follow_running_runs(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="llm_inference")
    sm = sessionmaker()
    run_id = uuid.uuid4()
    async with sm() as db:
        db.add(Run(
            id=run_id, experiment_id=uuid.UUID(exp["id"]), configuration_id=uuid.UUID(exp["configurations"][0]["id"]),
            study_id=uuid.UUID(study_id), adapter_id="llm_inference", status="running",
        ))
        await db.commit()
    locks = (await client.get(f"{API}/runs?study_id={study_id}", headers=auth)).json()["active_locks"]
    assert {"lock": "host_gpu_lease", "adapter_id": "llm_inference", "run_id": str(run_id), "experiment_id": exp["id"]} in locks


async def test_cancel_run_returns_the_cancelled_run(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    sm = sessionmaker()
    run_id = uuid.uuid4()
    async with sm() as db:
        db.add(Run(
            id=run_id, experiment_id=uuid.UUID(exp["id"]), configuration_id=uuid.UUID(exp["configurations"][0]["id"]),
            study_id=uuid.UUID(study_id), adapter_id="external_report", status="queued",
        ))
        await db.commit()

    r = await client.post(f"{API}/runs/{run_id}/cancel", headers=auth, json={"reason": "사용자 취소"})
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is True and body["run_id"] == str(run_id) and body["status"] == "cancelled"
    assert body["run"]["id"] == str(run_id) and body["run"]["status"] == "cancelled"
    assert body["run"]["error_message"] == "사용자 취소"

    # 이미 끝난 run 은 취소되지 않았다고 알리되 현재 상태를 보여 준다
    again = (await client.post(f"{API}/runs/{run_id}/cancel", headers=auth, json={})).json()
    assert again["cancelled"] is False and again["run"]["status"] == "cancelled"
    missing = await client.post(f"{API}/runs/{uuid.uuid4()}/cancel", headers=auth, json={})
    assert missing.status_code == 404


async def test_external_report_returns_measurements_and_provenance(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    run = await report(
        client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 12.5), point(LATENCY, 80.0)], provenance="manual_evaluation"
    )
    assert run["execution_origin"] == "external_report" and run["provenance"] == "manual_evaluation"
    assert {m["metric_key"]: m["value"] for m in run["measurements"]} == {"decode_tps": 12.5, "latency_ms": 80.0}
    assert all(m["meta"]["provenance"] == "manual_evaluation" for m in run["measurements"])
    detail = (await client.get(f"{API}/runs/{run['id']}", headers=auth)).json()
    assert detail["provenance"] == "manual_evaluation"


async def test_best_result_response_includes_decision_node_and_promoted_method(client, auth):
    study_id = await make_study(client, auth)
    method = await make_method(client, auth, targets=[f"zb_{uuid.uuid4().hex[:6]}"])
    exp = await make_experiment(client, auth, study_id, method_id=method["id"], adapter_id="external_report")
    cfg = exp["configurations"][0]
    run = await report(client, auth, exp["id"], cfg["id"], [point(SPEED, 30.0)])
    payload = {
        "experiment_id": exp["id"], "configuration_id": cfg["id"], "run_id": run["id"],
        "objective": "속도", "primary_metric": "decode_tps", "constraints": {"vram_mb": 2000}, "rationale": "가장 빠름",
    }

    rejected = (await client.post(f"{API}/studies/{study_id}/best-results", headers=auth, json={**payload, "status": "rejected", "promote_method": True})).json()
    assert rejected["status"] == "rejected" and rejected["method"] is None
    assert rejected["method_promotion"] == {"requested": True, "status": "skipped", "reason": "result_not_adopted"}
    assert rejected["decision_node"]["kind"] == "decision" and rejected["decision_node"]["epistemic_status"] == "rejected"

    adopted = (await client.post(f"{API}/studies/{study_id}/best-results", headers=auth, json={**payload, "promote_method": True})).json()
    assert adopted["status"] == "adopted" and adopted["constraints"] == {"vram_mb": 2000}
    assert adopted["decision_node"]["id"] == adopted["decision_node_id"]
    assert adopted["method"]["id"] == method["id"] and adopted["method"]["lifecycle_status"] == "candidate"
    assert adopted["method"]["promoted_from_run_id"] == run["id"]
    assert adopted["method_promotion"]["status"] == "promoted"


async def test_system_seed_best_result_is_adopted_while_method_promotion_is_skipped(client, auth):
    study_id = await make_study(client, auth)
    sync = await client.post(f"{API}/catalog/sync", headers=auth)
    assert sync.status_code == 200, sync.text
    methods = (await client.get(f"{API}/methods?target=rag&limit=100", headers=auth)).json()["items"]
    method = next(item for item in methods if item["origin"] == "system_seed")
    exp = await make_experiment(
        client, auth, study_id, method_id=method["id"], adapter_id="external_report"
    )
    cfg = exp["configurations"][0]
    run = await report(client, auth, exp["id"], cfg["id"], [point(SPEED, 15.0)])

    direct_promotion = await client.post(
        f"{API}/methods/{method['id']}/promote",
        headers=auth,
        json={"run_id": run["id"], "decision_note": "직접 승격 거부 확인"},
    )
    assert direct_promotion.status_code == 422, direct_promotion.text
    assert "system_seed_methods_cannot_be_promoted" in direct_promotion.text

    response = await client.post(
        f"{API}/studies/{study_id}/best-results",
        headers=auth,
        json={
            "experiment_id": exp["id"], "configuration_id": cfg["id"], "run_id": run["id"],
            "objective": "RAG 검색 품질", "primary_metric": "decode_tps", "constraints": {},
            "rationale": "system seed 결과 채택", "promote_method": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "adopted"
    assert body["decision_node"]["conditions"]["method_promotion"] == {
        "requested": True, "status": "skipped", "reason": "system_seed_method"
    }
    assert body["method"] is None

    method_after = (await client.get(f"{API}/methods/{method['id']}", headers=auth)).json()
    assert method_after["lifecycle_status"] == method["lifecycle_status"]
    assert method_after["promoted_from_run_id"] == method["promoted_from_run_id"]


async def test_external_report_only_hypothesis_can_validate_save_and_accept_report(client, auth):
    study_id = await make_study(client, auth)
    method = await make_method(client, auth, targets=[f"zh_{uuid.uuid4().hex[:6]}"])
    validation = await client.post(
        f"{API}/experiments/validate",
        headers=auth,
        json={
            "study_id": study_id, "method_id": method["id"], "adapter_id": "external_report",
            "strategy": "single", "search_space": {}, "fixed_inputs": {},
            "budget": {"max_runs": 1, "max_total_seconds": 60},
        },
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True
    assert validation.json()["warnings"]

    exp = await make_experiment(
        client, auth, study_id, method_id=method["id"], adapter_id="external_report"
    )
    run = await report(client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 8.0)])
    assert run["execution_origin"] == "external_report"


async def test_non_executable_local_method_is_rejected_before_run_dispatch(client, auth):
    study_id = await make_study(client, auth)
    await client.post(f"{API}/catalog/sync", headers=auth)
    methods = (await client.get(f"{API}/methods?q=qlora&limit=100", headers=auth)).json()["items"]
    method = next(item for item in methods if item["slug"] == "qlora")
    exp = await make_experiment(
        client, auth, study_id, method_id=method["id"], adapter_id="llm_inference",
        fixed_inputs={"model": "qwen"},
        budget={"max_runs": 1, "max_total_seconds": 60},
    )

    response = await client.post(f"{API}/experiments/{exp['id']}/run", headers=auth, json={})
    assert response.status_code == 422, response.text
    assert "로컬 자동 실행을 지원하지 않습니다" in response.text
    detail = (await client.get(f"{API}/experiments/{exp['id']}", headers=auth)).json()
    assert detail["runs"] == []


async def test_export_report_returns_artifact_metadata(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    await report(client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 30.0)])
    r = await client.post(f"{API}/studies/{study_id}/export-report", headers=auth, json={"experiment_ids": [exp["id"]], "promote_to_library": False})
    assert r.status_code == 200
    body = r.json()
    assert body["artifact"]["artifact_id"] == body["artifact_id"]
    assert body["artifact"]["role"] == "report_markdown" and body["artifact"]["promoted_to_library"] is False
    assert body["preview"].startswith("# AI Research Lab v2 리포트")


async def test_runs_expose_provenance_wherever_they_are_listed(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(client, auth, study_id, adapter_id="external_report")
    run = await report(client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 1.0)], provenance="imported_log")

    detail = (await client.get(f"{API}/experiments/{exp['id']}", headers=auth)).json()
    assert [r["provenance"] for r in detail["runs"]] == ["imported_log"]
    listed = (await client.get(f"{API}/runs?experiment_id={exp['id']}", headers=auth)).json()["runs"]
    assert [r["provenance"] for r in listed] == ["imported_log"]
    assert run["provenance"] == "imported_log"


async def test_validate_resource_estimate_uses_contract_field_names(client, auth):
    study_id = await make_study(client, auth)
    r = await client.post(
        f"{API}/experiments/validate",
        headers=auth,
        json={"study_id": study_id, "adapter_id": "retrieval_eval", "strategy": "grid", "search_space": {"k": [1, 2, 3]}},
    )
    assert r.status_code == 200
    est = r.json()["resource_estimate"]
    assert est["total_runs"] == 3
    assert est["estimated_seconds"] == est["estimated_total_seconds"] == 9
    assert est["estimated_memory_mb"] > 0 and est["requires_cpu_lock"] is True


# ---- 독립 검증 D2~D4: 로컬 실행 경로는 모두 같은 readiness 검사를 거친다 ----


@pytest.fixture
def batch_calls(monkeypatch):
    from ttakkari.research import runner

    calls = []

    async def fake_batch(experiment_id, run_ids, adapter_registry=None):
        calls.append((str(experiment_id), len(run_ids)))

    monkeypatch.setattr(runner, "run_experiment_batch", fake_batch)
    return calls


async def _system_method(client, auth, slug):
    await client.post(f"{API}/catalog/sync", headers=auth)
    items = (await client.get(f"{API}/methods?q={slug}&limit=100", headers=auth)).json()["items"]
    return next(item for item in items if item["slug"] == slug)


async def _run_count():
    from sqlalchemy import func, select

    from ttakkari.db import sessionmaker
    from ttakkari.research.models import Run

    async with sessionmaker()() as db:
        return (await db.execute(select(func.count()).select_from(Run))).scalar()


async def test_external_report_adapter_experiment_cannot_be_dispatched_as_local_run(client, auth, batch_calls):
    study_id = await make_study(client, auth)
    method = await _system_method(client, auth, "flash_attention")
    exp = await make_experiment(
        client, auth, study_id, method_id=method["id"], adapter_id="external_report",
        fixed_inputs={"provenance": "external_benchmark", "measurements": [{"metric_key": "decode_tps", "value": 99.0}]},
    )
    before = await _run_count()

    response = await client.post(f"{API}/experiments/{exp['id']}/run", headers=auth, json={})

    assert response.status_code == 422, response.text
    assert "external_report" in response.text
    assert await _run_count() == before
    assert batch_calls == []
    # 외부 결과는 external-report API로만, external 출처로 저장된다.
    run = await report(client, auth, exp["id"], exp["configurations"][0]["id"], [point(SPEED, 99.0)])
    assert run["execution_origin"] == "external_report"


async def test_auto_start_does_not_dispatch_external_report_experiment(client, auth, batch_calls):
    study_id = await make_study(client, auth)
    method = await _system_method(client, auth, "qlora")
    before = await _run_count()
    exp = await make_experiment(
        client, auth, study_id, method_id=method["id"], adapter_id="external_report", auto_start=True
    )
    assert exp["runs"] == []
    assert await _run_count() == before
    assert batch_calls == []


async def test_compared_only_non_executable_method_cannot_bypass_local_readiness(client, auth, batch_calls):
    study_id = await make_study(client, auth)
    qlora = await _system_method(client, auth, "qlora")
    body = {
        "study_id": study_id, "compared_method_ids": [qlora["id"]], "adapter_id": "llm_inference",
        "strategy": "single", "search_space": {}, "fixed_inputs": {"model": "qwen"},
        "budget": {"max_runs": 1, "max_total_seconds": 60},
    }
    validation = await client.post(f"{API}/experiments/validate", headers=auth, json=body)
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is False
    assert any("로컬 자동 실행을 지원하지 않습니다" in e for e in validation.json()["errors"])

    exp = await make_experiment(
        client, auth, study_id, compared_method_ids=[qlora["id"]], adapter_id="llm_inference",
        fixed_inputs={"model": "qwen"}, budget={"max_runs": 1, "max_total_seconds": 60},
    )
    before = await _run_count()
    response = await client.post(f"{API}/experiments/{exp['id']}/run", headers=auth, json={})
    assert response.status_code == 422, response.text
    assert await _run_count() == before
    assert batch_calls == []


async def test_primary_executable_with_non_executable_compared_method_is_rejected_for_local_run(client, auth, batch_calls):
    study_id = await make_study(client, auth)
    flash = await _system_method(client, auth, "flash_attention")
    qlora = await _system_method(client, auth, "qlora")
    exp = await make_experiment(
        client, auth, study_id, method_id=flash["id"], compared_method_ids=[qlora["id"]],
        adapter_id="llm_inference", fixed_inputs={"model": "qwen"},
        budget={"max_runs": 1, "max_total_seconds": 60},
    )
    response = await client.post(f"{API}/experiments/{exp['id']}/run", headers=auth, json={})
    assert response.status_code == 422, response.text
    assert batch_calls == []


async def test_legacy_execute_applies_method_readiness_but_keeps_v1_experiments(client, auth, monkeypatch):
    from ttakkari.api import research as research_api

    evaluated = []
    monkeypatch.setattr(research_api.benchmark, "evaluate", lambda cfg, secs: evaluated.append(1) or {"stub": True})
    monkeypatch.setattr(research_api.benchmark, "code_digest", lambda: "stub")

    study_id = await make_study(client, auth)
    qlora = await _system_method(client, auth, "qlora")
    linked = await make_experiment(
        client, auth, study_id, method_id=qlora["id"], adapter_id="retrieval_eval", config={"k": 1}
    )
    blocked = await client.post(f"{API}/experiments/{linked['id']}/execute", headers=auth)
    assert blocked.status_code == 422, blocked.text
    assert "로컬 자동 실행을 지원하지 않습니다" in blocked.text
    assert evaluated == []
    state = (await client.get(f"{API}/experiments/{linked['id']}", headers=auth)).json()
    assert state["state"] == "queued"

    compared_only = await make_experiment(
        client, auth, study_id, compared_method_ids=[qlora["id"]], adapter_id="retrieval_eval", config={"k": 1}
    )
    blocked = await client.post(f"{API}/experiments/{compared_only['id']}/execute", headers=auth)
    assert blocked.status_code == 422, blocked.text
    assert evaluated == []

    # v1 experiment without any method link keeps the legacy behaviour.
    legacy = await make_experiment(client, auth, study_id, adapter_id="retrieval_eval", config={"k": 1})
    ok = await client.post(f"{API}/experiments/{legacy['id']}/execute", headers=auth)
    assert ok.status_code == 200, ok.text
    assert evaluated == [1]


async def _experiment_count():
    from sqlalchemy import func, select

    from ttakkari.db import sessionmaker
    from ttakkari.research.models import Experiment

    async with sessionmaker()() as db:
        return (await db.execute(select(func.count()).select_from(Experiment))).scalar()


async def test_unknown_catalog_method_ids_are_rejected_with_422(client, auth, batch_calls):
    study_id = await make_study(client, auth)
    qlora = await _system_method(client, auth, "qlora")
    ghost = str(uuid.uuid4())
    plan = {
        "study_id": study_id, "adapter_id": "retrieval_eval", "strategy": "single",
        "search_space": {}, "fixed_inputs": {"k": 1}, "budget": {"max_runs": 1, "max_total_seconds": 60},
    }
    experiment = {"title": "ghost method", "primary_metric": "decode_tps", "config": {"k": 1}}
    experiments_before, runs_before = await _experiment_count(), await _run_count()

    for field, value in (
        ("method_id", ghost),
        ("compared_method_ids", [ghost]),
        ("compared_method_ids", [qlora["id"], ghost]),
    ):
        validated = await client.post(f"{API}/experiments/validate", headers=auth, json={**plan, field: value})
        assert validated.status_code == 422, (field, validated.text)
        assert ghost in validated.text

        created = await client.post(f"{API}/studies/{study_id}/experiments", headers=auth, json={**experiment, field: value})
        assert created.status_code == 422, (field, created.text)
        assert "research_catalog_method_not_found" in created.text
        assert ghost in created.text

    assert await _experiment_count() == experiments_before
    assert await _run_count() == runs_before
    assert batch_calls == []


async def test_known_catalog_method_ids_still_pass_validation(client, auth):
    study_id = await make_study(client, auth)
    flash = await _system_method(client, auth, "flash_attention")
    validated = await client.post(
        f"{API}/experiments/validate", headers=auth,
        json={
            "study_id": study_id, "method_id": flash["id"], "adapter_id": "retrieval_eval",
            "strategy": "single", "fixed_inputs": {"k": 1}, "budget": {"max_runs": 1, "max_total_seconds": 60},
        },
    )
    assert validated.status_code == 200, validated.text
    assert not any("research_catalog_method_not_found" in e for e in validated.json()["errors"])


async def test_source_reuse_requires_configuration_id_when_other_source_fields_given(client, auth):
    study_id = await make_study(client, auth)
    random_id = str(uuid.uuid4())

    # Providing source_run_id without source_configuration_id -> 422
    r = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={"title": "No cfg", "source_run_id": random_id},
    )
    assert r.status_code == 422, r.text
    assert "source_configuration_id_required" in r.text

    # Providing source_experiment_id without source_configuration_id -> 422
    r = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={"title": "No cfg", "source_experiment_id": random_id},
    )
    assert r.status_code == 422, r.text
    assert "source_configuration_id_required" in r.text

    # Providing source_study_id without source_configuration_id -> 422
    r = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={"title": "No cfg", "source_study_id": random_id},
    )
    assert r.status_code == 422, r.text
    assert "source_configuration_id_required" in r.text


async def test_source_reuse_rejects_missing_entities(client, auth):
    study_id = await make_study(client, auth)
    random_id = str(uuid.uuid4())

    # Non-existent source_configuration_id -> 404
    r = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={"title": "Missing cfg", "source_configuration_id": random_id},
    )
    assert r.status_code == 404, r.text
    assert "source_configuration_not_found" in r.text


async def test_source_reuse_derives_experiment_and_study_and_rejects_mismatch(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(
        client, auth, study_id,
        config={"k": 5, "retriever_mode": "bm25"},
        adapter_id="retrieval_eval",
    )
    cfg_id = exp["configurations"][0]["id"]
    exp_id = exp["id"]

    # 1. Providing only source_configuration_id -> derives experiment and study
    r = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Derived lineage",
            "source_configuration_id": cfg_id,
            "adapter_id": "retrieval_eval",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["source_configuration_id"] == cfg_id
    assert created["source_experiment_id"] == exp_id
    assert created["source_study_id"] == study_id
    assert created["experiment"]["source_configuration_id"] == cfg_id
    assert created["experiment"]["source_experiment_id"] == exp_id
    assert created["experiment"]["source_study_id"] == study_id

    # 2. Providing mismatched source_experiment_id -> 422
    fake_exp_id = str(uuid.uuid4())
    r_bad_exp = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Mismatch exp",
            "source_configuration_id": cfg_id,
            "source_experiment_id": fake_exp_id,
            "adapter_id": "retrieval_eval",
        },
    )
    assert r_bad_exp.status_code == 422, r_bad_exp.text
    assert "source_experiment_id_mismatch" in r_bad_exp.text

    # 3. Providing mismatched source_study_id -> 422
    fake_std_id = str(uuid.uuid4())
    r_bad_std = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Mismatch study",
            "source_configuration_id": cfg_id,
            "source_study_id": fake_std_id,
            "adapter_id": "retrieval_eval",
        },
    )
    assert r_bad_std.status_code == 422, r_bad_std.text
    assert "source_study_id_mismatch" in r_bad_std.text


async def test_source_reuse_run_lineage_and_allows_failed_run(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(
        client, auth, study_id,
        config={"k": 5},
        adapter_id="retrieval_eval",
    )
    cfg_id = exp["configurations"][0]["id"]
    exp_id = exp["id"]

    # Insert two runs in DB directly: one matching (failed), one belonging to another config
    run_matching_id = uuid.uuid4()
    run_other_id = uuid.uuid4()
    other_cfg_id = uuid.uuid4()

    from ttakkari.db import sessionmaker
    from ttakkari.research.models import Configuration, Run
    from ttakkari.models import utcnow

    async with sessionmaker()() as db:
        other_cfg = Configuration(
            id=other_cfg_id,
            experiment_id=uuid.UUID(exp_id),
            label="Other",
            role="candidate",
            parameters={"k": 10},
            config_hash="h10",
            created_at=utcnow(),
        )
        db.add(other_cfg)
        # matching failed run
        matching_run = Run(
            id=run_matching_id,
            experiment_id=uuid.UUID(exp_id),
            configuration_id=uuid.UUID(cfg_id),
            study_id=uuid.UUID(study_id),
            adapter_id="retrieval_eval",
            status="failed",
            error_code="test_failure",
            created_at=utcnow(),
        )
        db.add(matching_run)
        # other run on other cfg
        other_run = Run(
            id=run_other_id,
            experiment_id=uuid.UUID(exp_id),
            configuration_id=other_cfg_id,
            study_id=uuid.UUID(study_id),
            adapter_id="retrieval_eval",
            status="succeeded",
            created_at=utcnow(),
        )
        db.add(other_run)
        await db.commit()

    # 1. Non-existent source_run_id -> 404
    r_not_found = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Missing run",
            "source_configuration_id": cfg_id,
            "source_run_id": str(uuid.uuid4()),
            "adapter_id": "retrieval_eval",
        },
    )
    assert r_not_found.status_code == 404, r_not_found.text
    assert "source_run_not_found" in r_not_found.text

    # 2. Run belonging to another configuration -> 422 lineage mismatch
    r_mismatch = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Mismatch run",
            "source_configuration_id": cfg_id,
            "source_run_id": str(run_other_id),
            "adapter_id": "retrieval_eval",
        },
    )
    assert r_mismatch.status_code == 422, r_mismatch.text
    assert "source_run_lineage_mismatch" in r_mismatch.text

    # 3. Matching failed run -> succeeds and preserves source_run_id!
    r_success = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Reuse failed run config",
            "source_configuration_id": cfg_id,
            "source_run_id": str(run_matching_id),
            "adapter_id": "retrieval_eval",
        },
    )
    assert r_success.status_code == 201, r_success.text
    data = r_success.json()
    assert data["source_run_id"] == str(run_matching_id)
    assert data["source_configuration_id"] == cfg_id


async def test_source_reuse_rejects_different_scope(client, auth):
    study_personal = await make_study(client, auth, scope="personal")
    study_work = await make_study(client, auth, scope="work")

    exp_personal = await make_experiment(
        client, auth, study_personal,
        config={"k": 5},
        adapter_id="retrieval_eval",
    )
    cfg_personal_id = exp_personal["configurations"][0]["id"]

    # Reusing personal configuration in work study -> 422 source_scope_mismatch
    r = await client.post(
        f"{API}/studies/{study_work}/experiments",
        headers=auth,
        json={
            "title": "Cross-scope reuse",
            "source_configuration_id": cfg_personal_id,
            "adapter_id": "retrieval_eval",
        },
    )
    assert r.status_code == 422, r.text
    assert "source_scope_mismatch" in r.text


async def test_source_reuse_rejects_adapter_mismatch(client, auth):
    study_id = await make_study(client, auth)
    exp = await make_experiment(
        client, auth, study_id,
        config={"k": 5},
        adapter_id="retrieval_eval",
    )
    cfg_id = exp["configurations"][0]["id"]

    # Source is retrieval_eval, but target requests external_report -> 422
    r = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Adapter mismatch",
            "source_configuration_id": cfg_id,
            "adapter_id": "external_report",
        },
    )
    assert r.status_code == 422, r.text
    assert "source_adapter_mismatch" in r.text


async def test_source_reuse_inherits_methods_and_clones_configuration_with_override(client, auth):
    study_id = await make_study(client, auth)
    hybrid = await _system_method(client, auth, "hybrid_retrieval")
    exp = await make_experiment(
        client, auth, study_id,
        method_id=hybrid["id"],
        config={"k": 5, "retriever_mode": "bm25", "rrf_k": 60},
        adapter_id="retrieval_eval",
    )
    cfg_id = exp["configurations"][0]["id"]
    original_params = exp["configurations"][0]["parameters"]

    # Create follow-up experiment without method_id (inherit), overriding k to 20
    r = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Follow-up experiment",
            "source_configuration_id": cfg_id,
            "adapter_id": "retrieval_eval",
            "fixed_inputs": {"k": 20},
            "search_space": {"rrf_k": [30, 90]},
            "strategy": "ab",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()

    # 1. method_id inherited
    assert data["method_id"] == hybrid["id"]
    assert data["source_configuration_id"] == cfg_id

    # 2. Parameters cloned from source with override
    configs = data["configurations"]
    assert len(configs) >= 2  # strategy=ab produces baseline + treatment
    baseline_cfg = next(c for c in configs if c["role"] == "baseline")
    assert baseline_cfg["parameters"]["k"] == 20
    assert baseline_cfg["parameters"]["retriever_mode"] == "bm25"
    assert baseline_cfg["parameters"]["rrf_k"] in (30, 60, 90)

    # 3. Source configuration parameters in DB must remain unmodified (immutability)
    from ttakkari.db import sessionmaker
    from ttakkari.research.models import Configuration
    async with sessionmaker()() as db:
        src_row = await db.get(Configuration, uuid.UUID(cfg_id))
        assert src_row.parameters == original_params
        assert src_row.parameters["k"] == 5


async def test_source_reuse_immutability_and_observability_across_get_resume_event_and_reload(client, auth):
    study_id = await make_study(client, auth)
    exp1 = await make_experiment(
        client, auth, study_id,
        config={"k": 5, "threshold": 0.8},
        adapter_id="retrieval_eval",
    )
    cfg1_id = exp1["configurations"][0]["id"]
    exp1_id = exp1["id"]

    # Target experiment in new study
    target_study_id = await make_study(client, auth)
    r_target = await client.post(
        f"{API}/studies/{target_study_id}/experiments",
        headers=auth,
        json={
            "title": "Observability Exp",
            "source_configuration_id": cfg1_id,
            "adapter_id": "retrieval_eval",
            "fixed_inputs": {"threshold": 0.95},
        },
    )
    assert r_target.status_code == 201, r_target.text
    target_exp = r_target.json()
    target_exp_id = target_exp["id"]

    # 1. Observable in GET /experiments/{id}
    r_get = await client.get(f"{API}/experiments/{target_exp_id}", headers=auth)
    assert r_get.status_code == 200
    get_data = r_get.json()
    assert get_data["source_study_id"] == study_id
    assert get_data["source_experiment_id"] == exp1_id
    assert get_data["source_configuration_id"] == cfg1_id
    assert get_data["source_run_id"] is None
    # Also in experiment wrapper
    assert get_data["experiment"]["source_configuration_id"] == cfg1_id
    assert get_data["experiment"]["source_experiment_id"] == exp1_id
    assert get_data["experiment"]["source_study_id"] == study_id

    # 2. Observable in Study resume GET /studies/{id}/resume
    r_resume = await client.get(f"{API}/studies/{target_study_id}/resume", headers=auth)
    assert r_resume.status_code == 200
    resume_data = r_resume.json()
    resumed_exp = next(e for e in resume_data["experiments"] if e["id"] == target_exp_id)
    assert resumed_exp["source_configuration_id"] == cfg1_id
    assert resumed_exp["source_experiment_id"] == exp1_id
    assert resumed_exp["source_study_id"] == study_id

    # 3. Observable in plan event
    timeline = resume_data["timeline"]
    plan_event = next(
        e for e in timeline
        if e["kind"] == "experiment_planned" and e.get("payload", {}).get("id") == target_exp_id
    )
    payload = plan_event["payload"]
    assert payload["source_configuration_id"] == cfg1_id
    assert payload["source_experiment_id"] == exp1_id
    assert payload["source_study_id"] == study_id
    assert len(payload["configurations"]) >= 1
    assert payload["configurations"][0]["parameters"]["threshold"] == 0.95

    # 4. Fresh DB session reload preserves lineage
    from ttakkari.db import sessionmaker
    from ttakkari.research.models import Experiment
    async with sessionmaker()() as db:
        loaded_exp = await db.get(Experiment, uuid.UUID(target_exp_id))
        assert str(loaded_exp.source_configuration_id) == cfg1_id
        assert str(loaded_exp.source_experiment_id) == exp1_id
        assert str(loaded_exp.source_study_id) == study_id
        assert loaded_exp.source_run_id is None


async def test_source_reuse_preserves_lineage_after_run_execution(client, auth):
    study_id = await make_study(client, auth)
    exp1 = await make_experiment(
        client, auth, study_id,
        config={"preset_id": "ko_tech_docs_v1", "k": 5},
        adapter_id="retrieval_eval",
    )
    cfg1_id = exp1["configurations"][0]["id"]
    exp1_id = exp1["id"]

    # Target experiment with auto_start=True
    r_target = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Executed Target",
            "source_configuration_id": cfg1_id,
            "adapter_id": "retrieval_eval",
            "auto_start": True,
        },
    )
    assert r_target.status_code == 201, r_target.text
    target_exp_id = r_target.json()["id"]

    # Check after run dispatch that source 4-fields are intact
    r_get = await client.get(f"{API}/experiments/{target_exp_id}", headers=auth)
    assert r_get.status_code == 200
    data = r_get.json()
    assert data["source_configuration_id"] == cfg1_id
    assert data["source_experiment_id"] == exp1_id
    assert data["source_study_id"] == study_id


async def test_shadowed_k_sweep_detection_in_validate_and_create(client, auth):
    study_id = await make_study(client, auth)

    # 1. k sweep [5, 20] with fixed scalar top_k=20 -> validate reports error
    r_val = await client.post(
        f"{API}/experiments/validate",
        headers=auth,
        json={
            "study_id": study_id,
            "adapter_id": "retrieval_eval",
            "strategy": "ab",
            "search_space": {"k": [5, 20]},
            "fixed_inputs": {"top_k": 20, "preset_id": "ko_tech_docs_v1"},
        },
    )
    assert r_val.status_code == 200
    val_body = r_val.json()
    assert val_body["valid"] is False
    assert any("shadowed" in err for err in val_body["errors"])

    # 2. k sweep [5, 20] with fixed scalar top_k=20 -> create is rejected with 422
    r_create_bad = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Shadowed sweep create",
            "adapter_id": "retrieval_eval",
            "strategy": "ab",
            "search_space": {"k": [5, 20]},
            "fixed_inputs": {"top_k": 20, "preset_id": "ko_tech_docs_v1"},
        },
    )
    assert r_create_bad.status_code == 422, r_create_bad.text
    assert "shadowed" in r_create_bad.text

    # 3. Scalar top_k=20 and k=5 (not a sweep) -> passes validation and creation (existing precedence contract preserved)
    r_scalar = await client.post(
        f"{API}/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "Scalar top_k and k",
            "adapter_id": "retrieval_eval",
            "strategy": "single",
            "config": {"k": 5, "top_k": 20, "preset_id": "ko_tech_docs_v1"},
        },
    )
    assert r_scalar.status_code == 201, r_scalar.text


@pytest.mark.parametrize("adapter_id,strategy,search_space,fixed,blocked", [
    ("retrieval_eval", "grid", {"k": [5, 20], "top_k": [5, 20]}, {}, True),
    ("retrieval_eval", "ab", {"k": [5, 20], "top_k": [5, 20]}, {"top_k": 10}, True),
    ("retrieval_eval", "single", {"k": [5, 20]}, {"top_k": 10}, False),
    ("retrieval_eval", "repeat", {"k": [5, 20]}, {"top_k": 10}, False),
    ("external_report", "grid", {"k": [5, 20], "top_k": [5, 20]}, {}, False),
    ("external_report", "grid", {"k": [5, 20]}, {"top_k": 10}, False),
    ("retrieval_eval", "grid", {"top_k": [5, 20]}, {"top_k": 10, "k": 5}, False),
])
async def test_shadowed_cutoff_validate_create_agree_on_adapter_and_strategy(
    client, auth, adapter_id, strategy, search_space, fixed, blocked,
):
    study_id = await make_study(client, auth)
    body = {"adapter_id": adapter_id, "strategy": strategy, "search_space": search_space,
            "fixed_inputs": {"preset_id": "ko_tech_docs_v1", **fixed}}
    val = await client.post(f"{API}/experiments/validate", headers=auth, json={"study_id": study_id, **body})
    assert val.status_code == 200, val.text
    assert val.json()["valid"] is (not blocked), val.text
    assert any("shadowed" in e for e in val.json()["errors"]) is blocked
    created = await client.post(f"{API}/studies/{study_id}/experiments", headers=auth, json=body)
    assert created.status_code == (422 if blocked else 201), created.text
    if not blocked and adapter_id == "retrieval_eval":
        cutoffs = [c["parameters"]["top_k"] for c in created.json()["configurations"]]
        assert cutoffs == ([5, 20] if strategy == "grid" else [10])


@pytest.mark.parametrize("interpretation_type", ["success", "failure"])
async def test_interpretation_node_is_saved_unverified_and_restored(client, auth, interpretation_type):
    study_id = await make_study(client, auth)
    exp_id, cfg_id, run_id = (str(uuid.uuid4()) for _ in range(3))
    body = {"kind": "interpretation", "title": "Researcher note", "summary": "Unverified cause",
            "conditions": {"experiment_id": exp_id, "configuration_id": cfg_id, "run_id": run_id},
            "details": {"interpretation_type": interpretation_type, "hypothesis": "Unverified cause"}}
    saved = await client.post(f"{API}/studies/{study_id}/nodes", headers=auth, json=body)
    assert saved.status_code == 201, saved.text
    assert saved.json()["epistemic_status"] == "unverified"
    restored = await client.get(f"{API}/studies/{study_id}/resume", headers=auth)
    node = next(n for n in restored.json()["nodes"] if n["id"] == saved.json()["id"])
    assert node["epistemic_status"] == "unverified"
    assert node["conditions"] == body["conditions"]
    assert node["details"] == body["details"]
    rejected = await client.post(f"{API}/studies/{study_id}/nodes", headers=auth,
                                 json={**body, "epistemic_status": "locally_measured"})
    assert rejected.status_code == 422


async def test_reuse_defaults_to_source_adapter_when_request_omits_adapter(client, auth):
    study_id = await make_study(client, auth)
    source = await make_experiment(client, auth, study_id, adapter_id="external_report", strategy="single")
    response = await client.post(f"{API}/studies/{study_id}/experiments", headers=auth, json={
        "title": "Reuse external definition", "source_configuration_id": source["configurations"][0]["id"],
    })
    assert response.status_code == 201, response.text
    assert response.json()["adapter_id"] == "external_report"


