"""Catalog seed -> catalog_loader -> DB -> readiness path (real loader, real adapter registry, no injected results).

The contract test in test_catalog_seed_contract.py checks the JSON. This file checks what the running system
does with it: a deprecated record must stay non-executable after loading, and the hardware-sensitive methods
must keep their explicit tuples and adapter routing once they went through the loader.
"""

import copy
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from ttakkari.models import utcnow
from ttakkari.db import sessionmaker
from ttakkari.research import catalog_loader
from ttakkari.research.adapters.registry import get_adapter_registry
from ttakkari.research.models import BestResult, Configuration, Experiment, Method, Node, Run, Study
from ttakkari.research.planner import validate_experiment_plan
from ttakkari.research.readiness import compute_execution_status
from ttakkari.research.service import adopt_best_result, promote_method

SEED_DIR = Path(catalog_loader.__file__).parent / "catalog_seed"
NOT_EXECUTABLE_STATUSES = {"hypothesis_stage", "adapter_required", "environment_unavailable"}


def _seed_method(slug):
    for p in sorted(SEED_DIR.glob("*.json")):
        if p.name == "taxonomy.json":
            continue
        for m in json.loads(p.read_text(encoding="utf-8")).get("methods", []):
            if m["slug"] == slug:
                return m
    raise AssertionError(f"seed method not found: {slug}")


async def _sync(client, auth):
    resp = await client.post("/api/research/catalog/sync?force=true", headers=auth)
    assert resp.status_code == 200, resp.text


async def test_deprecated_kokoro_stays_non_executable_through_loader_and_api(client, auth):
    await _sync(client, auth)
    resp = await client.get("/api/research/methods/tts_kokoro_pipeline_tuning", headers=auth)
    assert resp.status_code == 200, resp.text
    method = resp.json()

    # loader kept the seed lifecycle instead of forcing "validated"
    assert method["lifecycle_status"] == "deprecated"
    # nothing selectable or routable survived loading
    assert method["adapter_hints"] == []
    assert method["parameters"] == []
    assert method["compatibility"]["runtimes"] == []
    assert method["compatibility"]["hardware"] == []
    # readiness result computed by the service from the loaded row
    assert method["is_executable"] is False
    assert method["execution_status"] in NOT_EXECUTABLE_STATUSES

    listing = await client.get("/api/research/methods?lifecycle_status=deprecated&limit=100", headers=auth)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [i["slug"] for i in items] == ["tts_kokoro_pipeline_tuning"]
    assert all(i["is_executable"] is False for i in items)

    # no live method can reach Kokoro through a relation either
    for item in (await client.get("/api/research/methods?target=tts&limit=100", headers=auth)).json()["items"]:
        detail = (await client.get(f"/api/research/methods/{item['slug']}", headers=auth)).json()
        if item["slug"] == "tts_kokoro_pipeline_tuning":
            continue
        assert all(r["to_method_slug"] != "tts_kokoro_pipeline_tuning" for r in detail["relations"])


async def test_resync_restores_deprecated_lifecycle_on_existing_rows():
    """Rows loaded before the loader honored lifecycle_status must be corrected on the next sync."""
    sm = sessionmaker()
    async with sm() as db:
        await catalog_loader.load_catalog_seeds(db)
        await db.execute(
            update(Method)
            .where(Method.scope == "system", Method.slug == "tts_kokoro_pipeline_tuning")
            .values(lifecycle_status="validated")
        )
        await db.commit()

        await catalog_loader.load_catalog_seeds(db)
        await db.commit()
        row = (
            await db.execute(
                select(Method).where(Method.scope == "system", Method.slug == "tts_kokoro_pipeline_tuning")
            )
        ).scalar_one()
        assert row.lifecycle_status == "deprecated"
        status = compute_execution_status(row, get_adapter_registry())
        assert status.is_executable is False
        assert status.matched_adapter_id is None


