# 카탈로그 시드 근거·수치·호환성 규칙 (4차 교정)

대상: `src/ttakkari/research/catalog_seed/*.json`. 강제는 `tests/test_catalog_seed_contract.py`(JSON 의미 계약)와 `tests/test_catalog_seed_readiness_path.py`(loader→DB→readiness 경로)가 한다. 이 문서는 판단 기준과 항목별 결정을 남긴다.

## 1. evidence_status 판단 기준

| 상태 | 조건 |
|---|---|
| `source_reported` | 논문·공식 문서가 구체 주장을 보고함. evidence claim에 보고 범위와 조건(`조건:`)을 적는다. |
| `widely_practiced` | 기법·기능이 널리 쓰인다는 것을 공식 문서, 대표 구현, 원논문 중 1개 이상이 직접 뒷받침함. evidence는 "기능이 존재하고 이렇게 쓴다"까지만 말하며 효과 수치를 담지 않는다. |
| `unverified` | 근거 없음. evidence 배열은 비우고, 효과를 단정하지 않으며 가설임을 문구에 남긴다. |

기법이 존재한다는 사실과 이 카탈로그가 적은 효과·수치·현재 환경 적용은 별개다. 후자는 위 표의 조건을 따로 만족해야 한다.

## 2. 빈 evidence 50건 처리 (widely_practiced 40 + unverified 10)

- widely_practiced 40건 중 39건: 항목마다 공식 문서·대표 구현·원논문 1~3개를 달고 `widely_practiced`를 유지했다. 효과 수치는 전부 제거하거나 정성화했다.
- 1건 강등: `system_memory_bandwidth_profiling`. 인용 후보가 비공식 커뮤니티 측정 토론뿐이어서 `unverified`로 내리고 evidence를 비웠다(토론 URL은 notes에 참고로만 남김).
- 기존 unverified 10건은 유지하고 근거 없는 단정, 수치, 호스트 사실(예: 800GB/s)을 정성화했다.

| 영역 | slug → 근거 |
|---|---|
| agents | agent_tool_schema_optimization → OpenAI function calling·Anthropic tool use 문서 / multi_agent_role_delegation → AutoGen 논문·Anthropic building effective agents / context_sliding_window_summarization → MemGPT / agent_tool_selection_routing → ToolLLM / agent_safety_guardrails → Llama Guard |
| classical_ml | feature_selection_recursive_elimination → sklearn RFE·permutation importance / target_encoding_smoothing → sklearn TargetEncoder / ensemble_stacking_blending → sklearn StackingClassifier / approximate_nearest_neighbor_annoy_hnsw → HNSW 논문·spotify/annoy / regularized_linear_models → sklearn linear_model / time_series_cross_validation → sklearn TimeSeriesSplit / feature_scaling_outlier_clipping → sklearn RobustScaler |
| generative | diffusion_step_count_tuning → DDIM·DPM-Solver 논문 / diffusion_vae_tiling_slicing → Diffusers memory 문서 / lora_weight_merging_multimodal → Diffusers adapters 문서 |
| llm_inference | kv_cache_reuse·prompt_context_length_tuning·cpu_gpu_offloading → llama.cpp server README / kernel_fusion → FlashAttention 논문(원리) / runtime_backend_comparison → llama.cpp·vLLM 저장소 |
| rag | document_chunking_strategy → LangChain text splitters / query_rewriting → Rewrite-Retrieve-Read 논문 / retrieval_result_caching → GPTCache |
| stt | stt_chunk_window_overlap·stt_audio_preprocessing → openai/whisper audio.py(SAMPLE_RATE=16000, CHUNK_LENGTH=30)·whisper.cpp README / stt_beam_search_decoding·stt_context_prompt_conditioning·stt_hallucination_detection·stt_language_detection_routing → openai/whisper transcribe.py·whisper-cli README / stt_word_level_timestamps → openai/whisper timing.py(DTW)·whisper.cpp README |
| systems | system_cpu_thread_binding·system_batch_size_throughput_tuning·system_memory_allocation_tuning·system_numa_memory_interleaving → llama.cpp server README(+llama-bench, numactl man) / system_gpu_kernel_profiling → Nsight Systems·Apple Metal 도구 문서 / system_compiler_optimization_flags → llama.cpp build.md / system_power_thermal_throttling_analysis → Apple 열 상태 문서 |
| training | gradient_accumulation → Accelerate 문서·mlx-lm LORA.md / training_data_sampling_augmentation → DoReMi·back-translation 논문 |
| tts | tts_speech_stitching → FFmpeg acrossfade 문서 |

