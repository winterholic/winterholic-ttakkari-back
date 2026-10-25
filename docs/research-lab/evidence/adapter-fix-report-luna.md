# 실행 어댑터 결함 교정 및 감사 해소 보고서

- 작업 일자: 2026-10-25
- 대상 브랜치: `research-v2`
- 선행 작업: 커밋 `84f6648` (계약·레지스트리), `884e3ee` (어댑터·프리셋·증거), `2eef880` (화이트리스트·시드 매핑)
- 검토 근거: `docs/research-lab/evidence/adapter-review-2eef880.md` (독립 GPT Luna worker FAILED 판정 감사 결과)

---

## 1. 해소된 결함 및 구현 내역

### 1) LLM 호스트 GPU 수명주기, stop() 시그니처 및 소유권 관리
- **결함:** `llm_inference.py`에서 `HostGpu.stop(reason=...)`로 호출하여 `TypeError`가 발생했고 빈 `except`로 삼켜져 서버 프로세스가 남을 수 있었음.
- **교정:**
  - `HostGpu.stop()`의 실제 시그니처(`(self) -> dict[str, Any]`, 무인자)에 맞추어 `asyncio.to_thread(host_gpu.stop)`으로 수정.
  - 본인이 `start()`를 성공적으로 호출하여 시작한 인스턴스(`server_started=True`)만 정리하도록 소유권을 검증.
  - 정리 시도 및 성공 여부, 정리 에러(`cleanup_info`)를 결과 `raw_output["cleanup"]`에 명시적으로 기록하여 은폐 없이 추적 가능하도록 계약 완성.

### 2) TTS STT 평가 경로 수정, 계약 및 CER/WER 산출
- **결함:** `stt_runner.run_worker(module=..., request=..., settings=s)` 호출로 실제 시그니처(`audio_path: Path, params: dict, settings, *, production: bool = True`)와 불일치했고, 에러를 삼킨 뒤 성공 처리하였으며 `stt_cer_vs_raw`가 없었음.
- **교정:**
  - 실제 `stt_runner.run_worker` 시그니처에 맞추어 `audio_path=wav_path, params={"language": "ko"}, settings=s, production=True`로 호출.
  - 반환 텍스트 추출 시 `stt_out.get("text")` 스키마 반영.
  - `chunk_sentence_map`을 통해 각 청크별 원문(`raw_reference`)과 정규화문(`normalized_reference`)을 매핑.
  - `stt_cer_vs_raw`, `stt_wer_vs_raw`, `stt_cer_vs_normalized`, `stt_wer_vs_normalized`를 모두 산출하고 `is_proxy=True` 지정.
  - `mode="synthesize_and_stt"`에서 STT 런타임 부재 또는 워커 실행 실패 시 침묵 성공하지 않고 명확한 `status="failed"`, `error_code="stt_worker_failed"` 계약 적용.

### 3) Retrieval 가중치 결합 및 무시되는 설정 실소비
- **결함:** `dense_weight` 설정 시 `hybrid_rrf`로 강제되어 `hybrid_alpha`가 소비되지 않았고, `normalize_embeddings`도 소비 코드가 없었음.
- **교정:**
  - 카탈로그 시드 명세(선형 점수 결합)에 따라 `dense_weight` 입력 시 `retriever_mode="hybrid_linear"`로 매핑.
  - `hybrid_linear`에서 `bm25_w = 1.0 - dense_weight`, `dense_w = dense_weight`로 실제 가중치 결합 수행.
  - `hybrid_rrf`에서도 가중치가 있을 경우 가중 RRF(`bm25_w * 2 / (rrf_k + rank_bm25) + dense_w * 2 / (rrf_k + rank_dense)`)로 가중치 소비.
  - `_l2_normalize(vec)` 함수를 구현하여 `normalize_embeddings=True` 시 단위 벡터 정규화 코사인 유사도, `False` 시 비정규화 벡터 내적을 수행하여 설정에 따라 점수가 달라짐을 통제 코퍼스 테스트로 검증.

### 4) LLM 설정·기법 과장 제거 및 HostGpu 지원 인자 일치
- **결함:** `temperature`, `main_gpu`, `quant_method`가 실행에 반영되지 않고, 미지원 `quant_type`이 Q4 기본 모델로 침묵 대체되었으며, 미구현 메서드가 표기됨.
- **교정:**
  - `supported_methods`에서 런타임 양자화/스레드 바인딩이 아닌 `weight_quantization`, `system_cpu_thread_binding` 제거.
  - `main_gpu`, `quant_method` 설정 시 `validate_config`에서 지원 불가 이유를 명시하고 사전 거절.
  - `temperature!=0` 설정 시 `HostGpu.bench`의 재현성 고정 원칙을 밝히고 사전 거절 (`temperature=0`만 허용).
  - `quant_type`은 사전 양자화 모델이 매핑된 `Q4_K_M`, `Q2_K`만 허용하고 그 외 값 및 모델 누락 시 침묵 대체 없이 거절.

### 5) LLM 최상위 swap 메트릭 추출
- **결함:** `HostGpu.bench` 반환값의 최상위 `swap` 대신 `raw.status.swap`을 읽어 `swap_peak_growth_mb`가 누락됨.
- **교정:** `raw.get("swap")` 최상위 필드를 우선 읽도록 교정하여 `swap_peak_growth_mb` 메트릭이 정상 생성됨.

### 6) TTS / LLM 실행 보호 및 취소 계약
- **결함:** TTS adapter가 예산 잠금을 거치지 않고, 취소 이벤트(`cancel_event`) 처리가 단계별로 확인되지 않음.
- **교정:** TTS 합성 시 `ttakkari.tts.service._execution_lock` 잠금을 획득하여 동시 실행을 보호하고, 합성/STT/벤치마크 단계별 `cancel_event` 확인 시 즉시 자원을 해제하고 `status="cancelled"` 반환.