async def test_loader_round_trips_explicit_tuples_and_adapter_routing():
    sm = sessionmaker()
    async with sm() as db:
        await catalog_loader.load_catalog_seeds(db)
        await db.commit()
        rows = {
            r.slug: r
            for r in (
                await db.execute(
                    select(Method).where(
                        Method.scope == "system",
                        Method.slug.in_(
                            ["qlora", "kv_cache_quantization", "flash_attention", "speculative_decoding"]
                        ),
                    )
                )
            ).scalars()
        }
    assert set(rows) == {"qlora", "kv_cache_quantization", "flash_attention", "speculative_decoding"}
    for slug, row in rows.items():
        # DB row carries exactly the seed's tuples (nothing dropped or re-derived by the loader)
        assert row.compatibility["combinations"] == _seed_method(slug)["compatibility"]["combinations"]

    reg = get_adapter_registry()
    # QLoRA has no training adapter: external result registration only, never routed to the local LLM executor
    assert rows["qlora"].adapter_hints == ["external_report"]
    qlora = compute_execution_status(rows["qlora"], reg)
    assert qlora.matched_adapter_id == "external_report"
    assert qlora.status not in {"immediately_executable", "tool_integrated"}

    # flash_attention / kv_cache_quantization go to the host llama-server adapter; whether the host is ready
    # depends on the machine, so assert routing and "never adapter_required", not readiness of this host
    for slug in ("flash_attention", "kv_cache_quantization"):
        res = compute_execution_status(rows[slug], reg)
        assert res.matched_adapter_id == "llm_inference"
        assert res.status in {"tool_integrated", "environment_unavailable"}
        assert res.unsupported_params == []

    # speculative decoding: only spec_type is consumed by the adapter; the draft-model keys stay unsupported
    spec = compute_execution_status(rows["speculative_decoding"], reg)
    assert spec.matched_adapter_id == "llm_inference"
    assert "spec_type" not in spec.unsupported_params
    assert {"draft_model", "draft_tokens"} <= set(spec.unsupported_params)


