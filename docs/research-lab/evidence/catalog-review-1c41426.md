# AI Research Lab v2 카탈로그 시드 3차 교정 독립 재검증

- **판정: FAILED** — 이전 A~F 결함 중 다수가 해소됐지만, EBU R128의 -16 LUFS가 본문에 남아 공식 근거와 충돌한다. Hardware/runtime 조합 튜플은 저장·반환될 뿐 readiness/adapter 실행 판정의 제약으로 소비되지 않는다.
- 대상 브랜치/HEAD: research-v2, 1c41426421bf65b3b1d98eda82b4545b848d40f9 (대상 시드 b18dd20, 후속 API 정합 1ddb475, 테스트 정렬 1c41426).
- 기준: docs/research-lab/evidence/catalog-review-978961e.md, catalog-review-95bba6e.md, docs/research-lab/catalog-seed-rules.md, docs/design/local-tts.md.
- 범위: 읽기 전용 검사와 앞선 같은 HEAD의 테스트 실행 결과를 검토했다. **기능 코드·시드 수정 없음. 보고서만 추가.**

## 판정표

| 이전 결함 | 판정 | 근거 및 영향 |
|---|---|---|
| A. 빈 evidence 50건의 상태 분류 | **PASS (구조) / 부분 확인 (출처 의미)** | 현재 39 widely_practiced에는 출처가 있고, 11 unverified는 근거가 비었다. source_reported 62건도 출처가 있다. 표본 24개 seed 주장(17개 고유 문서·논문)의 출처가 해당 기법이나 구현을 직접 다루는지 대조했다. 39건 전체의 주장별 검증은 아니다. |
| B. 전 문자열 숫자·효과 주장 | **FAIL** | tts_audio_postprocessing.principle에 -16 LUFS가 “EBU R128 표준” 값으로 남아 있다. 같은 항목 evidence는 EBU 권고를 -23 LUFS라고 명시한다. 계약 checker의 수치 패턴에 LUFS가 없어 모순을 놓쳤다. 본문이 잘못된 국제 방송 표준 목표를 가르친다. |
| C. Kokoro lifecycle/readiness | **PASS (현재 seed 경로)** | Kokoro는 deprecated이며 parameters/metrics/relations/adapter_hints 및 compatibility 실행 조합이 비어 있다. loader는 seed lifecycle을 기존 system 행에 반영한다. API 경로 테스트는 비실행 상태와 재동기화 복구를 확인한다. readiness에 deprecated 전용 차단 분기는 없지만 현재 빈 parameters/metrics 및 adapter_hints로 실행 불가다. |
| D. FlashAttention·QLoRA 조건 | **부분 PASS** | FlashAttention 논문 수치는 BERT-large seq 512 15%, GPT-2 seq 1K 3×, LRA seq 1K–4K 2.4×로 분리되고 Metal을 별도 실측으로 제한한다. QLoRA 65B/48GB 주장은 원 논문 조건과 정합하나 Apple Silicon/MLX 지원은 별도 구현 근거에 의존한다. 조합 필드는 런타임에서 강제되지 않는다(E 참조). |
| E. KV quant/speculative decoding 조합 | **부분 PASS / 실행 소비 FAIL** | 데이터는 4개 핵심 방법에 하드웨어×런타임 튜플을 기록하고 spec_type=ngram-mod와 미지원 draft model을 분리했다. 테스트는 loader round-trip과 adapter의 파라미터 whitelist를 확인한다. readiness는 compatibility.hardware 합집합만 확인하고 combinations는 읽지 않는다. 따라서 local_executable/unverified 튜플은 실행 판정에서 게이트가 아니다. |
| F. 계약 테스트의 의미 검증 | **부분 PASS / FAIL** | evidence 상태, 조건 연결 및 여러 주장 유형을 검사하도록 넓어졌고 전용 loader→DB→API 테스트도 있다. 하지만 LUFS 수치 모순을 놓쳤으며, 조합 테스트는 저장 보존만 검사하고 readiness의 tuple gating은 검사하지 않는다. |
| required parameter 0개 | **PASS** | 184개 전부 required=false다. 연구 방법을 실행하기 위해 공통으로 강제해야 하는 설정과 실험자가 선택하는 sweep knob는 다르다. 종전 하한을 만족하던 draft_model은 어댑터가 거부하므로 required 수를 늘리는 근거가 아니다. 계약은 0 ≤ required < total로 명시한다. |

## 확인 근거

### 전수 수치 검사

