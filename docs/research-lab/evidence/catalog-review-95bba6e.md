# AI Research Lab v2 방법론 카탈로그 시드 독립 재검증

- **판정: FAILED** — 구조 교정과 요구 목록 범위는 확인했지만, 근거·정량 주장·TTS 엔진 정합성에서 §2.3 위반이 남아 있다.
- 대상: `research-v2`, HEAD `95bba6e8e12fcfd3536c688613416f7ae5ec1974`; 이전 실패 `a972fe6` 대비 교정 커밋 확인.
- 비교 기준: `docs/research-lab/refactor-v2-requirements.md` §2(특히 §2.3), `docs/research-lab/refactor-v2.md` §2, 프로젝트 `docs/design/local-tts.md`.
- 범위: 카탈로그 JSON과 계약 테스트는 읽기·실행만 했다. 다른 작업자의 변경 파일은 건드리지 않았다. 본 보고서만 추가한다.

## 판정 요약

| 영역 | 판정 | 관찰 |
|---|---|---|
| 파라미터 스키마 | **PASS (구조)** | 112개 방법, 185개 파라미터. 전부 `required`·`sweepable` 존재 및 boolean. slug 중복, taxonomy 참조 오류, relation target 오류 없음. 타입별 options, 숫자 default 경계도 별도 검사에서 오류 없음. |
| 요구 §2.3 초기 방법론 목록 | **PASS (포함)** | 아래 전수 대조 결과 필수 항목 누락 없음. 포함 자체가 실행 가능성이나 내용의 진실성을 보증하지는 않는다. |
| 근거 상태 분류 | **FAIL** | 50개 evidence 빈 항목이 전부 `widely_practiced`, `unverified` 0개. 일부 관행은 일반적으로 알려져 있어도 50개 전부가 이 분류를 충족한다는 근거가 없고, 여러 항목은 관행 설명을 넘어 수치 효과를 주장한다. |
| 근거 없는 수치·절대 주장 | **FAIL** | 빈 evidence 항목 중 28개에 수치/효과 수치가 남아 있다. 근거 없는 절대 표현도 다수. source_reported 항목에도 인용이 해당 수치나 호환성 주장을 입증하지 않는 사례가 보인다. |
| Kokoro·TTS 정합성 | **FAIL** | 폐기된 한국어 Kokoro 가설이 실행 가능한 튜닝 항목처럼 남았고, 주변 방법의 런타임·relation에도 Kokoro가 연결된다. 현재 운영 엔진 Supertonic 3 및 미구현 조건은 일부 notes에만 적혀 있다. |
| 계약 테스트 | **부분 PASS / 의미 검증 불충분** | 지정 테스트는 5개 모두 통과했지만, 수치·evidence 상태 의미·호환성 정합성에 대한 검사가 거의 없다. 형식 통과를 콘텐츠 검증으로 해석할 수 없다. |

## 이전 실패 사유 재확인

1. **파라미터 185개 required/sweepable 누락** — 해소. 185개 모두 두 필드가 있고 boolean이다. 전체 테스트도 통과했다.
2. **빈 evidence 69개 중 근거 없는 수치·“보장/완벽/실시간”** — 빈 evidence 수 자체는 50개로 줄었지만 모두 `widely_practiced`로 바뀌었고, 수치 표현 문제는 남았다(아래 표). 절대 표현을 더 넓게 표현만 고친 곳도 있어 전부 해소된 것은 아니다.
3. **TTS CER 80%, Kokoro 한국어, RTF<0.1, 200ms 미만** — 전수 검색에서 CER 80%, RTF<0.1, 200ms 미만 문구는 찾지 못했다. TTS Korean normalization 효과는 숫자를 빼고 proxy 한계를 표기했다. 그러나 Kokoro를 폐기된 가설로 명확히 격리하지 못했고 `tts_kokoro_pipeline_tuning`은 항목으로 남아 있다. 해당 항목의 운영 Supertonic 수치 `TTFA 0.70~1.03초`, `RTF 0.17~0.19`는 project local-tts 설계에 적힌 실측값과 맞지만, 그 항목의 evidence는 Kokoro 저장소만 가리켜 해당 Supertonic 수치를 직접 뒷받침하지 않는다.
4. **FlashAttention 논문 수치를 일반 환경/Metal에 적용** — 큰 오류는 부분 교정. `flash_attention` compatibility notes와 expected_effects는 CUDA 원논문 조건 및 Apple Metal의 실측 필요성을 명시한다. 다만 근거 claim의 “표준 대비 2~4배”는 논문 결과를 한 덩어리로 단순화했고, 실제 논문 초록의 결과는 BERT-large 15%, GPT-2 3배, Long Range Arena 2.4배로 데이터·모델별이다. 기존처럼 Metal까지 보편 성능으로 직접 주장하지는 않지만 claim 문구는 정밀화 필요.
5. **QLoRA 24GB에서 65B/70B 학습 주장** — 해소. `training.json::qlora`는 33B/24GB, 65B/48GB(A100)로 분리 기재하고 evidence claim도 65B/48GB다. Apple Silicon MLX 지원은 CUDA/bitsandbytes와 별개 메커니즘이라고 설명한다. 다만 compatibility의 하드웨어-런타임 조합은 별도 행렬로 표현되지 않아 조건을 더 구체화할 필요가 있다.

