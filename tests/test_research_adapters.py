"""Unit tests for Research Lab v2 execution adapters and registry."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from ttakkari.research.adapters import (
    AdapterEnvironmentStatus,
    AdapterRegistry,
    AdapterRunResult,
    ExecutionAdapter,
    MeasurementRecord,
    ResourceEstimate,
    get_adapter_registry,
)
from ttakkari.research.adapters.external_report import ExternalReportAdapter
from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
from ttakkari.research.adapters.presets import load_retrieval_preset, load_tts_preset
from ttakkari.research.adapters.retrieval_eval import RetrievalEvalAdapter
from ttakkari.research.adapters.tts_synthesis import TtsSynthesisAdapter


# Research adapters do not require database connection. Override conftest DB autouse fixtures.
@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    yield


@pytest.fixture(autouse=True)
def _clean():
    yield


def test_registry_discovery_and_lookup() -> None:
    registry = get_adapter_registry(populate_builtins=True)
    adapters = registry.list_adapters()
    adapter_ids = {ad.id for ad in adapters}

    assert "llm_inference" in adapter_ids
    assert "retrieval_eval" in adapter_ids
    assert "tts_synthesis" in adapter_ids
    assert "external_report" in adapter_ids

    # Protocol check
    for ad in adapters:
        assert isinstance(ad, ExecutionAdapter)

    # Method lookup
    assert registry.find_for_method("bm25-plus-dense-rrf") is not None
    assert registry.find_for_method("bm25-plus-dense-rrf").id == "retrieval_eval"

    assert registry.find_for_method("kv-cache-quantization") is not None
    assert registry.find_for_method("kv-cache-quantization").id == "llm_inference"

    assert registry.find_for_method("tts-normalization-g2p") is not None
    assert registry.find_for_method("tts-normalization-g2p").id == "tts_synthesis"

    assert registry.find_for_method("any-unknown-hypothesis-slug") is not None
    assert registry.find_for_method("any-unknown-hypothesis-slug").id == "external_report"


def test_presets_loading() -> None:
    # Retrieval preset
    ret_preset = load_retrieval_preset("ko_tech_docs_v1")
    assert len(ret_preset["corpus"]) >= 30
    assert len(ret_preset["queries"]) >= 15
    for q in ret_preset["queries"]:
        assert "relevant_ids" in q
        assert "relevance_grades" in q

    # TTS preset
    tts_preset = load_tts_preset("ko_mixed_pronunciation_v1")
    assert 10 <= len(tts_preset["sentences"]) <= 20


@pytest.mark.asyncio
async def test_external_report_adapter_lifecycle() -> None:
    adapter = ExternalReportAdapter()

    # Environment
    env = adapter.check_environment(scope="personal")
    assert env.ready is True
    assert env.execution_mode == "manual"

    # Validation
    bad_config = {"provenance": "invalid_origin", "measurements": []}
    errors = adapter.validate_config(bad_config, scope="personal")
    assert len(errors) > 0

    good_config = {
        "provenance": "external_benchmark",
        "source_title": "Llama 3 Technical Report",
        "source_year": 2024,
        "measurements": [
            {
                "metric_key": "mmlu_score",
                "metric_label": "MMLU 정확도",
                "value": 0.825,
                "unit": "ratio",
                "direction": "higher_is_better",
            }
        ],
    }
    assert len(adapter.validate_config(good_config, scope="personal")) == 0

    # Execution
    run_id = uuid4()
    result = await adapter.execute_run(run_id, good_config, scope="personal")
    assert result.status == "succeeded"
    assert result.environment_snapshot["execution_origin"] == "external_report"

    # Measurements
    records = adapter.to_measurements(result, good_config)
    assert len(records) == 1
    assert records[0].metric_key == "mmlu_score"
    assert records[0].value == 0.825
    assert records[0].meta["execution_origin"] == "external_report"


@pytest.mark.asyncio
async def test_retrieval_eval_adapter_execution() -> None:
    adapter = RetrievalEvalAdapter()

    # Environment
    env = adapter.check_environment(scope="personal")
    assert env.ready is True
    assert "hash_embedder" in env.available_models

    # Neural reranker must be rejected honestly
    bad_config = {
        "preset_id": "ko_tech_docs_v1",
        "rerank_mode": "cross_encoder_roberta",
    }
    errors = adapter.validate_config(bad_config, scope="personal")
    assert any("미지원" in e for e in errors)

    # 1. Execute BM25 on internal preset
    run_id1 = uuid4()
    config_bm25 = {
        "preset_id": "ko_tech_docs_v1",
        "retriever_mode": "bm25",
        "k": 5,
        "chunking_strategy": "none",
    }
    result_bm25 = await adapter.execute_run(run_id1, config_bm25, scope="personal")
    assert result_bm25.status == "succeeded"
    assert result_bm25.raw_output["corpus_count"] >= 30
    assert result_bm25.raw_output["query_count"] >= 15

    summary_bm25 = result_bm25.raw_output["summary"]
    assert 0.0 <= summary_bm25["recall_at_k"] <= 1.0
    assert 0.0 <= summary_bm25["ndcg_at_k"] <= 1.0

    measurements_bm25 = adapter.to_measurements(result_bm25, config_bm25)
    keys_bm25 = {m.metric_key for m in measurements_bm25}
    assert "recall_at_5" in keys_bm25
    assert "mrr_at_5" in keys_bm25
    assert "ndcg_at_5" in keys_bm25
    assert "query_latency_ms_mean" in keys_bm25

    # 2. Execute Hybrid RRF with token_overlap reranking and fixed_window chunking
    run_id2 = uuid4()
    config_hybrid = {
        "preset_id": "ko_tech_docs_v1",
        "retriever_mode": "hybrid_rrf",
        "k": 5,
        "chunking_strategy": "fixed_window",
        "chunk_size": 80,
        "chunk_overlap": 20,
        "rerank_mode": "token_overlap_top_n",
        "rerank_window": 10,
    }
    result_hybrid = await adapter.execute_run(run_id2, config_hybrid, scope="personal")
    assert result_hybrid.status == "succeeded"
    assert result_hybrid.raw_output["chunk_count"] > result_hybrid.raw_output["corpus_count"]

    measurements_hybrid = adapter.to_measurements(result_hybrid, config_hybrid)
    assert len(measurements_hybrid) >= 6


@pytest.mark.asyncio
async def test_tts_synthesis_adapter_normalize_only() -> None:
    adapter = TtsSynthesisAdapter()

    # Environment
    env = adapter.check_environment(scope="personal")
    assert env.ready is True

    # Kokoro should be mapped to supertonic with note
    bad_speed = {"preset_id": "ko_mixed_pronunciation_v1", "speed": 5.0}
    errors = adapter.validate_config(bad_speed, scope="personal")
    assert len(errors) > 0

    # Normalization with custom dictionary
    run_id = uuid4()
    config_norm = {
        "preset_id": "ko_mixed_pronunciation_v1",
        "mode": "normalize_only",
        "engine": "kokoro",  # alias test
        "convert_numbers": True,
        "read_latin": True,
        "custom_pronunciation_dict": {
            "Qwen": "큐웬",
            "Metal": "메탈",
            "REST API": "레스트 에이피아이",
        },
    }
    result = await adapter.execute_run(run_id, config_norm, scope="personal")
    assert result.status == "succeeded"

    raw = result.raw_output
    assert raw["engine"] == "supertonic"
    assert raw["sentence_count"] >= 10

    # Check that custom pronunciations and numbers were converted
    found_custom = False
    for s in raw["sentences"]:
        norm_text = s["normalized_text"]
        if "큐웬" in norm_text or "메탈" in norm_text or "레스트 에이피아이" in norm_text:
            found_custom = True
            break
    assert found_custom is True

    measurements = adapter.to_measurements(result, config_norm)
    keys = {m.metric_key for m in measurements}
    assert "chunk_count" in keys
    assert "normalized_char_delta" in keys


@pytest.mark.asyncio
async def test_llm_inference_adapter_scope_and_validation() -> None:
    adapter = LlmInferenceAdapter()

    # Scope isolation: work scope must be rejected
    env_work = adapter.check_environment(scope="work")
    assert env_work.ready is False
    assert "personal" in str(env_work.reason)

    val_work = adapter.validate_config({"model": "qwen3.5-4b-q4_k_m"}, scope="work")
    assert any("personal" in e for e in val_work)

    # Estimate resources
    estimate = adapter.estimate_resources({"model": "qwen3.5-4b-q4_k_m"})
    assert estimate.requires_gpu_lease is True

    # Cancellation check
    cancel_event = asyncio.Event()
    cancel_event.set()
    run_id = uuid4()
    config = {
        "model": "qwen3.5-4b-q4_k_m",
        "prompt": "안녕",
        "repetitions": 1,
    }
    # When already cancelled, returns cancelled status gracefully
    result = await adapter.execute_run(run_id, config, scope="personal", cancel_event=cancel_event)
    assert result.status == "cancelled"