async def test_readiness_and_planner_enforce_combinations_and_negative_control():
    sm = sessionmaker()
    async with sm() as db:
        await catalog_loader.load_catalog_seeds(db)
        await db.commit()
        methods = {
            r.slug: r
            for r in (
                await db.execute(
                    select(Method).where(
                        Method.scope == "system",
                        Method.slug.in_(["flash_attention", "qlora"]),
                    )
                )
            ).scalars()
        }

    reg = get_adapter_registry()

    # 1. flash_attention: on Mac Studio (Darwin arm64) with llama.cpp,
    # authoritative tuple (apple_silicon_metal x llama.cpp = local_executable) is satisfied.
    fa_method = methods["flash_attention"]
    st_fa = compute_execution_status(fa_method, reg)
    assert st_fa.is_executable is True
    assert st_fa.status in ("immediately_executable", "tool_integrated")

    # 2. qlora: has no local_executable combination. Must be non-executable and environment_unavailable.
    qlora_method = methods["qlora"]
    st_qlora = compute_execution_status(qlora_method, reg)
    assert st_qlora.is_executable is False
    assert st_qlora.status == "environment_unavailable"
    assert "로컬 실행 가능한" in (st_qlora.unavailable_reason or "")

    # 3. Negative control (hypothetical tuple mutation):
    # If the combination (apple_silicon_metal x llama.cpp) is downgraded to "unverified" or "upstream_supported",
    # readiness must turn non-executable and fail-closed.
    mutated_fa = copy.deepcopy(fa_method.compatibility)
    for c in mutated_fa["combinations"]:
        if c.get("hardware") == "apple_silicon_metal" and c.get("runtime") == "llama.cpp":
            c["status"] = "upstream_supported"
            c["note"] = "가상 다운그레이드 테스트"

    hypothetical_method = {
        "slug": "flash_attention",
        "lifecycle_status": "validated",
        "parameters": fa_method.parameters,
        "metrics": fa_method.metrics,
        "adapter_hints": fa_method.adapter_hints,
        "compatibility": mutated_fa,
    }
    st_hypo = compute_execution_status(hypothetical_method, reg)
    assert st_hypo.is_executable is False
    assert st_hypo.status == "environment_unavailable"
    assert "로컬 실행 불가" in (st_hypo.unavailable_reason or "")

    # 4. Negative control: adapter details missing runtime/hardware identity -> fail-closed
    class AmbiguousAdapter:
        id = "llm_inference"
        supported_methods = ["flash_attention"]
        supported_parameters = ["flash_attn_enabled"]

        def check_environment(self, *, scope="personal"):
            from ttakkari.research.adapters.base import AdapterEnvironmentStatus

            return AdapterEnvironmentStatus(
                adapter_id="llm_inference",
                ready=True,
                execution_mode="host_supervisor",
                details={},  # Missing runtime and hardware
            )

    class DummyRegistry:
        def get_adapter(self, hint):
            return AmbiguousAdapter() if hint == "llm_inference" else None

    st_ambig = compute_execution_status(fa_method, DummyRegistry())
    assert st_ambig.is_executable is False
    assert st_ambig.status == "environment_unavailable"
    assert "fail-closed" in (st_ambig.unavailable_reason or "")

    # 5. Planner validation integration and negative control:
    # Local execution must reject non-executable methods, while external-report
    # designs remain valid and carry their local-readiness gap as a warning.
    async with sm() as db:
        study_id = uuid.uuid4()
        plan_ok = await validate_experiment_plan(
            db,
            study_id=study_id,
            method_id=fa_method.id,
            compared_method_ids=[],
            adapter_id="llm_inference",
            strategy="grid",
            search_space={"flash_attn_enabled": [True, False]},
            fixed_inputs={"model": "qwen3.5-4b-q4_k_m"},
            budget={"max_runs": 2},
        )
        assert plan_ok.valid is True
        assert len(plan_ok.errors) == 0

        plan_bad = await validate_experiment_plan(
            db,
            study_id=study_id,
            method_id=qlora_method.id,
            compared_method_ids=[],
            adapter_id="external_report",
            strategy="single",
            search_space={},
            fixed_inputs={"model": "qwen"},
            budget={"max_runs": 1},
        )
        assert plan_bad.valid is True
        assert any("로컬 자동 실행을 지원하지 않습니다" in warning for warning in plan_bad.warnings)

        plan_local = await validate_experiment_plan(
            db,
            study_id=study_id,
            method_id=qlora_method.id,
            compared_method_ids=[],
            adapter_id="llm_inference",
            strategy="single",
            search_space={},
            fixed_inputs={"model": "qwen"},
            budget={"max_runs": 1},
        )
        assert plan_local.valid is False
        assert any("로컬 자동 실행을 지원하지 않습니다" in err for err in plan_local.errors)

        plan_missing_model = await validate_experiment_plan(
            db,
            study_id=study_id,
            method_id=fa_method.id,
            compared_method_ids=[],
            adapter_id="llm_inference",
            strategy="single",
            search_space={},
            fixed_inputs={},
            budget={"max_runs": 1},
        )
        assert plan_missing_model.valid is False
        assert any("model 또는 지원되는 quant_type" in err for err in plan_missing_model.errors)


@pytest.mark.parametrize("combos", [[], None, {}, [None], [{"hardware": "apple_silicon_metal"}]])
async def test_explicit_malformed_or_empty_combinations_fail_closed(combos):
    class LocalAdapter:
        id = "llm_inference"
        supported_methods = ["sample"]
        supported_parameters = ["enabled"]

        def check_environment(self, *, scope="personal"):
            from ttakkari.research.adapters.base import AdapterEnvironmentStatus

            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=True,
                execution_mode="host_supervisor",
                details={"hardware": "apple_silicon_metal", "runtime": "llama.cpp"},
            )

    class Registry:
        def get_adapter(self, hint):
            return LocalAdapter() if hint == "llm_inference" else None

    compatibility = {"hardware": ["apple_silicon_metal"], "runtimes": ["llama.cpp"]}
    compatibility["combinations"] = combos
    method = {
        "slug": "sample",
        "lifecycle_status": "validated",
        "parameters": [{"key": "enabled"}],
        "metrics": ["quality"],
        "adapter_hints": ["llm_inference"],
        "compatibility": compatibility,
    }
    result = compute_execution_status(method, Registry())
    assert result.is_executable is False
    assert result.status == "environment_unavailable"