## 근거 상태: `widely_practiced` 표본 판정

빈 evidence 50건은 모두 `widely_practiced`다. 아래 18개 표본을 서로 다른 영역에서 확인했다. “기법의 존재/사용이 흔함”과 “이 항목의 제시 문장·수치·현재 환경 적용이 업계 표준”은 별개다.

| 파일 / slug | 확인한 주장 | 판정 |
|---|---|---|
| `agents_orchestration.json::agent_tool_schema_optimization` | 도구 설명 개선으로 잘못된 호출 오류 70% 이상 격감 | 도구 스키마를 명료하게 쓰는 것은 권고 관행일 수 있으나 70% 효과는 evidence 없는 정량 주장. `widely_practiced`로 수치를 정당화할 수 없음. |
| `agents_orchestration.json::context_sliding_window_summarization` | 최근 K턴 보존, API 비용 60% 절감 | 요약 기반 context 관리 자체는 통용되는 설계지만 K와 60%는 특정 설정/비교군/평가 결과가 없으면 일반 사실 아님. |
| `agents_orchestration.json::agent_safety_guardrails` | 공격 99% 무력화 | 과장된 보안 보증. guardrail의 존재는 표준적 방어 전략이라고 해도 모든 공격의 99% 차단을 뜻하지 않음. |
| `generative_multimodal.json::diffusion_step_count_tuning` | 10~50 step, 생성 지연 40~50% 단축 | step 범위와 이득은 모델·sampler·품질 기준에 종속. 근거와 조건 없음. |
| `generative_multimodal.json::diffusion_vae_tiling_slicing` | 1024×1024+, 2k/4k, VRAM 70% 절감 | tiling은 특정 구현에 있는 기법이나 일률적인 메모리 절감 수치는 미입증. |
| `generative_multimodal.json::vlm_image_resolution_patching` | OCR/문서 정확도 40% 이상 향상 | 모델·해상도·벤치마크가 특정되지 않은 수치. |
| `systems.json::system_cpu_thread_binding` | P-core pinning, CPU 추론 15~30% 향상 | OS/런타임/칩마다 다름. 일반 관행으로 고정 가능한 효과 아님. |
| `systems.json::system_batch_size_throughput_tuning` | GPU 100% 활용, 처리량 3배 이상 | batch 탐색은 통상적이나 100% 사용과 3배 효과를 보편 결과처럼 제시할 근거 없음. |
| `systems.json::system_memory_allocation_tuning` | mlock으로 swap lag 완전 제거, `jemalloc/mimalloc` | mlock의 OS·메모리 압력 전제 및 allocator 영향 조건이 빠짐. “완전 제거”는 보장처럼 쓰임. |
| `systems.json::system_workload_engine_routing` | llama.cpp/vLLM/CoreML 등 라우팅으로 완수 시간 30% 단축 | 라우팅 개념은 plausible하지만 현재 해당 엔진 연결·워크로드 계측 여부도 표시되지 않고 30% 근거 없음. |
| `stt.json::stt_chunk_window_overlap` | 30초 윈도우와 경계 오류 90% 제거 | Whisper 처리 방식 설명과 90% 개선은 별도 주장. 오디오/모델별 효과 근거 없음. |
| `stt.json::stt_context_prompt_conditioning` | Kokoro를 초기 힌트 문자열에 포함, 전문용어 정확도 50% 개선 | 옵션은 Whisper 계열에 존재하지만 그 수치 일반화 근거 없음. Kokoro는 음성 합성 이름이므로 이 STT 프롬프트 예시는 도메인 데이터로서 부적절/혼란. |
| `stt.json::stt_hallucination_detection` | 무음 구간 환각 99% 차단 | heuristic threshold의 데이터셋별 정밀도·재현율 없이 보안 필터처럼 보장 수치 사용. |
| `stt.json::stt_word_level_timestamps` | “밀리초 수준” 정렬, 실시간 하이라이팅 | 대략적 타임스탬프보다 세밀한 정렬은 모델/정렬 방식 의존. 실시간으로 제공된다고 보장할 근거 없음. |
| `rag_retrieval.json::document_chunking_strategy` | Recall@k 최대 15~20% 개선 | 일반적인 chunking 탐색 효과가 아닌 특정 벤치/코퍼스 결과처럼 읽힘. 비교 데이터 없음. |
| `rag_retrieval.json::query_rewriting` | 검색 실패 90% 차단, 50~200ms 증가 | 양쪽 수치 모두 모델·환경·측정 기준 없음. 후자는 보편 지연값으로 제시할 수 없음. |
| `rag_retrieval.json::retrieval_result_caching` | 유사도 0.95, 지연 95% 단축·비용 0 | 0.95는 예시 threshold라고 쓰지만 보편 기본값처럼 이해될 수 있고, 95%/zero 비용은 입력·캐시 hit 조건 및 검증 없는 주장. |
| `tts.json::tts_speech_stitching` | 5~10ms crossfade / 150~350ms 무음, 매끄러운 연결 보장 | 구현 파라미터 권고값으로는 쓸 수 있으나 근거·기본값 성격을 밝히지 않음. 호흡·문장 경계마다 달라짐. |

