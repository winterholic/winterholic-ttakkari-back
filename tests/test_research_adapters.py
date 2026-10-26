"""Unit tests for Research Lab v2 execution adapters, registry, and seed integration."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from ttakkari.host_gpu import Busy, GpuError, HostGpu, LaunchMismatch
from ttakkari.meetings.local_stt import runner as stt_runner
from ttakkari.research.adapters import (
    AdapterRunResult,
    ExecutionAdapter,
    get_adapter_registry,
)
from ttakkari.research.adapters.external_report import ExternalReportAdapter
from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
from ttakkari.research.adapters.presets import load_retrieval_preset, load_tts_preset
from ttakkari.research.adapters.retrieval_eval import RetrievalEvalAdapter
from ttakkari.research.adapters.tts_synthesis import TtsSynthesisAdapter
from ttakkari.research.readiness import compute_execution_status
from ttakkari.tts import service as tts_service
from ttakkari.tts.providers import (
    ProviderAvailability,
    ProviderState,
    TtsChunkResult,
    TtsResult,
)
from ttakkari.tts.supertonic import SupertonicProvider


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
        "source_url": "https://arxiv.org/abs/2407.21783",
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
    for key in ("source_title", "source_url", "source_year"):
        assert records[0].meta[key] == good_config[key]


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


_LLM_CONFIG = {"model": "qwen3.5-4b-q4_k_m", "n_gpu_layers": 33}
_BENCH_RESULT = {
    "execution_path": "host_gpu",
    "median_generation_tokens_per_second": 45.0,
    "median_prompt_tokens_per_second": 120.0,
    "runs": [{"prompt_tokens": 30}],
    "wall_seconds": 1.5,
    "swap": {"peak_growth_bytes": 5242880},
}
_STOPPED = {"phase": "stopped", "running": False, "stopped_now": True}


@contextlib.contextmanager
def _autospecced_host_gpu(*, owner_states, stop_result=None, stop_error=None, lock_held=False):
    """Replace HostGpu methods with autospec'd mocks so a wrong call signature fails the test.

    `owner_states` is the sequence of values `HostGpu._current()` returns (the last one repeats):
    the adapter reads it right after start() and again once the server is ready, to record the
    launch id and the supervisor/server process identity of its own launch.
    """
    pending = list(owner_states)

    def next_state(self):
        return pending.pop(0) if len(pending) > 1 else (pending[0] if pending else None)

    with patch.object(HostGpu, "start", autospec=True, return_value={"ready": True}) as start, \
         patch.object(HostGpu, "status", autospec=True, return_value={"ready": True}), \
         patch.object(HostGpu, "bench", autospec=True, return_value=dict(_BENCH_RESULT)) as bench, \
         patch.object(HostGpu, "_current", autospec=True, side_effect=next_state) as current, \
         patch.object(HostGpu, "stop", autospec=True, return_value=stop_result, side_effect=stop_error) as stop, \
         patch("ttakkari.host_gpu._lock_held", autospec=True, return_value=lock_held):
        yield SimpleNamespace(start=start, bench=bench, current=current, stop=stop)


@pytest.mark.asyncio
async def test_llm_cleanup_stops_the_launch_this_run_started() -> None:
    live = {"launch_id": "launch-A", "phase": "ready"}
    with _autospecced_host_gpu(owner_states=[live, live], stop_result=dict(_STOPPED)) as m:
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    assert result.status == "succeeded"
    assert m.stop.call_count == 1
    # ownership is enforced inside stop() on the state it reads, not by a separate pre-read here
    assert m.stop.call_args.kwargs == {"expected_launch_id": "launch-A"}
    cleanup = result.raw_output["cleanup"]
    assert cleanup["attempted"] is True
    assert cleanup["success"] is True
    assert cleanup["launch_id"] == "launch-A"


@pytest.fixture
def child_process():
    """A real process whose pid + create_time identity the adapter can later look up (no GPU server)."""
    from ttakkari import host_gpu_guard

    procs: list[subprocess.Popen] = []

    def spawn(*, alive: bool) -> tuple[int, float]:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        procs.append(proc)
        start = None
        for _ in range(100):  # create_time becomes readable right after exec
            start = host_gpu_guard.proc_start(proc.pid)
            if start is not None:
                break
            time.sleep(0.02)
        assert start is not None
        if not alive:
            proc.kill()
            proc.wait()
        return proc.pid, start

    yield spawn
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _launch_state(sup, srv, launch_id: str = "launch-A") -> dict:
    return {
        "launch_id": launch_id, "phase": "ready",
        "supervisor_pid": sup[0] if sup else None, "supervisor_start": sup[1] if sup else None,
        "server_pid": srv[0] if srv else None, "server_start": srv[1] if srv else None,
    }


_REFUSAL = LaunchMismatch("current host GPU launch 'launch-B' is not the expected launch 'launch-A'")


@pytest.mark.asyncio
async def test_llm_launch_replaced_is_success_only_when_our_processes_are_proven_absent(child_process) -> None:
    mine = _launch_state(child_process(alive=False), child_process(alive=False))
    with _autospecced_host_gpu(owner_states=[mine], stop_error=_REFUSAL) as m:
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    assert m.stop.call_count == 1  # attempted with our launch id, refused by HostGpu
    cleanup = result.raw_output["cleanup"]
    assert cleanup["launch_id"] == "launch-A"
    assert cleanup["stopped"] is False
    assert cleanup["skipped_reason"] == "launch_replaced"
    assert cleanup["process_evidence"] == {"supervisor": "absent", "server": "absent"}
    assert cleanup["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("alive", ["supervisor", "server"])
async def test_llm_launch_replaced_with_our_process_still_present_is_failure(child_process, alive) -> None:
    sup = child_process(alive=alive == "supervisor")
    srv = child_process(alive=alive == "server")
    with _autospecced_host_gpu(owner_states=[_launch_state(sup, srv)], stop_error=_REFUSAL):
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    cleanup = result.raw_output["cleanup"]
    assert cleanup["success"] is False
    assert cleanup["process_evidence"][alive] == "present"
    assert "process_present" in cleanup["error"]


@pytest.mark.asyncio
async def test_llm_launch_replaced_without_recorded_server_identity_is_unverified(child_process) -> None:
    # supervisor is gone but the server pid was never recorded: absence of the server is not proven
    mine = _launch_state(child_process(alive=False), None)
    with _autospecced_host_gpu(owner_states=[mine], stop_error=_REFUSAL):
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    cleanup = result.raw_output["cleanup"]
    assert cleanup["success"] is False
    assert cleanup["process_evidence"] == {"supervisor": "absent", "server": "unknown"}
    assert "termination_unverified" in cleanup["error"]


@pytest.mark.asyncio
async def test_llm_server_identity_recorded_after_start_is_picked_up_before_cleanup(child_process) -> None:
    sup = child_process(alive=False)
    at_start = _launch_state(sup, None)  # supervisor has not spawned llama-server yet
    when_ready = _launch_state(sup, child_process(alive=False))
    with _autospecced_host_gpu(owner_states=[at_start, when_ready], stop_error=_REFUSAL):
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    cleanup = result.raw_output["cleanup"]
    assert cleanup["process_evidence"] == {"supervisor": "absent", "server": "absent"}
    assert cleanup["success"] is True


@pytest.mark.asyncio
async def test_llm_launch_replaced_never_signals_the_replacing_launch(child_process) -> None:
    mine = _launch_state(child_process(alive=True), child_process(alive=True))
    with _autospecced_host_gpu(owner_states=[mine], stop_error=_REFUSAL) as m, \
         patch("ttakkari.host_gpu.os.kill", autospec=True) as kill:
        await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    assert m.stop.call_count == 1
    assert kill.call_count == 0


@pytest.mark.asyncio
async def test_llm_cleanup_skips_stop_when_launch_already_ended() -> None:
    mine = {"launch_id": "launch-A", "phase": "ready"}
    already = {"phase": "stopped", "running": False, "stopped_now": False}
    with _autospecced_host_gpu(owner_states=[mine], stop_result=already):
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    cleanup = result.raw_output["cleanup"]
    assert cleanup["stopped"] is False
    assert cleanup["skipped_reason"] == "already_stopped"
    assert cleanup["success"] is True


@pytest.mark.asyncio
async def test_llm_cleanup_without_verified_ownership_never_stops_and_reports_failure() -> None:
    with _autospecced_host_gpu(owner_states=[{"phase": "ready"}]) as m:  # no launch_id in state
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    assert m.stop.call_count == 0
    cleanup = result.raw_output["cleanup"]
    assert cleanup["success"] is False
    assert "ownership" in cleanup["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_result", "lock_held", "expected_error"),
    [
        # stop() waited out its 40s and the supervisor is still live
        ({"phase": "ready", "running": True, "stopped_now": True}, False, "still_running"),
        # state says stopped but the resource lock was never released
        ({"phase": "stopped", "running": False, "stopped_now": True}, True, "lock_still_held"),
        (None, False, "unexpected_stop_result"),
    ],
)
async def test_llm_cleanup_interprets_stop_result_instead_of_assuming_success(
    stop_result, lock_held, expected_error
) -> None:
    live = {"launch_id": "launch-A", "phase": "ready"}
    with _autospecced_host_gpu(owner_states=[live, live], stop_result=stop_result, lock_held=lock_held) as m:
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    assert m.stop.call_count == 1
    cleanup = result.raw_output["cleanup"]
    assert cleanup["attempted"] is True
    assert cleanup["success"] is False
    assert expected_error in cleanup["error"]


@pytest.mark.asyncio
async def test_llm_cleanup_records_stop_exception() -> None:
    live = {"launch_id": "launch-A", "phase": "ready"}
    err = RuntimeError("Lease manager failed to signal supervisor")
    with _autospecced_host_gpu(owner_states=[live, live], stop_error=err):
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    cleanup = result.raw_output["cleanup"]
    assert cleanup["attempted"] is True
    assert cleanup["success"] is False
    assert "RuntimeError" in cleanup["error"]


@pytest.mark.asyncio
async def test_llm_start_state_timeout_is_reported_as_unverified_and_stops_nothing() -> None:
    # real contract: HostGpu.start raises this after 20s while the supervisor may still come up later
    assert "did not report a state" in inspect.getsource(HostGpu.start)
    timeout = GpuError("supervisor did not report a state within 20s")
    with _autospecced_host_gpu(owner_states=[]) as m:
        m.start.side_effect = timeout
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    assert result.status == "failed"
    assert m.stop.call_count == 0
    assert m.bench.call_count == 0
    cleanup = result.raw_output["cleanup"]
    assert cleanup["attempted"] is False
    assert cleanup["success"] is False
    assert cleanup["ownership"] == "unverified"
    assert "did not report a state" in cleanup["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_error",
    [Busy("a host GPU test instance is already running; stop it first"), GpuError("supervisor exited 1 before starting the server")],
)
async def test_llm_start_rejected_or_dead_supervisor_has_nothing_to_clean(start_error) -> None:
    with _autospecced_host_gpu(owner_states=[]) as m:
        m.start.side_effect = start_error
        result = await LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal")

    assert result.status == "failed"
    assert m.stop.call_count == 0
    cleanup = result.raw_output["cleanup"]
    assert cleanup["attempted"] is False
    assert cleanup["success"] is True
    assert cleanup["skipped_reason"] == "server_not_started"


@pytest.mark.asyncio
async def test_llm_cleanup_runs_on_cancellation_after_start() -> None:
    live = {"launch_id": "launch-A", "phase": "ready"}
    cancel_event = asyncio.Event()
    with _autospecced_host_gpu(owner_states=[live, live], stop_result=dict(_STOPPED)) as m:
        m.start.side_effect = lambda self, req: cancel_event.set() or {"ready": True}
        result = await LlmInferenceAdapter().execute_run(
            uuid4(), _LLM_CONFIG, scope="personal", cancel_event=cancel_event
        )

    assert result.status == "cancelled"
    assert m.bench.call_count == 0
    assert m.stop.call_count == 1
    assert result.raw_output["cleanup"]["success"] is True


@pytest.mark.asyncio
async def test_llm_task_cancel_during_start_waits_for_owner_then_cleans() -> None:
    live = {"launch_id": "launch-A", "phase": "ready"}
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def pending_start(self, req):
        entered.set()
        try:
            assert release.wait(2)
            return {"ready": True}
        finally:
            returned.set()

    with _autospecced_host_gpu(owner_states=[live, live], stop_result=dict(_STOPPED)) as m:
        m.start.side_effect = pending_start
        task = asyncio.create_task(LlmInferenceAdapter().execute_run(uuid4(), _LLM_CONFIG, scope="personal"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "startup thread still owns an unobserved launch"
        finally:
            release.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            assert await asyncio.to_thread(returned.wait, 1)
        assert isinstance(result, AdapterRunResult)
        assert result.status == "cancelled"
        assert result.raw_output["cleanup"]["success"] is True
        assert m.stop.call_count == 1
        assert m.bench.call_count == 0


@pytest.mark.asyncio
async def test_tts_cancel_awaits_synthesis_child_cleanup() -> None:
    from ttakkari.research.adapters.tts_synthesis import _await_unless_cancelled

    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def synthesis():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    child = asyncio.create_task(synthesis())
    parent = asyncio.create_task(_await_unless_cancelled(child, None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        parent.cancel()
        await asyncio.wait_for(cleanup.wait(), 1)
        assert not parent.done(), "provider cleanup must finish before the adapter releases ownership"
    finally:
        release.set()
        await asyncio.gather(parent, child, return_exceptions=True)


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


@pytest.mark.parametrize("config", [{"quant_type": "Q2K"}, {"model": "qwen3.5-4b-q4_k_m", "quant_type": "Q2K"}])
def test_llm_quant_type_accepts_only_exact_registered_values(config) -> None:
    errors = LlmInferenceAdapter().validate_config(config, scope="personal")
    assert any("지원되지 않는 quant_type" in e and "Q2K" in e for e in errors)


@pytest.mark.parametrize("bad", ["q4_k_m", "Q4_K_M ", "Q2k", ["Q4_K_M"], 4])
def test_llm_quant_type_rejects_non_canonical_without_crashing(bad) -> None:
    errors = LlmInferenceAdapter().validate_config({"quant_type": bad}, scope="personal")
    assert any("quant_type" in e for e in errors)


def test_llm_quant_type_conflicting_with_explicit_model_is_rejected() -> None:
    errors = LlmInferenceAdapter().validate_config(
        {"model": "qwen3.5-4b-q4_k_m", "quant_type": "Q2_K"}, scope="personal"
    )
    assert any("quant_type" in e and "model" in e for e in errors)


@pytest.mark.parametrize("adapter", [LlmInferenceAdapter(), TtsSynthesisAdapter()], ids=lambda a: a.id)
def test_supported_parameters_never_contain_keys_validate_config_rejects(adapter) -> None:
    base = {"texts": ["t"]} if adapter.id == "tts_synthesis" else {"model": "qwen3.5-4b-q4_k_m"}
    for key in adapter.supported_parameters:
        errors = adapter.validate_config({**base, key: 0}, scope="personal")
        rejected = [e for e in errors if key in e and "미지원" in e]
        assert not rejected, f"{adapter.id} lists {key!r} as supported but rejects it: {rejected}"


def test_llm_supported_parameters_exclude_main_gpu_and_quant_method() -> None:
    assert not {"main_gpu", "quant_method"} & set(LlmInferenceAdapter.supported_parameters)


def test_tts_supported_parameters_exclude_split_on_comma_and_total_steps() -> None:
    assert not {"split_on_comma", "total_steps"} & set(TtsSynthesisAdapter.supported_parameters)


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


_TTS_TEXTS = ["안녕하세요 음성 합성 테스트입니다", "두 번째 문장입니다"]


def _tts_result(count: int) -> TtsResult:
    return TtsResult(
        chunks=[
            TtsChunkResult(index=i, path=f"/tmp/research_chunk_{i}.wav", duration_seconds=5.0,
                           sample_rate=24000, bytes=10000)
            for i in range(count)
        ],
        metrics={"synth_seconds": 1.0, "audio_seconds": 5.0, "load_seconds": 0.2, "peak_rss_bytes": 104857600},
    )


@contextlib.contextmanager
def _autospecced_tts(*, chunks: int = 1, run_worker=None, synthesize=None):
    """Autospec SupertonicProvider/STT runner so signature drift fails the test."""
    from ttakkari.config import get_settings

    settings = get_settings().model_copy(update={"local_stt_python": sys.executable})
    ready = ProviderAvailability(state=ProviderState.ready, reason="Ready")
    with patch("ttakkari.config.get_settings", autospec=True, return_value=settings), \
         patch.object(SupertonicProvider, "availability", autospec=True, return_value=ready), \
         patch.object(SupertonicProvider, "synthesize", autospec=True,
                      return_value=_tts_result(chunks), side_effect=synthesize) as synth, \
         patch.object(stt_runner, "run_worker", autospec=True, side_effect=run_worker) as worker:
        yield SimpleNamespace(synthesize=synth, run_worker=worker)


@pytest.mark.asyncio
async def test_tts_synthesis_stt_runner_signature_and_both_cer_metrics() -> None:
    adapter = TtsSynthesisAdapter()
    config = {"texts": _TTS_TEXTS[:1], "mode": "synthesize_and_stt", "voice": "M1"}

    async def fake_run_worker(audio_path, params, settings, *, production=True):
        assert isinstance(audio_path, Path)
        assert isinstance(params, dict)
        assert production is True
        return {"text": "안녕하세요 음성 합성 테스트입니다"}

    with _autospecced_tts(chunks=1, run_worker=fake_run_worker) as m:
        result = await adapter.execute_run(uuid4(), config, scope="personal")
        assert result.status == "succeeded"
        assert m.run_worker.call_count == 1
        evaluated = result.raw_output["stt_eval"]
        assert [e["recognized_text"] for e in evaluated] == ["안녕하세요 음성 합성 테스트입니다"]
        assert isinstance(evaluated[0]["cer_vs_normalized"], float)
        assert isinstance(evaluated[0]["cer_vs_raw"], float)
        assert evaluated[0]["cer_vs_normalized"] == 0.0
        assert evaluated[0]["cer_vs_raw"] == 0.0

        measurements = adapter.to_measurements(result, config)
        keys = {x.metric_key: x for x in measurements}
        assert {"stt_cer_vs_normalized", "stt_cer_vs_raw", "stt_wer_vs_normalized", "stt_wer_vs_raw"} <= set(keys)
        assert keys["stt_cer_vs_normalized"].is_proxy is True
        assert keys["stt_cer_vs_raw"].is_proxy is True

        m.run_worker.side_effect = RuntimeError("STT worker crashed with OOM")
        result_failed = await adapter.execute_run(uuid4(), config, scope="personal")
        assert result_failed.status == "failed"
        assert result_failed.error_code == "stt_worker_failed"


@pytest.mark.asyncio
async def test_tts_partial_stt_chunk_failure_fails_run_and_lists_failed_chunks() -> None:
    """RunStatus has no 'partial'; a missing evaluation sample must not read as a complete result."""
    calls = 0

    async def flaky_run_worker(audio_path, params, settings, *, production=True):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("worker crashed on chunk 1")
        return {"text": "안녕하세요 음성 합성 테스트입니다"}

    config = {"texts": _TTS_TEXTS, "mode": "synthesize_and_stt", "voice": "M1"}
    with _autospecced_tts(chunks=2, run_worker=flaky_run_worker):
        result = await TtsSynthesisAdapter().execute_run(uuid4(), config, scope="personal")

    assert result.status == "failed"
    assert result.error_code == "stt_worker_failed"
    assert result.raw_output["failed_chunks"] == [1]
    assert result.raw_output["stt_chunk_count"] == 2
    assert [e["chunk_index"] for e in result.raw_output["stt_eval"]] == [0, 1]
    assert "1/2" in result.error_message
    assert TtsSynthesisAdapter().to_measurements(result, config) == []


@pytest.mark.asyncio
async def test_tts_cancel_while_waiting_for_execution_lock_never_synthesizes() -> None:
    cancel_event = asyncio.Event()
    config = {"texts": _TTS_TEXTS[:1], "mode": "synthesize"}
    with _autospecced_tts(chunks=1) as m:
        await tts_service._execution_lock.acquire()
        try:
            task = asyncio.create_task(
                TtsSynthesisAdapter().execute_run(uuid4(), config, scope="personal", cancel_event=cancel_event)
            )
            await asyncio.sleep(0.05)
            cancel_event.set()
            # the lock is still held by someone else: the run must still react to cancel
            result = await asyncio.wait_for(task, timeout=3)
        finally:
            tts_service._execution_lock.release()

    assert result.status == "cancelled"
    assert m.synthesize.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_mode", ["event", "task"])
async def test_tts_cancel_during_synthesis_stops_provider_and_frees_lock(cancel_mode) -> None:
    cancel_event = asyncio.Event()
    started = asyncio.Event()
    state = {"cancelled": False}

    async def blocking_synthesize(self, chunks, *, voice, speed, lang, out_dir):
        state["out_dir"] = out_dir
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    config = {"texts": _TTS_TEXTS[:1], "mode": "synthesize"}
    with _autospecced_tts(chunks=1, synthesize=blocking_synthesize):
        task = asyncio.create_task(
            TtsSynthesisAdapter().execute_run(uuid4(), config, scope="personal", cancel_event=cancel_event)
        )
        await asyncio.wait_for(started.wait(), timeout=3)
        if cancel_mode == "task":
            task.cancel()
        else:
            cancel_event.set()
        result = await asyncio.wait_for(task, timeout=3)

    assert result.status == "cancelled"
    assert result.raw_output["reason"] == ("task_cancelled" if cancel_mode == "task" else "cancelled_during_synthesis")
    assert state["cancelled"] is True
    assert not tts_service._execution_lock.locked()
    assert not Path(state["out_dir"]).exists()


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

    # 2. normalize_embeddings is covered by the ranking/rejection tests below (False is unsupported).

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


# BM25 and the hashed 2-gram dense retriever disagree on this corpus: the exact-token doc wins BM25,
# the many-near-token doc wins dense.
_DISAGREE_CORPUS = [
    {"id": "doc-bm25", "title": "", "text": "gpu 가속 설정 안내 문서이며 이 문서는 메모리 배치 스레드 캐시 컨텍스트 길이 서버 포트 모델 로딩 타임아웃 스왑 감시 로그 정리 절차를 길게 설명한다"},
    {"id": "doc-dense", "title": "", "text": "gpus gpuz gpux 가속기 가속화 가속도"},
    {"id": "doc-x", "title": "", "text": "날씨 점심 메뉴 추천"},
]
_DISAGREE_QUERIES = [
    {"id": "q-1", "query": "gpu 가속", "relevant_ids": ["doc-bm25"], "relevance_grades": {"doc-bm25": 3}}
]


async def _ranking(**cfg) -> tuple[list[str], dict]:
    res = await RetrievalEvalAdapter().execute_run(
        uuid4(), {"corpus": _DISAGREE_CORPUS, "queries": _DISAGREE_QUERIES, **cfg}, scope="personal"
    )
    assert res.status == "succeeded", res.error_message
    query = res.raw_output["queries"][0]
    return query["ranked_hits"], query


@pytest.mark.asyncio
async def test_retrieval_dense_weight_moves_the_actual_ranking_between_bm25_and_dense() -> None:
    bm25_rank, _ = await _ranking(retriever_mode="bm25")
    dense_rank, _ = await _ranking(retriever_mode="dense_hash")
    assert bm25_rank != dense_rank  # corpus precondition: the two retrievers disagree
    assert bm25_rank[0] == "doc-bm25" and dense_rank[0] == "doc-dense"

    lexical_rank, lexical_q = await _ranking(retriever_mode="hybrid_linear", dense_weight=0.0)
    dense_only_rank, dense_q = await _ranking(retriever_mode="hybrid_linear", dense_weight=1.0)

    assert lexical_rank == bm25_rank
    assert dense_only_rank == dense_rank
    assert lexical_q["mrr_at_k"] == 1.0
    assert dense_q["mrr_at_k"] == 0.5  # relevant doc-bm25 drops from rank 1 to rank 2


def test_hash_embedder_vectors_are_already_unit_length_so_normalize_false_is_not_realizable() -> None:
    import math

    from ttakkari.context.embedder import HashEmbedder

    vectors = asyncio.run(HashEmbedder().embed(["gpu 가속 설정", "gpus gpuz gpux 가속기"], query=False))
    for v in vectors:
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


@pytest.mark.asyncio
async def test_retrieval_normalize_embeddings_false_is_rejected_not_silently_ignored() -> None:
    adapter = RetrievalEvalAdapter()
    cfg = {"corpus": _DISAGREE_CORPUS, "queries": _DISAGREE_QUERIES, "retriever_mode": "dense_hash"}

    errors = adapter.validate_config({**cfg, "normalize_embeddings": False}, scope="personal")
    assert any("normalize_embeddings" in e and "미지원" in e for e in errors)
    assert adapter.validate_config({**cfg, "normalize_embeddings": True}, scope="personal") == []

    res = await adapter.execute_run(uuid4(), {**cfg, "normalize_embeddings": False}, scope="personal")
    assert res.status == "failed"
    assert res.error_code == "invalid_config"

    # default (unset) and True produce the same, truthfully normalized, ranking
    assert (await _ranking(retriever_mode="dense_hash", normalize_embeddings=True))[0] == \
        (await _ranking(retriever_mode="dense_hash"))[0]


def test_retrieval_normalize_embeddings_is_flagged_partial_not_fully_supported() -> None:
    """The catalog keeps the parameter; readiness must show it as not fully supported."""
    assert "normalize_embeddings" not in RetrievalEvalAdapter.supported_parameters
    method = {
        "slug": "reciprocal_rank_fusion",
        "lifecycle_status": "validated",
        "adapter_hints": ["retrieval_eval"],
        "parameters": [{"key": "rrf_k"}, {"key": "normalize_embeddings"}],
        "metrics": ["recall_at_k"],
    }
    status = compute_execution_status(
        method, adapter_registry=get_adapter_registry(populate_builtins=True), scope="personal"
    )
    assert status.status == "partially_supported"
    assert status.unsupported_params == ["normalize_embeddings"]
    assert status.is_executable is True


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
