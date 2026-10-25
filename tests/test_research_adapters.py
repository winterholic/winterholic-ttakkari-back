"""Unit tests for Research Lab v2 execution adapters, registry, and seed integration."""

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
from ttakkari.research.readiness import compute_execution_status


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

    # Method lookup with exact whitelist matching
    assert registry.find_for_method("hybrid_retrieval").id == "retrieval_eval"
    assert registry.find_for_method("reciprocal_rank_fusion").id == "retrieval_eval"
    assert registry.find_for_method("document_chunking_strategy").id == "retrieval_eval"

    assert registry.find_for_method("kv_cache_quantization").id == "llm_inference"
    assert registry.find_for_method("flash_attention").id == "llm_inference"
    assert registry.find_for_method("cpu_gpu_offloading").id == "llm_inference"

    assert registry.find_for_method("tts_korean_text_normalization").id == "tts_synthesis"
    assert registry.find_for_method("tts_sentence_segmentation").id == "tts_synthesis"
    assert registry.find_for_method("tts_duration_prediction").id == "tts_synthesis"

    # Unimplemented TTS method (e.g. g2p, vocoder) must NOT match tts_synthesis; fallbacks to external_report
    assert registry.find_for_method("tts_vocoder_benchmark").id == "external_report"
    assert registry.find_for_method("tts_korean_g2p").id == "external_report"
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
async def test_retrieval_eval_adapter_catalog_mapping_and_execution() -> None:
    adapter = RetrievalEvalAdapter()

    # Environment
    env = adapter.check_environment(scope="personal")
    assert env.ready is True
    assert "hash_embedder" in env.available_models

    # Neural reranker must be rejected honestly
    bad_config = {
        "preset_id": "ko_tech_docs_v1",
        "reranker_model": "bge-reranker-large",
    }
    errors = adapter.validate_config(bad_config, scope="personal")
    assert any("미지원" in e for e in errors)

    # 1. Execute with catalog seed parameters: hybrid_retrieval (dense_weight=0.4, top_k=5)
    run_id1 = uuid4()
    config_hybrid_seed = {
        "preset_id": "ko_tech_docs_v1",
        "dense_weight": 0.4,
        "top_k": 5,
        "chunking_mode": "recursive_sentence",
    }
    assert len(adapter.validate_config(config_hybrid_seed, scope="personal")) == 0
    result_hybrid = await adapter.execute_run(run_id1, config_hybrid_seed, scope="personal")
    assert result_hybrid.status == "succeeded"
    assert result_hybrid.raw_output["k"] == 5
    assert result_hybrid.raw_output["retriever_mode"] == "hybrid_rrf"

    measurements_hybrid = adapter.to_measurements(result_hybrid, config_hybrid_seed)
    keys_hybrid = {m.metric_key for m in measurements_hybrid}
    assert "recall_at_5" in keys_hybrid
    assert "mrr_at_5" in keys_hybrid
    assert "ndcg_at_5" in keys_hybrid
    assert "query_latency_ms_mean" in keys_hybrid

    # 2. Execute document_chunking_strategy seed params (chunking_mode='fixed_character', chunk_size=80, chunk_overlap=20)
    run_id2 = uuid4()
    config_chunk_seed = {
        "preset_id": "ko_tech_docs_v1",
        "chunking_mode": "fixed_character",
        "chunk_size": 80,
        "chunk_overlap": 20,
        "retriever_mode": "bm25",
        "top_k": 5,
    }
    result_chunk = await adapter.execute_run(run_id2, config_chunk_seed, scope="personal")
    assert result_chunk.status == "succeeded"
    assert result_chunk.raw_output["chunk_count"] > result_chunk.raw_output["corpus_count"]


@pytest.mark.asyncio
async def test_tts_synthesis_adapter_catalog_mapping_and_execution() -> None:
    adapter = TtsSynthesisAdapter()

    # Environment
    env = adapter.check_environment(scope="personal")
    assert env.ready is True

    # 1. Unimplemented parameters (G2P descriptive_lenition) must be rejected with reason
    bad_g2p_config = {
        "preset_id": "ko_mixed_pronunciation_v1",
        "descriptive_lenition": True,
        "phoneme_format": "ipa",
    }
    errors_g2p = adapter.validate_config(bad_g2p_config, scope="personal")
    assert any("G2P" in e and "미지원" in e for e in errors_g2p)

    # 2. Kokoro request must be mapped to supertonic with explicit warning
    config_kokoro = {
        "preset_id": "ko_mixed_pronunciation_v1",
        "mode": "normalize_only",
        "engine": "kokoro",
        "handle_measure_units": True,
        "english_acronym_mode": "korean_alphabet",
    }
    run_id_kokoro = uuid4()
    res_kokoro = await adapter.execute_run(run_id_kokoro, config_kokoro, scope="personal")
    assert res_kokoro.status == "succeeded"
    assert res_kokoro.raw_output["engine"] == "supertonic"
    assert any("Kokoro" in w and "폐기" in w for w in res_kokoro.raw_output["warnings"])

    # 3. Seed parameter mapping: handle_measure_units, english_acronym_mode, custom dictionary
    run_id = uuid4()
    config_seed = {
        "preset_id": "ko_mixed_pronunciation_v1",
        "mode": "normalize_only",
        "handle_measure_units": True,
        "english_acronym_mode": "korean_alphabet",
        "max_chunk_length": 150,
        "custom_pronunciation_dict": {
            "Qwen": "큐웬",
            "Metal": "메탈",
            "REST API": "레스트 에이피아이",
        },
    }
    result = await adapter.execute_run(run_id, config_seed, scope="personal")
    assert result.status == "succeeded"

    raw = result.raw_output
    assert raw["engine"] == "supertonic"
    assert raw["sentence_count"] >= 10

    found_custom = False
    for s in raw["sentences"]:
        norm_text = s["normalized_text"]
        if "큐웬" in norm_text or "메탈" in norm_text or "레스트 에이피아이" in norm_text:
            found_custom = True
            break
    assert found_custom is True

    measurements = adapter.to_measurements(result, config_seed)
    keys = {m.metric_key for m in measurements}
    assert "chunk_count" in keys
    assert "normalized_char_delta" in keys