### 7) TTS 지원 범위 과장 축소 및 audit
- **결함:** `split_on_comma`, `total_steps` 미전달, `as_word` 등의 Boolean 단일 매핑, 미구현 메서드 표기.
- **교정:**
  - `supported_methods`에서 `tts_duration_prediction`, `tts_speaker_embedding_voice_consistency` 제거 (실제 미수행).
  - `english_acronym_mode`에서 미구현된 `as_word`, `spelled_out` 입력 시 `validate_config`에서 사전 거절 (`korean_alphabet`, `raw`만 지원).
  - `split_on_comma`, `total_steps` 입력 시 `validate_config`에서 미지원 이유를 밝히고 사전 거절.

### 8) 환경 준비 상태 정직한 분리
- **결함:** Supertonic 가용성과 무관하게 항상 `ready=True`를 반환하고 retrieval도 E5 부재 시 상태 구분이 모호함.
- **교정:** TTS `check_environment`에서 Supertonic 모델 가용성(`synth_ready`)을 직접 반영하여 모델 부재 시 `ready=False` 및 전처리 전용 모드 안내. Retrieval도 기본 인프로세스와 E5 모델의 준비 상태를 명확히 분리하여 반환.

### 9) ko_tech_docs_v1 qrels 정합성 교정
- **결함:** 기술문서 평가셋의 q-02~q-05 질의에 직접 관련성이 낮은 문서들이 보조 정답으로 포함되어 평가를 왜곡함.
- **교정:**
  - `q-02` (KV 캐시 양자화): `doc-01` (호스트 GPU 실행기) 제거 -> `["doc-03"]`
  - `q-03` (Flash Attention): `doc-03` (KV 캐시) 제거 -> `["doc-04"]`
  - `q-04` (Kokoro 대신 Supertonic): `doc-07` (텍스트 정규화) 제거 -> `["doc-06"]`
  - `q-05` (텍스트 정규화 절차): 보조 문서 제거 및 직접 문서만 지정 -> `["doc-07"]`

---

## 2. 검증 실행 증거

### 회귀 테스트 실행 명령 및 결과
```text
$ PYTHONPATH=src pytest tests/test_research_adapters.py
============================= test session starts ==============================
platform darwin -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2
configfile: pyproject.toml
plugins: timeout-2.4.0, asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=session, asyncio_default_test_loop_scope=session
collecting ... collected 14 items

tests/test_research_adapters.py ..............                           [100%]

============================== 14 passed in 0.12s ==============================
```

### 테스트 케이스 상세 (14건 전수 통과)
1. `test_registry_discovery_and_lookup`: 프로토콜 및 화이트리스트 기반 어댑터 매핑, 미구현 메서드의 external_report 라우팅 확인.
2. `test_presets_loading`: 기술문서 및 음성 프리셋 로딩 확인.
3. `test_external_report_adapter_lifecycle`: 외부 보고 어댑터 검증 및 실행.
4. `test_retrieval_eval_adapter_catalog_mapping_and_execution`: RAG 시드 파라미터 매핑 및 선형 결합(hybrid_linear) 실행.
5. `test_tts_synthesis_adapter_catalog_mapping_and_execution`: TTS 정규화 및 사용자 발음사전 치환 실행.
6. `test_llm_inference_adapter_catalog_mapping_and_validation`: LLM 스코프 격리, 시드 파라미터 매핑 및 취소 확인.
7. `test_readiness_axis_f_determination_with_seed_methods`: readiness F축 판정 로직 확인.
8. `test_llm_inference_stop_signature_and_cleanup_contract`: HostGpu.stop 무인자 시그니처 바인드 및 cleanup 기록 확인.
9. `test_llm_inference_parameter_rejections_and_quant_type_mapping`: main_gpu, quant_method, temperature, quant_type 거절 및 매핑 확인.
10. `test_llm_inference_swap_top_level_metric`: 최상위 swap 딕셔너리 기반 swap_peak_growth_mb 정상 생성 확인.
11. `test_tts_synthesis_stt_runner_signature_and_both_cer_metrics`: stt_runner.run_worker 실제 시그니처 바인드, raw/normalized CER·WER 산출 및 STT 실패 계약 확인.
12. `test_tts_synthesis_unsupported_parameters_and_methods_audit`: english_acronym_mode(as_word/spelled_out), split_on_comma, total_steps 거절 및 미구현 메서드 제외 확인.
13. `test_retrieval_dense_weight_and_normalize_embeddings_influence`: 통제 코퍼스에서 dense_weight(0.05 vs 0.95), normalize_embeddings(True vs False) 점수 영향 및 유효성 검사 확인.
14. `test_preset_qrels_consistency`: ko_tech_docs_v1 qrels의 q-02~q-05 정합성 확인.

---

## 3. 미검증 범위 및 환경 한계
- 본 실행은 로컬 샌드박스 내부 테스트 환경에서 수행되었으며, 실제 하드웨어 GPU 및 Metal llama-server 데몬을 구동한 호스트 실측은 호스트 GPU MCP 경로(`host_gpu_start`/`bench`)를 통해 별도로 수행되어야 합니다.
- STT/TTS는 로컬 음성 모델 가중치 및 Whisper 런타임에 대한 단위 테스트 시그니처 바인딩으로 검증되었으며, 인간 청취 품질(MOS) 평가는 대리 지표(CER/WER)와 구분되어야 합니다.