@pytest.mark.parametrize(
    "combos",
    [
        [
            {"hardware": "apple_silicon_metal", "runtime": "llama.cpp", "status": "local_executable"},
            {"hardware": "apple_silicon_metal", "runtime": "llama.cpp", "status": "upstream_supported"},
        ],
        [
            {"hardware": "apple_silicon_metal", "runtime": "llama.cpp", "status": "upstream_supported"},
            {"hardware": "apple_silicon_metal", "runtime": "llama.cpp", "status": "local_executable"},
        ],
    ],
)
async def test_conflicting_duplicate_hardware_runtime_tuples_fail_closed(combos):
    class LocalAdapter:
        id = "llm_inference"
        supported_methods = ["sample"]
        supported_parameters = ["enabled"]

        def check_environment(self, *, scope="personal"):
            from ttakkari.research.adapters.base import AdapterEnvironmentStatus

            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=True,
                execution_mode="host_supervisor",
                details={"hardware": "apple_silicon_metal", "runtime": "llama.cpp"},
            )

    class Registry:
        def get_adapter(self, hint):
            return LocalAdapter() if hint == "llm_inference" else None

    result = compute_execution_status(
        {
            "slug": "sample",
            "lifecycle_status": "validated",
            "parameters": [{"key": "enabled"}],
            "metrics": ["quality"],
            "adapter_hints": ["llm_inference"],
            "compatibility": {"combinations": combos},
        },
        Registry(),
    )
    assert result.is_executable is False
    assert result.status == "environment_unavailable"
    assert "상충" in (result.unavailable_reason or "")


async def test_model_conditioned_tuples_are_not_duplicate_hardware_runtime_tuples():
    class LocalAdapter:
        id = "llm_inference"
        supported_methods = ["sample"]
        supported_parameters = ["enabled"]

        def check_environment(self, *, scope="personal"):
            from ttakkari.research.adapters.base import AdapterEnvironmentStatus

            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=True,
                execution_mode="host_supervisor",
                available_models=["model-a", "model-b"],
                details={"hardware": "apple_silicon_metal", "runtime": "llama.cpp", "model": "model-a"},
            )

    class Registry:
        def get_adapter(self, hint):
            return LocalAdapter() if hint == "llm_inference" else None

    result = compute_execution_status(
        {
            "slug": "sample",
            "lifecycle_status": "validated",
            "parameters": [{"key": "enabled"}],
            "metrics": ["quality"],
            "adapter_hints": ["llm_inference"],
            "compatibility": {
                "combinations": [
                    {"hardware": "apple_silicon_metal", "runtime": "llama.cpp", "model": "model-a", "status": "local_executable"},
                    {"hardware": "apple_silicon_metal", "runtime": "llama.cpp", "model": "model-b", "status": "upstream_supported"},
                ]
            },
        },
        Registry(),
    )
    assert result.is_executable is True


async def test_omitted_combinations_preserves_legacy_compatibility_meaning():
    class LocalAdapter:
        id = "llm_inference"
        supported_methods = ["sample"]
        supported_parameters = ["enabled"]

        def check_environment(self, *, scope="personal"):
            from ttakkari.research.adapters.base import AdapterEnvironmentStatus

            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=True,
                execution_mode="host_supervisor",
                details={"hardware": "apple_silicon_metal", "runtime": "llama.cpp"},
            )

    class Registry:
        def get_adapter(self, hint):
            return LocalAdapter() if hint == "llm_inference" else None

    result = compute_execution_status(
        {
            "slug": "sample", "lifecycle_status": "validated",
            "parameters": [{"key": "enabled"}], "metrics": ["quality"],
            "adapter_hints": ["llm_inference"],
            "compatibility": {"hardware": ["apple_silicon_metal"], "runtimes": ["llama.cpp"]},
        },
        Registry(),
    )
    assert result.is_executable is True