@pytest.mark.asyncio
async def test_llm_inference_adapter_catalog_mapping_and_validation() -> None:
    adapter = LlmInferenceAdapter()

    # Scope isolation: work scope must be rejected
    env_work = adapter.check_environment(scope="work")
    assert env_work.ready is False
    assert "personal" in str(env_work.reason)

    val_work = adapter.validate_config({"model": "qwen3.5-4b-q4_k_m"}, scope="work")
    assert any("personal" in e for e in val_work)

    # Unsupported speculative decoding with draft_model must be rejected
    bad_spec = {
        "model": "qwen3.5-4b-q4_k_m",
        "draft_model": "qwen-0.5b",
        "draft_tokens": 5,
    }
    errors_spec = adapter.validate_config(bad_spec, scope="personal")
    assert any("Draft" in e and "미지원" in e for e in errors_spec)

    # Seed parameter mapping check: n_gpu_layers, flash_attn_enabled, logical_batch_size
    good_seed_config = {
        "model": "qwen3.5-4b-q4_k_m",
        "n_gpu_layers": 33,
        "flash_attn_enabled": True,
        "logical_batch_size": 1024,
        "physical_ubatch_size": 256,
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
    }
    assert len(adapter.validate_config(good_seed_config, scope="personal")) == 0

    # Cancellation check
    cancel_event = asyncio.Event()
    cancel_event.set()
    run_id = uuid4()
    result = await adapter.execute_run(
        run_id, good_seed_config, scope="personal", cancel_event=cancel_event
    )
    assert result.status == "cancelled"


def test_readiness_axis_f_determination_with_seed_methods() -> None:
    """Verify that readiness.py calculates correct Axis F status for real catalog seed methods."""
    registry = get_adapter_registry(populate_builtins=True)

    # 1. Supported TTS method (tts_korean_text_normalization) -> immediately_executable
    norm_method = {
        "slug": "tts_korean_text_normalization",
        "lifecycle_status": "validated",
        "adapter_hints": ["tts_synthesis"],
        "parameters": [
            {"key": "handle_measure_units"},
            {"key": "english_acronym_mode"},
        ],
        "metrics": ["rtf", "normalized_char_delta"],
    }
    st_norm = compute_execution_status(norm_method, adapter_registry=registry, scope="personal")
    assert st_norm.status in ("immediately_executable", "tool_integrated")
    assert st_norm.is_executable is True
    assert len(st_norm.unsupported_params) == 0

    # 2. Partially supported TTS method (tts_korean_g2p has descriptive_lenition) -> partially_supported
    g2p_method = {
        "slug": "tts_korean_g2p",
        "lifecycle_status": "validated",
        "adapter_hints": ["tts_synthesis"],
        "parameters": [
            {"key": "descriptive_lenition"},
            {"key": "phoneme_format"},
        ],
        "metrics": ["cer"],
    }
    st_g2p = compute_execution_status(g2p_method, adapter_registry=registry, scope="personal")
    assert st_g2p.status == "partially_supported"
    assert "descriptive_lenition" in st_g2p.unsupported_params

    # 3. Supported LLM method (kv_cache_quantization) -> tool_integrated
    kv_method = {
        "slug": "kv_cache_quantization",
        "lifecycle_status": "validated",
        "adapter_hints": ["llm_inference"],
        "parameters": [
            {"key": "cache_type_k"},
            {"key": "cache_type_v"},
        ],
        "metrics": ["decode_tps", "swap_peak_growth_mb"],
    }
    st_kv = compute_execution_status(kv_method, adapter_registry=registry, scope="personal")
    assert st_kv.status == "tool_integrated"
    assert st_kv.is_executable is True

    # 4. Scope isolation: LLM method in work scope -> environment_unavailable
    st_kv_work = compute_execution_status(kv_method, adapter_registry=registry, scope="work")
    assert st_kv_work.status == "environment_unavailable"
    assert "회사(work)" in str(st_kv_work.unavailable_reason)

    # 5. Supported RAG method (reciprocal_rank_fusion) -> immediately_executable
    rrf_method = {
        "slug": "reciprocal_rank_fusion",
        "lifecycle_status": "validated",
        "adapter_hints": ["retrieval_eval"],
        "parameters": [{"key": "rrf_k"}],
        "metrics": ["recall_at_k", "ndcg_at_k"],
    }
    st_rrf = compute_execution_status(rrf_method, adapter_registry=registry, scope="personal")
    assert st_rrf.status in ("immediately_executable", "tool_integrated")
    assert st_rrf.is_executable is True
