"""Unit tests for Research Lab v2 execution adapters, registry, and seed integration."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from ttakkari.host_gpu import HostGpu
from ttakkari.meetings.local_stt import runner as stt_runner
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

    # Unimplemented / non-executable TTS methods must NOT match tts_synthesis; fallback to external_report
    assert registry.find_for_method("tts_duration_prediction").id == "external_report"
    assert registry.find_for_method("tts_speaker_embedding_voice_consistency").id == "external_report"
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
    assert result_hybrid.raw_output["retriever_mode"] == "hybrid_linear"
    assert result_hybrid.raw_output["dense_weight"] == 0.4
    assert result_hybrid.raw_output["hybrid_alpha"] == 0.6

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


@pytest.mark.asyncio
async def test_llm_inference_stop_signature_and_cleanup_contract() -> None:
    adapter = LlmInferenceAdapter()
    config = {
        "model": "qwen3.5-4b-q4_k_m",
        "n_gpu_layers": 33,
    }
    run_id = uuid4()

    # 1. Verify that real HostGpu.stop signature takes no arguments other than self
    sig = inspect.signature(HostGpu.stop)
    assert len(sig.parameters) == 1  # only 'self'

    # 2. Strict mock verifying stop is called without extra parameters (like reason=...)
    with patch("ttakkari.host_gpu.HostGpu") as mock_cls:
        instance = mock_cls.return_value
        instance.start.return_value = {"ready": True}
        instance.status.return_value = {"ready": True}
        instance.bench.return_value = {
            "execution_path": "host_gpu",
            "median_generation_tokens_per_second": 45.0,
            "median_prompt_tokens_per_second": 120.0,
            "runs": [{"prompt_tokens": 30}],
            "wall_seconds": 1.5,
            "swap": {"peak_growth_bytes": 5242880},  # 5 MB
        }

        def strict_stop(*args, **kwargs):
            if args or kwargs:
                raise TypeError(f"stop() takes 0 positional arguments but {len(args)} were given")
            return {"phase": "stopped", "stopped_now": True}

        instance.stop.side_effect = strict_stop

        result = await adapter.execute_run(run_id, config, scope="personal")
        assert result.status == "succeeded"
        assert instance.stop.call_count == 1
        cleanup = result.raw_output.get("cleanup")
        assert cleanup is not None
        assert cleanup["attempted"] is True
        assert cleanup["success"] is True

        # 3. Verify cleanup error recording when stop fails
        def failing_stop(*args, **kwargs):
            if args or kwargs:
                raise TypeError("stop() takes 0 arguments")
            raise RuntimeError("Lease manager failed to signal supervisor")

        instance.stop.side_effect = failing_stop
        result_fail = await adapter.execute_run(uuid4(), config, scope="personal")
        cleanup_fail = result_fail.raw_output.get("cleanup")
        assert cleanup_fail["attempted"] is True
        assert cleanup_fail["success"] is False
        assert "RuntimeError" in cleanup_fail["error"]


def test_llm_inference_parameter_rejections_and_quant_type_mapping() -> None:
    adapter = LlmInferenceAdapter()

    # 1. main_gpu must be rejected
    errors_main = adapter.validate_config({"model": "qwen3.5-4b-q4_k_m", "main_gpu": 0}, scope="personal")
    assert any("main_gpu" in e and "미지원" in e for e in errors_main)

    # 2. quant_method must be rejected
    errors_qm = adapter.validate_config({"model": "qwen3.5-4b-q4_k_m", "quant_method": "awq"}, scope="personal")
    assert any("quant_method" in e and "미지원" in e for e in errors_qm)

    # 3. non-zero temperature must be rejected
    errors_temp = adapter.validate_config({"model": "qwen3.5-4b-q4_k_m", "temperature": 0.7}, scope="personal")
    assert any("temperature=0" in e for e in errors_temp)
    # temperature 0 is accepted
    errors_zero = adapter.validate_config({"model": "qwen3.5-4b-q4_k_m", "temperature": 0}, scope="personal")
    assert len(errors_zero) == 0

    # 4. quant_type mapping
    assert len(adapter.validate_config({"quant_type": "Q4_K_M"}, scope="personal")) == 0
    assert len(adapter.validate_config({"quant_type": "Q2_K"}, scope="personal")) == 0
    # unsupported quant_type must NOT fall back silently, but be rejected
    errors_bad_quant = adapter.validate_config({"quant_type": "Q8_0"}, scope="personal")
    assert any("지원되지 않는 quant_type" in e for e in errors_bad_quant)

    # Neither model nor quant_type
    errors_missing = adapter.validate_config({"gpu_layers": 33}, scope="personal")
    assert any("model 또는 지원되는 quant_type" in e for e in errors_missing)


def test_llm_inference_swap_top_level_metric() -> None:
    adapter = LlmInferenceAdapter()
    run_res = AdapterRunResult(
        status="succeeded",
        raw_output={
            "execution_path": "host_gpu",
            "median_generation_tokens_per_second": 50.0,
            "median_prompt_tokens_per_second": 100.0,
            "runs": [{"prompt_tokens": 50}],
            "wall_seconds": 2.0,
            # HostGpu bench() returns top-level swap dict
            "swap": {"peak_growth_bytes": 10485760},  # 10.0 MB
        },
        environment_snapshot={"scope": "personal"},
        wall_seconds=2.0,
    )
    records = adapter.to_measurements(run_res, {})
    keys = {r.metric_key: r for r in records}
    assert "swap_peak_growth_mb" in keys
    assert keys["swap_peak_growth_mb"].value == 10.0
    assert keys["swap_peak_growth_mb"].unit == "MB"


@pytest.mark.asyncio
async def test_tts_synthesis_stt_runner_signature_and_both_cer_metrics() -> None:
    adapter = TtsSynthesisAdapter()

    # Verify signature of stt runner.run_worker
    sig = inspect.signature(stt_runner.run_worker)
    assert "audio_path" in sig.parameters
    assert "params" in sig.parameters
    assert "settings" in sig.parameters
    assert "production" in sig.parameters

    config = {
        "texts": ["안녕하세요. 음성 합성 테스트입니다."],
        "mode": "synthesize_and_stt",
        "voice": "M1",
    }
    run_id = uuid4()

    with patch("ttakkari.tts.supertonic.SupertonicProvider.availability") as mock_avail, \
         patch("ttakkari.tts.supertonic.SupertonicProvider.synthesize") as mock_synth, \
         patch("ttakkari.meetings.local_stt.runner.run_worker") as mock_run_worker, \
         patch("ttakkari.config.get_settings") as mock_settings:

        avail_obj = MagicMock()
        avail_obj.ready = True
        avail_obj.reason = "Ready"
        mock_avail.return_value = avail_obj

        synth_res = MagicMock()
        synth_res.metrics = {"synth_seconds": 1.0, "audio_seconds": 5.0, "load_seconds": 0.2, "peak_rss_bytes": 104857600}
        chunk_mock = MagicMock()
        chunk_mock.path = "/tmp/dummy_chunk.wav"
        chunk_mock.duration_seconds = 5.0
        chunk_mock.sample_rate = 24000
        chunk_mock.bytes = 10000
        synth_res.chunks = [chunk_mock]
        mock_synth.return_value = synth_res

        settings_obj = MagicMock()
        settings_obj.local_stt_python = "/usr/bin/python3"
        mock_settings.return_value = settings_obj

        # Strict run_worker check: must accept audio_path, params, settings, production
        async def fake_run_worker(audio_path, params, settings, *, production=True):
            assert isinstance(audio_path, Path)
            assert isinstance(params, dict)
            assert production is True
            return {"text": "안녕하세요 음성 합성 테스트입니다"}

        mock_run_worker.side_effect = fake_run_worker

        result = await adapter.execute_run(run_id, config, scope="personal")
        assert result.status == "succeeded"
        assert mock_run_worker.call_count == 1

        measurements = adapter.to_measurements(result, config)
        keys = {m.metric_key: m for m in measurements}
        assert "stt_cer_vs_normalized" in keys
        assert "stt_cer_vs_raw" in keys
        assert "stt_wer_vs_normalized" in keys
        assert "stt_wer_vs_raw" in keys
        assert keys["stt_cer_vs_normalized"].is_proxy is True
        assert keys["stt_cer_vs_raw"].is_proxy is True

        # Test STT worker failure in synthesize_and_stt mode -> contract is status="failed"
        mock_run_worker.side_effect = RuntimeError("STT worker crashed with OOM")
        result_failed = await adapter.execute_run(uuid4(), config, scope="personal")
        assert result_failed.status == "failed"
        assert result_failed.error_code == "stt_worker_failed"


def test_tts_synthesis_unsupported_parameters_and_methods_audit() -> None:
    adapter = TtsSynthesisAdapter()

    # 1. english_acronym_mode 'as_word' and 'spelled_out' must be rejected
    for bad_mode in ("as_word", "spelled_out"):
        errors = adapter.validate_config({
            "texts": ["test"],
            "english_acronym_mode": bad_mode,
        }, scope="personal")
        assert any("english_acronym_mode" in e and "미지원" in e for e in errors)

    assert len(adapter.validate_config({"texts": ["test"], "english_acronym_mode": "korean_alphabet"}, scope="personal")) == 0
    assert len(adapter.validate_config({"texts": ["test"], "english_acronym_mode": "raw"}, scope="personal")) == 0

    # 2. split_on_comma must be rejected
    errors_comma = adapter.validate_config({"texts": ["test"], "split_on_comma": True}, scope="personal")
    assert any("split_on_comma" in e and "미지원" in e for e in errors_comma)

    # 3. total_steps must be rejected
    errors_steps = adapter.validate_config({"texts": ["test"], "total_steps": 10}, scope="personal")
    assert any("total_steps" in e and "미지원" in e for e in errors_steps)

    # 4. supported_methods audit
    assert "tts_duration_prediction" not in adapter.supported_methods
    assert "tts_speaker_embedding_voice_consistency" not in adapter.supported_methods


@pytest.mark.asyncio
async def test_retrieval_dense_weight_and_normalize_embeddings_influence() -> None:
    adapter = RetrievalEvalAdapter()

    corpus = [
        {"id": "doc-a", "title": "문서 A", "text": "파이썬 데이터 분석 및 머신러닝 기초"},
        {"id": "doc-b", "title": "문서 B", "text": "자바스크립트 프론트엔드 리액트 개발"},
        {"id": "doc-c", "title": "문서 C", "text": "파이썬 웹 백엔드 및 FastAPI 비동기 개발"},
    ]
    queries = [
        {"id": "q-1", "query": "파이썬 데이터 분석", "relevant_ids": ["doc-a"], "relevance_grades": {"doc-a": 3}}
    ]

    # 1. Influence of dense_weight in linear combination
    cfg_dense_low = {
        "corpus": corpus,
        "queries": queries,
        "dense_weight": 0.05,
        "retriever_mode": "hybrid_linear",
    }
    cfg_dense_high = {
        "corpus": corpus,
        "queries": queries,
        "dense_weight": 0.95,
        "retriever_mode": "hybrid_linear",
    }
    res_low = await adapter.execute_run(uuid4(), cfg_dense_low, scope="personal")
    res_high = await adapter.execute_run(uuid4(), cfg_dense_high, scope="personal")
    assert res_low.status == "succeeded"
    assert res_high.status == "succeeded"
    assert res_low.raw_output["dense_weight"] == 0.05
    assert res_high.raw_output["dense_weight"] == 0.95
    assert res_low.raw_output["hybrid_alpha"] == 0.95
    assert res_high.raw_output["hybrid_alpha"] == 0.05

    # 2. Influence of normalize_embeddings
    cfg_norm_true = {
        "corpus": corpus,
        "queries": queries,
        "retriever_mode": "dense_hash",
        "normalize_embeddings": True,
    }
    cfg_norm_false = {
        "corpus": corpus,
        "queries": queries,
        "retriever_mode": "dense_hash",
        "normalize_embeddings": False,
    }
    res_norm_t = await adapter.execute_run(uuid4(), cfg_norm_true, scope="personal")
    res_norm_f = await adapter.execute_run(uuid4(), cfg_norm_false, scope="personal")
    assert res_norm_t.status == "succeeded"
    assert res_norm_f.status == "succeeded"
    assert res_norm_t.raw_output["normalize_embeddings"] is True
    assert res_norm_f.raw_output["normalize_embeddings"] is False

    # 3. Validation errors for invalid dense_weight and rrf_k
    bad_cfg = {
        "corpus": corpus,
        "queries": queries,
        "dense_weight": 1.5,
        "rrf_k": -10,
    }
    errors = adapter.validate_config(bad_cfg, scope="personal")
    assert any("dense_weight" in e for e in errors)
    assert any("rrf_k" in e for e in errors)


def test_preset_qrels_consistency() -> None:
    preset = load_retrieval_preset("ko_tech_docs_v1")
    q_map = {q["id"]: q for q in preset["queries"]}

    # q-02: KV cache 양자화 -> doc-01 (호스트 GPU 실행기) must NOT be relevant
    assert "doc-01" not in q_map["q-02"]["relevant_ids"]
    assert "doc-03" in q_map["q-02"]["relevant_ids"]

    # q-03: Flash Attention -> doc-03 (KV cache) must NOT be relevant
    assert "doc-03" not in q_map["q-03"]["relevant_ids"]
    assert "doc-04" in q_map["q-03"]["relevant_ids"]

    # q-04: Supertonic 3 vs Kokoro -> doc-07 (텍스트 정규화) must NOT be relevant
    assert "doc-07" not in q_map["q-04"]["relevant_ids"]
    assert "doc-06" in q_map["q-04"]["relevant_ids"]

    # q-05: 텍스트 정규화 숫자/약어 변환 -> doc-07 is the direct answer
    assert "doc-07" in q_map["q-05"]["relevant_ids"]