판단 한계: URL 도달성과 문서가 해당 기능을 다룬다는 수준까지 확인했다. ACM(dl.acm.org, doi.org)은 봇 차단으로 열리지 않아 hybrid_retrieval의 RRF 논문 URL과 query_expansion의 서베이 DOI는 제목 대응을 확인하지 못했다(`확인 필요`로 표기). LlamaIndex는 포털 URL만 확인했다.

## 3. 수치 주장 규칙 (전 문자열 대상)

- 검사 대상: `expected_effects`뿐 아니라 summary, principle, side_effects, compatibility.notes·combinations.note, relations.note, 파라미터 label·description·option label, evidence claim 전부.
- 효과 수치 패턴: `%`, `N배`, `N×`/`Nx`, GB·MB·ms 등 용량·시간, 소수점 비트 절감, TTFA·RTF·FID·MRR·pass@k 같은 지표 값, `N초 이내` 같은 시간 단정.
- 규칙 1. `source_reported` 메서드의 수치는 같은 숫자를 담은 evidence claim이 있고 그 claim에 `조건:`(모델·하드웨어·길이·비교 대상)이 있으며, 수치 문장이 논문·README 등 출처를 같은 문장에서 밝힐 때만 허용.
- 규칙 2. `widely_practiced`·`unverified` 메서드의 문장에는 효과 수치를 두지 않는다. widely_practiced의 evidence claim에도 두지 않는다.
- 규칙 3. 크기를 말하는 형용어(대폭, 획기적, 월등, 초고속, 극대화, 수배 등)와 절대 표현은 숫자와 같은 취급으로 금지한다.
- 설정값(beam 1~2, 30초 윈도우, 16kHz 같은 규격·파라미터 범위)은 효과 수치가 아니므로 대상이 아니다. Whisper 30초·16kHz는 `audio.py` 상수로 근거를 달았다.

처리 결과(HEAD 대비, 직접 집계): 효과 수치가 claim 필드에 있던 메서드 33건 중 23건은 수치를 모두 제거하거나 정성화했고 10건은 출처·조건과 함께 유지했다. claim 필드의 효과 수치 토큰은 53개에서 19개로 줄었다. 크기 형용어가 있던 메서드는 44건이었고 모두 정성 표현으로 바꿨다. 기존 source_reported evidence 63개의 claim을 전부 다시 썼다(초록·README·코드 대조 가능한 것은 대조, 대조하지 못한 것은 `확인 필요`로 표기).

원문 대조로 바로잡은 기존 오류(대표): AWQ claim의 "perplexity 저하 없이"(초록에 없음), Orca에 vLLM 수치(2~4배)를 붙인 것(Orca 초록은 36.9배, GPT-3 175B vs FasterTransformer), SGLang 6.4배를 RadixAttention 단독 효과로 쓴 것, FlashAttention 요약의 "HBM 접근 O(N^2)→O(N)", Lion·LoRA·IA3·Whisper 문구의 출처 없는 수치, EBU R128에 없는 -16 LUFS, Megatron-LM을 "서빙"으로 적은 것, whisper.cpp에 없는 `--max-temperatures`·`--fallback-threshold` 플래그.

원문 수치를 초록에서 확인하지 못해 제거하거나 `확인 필요`로 둔 것: QLoRA 33B/24GB·double quantization 0.37비트, IA3 0.01%, Whisper 온도 폴백의 본문 절(ar5iv 전문 조회 실패). QLoRA 65B/48GB는 초록 일치.

## 4. Kokoro (deprecated)

`tts_kokoro_pipeline_tuning`은 과거 기록으로만 남긴다. `parameters`·`metrics`·`relations`·`adapter_hints`를 비우고 `compatibility`의 requires·hardware·runtimes를 비우며 `execution: "unsupported"`를 둔다. 다른 메서드는 Kokoro를 runtimes·tags·parameters·combinations에 두지 못하고 relation으로 연결할 수 없으며, notes에서 언급할 때는 미지원·폐기를 함께 적는다.