따라서 **50건 전부가 잘못된 업계 관행이라고 단정하지는 않는다.** 다만 18건 표본만으로도 `widely_practiced`는 일반적 기법 존재를 근거로 항목 전체를 덮는 데 쓰였고, 특히 수치 주장의 출처 상태를 숨긴다. §2.3이 허용한 `unverified`로 둔 뒤 개념 설명과 근거 있는 source-reported claim을 분리해야 한다. 위 표본의 재현은 `python` JSON 스캔으로 `evidence_status`, `evidence`, effect/compatibility 텍스트를 확인했다.

### 빈 evidence이면서 수치가 남은 항목 28개

아래는 **수치 표현 전수 스캔**에서 `evidence=[]`인데 설명/효과/부작용에 숫자 또는 성능 수치가 포함된 전부다. 숫자는 범위·구성 파라미터를 포함하며, 특히 효과 수치는 삭제하거나 근거·대상 조건·비교군과 함께 `source_reported`로 바꿔야 한다.

| 파일 | slug: 근거 없는 수치 표현 |
|---|---|
| `agents_orchestration.json` | `agent_tool_schema_optimization`: 오류율 70% 이상 격감; `context_sliding_window_summarization`: 비용 60%, 최근 K턴; `agent_tool_selection_routing`: 상위 3~5개, 토큰 80% 절감; `agent_safety_guardrails`: 공격 99% 무력화 |
| `classical_ml.json` | `feature_selection_recursive_elimination`: 지연 50% 단축; `ensemble_stacking_blending`: 오차 1~3% 추가 감소; `feature_scaling_outlier_clipping`: 상하위 1% 윈저화 |
| `generative_multimodal.json` | `diffusion_step_count_tuning`: 10~50 step, 지연 40~50% 단축, 12 이하; `diffusion_vae_tiling_slicing`: 1024×1024, 2k/4k, VRAM 70%, 시간 1.5~2배; `vlm_image_resolution_patching`: 정확도 40% 이상 |
| `llm_inference.json` | `kernel_fusion`: 지연 10~25%; `prompt_context_length_tuning`: 128k 예시 및 “수 GB” 절감 |
| `rag_retrieval.json` | `document_chunking_strategy`: Recall@k 15~20%; `query_rewriting`: 실패 90%, 지연 50~200ms; `retrieval_result_caching`: threshold 0.95, 지연 95%, 비용 0 |
| `stt.json` | `stt_chunk_window_overlap`: 30초, 경계 오류 90%; `stt_beam_search_decoding`: CER 10~15%; `stt_context_prompt_conditioning`: 30초, 정확도 50%; `stt_hallucination_detection`: 99%; `stt_language_detection_routing`: 30초 |
| `systems.json` | `system_cpu_thread_binding`: 15~30%; `system_batch_size_throughput_tuning`: GPU 100%, 처리량 3배; `system_gpu_kernel_profiling`: 상위 1~2개; `system_data_transfer_overhead_minimization`: 지연 0초; `system_workload_engine_routing`: 30%; `system_numa_memory_interleaving`: 2~3배 |
| `training.json` | `training_data_sampling_augmentation`: 10k~50k 샘플 |
| `tts.json` | `tts_speech_stitching`: 5~10ms, 150~350ms |