async def test_system_seed_promotion_rejected_and_user_scope_promotion_preserved_across_sync():
    """Verify system seed promotion is rejected (422) and user-scope promotion is preserved across catalog sync."""
    sm = sessionmaker()
    async with sm() as db:
        await catalog_loader.load_catalog_seeds(db)
        await db.commit()

        # 1. Fetch a system seed method
        sys_method = (
            await db.execute(select(Method).where(Method.scope == "system", Method.slug == "flash_attention"))
        ).scalar_one()

        # Set up a succeeded run
        study = Study(
            id=uuid.uuid4(),
            scope="personal",
            title="Promotion Test Study",
            goal="방법론 승격 거부 및 보존 테스트",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(study)
        await db.flush()

        exp = Experiment(
            id=uuid.uuid4(),
            study_id=study.id,
            title="Exp 1",
            hypothesis="가설",
            rationale="이유",
            adapter="llm_inference",
            adapter_id="llm_inference",
            method_id=sys_method.id,
            strategy="grid",
            search_space={},
            fixed_inputs={},
            created_at=utcnow(),
        )
        db.add(exp)
        await db.flush()

        cfg = Configuration(
            id=uuid.uuid4(),
            experiment_id=exp.id,
            label="Cfg 1",
            parameters={"flash_attn_enabled": True},
            config_hash="h1",
            created_at=utcnow(),
        )
        db.add(cfg)
        await db.flush()

        succ_run = Run(
            id=uuid.uuid4(),
            study_id=study.id,
            experiment_id=exp.id,
            configuration_id=cfg.id,
            adapter_id="llm_inference",
            status="succeeded",
            execution_origin="local_measured",
            raw_output={},
            environment_snapshot={},
            created_at=utcnow(),
        )
        db.add(succ_run)
        await db.flush()

        # 2. Direct promotion of system seed method must be rejected with 422
        with pytest.raises(HTTPException) as exc_info:
            await promote_method(
                db,
                method_id=sys_method.id,
                run_id=succ_run.id,
                decision_note="시스템 시드 승격 시도",
                target_status="validated",
            )
        assert exc_info.value.status_code == 422
        assert "system_seed_methods_cannot_be_promoted" in str(exc_info.value.detail)

        # 3. BestResult adoption is independent from the optional seed promotion.
        adopted = await adopt_best_result(
            db,
            study_id=study.id,
            experiment_id=exp.id,
            configuration_id=cfg.id,
            run_id=succ_run.id,
            objective="maximize_tps",
            primary_metric="decode_tps",
            constraints={},
            rationale="시스템 시드 채택 및 승격 생략",
            promote_method_flag=True,
        )
        assert adopted.status == "adopted"
        dec_nodes = (
            await db.execute(select(Node).where(Node.study_id == study.id, Node.kind == "decision"))
        ).scalars().all()
        assert len(dec_nodes) == 1
        assert dec_nodes[0].conditions["method_promotion"] == {
            "requested": True, "status": "skipped", "reason": "system_seed_method"
        }

        best_records = (
            await db.execute(select(BestResult).where(BestResult.study_id == study.id))
        ).scalars().all()
        assert len(best_records) == 1

        # 4. User-scope method promotion works as intended
        user_method = Method(
            id=uuid.uuid4(),
            scope="personal",
            slug=f"custom_hypothesis_{uuid.uuid4().hex[:6]}",
            version=1,
            origin="user_registered",
            lifecycle_status="hypothesis",
            name="Custom Hypothesis",
            name_ko="사용자 가설 방법론",
            summary="사용자가 정의한 연구 가설",
            category="llm_inference",
            parameters=[],
            metrics=[],
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(user_method)
        await db.flush()

        promoted_user_method = await promote_method(
            db,
            method_id=user_method.id,
            run_id=succ_run.id,
            decision_note="사용자 정의 가설 승격 성공",
            target_status="validated",
        )
        assert promoted_user_method.lifecycle_status == "validated"
        assert promoted_user_method.promoted_from_run_id == succ_run.id
        await db.commit()

        # 5. Catalog sync: user-scope method and its promotion must be preserved;
        # and deprecated seed (Kokoro) must stay deprecated!
        await catalog_loader.load_catalog_seeds(db)
        await db.commit()

        persisted_user_method = await db.get(Method, user_method.id)
        assert persisted_user_method is not None
        assert persisted_user_method.lifecycle_status == "validated"
        assert persisted_user_method.promoted_from_run_id == succ_run.id

        kokoro = (
            await db.execute(
                select(Method).where(Method.scope == "system", Method.slug == "tts_kokoro_pipeline_tuning")
            )
        ).scalar_one()
        assert kokoro.lifecycle_status == "deprecated"


# ---- D1: model-conditioned combinations must fail closed ----

_HW, _RT = "apple_silicon_metal", "llama.cpp"


def _model_status(combos, *, model=None, model_id=None, available=()):
    class LocalAdapter:
        id = "llm_inference"
        supported_methods = ["sample"]
        supported_parameters = ["enabled"]

        def check_environment(self, *, scope="personal"):
            from ttakkari.research.adapters.base import AdapterEnvironmentStatus

            details = {"hardware": _HW, "runtime": _RT}
            if model:
                details["model"] = model
            if model_id:
                details["model_id"] = model_id
            return AdapterEnvironmentStatus(
                adapter_id=self.id, ready=True, execution_mode="host_supervisor",
                available_models=list(available), details=details,
            )

    class Registry:
        def get_adapter(self, hint):
            return LocalAdapter() if hint == "llm_inference" else None

    return compute_execution_status(
        {
            "slug": "sample", "lifecycle_status": "validated",
            "parameters": [{"key": "enabled"}], "metrics": ["quality"],
            "adapter_hints": ["llm_inference"],
            "compatibility": {"combinations": combos},
        },
        Registry(),
    )


def _t(status, **extra):
    return {"hardware": _HW, "runtime": _RT, "status": status, **extra}


@pytest.mark.parametrize(
    "combos,kwargs,expected",
    [
        # generic local + specific unsupported for the adapter's model -> specific wins, blocked
        ([_t("local_executable"), _t("unsupported", model="X")], {"model": "X"}, False),
        ([_t("local_executable"), _t("unsupported", model="X")], {"model_id": "X"}, False),
        ([_t("unsupported", model="X"), _t("local_executable")], {"model": "X"}, False),
        # model / model_id spelling of the same model must be one condition (conflict)
        ([_t("unsupported", model="X"), _t("local_executable", model_id="X")], {"model": "X"}, False),
        ([_t("local_executable", model="X"), _t("unsupported", model_id="X")], {"model_id": "X"}, False),
        # same tuple declares two different model names
        ([_t("local_executable", model="X", model_id="Y")], {"model": "X"}, False),
        # adapter cannot identify its model: model-conditioned tuples are uncertain
        ([_t("local_executable", model="X")], {}, False),
        ([_t("local_executable"), _t("unsupported", model="X")], {}, False),
        ([_t("local_executable", model="X")], {"available": ["X"]}, False),
        # specific declaration overrides the generic one in both directions
        ([_t("unsupported"), _t("local_executable", model="X")], {"model": "X"}, True),
        ([_t("unsupported"), _t("local_executable", model_id="X")], {"model_id": "X"}, True),
        ([_t("local_executable"), _t("unsupported", model="Y")], {"model": "X"}, True),
        # conflict inside the same level is still fail-closed
        ([_t("local_executable", model="X"), _t("unsupported", model="X")], {"model": "X"}, False),
        # model with no declaration at all for it
        ([_t("local_executable", model="Y")], {"model": "X"}, False),
        # generic only: unchanged
        ([_t("local_executable")], {}, True),
    ],
)
async def test_model_conditioned_combinations_fail_closed(combos, kwargs, expected):
    assert _model_status(combos, **kwargs).is_executable is expected


async def test_unidentified_model_with_multiple_available_models_is_not_full_support():
    combos = [_t("local_executable", model="a"), _t("upstream_supported", model="b")]
    assert _model_status(combos, available=["a", "b"]).is_executable is False
    assert _model_status(combos, model="a", available=["a", "b"]).is_executable is True
    assert _model_status(combos, model="b", available=["a", "b"]).is_executable is False


@pytest.mark.parametrize("model_value", [123, "", "  ", ["x"]])
async def test_invalid_model_condition_value_fails_closed(model_value):
    combos = [_t("local_executable", model=model_value)]
    assert _model_status(combos, model="x").is_executable is False