- loader 결함: `catalog_loader`가 시드의 `lifecycle_status`를 무시하고 항상 `validated`로 적재했다(기존 행은 갱신도 안 함). 시드 값을 쓰도록 고쳤고 재동기화 시 기존 행도 복구한다. 이 결함 때문에 이전 계약 테스트(JSON만 확인)는 통과했지만 로드된 Kokoro는 `validated`였다.
- readiness는 `deprecated`를 직접 처리하지 않는다(코어 작업 영역). 현재는 hints·params·metrics가 비어 있어 `hypothesis_stage`로 떨어져 실행 불가가 된다. `tests/test_catalog_seed_readiness_path.py`가 실제 loader→DB→API(readiness) 경로로 확인한다. 옛 loader로 되돌리면 이 테스트가 실패함을 확인했다.

## 5. 하드웨어·런타임 조합 (`compatibility.combinations`)

`hardware`·`runtimes` 배열은 필터용 합집합이며 직교 곱이 아니다. 가능한 조합의 정본은 `combinations`이고, qlora·kv_cache_quantization·flash_attention·speculative_decoding은 필수다.

- `status`: `local_executable`(등록된 어댑터가 이 Mac Studio에서 실행), `upstream_supported`(상류 문서·논문에 있으나 여기서 실행하지 않음), `different_mechanism`(같은 아이디어의 다른 기능이라 이 메서드의 파라미터가 적용되지 않음), `external_only`(외부 결과 등록만), `unsupported`, `unverified`(`확인 필요` 필수).
- `unverified`·`unsupported`는 배열에 올리지 않는다. `local_executable`은 어댑터 레지스트리가 실제로 그 메서드나 파라미터를 소비할 때만 허용한다.

| 메서드 | Apple Silicon(Metal) 이 호스트 | 비고 |
|---|---|---|
| flash_attention | llama.cpp `-fa`만 `local_executable`(Metal 커널 성능은 실측 필요) | CUDA×pytorch는 flash-attn(Ampere/Ada/Hopper, README). 논문 수치는 NVIDIA GPU 학습 조건. PyTorch MPS·vLLM Metal은 `unverified` |
| kv_cache_quantization | llama.cpp만 `local_executable` | vLLM은 `fp8` KV 캐시로 q8_0/q4_0과 다른 메커니즘(`different_mechanism`). V 캐시 양자화의 FA 의존은 빌드별 확인 필요 |
| speculative_decoding | llama.cpp `spec_type=ngram-mod`만 `local_executable` | draft 모델은 어댑터가 거부하므로 외부 등록. `spec_type` 파라미터를 추가하고 draft 키는 비필수·비스윕으로 표시 |
| qlora | 어떤 조합도 `local_executable` 아님(학습 어댑터 없음, external_report) | bitsandbytes README는 macOS arm64 CPU/Metal에서 QLoRA 4-bit 지원 표시, mlx-lm은 양자화 모델 LoRA. 논문 65B/48GB는 CUDA·NF4 구성 조건이며 MLX와 NF4 동일성은 확인 필요 |

llama.cpp 플래그명·허용 값은 서버 README(master, 2026-10-25 조회) 기준이다. 호스트 `llama-server` 빌드 버전은 이번 세션에서 확인하지 못했다(PATH에 없음).

## 6. 구조 총계 변화

methods 112개 유지. parameters 185 → 184(+`speculative_decoding.spec_type`, −Kokoro 2개), relations 128 → 126(Kokoro 2개 제거). 필수(`required`) 파라미터가 0개가 되어 계약 테스트의 `required > 0` 하한을 풀었다(유일한 필수 파라미터가 어댑터가 거부하는 `draft_model`이었음).

## 7. 미처리·위임