**절대/보장 표현:** 빈 evidence 방법에서 다음 slug를 따로 확인했다: `agent_tool_schema_optimization`, `agent_safety_guardrails`, `feature_selection_recursive_elimination`, `diffusion_vae_tiling_slicing`, `vlm_image_resolution_patching`, `system_cpu_thread_binding`, `system_batch_size_throughput_tuning`, `system_metal_vs_cuda_path_comparison`, `system_memory_allocation_tuning`, `system_workload_engine_routing`, `stt_chunk_window_overlap`, `stt_beam_search_decoding`, `stt_context_prompt_conditioning`, `stt_hallucination_detection`, `stt_language_detection_routing`, `stt_word_level_timestamps`, `document_chunking_strategy`, `query_rewriting`, `retrieval_result_caching`, `context_sliding_window_summarization`, `agent_tool_selection_routing`, `regularized_linear_models`, `tts_speech_stitching`, `tts_streaming_chunk_synthesis`. 발견 표현은 “완전/완벽”, “보장”, “100%”, “실시간”, “정확한/즉시”, “99% 차단”, “90% 제거” 등이다. 일부는 부작용 문맥/기능 설명에도 나오므로 전부가 같은 심각도는 아니지만, 무엇이 제품 기능·목표·가설인지 표현으로 구분할 필요가 있다.

## Kokoro 전수 판정

`rg -o -i 'kokoro' src/ttakkari/research/catalog_seed/*.json` 결과 33회(반복 언급 포함), 10개 방법에서 확인했다.

| 파일 / slug | 내용 및 판정 |
|---|---|
| `tts.json::tts_korean_text_normalization` | `combines_with`가 `tts_kokoro_pipeline_tuning`을 한국어 전처리 대상처럼 연결. 현 운영 엔진은 Supertonic 3. Kokoro를 폐기된 한국어 타깃으로 명시하거나 해당 연결을 가설로 낮춰야 한다. |
| `tts.json::tts_korean_g2p` | tags에 `kokoro`. 일반 G2P 설명에 검색 alias 정도로는 가능하지만 운영 지원처럼 보이지 않게 태그 성격을 분명히 해야 한다. |
| `tts.json::tts_prosody_control` | runtime에 `kokoro`; notes는 Supertonic에서 F0 직접 제어에 추가 어댑터 필요라고 함. 엔진이 alternative인지 실행 지원인지 호환성에서 구분되지 않음. |
| `tts.json::tts_duration_prediction` | 원리에서 Kokoro 계열 언급, relation에서 Kokoro pipeline에 결합. Supertonic 현재 speed 파라미터와 별도인 모델 구조 가설로 제한 필요. |
| `tts.json::tts_streaming_chunk_synthesis` | runtime에 Kokoro/PyTorch가 있고, notes에는 현재 런타임이 단일 배치 완료 후 전달하며 실제 streaming은 Phase 2 필요라고 함. compatibility가 지원처럼 보이는 점은 자기모순. |
| `tts.json::tts_sampling_decoding_tuning` | diffusion/flow TTS 튜닝을 Kokoro decoder parameter라고 단정. Kokoro architecture/decoder와 설명한 sampling 파라미터의 일치 근거 없음. 폐기 엔진 실행 설정처럼 제시. |
| `tts.json::tts_speaker_embedding_voice_consistency` | notes에서 Kokoro 공식 가중치의 한국어 미지원 사실을 정확히 적음. 그러나 method의 다른 부분은 Supertonic 운영 엔진으로 설명하고 있어 Kokoro 경고는 비교 대상/미지원 대안으로 분리해야 함. |
| `tts.json::tts_kokoro_pipeline_tuning` | **주요 결함.** 제목·summary·parameters(default `pytorch_mps`, 24kHz, backend sweep)상 실행 가능한 한국어 합성 파이프라인처럼 보임. 본문에는 한국어 공식 음성 없음·커뮤니티 IPA 가설·Supertonic 운영을 함께 적지만, `adapter_hints`에 TTS 실행기가 지정되고 compatibility는 Kokoro/PyTorch·CUDA/Metal/CPU로 넓게 선언됨. 사용자 제공 프로젝트 결정상 Kokoro는 한국어 미지원으로 폐기되었으므로 hypothesis 또는 historical/unsupported로 내리고, 한국어 Kokoro 실행 선택지를 현재 지원처럼 노출하지 말 것. |
| `systems.json::system_workload_engine_routing` | runtime 목록에 `kokoro`를 포함해 엔진 라우팅 지원처럼 표현. 현재 Supertonic 3가 운영 엔진이므로 이름을 Supertonic으로 고치거나 legacy reference임을 표기해야 함. |
| `stt.json::stt_context_prompt_conditioning` | parameter default에 “Kokoro 음성 합성”이 들어감. Kokoro가 Whisper 도메인 용어로 유효하지 않고, 폐기된 TTS 엔진을 STT prompt 예시에 넣어 혼란을 줌. 삭제 권장. |