/tmp/rv/scan.py는 JSON method 객체의 모든 문자열 leaf(메서드 설명, compatibility, relations, parameters, evidence 포함)를 순회했다. 숫자 정규식은 후보 추출용이므로 설정값·버전·출처 수치도 포함한다. 정량 claim의 유효성은 계약 checker의 slug/field별 결과와 원문 대조로 판단했다.

> 명령: python3 /tmp/rv/scan.py
> 출력: source_reported numeric 52 field + 52 evidence; widely_practiced numeric 4 field + 2 evidence.
> 출력: 16 methods with NUM in non-evidence fields.

별도 실행한 동일 계약 의미 checker는 numeric/evidence/tuple/deprecated 위반 0건을 보고했다. 이는 checker가 인식하는 패턴만 검사한 결과이며 독립 수치 전수 검토를 대체하지 않는다.

> numeric violations=0
> evidence violations=0
> compat violations=0; deprecated violations=0

checker miss 재현:

> 명령: rg -n 'EBU R128|LUFS' src/ttakkari/research/catalog_seed/tts.json
> 810: principle — EBU R128 ... (-16 LUFS 등)
> 894: claim — 프로그램 라우드니스 목표 -23 LUFS

공식 EBU 페이지도 방송 프로그램 목표를 -23 LUFS로 적는다. 따라서 본문 -16 LUFS는 해당 표준으로 뒷받침되지 않는다. 별도의 온라인/팟캐스트 목표 선택은 가능하지만 현재 문장은 이를 EBU 표준 목표처럼 표현한다. [EBU R 128 공식 페이지](https://tech.ebu.ch/publications/r128)

### widely_practiced 표본 24개 주장, 17개 고유 출처

각 seed evidence의 문서 제목·URL·claim을 원문 페이지와 대조했다. 24개 주장 표본의 17개 고유 페이지에서 출처 자체가 해당 기법/기능을 다루는 것은 확인했다. 일부 수치·특정 모델 효과·이 카탈로그의 환경 적용은 그 출처만으로 일반화하지 않았다.

| seed 주장 표본 | 원문 대조 결과 |
|---|---|
| gradient_accumulation | Accelerate 문서는 gradient accumulation 사용 절차를 설명한다. [문서](https://huggingface.co/docs/accelerate/usage_guides/gradient_accumulation) |
| training_data_sampling_augmentation | DoReMi는 사전학습 데이터 혼합 비율 최적화를 다룬다. seed도 미세조정 샘플링 효과의 직접 근거가 아니라고 한정한다. [논문](https://arxiv.org/abs/2305.10429) |
| kv_cache_reuse, prompt_context_length_tuning, system_cpu_thread_binding, system_batch_size_throughput_tuning, system_memory_allocation_tuning, system_compiler_optimization_flags | llama.cpp 서버/build 문서에 cache·context·CPU·batch·load-mode·build 옵션이 있다. 옵션 존재는 이 Mac의 성능 주장을 증명하지 않는다. [서버 문서](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [build 문서](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md) |
| kernel_fusion 및 정량 claim 대조 | FlashAttention 논문은 타일링으로 GPU HBM/SRAM 간 읽기·쓰기를 줄이는 IO-aware attention을 설명한다. 별도 RMSNorm/RoPE 효과를 증명하지 않는다. [논문](https://arxiv.org/abs/2205.14135) |
| stt_chunk_window_overlap, stt_audio_preprocessing | OpenAI Whisper 구현은 16 kHz sample rate 및 30초 chunk 상수를 갖는다. overlap 효과는 이 구현 상수만으로 입증되지 않는다. [audio.py](https://github.com/openai/whisper/blob/main/whisper/audio.py) |
| stt_beam_search_decoding, stt_context_prompt_conditioning, stt_hallucination_detection | Whisper transcribe 구현에 beam/temperature 처리, initial_prompt, threshold/fallback 로직이 있다. 이는 조절 지점의 존재를 뒷받침하며 일반 정확도 개선·환각 차단률을 뜻하지 않는다. [transcribe.py](https://github.com/openai/whisper/blob/main/whisper/transcribe.py) |
| stt_word_level_timestamps | Whisper timing 구현은 교차 어텐션 기반 DTW 경로를 포함한다. [timing.py](https://github.com/openai/whisper/blob/main/whisper/timing.py) |
| tts_speech_stitching | FFmpeg acrossfade는 오디오 스트림 간 cross-fade와 duration/curve 설정을 문서화한다. [필터 문서](https://ffmpeg.org/ffmpeg-filters.html) |
| diffusion_step_count_tuning | DDIM은 동일 학습 절차에서 더 효율적인 sampling을 연구한다. 현재 SD/SDXL/Flux 속도를 입증하는 수치는 아니다. [논문](https://arxiv.org/abs/2010.02502) |
| diffusion_vae_tiling_slicing | Diffusers는 VAE tiling/slicing 등 메모리 절감 경로를 설명한다. 효과는 모델·해상도 조건에 묶인다. [문서](https://huggingface.co/docs/diffusers/optimization/memory) |
| agent_tool_selection_routing | ToolLLM은 관련 API를 검색하는 retriever를 포함한다. catalog의 top-k 설정 토큰 절감량을 직접 입증하지 않는다. [논문](https://arxiv.org/abs/2307.16789) |
| multi_agent_role_delegation | AutoGen 논문은 대화형 multi-agent 프레임워크를 제시한다. 특정 supervisor-worker 설정의 일반 효과 수치 근거는 아니다. [논문](https://arxiv.org/abs/2308.08155) |
| agent_safety_guardrails | Llama Guard는 대화 입출력 safeguard 분류 모델을 다룬다. 특정 공격 차단율을 보장하지 않는다. [논문](https://arxiv.org/abs/2312.06674) |
| feature_selection_recursive_elimination | scikit-learn RFE 문서는 추정기 중요도에 따라 특성을 반복 제거하는 구현과 설정을 정의한다. [문서](https://scikit-learn.org/stable/modules/generated/sklearn.feature_selection.RFE.html) |
| target_encoding_smoothing | scikit-learn TargetEncoder 문서는 범주별 타깃 평균의 smoothing과 cross-fitting을 다룬다. [문서](https://scikit-learn.org/stable/modules/generated/sklearn.preprocessing.TargetEncoder.html) |
| approximate_nearest_neighbor_annoy_hnsw | HNSW 원 논문은 계층적 근접 그래프 기반 ANN 검색을 다룬다. 경쟁 구현 대비 우위는 논문 평가 조건에 제한된다. [논문](https://arxiv.org/abs/1603.09320) |

### 다섯 정정 인용 원문 대조

1. **AWQ** — 현재 claim은 중요한 가중치 채널 보호와 TinyChat의 특정 GPU 결과를 기술한다. AWQ abstract에서 이전 “perplexity 저하 없이” 문구를 찾지 못했고 seed에서 제거됐다. 현재 3× claim은 TinyChat 구현·GPU로 조건화됐다. [AWQ 원문](https://arxiv.org/abs/2306.00978)
2. **Orca/vLLM** — continuous_batching의 36.9×는 Orca OSDI 논문(GPT-3 175B 대 FasterTransformer, 같은 지연 수준)에 연결돼 있다. 2–4×는 별도 PagedAttention/vLLM 논문에서 FasterTransformer·Orca 대비 보고한 수치다. 서로 다른 결과를 섞지 않았다. [Orca/USENIX](https://www.usenix.org/conference/osdi22/presentation/yu), [PagedAttention 원문](https://arxiv.org/abs/2309.06180)
3. **SGLang/RadixAttention** — 현재 evidence는 최대 6.4×를 RadixAttention 단독이 아니라 RadixAttention과 압축 FSM을 포함한 SGLang 런타임 전체에 귀속한다. 논문 abstract와 일치한다. [논문](https://arxiv.org/abs/2312.07104)
4. **EBU R128** — evidence의 -23 LUFS는 공식 페이지와 일치한다. 같은 method의 principle에 남은 -16 LUFS는 상충하므로 전체 정정은 끝나지 않았다. [공식 페이지](https://tech.ebu.ch/publications/r128)
5. **whisper.cpp 플래그** — README에는 --temperature, --temperature-inc, --no-fallback, -et 등이 보이고 --max-temperatures, --fallback-threshold는 보이지 않는다. seed는 구 플래그를 제거하고 실재 플래그와 확인 필요 표기로 바꿨다. [whisper.cpp CLI README](https://github.com/ggml-org/whisper.cpp/blob/master/examples/cli/README.md)

### loader lifecycle 및 Kokoro 경로

- catalog_loader.py는 기존 행을 (scope == system, slug, version == 1)로만 찾는다. 따라서 scope != system인 사용자 등록 행은 upsert 대상이 아니다.
- 기존 system seed 행에는 seed lifecycle_status를 덮어쓴다. promote_method 및 /methods/{method_id}/promote는 ID로 행을 직접 가져오며 scope 제한을 하지 않는다. system seed 행을 직접 승격하면 다음 catalog sync에서 lifecycle_status가 seed 값으로 되돌아갈 수 있다. 사용자 scope 행은 보존되지만 임의 system 행 승격은 보존되지 않는 경계가 확인됐다(R3 회귀 가능성). 별도 DB 재현은 하지 않았다.
- Kokoro는 deprecated, adapter_hints 빈 배열, parameters/metrics/relations 빈 배열, compatibility hardware/runtimes/combinations 빈 배열이다. API/readiness가 lifecycle만으로 차단하지는 않지만 현재 메서드는 비실행이다.
- 전용 경로 테스트는 catalog sync → loader의 기존 validated 행을 deprecated로 복구 → 상세/목록 API에서 is_executable=false → relation에서 Kokoro 미참조를 확인한다. 앞선 동일 HEAD 테스트 실행에 test_catalog_seed_readiness_path.py가 포함됐다.

> loader 코드: lifecycle_status = m.get("lifecycle_status", "validated")
> 기존 행 조회: scope == "system", slug, version == 1
> 갱신: existing.lifecycle_status = lifecycle_status

### hardware/runtime 조합 소비

- JSON 계약은 combinations 형식·상태, 필수 4개 방법의 tuple 존재, local tuple과 adapter 연결을 검사한다.
- loader 테스트는 네 방법의 tuple이 DB까지 보존되는지만 단언한다.
- readiness는 compatibility.hardware 배열을 읽지만 combinations 조회는 없다. adapters는 지원 method/parameter whitelist를 확인하지만 tuple의 runtime/hardware/status와 대조하지 않는다.
- API 모델은 compatibility JSON 전체를 반환한다. front-research-v2와 front의 코드 검색에서 combinations 소비 참조는 찾지 못했다. 따라서 현재 증거상 tuple은 저장·반환 데이터이며 execution gate가 아니다. 프론트에서 사용자에게 렌더되는 모습은 확인하지 않았다.

## 테스트 결과

이번 이어받기 전 제공된 같은 worktree·HEAD의 최근 실행 로그를 재사용했다(중복 실행하지 않음). 명령은 모두 ./.venv/bin/python -m pytest였다.

> tests/test_catalog_seed_contract.py tests/test_research_v2_catalog.py tests/test_catalog_seed_readiness_path.py tests/test_research_v2_core.py tests/test_research_adapters.py -q
> 101 passed in 5.05s
>
> tests -k catalog -q — 61 passed in 5.25s
> tests -k research -q — 106 passed in 10.05s

각 실행에서 db reset은 selective였고 forced full 및 fallback은 0이었다. 이 결과만으로 전체 테스트 스위트 통과를 주장하지 않는다.

## 미확인 범위와 재현

- widely_practiced 39건 중 24개 seed 주장(17개 고유 출처 페이지)을 직접 확인했다. 나머지 방법의 페이지 내용, 현재 버전별 세부 정확성, 원문과 seed claim의 전체 호응은 재검증하지 않았다.
- 위 다섯 정정 인용은 원문 대조했지만 source_reported 62건 전체를 재검증한 것은 아니다.
- broad scanner는 설정값/표준값/효과 수치를 모두 후보로 찾는다. 수치 효과는 source+condition 연결을 따로 판정했고 잔존 -16 LUFS는 checker가 놓친 실패다.
- R3 재현 조건: system scope/version 1 seed 행을 candidate로 승격한 뒤 load_catalog_seeds()를 실행하면 기존 행 갱신부가 lifecycle을 seed 값으로 복원한다. 코드 경로로 확인했으며 실제 DB 재현은 미실행이다.
- 프론트는 두 checkout의 정적 코드 검색만 했다. UI 출력은 확인하지 않았다.

## 수정 우선순위

1. tts_audio_postprocessing.principle의 -16 LUFS를 EBU 방송 표준과 분리하거나 제거하고, 기본값/설명도 국제 방송 표준 목표라고 오해되지 않게 한다. LUFS 수치를 checker에 포함한다.
2. tuple을 실행 가능성 근거로 쓸 경우 readiness가 runtime/hardware tuple을 adapter 지원·호스트 환경과 연결해야 한다. 그렇지 않으면 tuple 상태를 execution 지원 표기처럼 사용하지 않는다.
3. system seed 승격을 지원할지 결정한다. 지원한다면 sync가 승격 상태를 보존하고, 지원하지 않는다면 promote API에서 system seed를 거부하거나 사용자 범위 복사본으로 제한한다.

**최종 판정: FAILED.** 주된 승인 차단은 EBU 수치 모순이며, tuple 실행 소비와 system seed 승격 보존도 남은 행동 간극이다. 구조 총계 112 methods / 184 parameters / 126 relations, required 0, 39 widely_practiced / 11 unverified는 직접 집계 및 계약으로 확인했다.