- readiness(코어 영역): `external_report`만 연결된 메서드(lora, qlora 등)가 `partially_supported`·`is_executable=True`로 나온다. `llm_inference` 힌트가 있어도 어댑터가 파라미터를 거부하는 메서드(continuous_batching, prefix_caching 등)가 proxy 실행으로 분류된다. `deprecated` lifecycle 미처리. `AdapterRegistry.find_for_method`는 모든 slug를 `external_report`로 폴백한다.
- hardware×runtime 조합을 4개 메서드 밖으로 확장하지 않았다. 나머지 메서드의 배열은 필터용 합집합이며 계약은 `combinations`가 있을 때만 일관성을 검사한다.
- `kernel_fusion`의 어댑터 힌트를 `external_report`로 바꿨다(어댑터가 `fused_*`를 거부하고 런타임 스위치가 없음).
- `rag_retrieval`의 `normalize_embeddings` 파라미터는 지시에 따라 건드리지 않았다.
- 어댑터 검증 `adapter-review-5e8f17e.md`가 기준 커밋에서도 실패했다고 기록한 `test_catalog_loader_full_112_matching`(`stats["relations"]` 126 != 128)은 시드·테스트 기대값 결함이 아니었다. 기준 커밋(HEAD~1) 시드를 현재 환경에서 loader로 적재하면 `{'methods': 112, 'relations': 128}`로 일치한다. 검증 worktree의 테스트는 그 worktree의 시드(128)를 읽고 loader는 editable 설치된 작업 중 worktree의 시드(Kokoro 관계 제거 중 126)를 읽어 생긴 동시 수정 경합으로 판단한다(추정). 3차 교정 후에는 `tests/test_research_v2_catalog.py`의 기대값을 methods 112, parameters 184, relations 126으로 맞췄다.

## 8. 4차 교정 정책 및 결과 (TTS LUFS, combinations 실행 소비 게이팅, 시스템 시드 승격 거부)

1. **TTS LUFS 방송 표준 정합**:
   - `tts_audio_postprocessing`의 summary, principle, description, evidence에서 공식 방송 표준(EBU R128의 -23 LUFS)과 일반 선택 파라미터 목표값(-16 LUFS 기본값 및 -24~-10 범위)을 명확히 구분했다.
   - -16 LUFS를 EBU R128 표준이나 특정 온라인 관행으로 단정하지 않고, 사용자가 선택하는 파라미터 목표 음량으로 정돈했다.
   - 계약 검사기(`tests/test_catalog_seed_contract.py`)의 `NUMERIC_EFFECT_PATTERNS`에 `LUFS` 패턴을 추가하고, EBU R128과 -16 LUFS가 모순되게 결합된 문장을 주입할 경우 즉각 실패하는 음성 대조(negative control)를 추가했다.
2. **combinations 하드웨어×런타임 실행 게이팅**:
   - `readiness.py`가 `compatibility.combinations`를 정본으로 소비하도록 연결했다.
   - 어댑터 `check_environment`의 `details`(`runtime`, `hardware`)를 읽어와 선언된 조합 튜플과 대조하며, 런타임 식별 정보가 모호하거나 미지원/미검증 조합일 경우 `environment_unavailable`(`is_executable=False`)로 fail-closed 처리한다.
   - 이 Mac Studio(Apple Silicon Metal × llama.cpp)의 정상 실행 경로(`flash_attention`, `kv_cache_quantization`, `speculative_decoding`)는 `tool_integrated` 지원 표기를 정확히 유지한다.
   - `planner.py`의 `validate_experiment_plan`에도 동일한 실행 가능성 검증을 연결하여 실행 불가 방법론 선택 시 `valid=False`를 반환하도록 했다.
   - 가상 튜플 다운그레이드(hypothetical mutation) 및 식별 모호 어댑터 주입 시 fail-closed로 전환되는 음성 대조 테스트를 `tests/test_catalog_seed_readiness_path.py`에 추가했다.
3. **System Seed 라이프사이클 보호 및 직접 승격 422 거부**:
   - seed가 관리하는 system scope 행(`Method.scope == "system"`)에 대한 직접 승격을 `service.py`의 `promote_method` 및 `adopt_best_result`(채택+승격 플래그)에서 명시적 422(`system_seed_methods_cannot_be_promoted`)로 거부하도록 일관 정책을 적용했다.
   - 채택 경로에서 거부 발생 시 선행 DB 플러시로 인한 중간 상태(BestResult, Decision Node)가 남지 않도록 사전 검증을 통해 완전 무결성을 보장했다.
   - 사용자 scope 행(`scope != "system"`)은 정상 승격되며, 이후 `catalog_loader.load_catalog_seeds` 재실행(sync) 시에도 승격 상태, Run 연결, Decision 노드가 온전히 보존됨을 검증했다. Kokoro(`tts_kokoro_pipeline_tuning`)의 `deprecated` 상태도 sync 후 변함없이 유지된다.