프로젝트 정본 `docs/design/local-tts.md`는 `LOCAL_TTS_ENGINE=supertonic`, Supertonic 3 채택, Kokoro 한국어 음성 부재를 적는다. seed의 몇몇 notes가 이 사실을 고지하더라도 enum/default/compatibility/relations 자체가 실행 가능성을 암시하므로 면책 문구만으로 정합성이 확보되지 않는다.

## taxonomy·relation·파라미터 점검과 §2.3 범위

별도 읽기 전용 JSON 검사 결과:

- 112개 method slug 모두 고유. relations의 모든 target이 카탈로그 안에 존재.
- 각 method의 targets/layers/goals/interventions/research_methods 참조는 taxonomy에 존재.
- 185개 파라미터 type은 §2.3 11종 중 하나. enum/multi_enum의 options 존재 여부, 숫자형 default의 min/max 범위 검사 오류 없음.
- 분류나 스키마 형식 오류는 찾지 못함. 테스트가 보장하는 범위보다 위 검사를 더한 결과이며, 숫자 주장 진위를 검사한 것은 아니다.

요구 §2.3 최소 포함 목록은 전부 항목을 확인했다. 대표 매핑:

- **LLM/추론:** `weight_quantization`, `model_pruning`, `knowledge_distillation`, `kv_cache_quantization`, `kv_cache_reuse`, `flash_attention`, `speculative_decoding`, `continuous_batching`, `prefix_caching`, `kernel_fusion`, `memory_layout_optimization`, `prefill_decode_disaggregation`, `cpu_gpu_offloading`, `runtime_backend_comparison`, `prompt_context_length_tuning`.
- **학습:** `lora`, `qlora`, `full_parameter_finetuning`, `dpo`, `learning_rate_scheduling`, `optimizer_comparison`, `gradient_accumulation`, `mixed_precision_training`, `training_data_sampling_augmentation`, `hyperparameter_tuning_optuna`.
- **RAG:** `document_chunking_strategy`, `hybrid_retrieval`, `query_expansion`, `query_rewriting`, `cross_encoder_reranking`, `embedding_model_benchmark`, `vector_index_structure_tuning`, `context_compression`, `retrieval_result_caching`, `reciprocal_rank_fusion`.
- **TTS:** `tts_korean_text_normalization`, `tts_korean_g2p`, `tts_pronunciation_dictionary`, `tts_phoneme_alignment`, `tts_prosody_control`, `tts_duration_prediction`, `tts_vocoder_benchmark`, `tts_audio_postprocessing`, `tts_sentence_segmentation`, `tts_speech_stitching`, `tts_streaming_chunk_synthesis`, `tts_sampling_decoding_tuning`, `tts_speaker_embedding_voice_consistency`.
- **STT:** `stt_voice_activity_detection`, `stt_chunk_window_overlap`, `stt_beam_search_decoding`, `stt_temperature_fallback`, `stt_context_prompt_conditioning`, `stt_hallucination_detection`, `stt_speaker_diarization`, `stt_language_detection_routing`, `stt_audio_preprocessing`.
- **시스템:** `system_memory_bandwidth_profiling`, `system_tensor_parallelism`, `system_batch_size_throughput_tuning`, `system_cpu_thread_binding`, `system_gpu_kernel_profiling`, `system_metal_vs_cuda_path_comparison`, `system_data_transfer_overhead_minimization`, `system_memory_allocation_tuning`, `system_compiler_optimization_flags`, `system_workload_engine_routing`.

## 근거 URL 12개 표본 확인

각 URL은 열어 제목/본문 요지를 확인했다. “페이지가 존재하고 기술 주제와 맞음”과 “seed의 모든 효과 수치가 그 URL로 입증됨”은 구분했다. 대부분 전자는 확인했으나, 일반 논문/저장소가 catalog의 환경별 수치나 현재 Mac 지원을 증명하지는 않는다.

| seed slug / URL | 확인 결과 |
|---|---|
| `weight_quantization` — [AWQ arXiv](https://arxiv.org/abs/2306.00978) | 페이지/제목/activation-aware weight-only quantization 설명 일치. AWQ 일반 주제 근거. |
| `flash_attention` — [FlashAttention arXiv](https://arxiv.org/abs/2205.14135) | 존재. 초록은 BERT-large 15%, GPT-2 3×, LRA 2.4×의 서로 다른 조건을 명시. seed claim “2~4배”는 지나친 요약. Metal 수치 근거 아님. |
| `speculative_decoding` — [Speculative Decoding arXiv](https://arxiv.org/abs/2211.17192) | 존재/내용 일치. T5-XXL·T5X 구현에서 2~3× 보고. 모든 모델·runtime 보장은 아님. |
| `memory_layout_optimization` — [PagedAttention arXiv](https://arxiv.org/abs/2309.06180) | 존재/내용 일치. vLLM 및 평가에서 2~4× throughput 조건을 설명. Metal/llama.cpp에 동일 결과를 증명하지 않음. |
| `prefill_decode_disaggregation` — [Splitwise arXiv](https://arxiv.org/abs/2311.18677) | 존재/주제 일치. GPU cluster phase splitting 논문이라 일반 single Mac 성능 수치 아님. |
| `reflection_self_correction` — [Reflexion arXiv](https://arxiv.org/abs/2303.11366) | 존재/주제 일치. coding HumanEval 예시 성과는 해당 benchmark·모델 조건에 한정. |
| `tts_duration_prediction` — [FastSpeech arXiv](https://arxiv.org/abs/1905.09263) | 존재/내용 일치. phoneme duration prediction 설명 확인. 이 논문이 Kokoro나 Supertonic 파라미터 지원을 입증하지 않음. |
| `tts_prosody_control` — [StyleTTS 2 arXiv](https://arxiv.org/abs/2306.07691) | 존재/latent style diffusion 설명 확인. 해당 기법을 현재 Supertonic에 직접 적용할 수 있다는 근거 아님. |
| `tts_kokoro_pipeline_tuning` — [Kokoro repository](https://github.com/hexgrad/kokoro) | repository 존재. 웹 페이지 본문에서 “Korean”/“Supported Languages” 문자열을 찾지 못해 해당 페이지 단독으로 언어 목록 검증은 미완료. 프로젝트 `local-tts.md`는 Kokoro 한국어 음성 부재·채택 폐기를 명시. |
| `kv_cache_quantization` — [llama.cpp repository](https://github.com/ggerganov/llama.cpp) | 저장소 존재, 현재 upstream은 `ggml-org/llama.cpp`로 redirect. 이 페이지 표본 조회만으로 seed의 모든 KV 옵션/버전별 제약과 구체 절감률은 검증하지 못함. |
| `gradient_boosting_tree_tuning` — [NeurIPS LightGBM paper](https://proceedings.neurips.cc/paper/2017/hash/6449f44a102fde848669bdd9eb6b76fa-Abstract.html) | 페이지/초록 존재, GOSS·EFB 내용과 일치. |
| `hyde_hypothetical_document` — [HyDE arXiv](https://arxiv.org/abs/2212.10496) | 존재/내용 일치. query로 가상 문서를 만들고 dense retrieval을 수행하는 방법 설명. |

표본 12/12는 도달 가능했고, 논문/기법 주제는 대체로 일치했다. 그렇다고 evidence claim의 상세 수치나 하드웨어 조합을 모두 확인한 것은 아니다. 네이티브 소스 파일 URL 두 건은 HTTP URL이 아니다: `tts_sentence_segmentation` → `docs/tts/benchmark-2026-10-21.md`, `tts_speaker_embedding_voice_consistency` → `docs/design/local-tts.md`. 이들은 같은 repository의 상대 경로 문서이므로 외부 링크로 열리지 않으며 저장소 내부 근거 경로로 취급해야 한다.

## 모델·하드웨어 호환성

- **FlashAttention:** 주의 문구가 CUDA 논문 결과와 Metal 로컬 실측 필요를 나눠 이전의 공통 적용 오류를 상당 부분 해소했다. 하지만 호환성 표현은 `hardware`와 `runtimes`를 독립 배열로 둔다. `apple_silicon_metal` + `vllm` 같은 조합이 유효한지 소비자가 판별할 pair mapping이 없고, `pytorch`도 어떤 kernel/버전인지 불명확하다. `flash_attention` method의 CUDA 원논문 성능과 현재 런타임별 실지원은 별도 필드/조건으로 명시할 것.
- **QLoRA:** 33B/24GB와 65B/48GB 분리는 정확한 방향. `hardware: cuda, apple_silicon_metal`, `runtimes: pytorch, mlx`, `requires: bitsandbytes_or_mlx_quant`를 단순 독립 목록으로 제공하면 어떤 조합에서 QLoRA 동작하는지 과도하게 넓게 읽힐 수 있다. CUDA+bfloat/bitsandbytes와 MLX quantized LoRA는 서로 다른 지원 경로이므로 상호 조합을 직접 나열할 것.
- **KV quant:** `llama.cpp`에서 value cache quantization과 Flash Attention 요구를 notes에 한정한 점은 유용하다. 그러나 `hardware`에 CPU/Metal/CUDA, `runtimes`에 llama.cpp/vLLM을 나열하고 `requires: flash_attention`를 부여해 모델별·backend별 차이를 완전하게 설명하지 않는다. CPU 경로 또는 vLLM의 정확한 기능 지원 여부를 각 pair별 확인 없이 통합 지원으로 읽게 된다.
- **Speculative decoding:** draft model 경로와 ngram 대체를 compatibility/notes로 분리했는지 전수 검증하지 못했다. 실제 adapter에서 지원하는 method parameters와 호환성 교차 매트릭스 확인 필요. 여기서 “전부 미지원”이라고 판정하는 것은 아니다.
- 전반적으로 `compatibility.hardware`와 `.runtimes`의 교차 조합이 직교 목록인지 가능한 조합의 집합인지 스키마에서 정의되지 않았다. 모델·플랫폼별 지원 차이를 공통 지원처럼 표현하지 말라는 §2.3 원칙을 기계적으로 보증하기에 부족하다.

## 계약 테스트 검토

실행 명령:

```text
uv run pytest tests/test_catalog_seed_contract.py
```

첫 시도는 코드나 pytest 실패가 아니라 uv 기본 캐시 초기화에서 실패했다: `failed to open file '/Users/winterholic/.cache/uv/sdists-v9/.git': Operation not permitted`. 허용된 임시 캐시에서 같은 테스트를 다시 실행:

```text
UV_CACHE_DIR=/tmp/uv-catalog-review-95bba6e uv run pytest tests/test_catalog_seed_contract.py
```

핵심 출력:

```text
collected 5 items
..... [100%]
5 passed in 0.70s
```

테스트 본문은 실제 JSON 파싱, method 개수 112 고정, taxonomy 코드 참조, relation target 및 slug 유일성, 파라미터 필드/type 존재·bool, 일부 절대 표현에 대한 regex를 확인한다. 이는 유용한 **형식 계약 검사**지만 콘텐츠 검증으로는 불충분하다:

- forbidden regex는 9개 패턴만 하드코딩하며 이번에 검출한 “70%”, “99%”, “실시간”, “완벽히 연동” 등의 다수 문장을 놓친다.
- `evidence_status` 허용값/근거 배열과 상태 관계를 검사하지 않아 50개 빈 evidence + `widely_practiced`가 통과한다.
- 수치 claim에 출처·비교 조건이 있는지, evidence URL이 claim을 지지하는지 검사하지 않는다.
- 숫자 type default/min/max/step, enum default가 options에 포함되는지, parameter key 중복, relation type 등은 검증하지 않는다(이번 별도 수동 검사에서 해당 subset의 오류는 발견하지 않음).
- 하드웨어·runtime 호환성 교차 조건, 운영 TTS 엔진, deprecated model, adapter 실지원은 검사하지 않는다.

테스트 통과는 본문에 적힌 다섯 체크가 맞음을 뜻할 뿐, §2.3의 근거 수준·정량 claim·하드웨어 호환성이 맞다는 증거가 아니다.

## 권장 수정 우선순위

### 지금 수정

1. 50개 빈 evidence 항목 가운데 관행 자체를 source로 입증할 수 없는 것은 `evidence_status="unverified"`로 표기. 근거가 있는 관행 설명은 짧고 중립적인 principle로 제한하고 정량 결과·효과는 제거한다.
2. 위 수치 전수표 28건에서 숫자 성능 효과/운영값을 제거하거나, 해당 모델·데이터·하드웨어·runtime·비교군·측정 출처를 연결한다. `widely_practiced`는 효과 수치의 출처 대용으로 사용하지 않는다.
3. Kokoro를 현재 한국어 TTS 방법 목록에서 미지원/historical hypothesis로 명확히 낮춘다. `tts_kokoro_pipeline_tuning`의 현재 선택 가능한 `pytorch_mps`·CPU/CUDA backend와 adapter hint를 제거/비활성화하거나 별도 미지원 hypothesis로 만든다. 주변 Kokoro relation/runtime/tag와 STT prompt default를 정리한다. **운영 엔진 이름은 Supertonic 3**로 통일한다.
4. FlashAttention claim을 논문 구성별 수치와 하드웨어 조건으로 수정하고, Metal은 별도 실측 필요 상태 유지.
5. compatibility를 `(model/runtime/hardware)`별 허용 조합으로 구체화하고, 확인 전 조합은 `unverified`/미지원으로 분리. 적어도 QLoRA, KV quant, FlashAttention, speculative decoding을 우선한다.

### 검사기 개선

- 테스트에 빈 evidence와 `source_reported`/`widely_practiced` 사용 조건 검사를 추가한다. 단, 근거 유무를 단순히 status별 regex로 환원하지 말고 claim→source 결합과 허용 미검증 상태를 검증한다.
- narrative 숫자/비율/단위 표현을 찾아 허용목록 또는 설명 필드로 분류하고, 무근거 숫자가 seed에서 다시 유입되지 않도록 fixture/정책 검사를 둔다.
- `compatibility`의 runtime-hardware 조합을 모델로 검증하고 TTS 운영 엔진 회귀 검사를 둔다.
- 현재 테스트는 일부 절대 문구의 회귀 방지로 남겨도 되지만, 계약 통과를 콘텐츠 승인으로 표현하지 않는다.

## 재현 명령 및 작업 트리

- `git rev-parse HEAD` → `95bba6e8e12fcfd3536c688613416f7ae5ec1974`
- `git show --stat --oneline 95bba6e` → 9개 catalog JSON + `tests/test_catalog_seed_contract.py` 변경 확인.
- 스키마/evidence/Kokoro/coverage 전수 스캔은 Python JSON 표준 라이브러리와 `rg`로 수행. (모든 재현은 지정 worktree root에서.)
- `rg -o -i 'kokoro' src/ttakkari/research/catalog_seed/*.json | wc -l` → `33` literal hits.
- `rg -n -i 'CER.*80|80%.*CER|RTF.*0\.1|0\.1.*RTF|200\s?ms|65B.*24GB|24GB.*65B|flash.?attention.*[234]배|2~4배' src/ttakkari/research/catalog_seed` → CER 80%, RTF<0.1, 200ms, 65B/24GB 항목은 없음. 65B/48GB QLoRA와 FlashAttention 2~4×는 있음.
- `git status --short --branch` 초기 결과에 대상 카탈로그 외 미커밋 연구 API/실행기 파일이 있었다. 이를 수정하지 않았다.

**최종 요약:** 기존 커밋의 누락된 파라미터 필드는 복구됐고 요구 목록은 들어 있다. 그러나 근거 없는 성능 숫자 28건과 evidence 빈 50건의 상태 일괄 분류, Kokoro 실행 가능성 암시, 콘텐츠 검증을 보장하지 않는 테스트 때문에 이 커밋을 §2.3 적합으로 승인할 수 없다.
