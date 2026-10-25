# 실행 어댑터 독립 검증 기록 — 2eef880

## 판정

**FAILED — 인수 불가 결함 존재.** 이 문서는 2026-10-25에 수행한 기존 검증의 관측 결과를 보존한다. 교정 이후 재검증은 수행하지 않았다.

검증 대상 checkout: branch `research-v2`, HEAD `2eef880bcf1e5689d58429d5f4b983ca176a4c20`. 대상 변경은 `84f6648` 계약·레지스트리, `884e3ee` 어댑터·프리셋·evidence, `2eef880` seed whitelist·파라미터 매핑이다. 저장소에는 당시 백엔드 코어 등 다른 작업자의 변경이 있었고, 검증자는 수정·커밋·stash하지 않았다.

## 결함 목록

1. **LLM 정리 호출 불일치:** adapter가 `HostGpu.stop(reason=...)`를 호출하지만 실제 `stop()`은 인자를 받지 않는다. 오류를 삼켜 시작된 서버가 남을 수 있다.
2. **TTS STT 평가 경로 실패:** STT runner의 실제 시그니처와 호출 인자가 다르며 예외를 삼킨 뒤 Run을 성공 처리한다. `stt_cer_vs_raw`도 없다.
3. **검색 파라미터 무시:** `dense_weight`는 RRF 경로에서 사용되지 않고 `normalize_embeddings`도 소비 코드가 없다.
4. **LLM 설정·기법 과장:** `temperature`, `main_gpu`, `quant_method`가 실제 실행에 반영되지 않는다. 미지원 `quant_type` 일부가 오류 없이 기본 Q4 모델로 대체된다. `weight_quantization`은 실행 중 양자화가 아니다.
5. **LLM swap 누락:** HostGpu 반환값의 최상위 `swap` 대신 어댑터는 `status.swap`을 읽어 측정에서 빠진다.
6. **TTS 보호·취소 누락:** 기존 TTS 예산 잠금을 통하지 않고, 합성 또는 LLM bench가 시작된 뒤 `cancel_event`를 처리하지 않는다.
7. **TTS 지원 범위 과장:** `split_on_comma`, `total_steps` 설정이 미사용이고 `tts_duration_prediction`, `tts_speaker_embedding_voice_consistency`는 표기된 기법을 수행하지 않는다.
8. **환경 준비 상태 오표시:** retrieval은 E5 부재에도 `ready=True`; TTS도 합성 모델/런타임 상태와 무관하게 `ready=True`를 반환한다.
9. **qrels 정합성 문제:** 첫 5개 질의 수동 대조에서 q-02~q-04에 직접 관련성이 낮은 보조 정답이 포함되고 q-05 등급도 재검토 필요하다.

## 코드 근거와 영향

| # | 코드 근거 | 관측된 동작·영향 | 권장 교정 |
|---|---|---|---|
| 1 | `src/ttakkari/research/adapters/llm_inference.py:398-405`; 실제 시그니처 `src/ttakkari/host_gpu.py:831` | `asyncio.to_thread(host_gpu.stop, reason=...)`는 인자 오류를 내고 빈 `except`가 삼킨다. 정상·실패·취소 후 host GPU supervisor/lease가 남을 수 있다. | `stop()` 시그니처에 맞춰 호출하고 정리 실패를 기록한다. 모든 종료 경로를 테스트한다. |
| 2 | `src/ttakkari/research/adapters/tts_synthesis.py:474-495`; 실제 시그니처 `src/ttakkari/meetings/local_stt/runner.py:9-32` | adapter는 `module`·`request` 키워드로 부르지만 함수는 `audio_path`·`params` 인자를 받는다. 예외는 `stt_error`로만 저장되고 합성 Run은 성공 반환한다. 정규화 기준 CER/WER만 계산하며 raw 기준 CER는 없다. | 실제 runner 인자 형태로 연결한다. STT 단계를 성공/부분 실패로 구분하고 raw·normalized CER 모두 산출하거나 미지원으로 표시한다. |
| 3 | `src/ttakkari/research/adapters/retrieval_eval.py:313-327`, `449-452`; 지원 키 `:123-147` | `dense_weight`가 있으면 무조건 `hybrid_rrf`로 강제한다. 설정한 `hybrid_alpha`는 RRF 점수식에 쓰이지 않는다. `normalize_embeddings` 사용처도 없다. | dense 가중치가 의미하는 선형 결합을 구현하거나 키를 제거·거부한다. 임베딩 정규화도 실제 구현 전에는 지원 표시하지 않는다. |
| 4 | `src/ttakkari/research/adapters/llm_inference.py:41-73`, `172-182`, `274-317`, `367-372`; HostGpu bench 고정값 `src/ttakkari/host_gpu.py:801-803` | `temperature`는 bench에 전달되지 않고 HostGpu는 0으로 고정한다. `main_gpu`, `quant_method`는 요청에 매핑되지 않는다. `quant_type` 중 `Q4_K_M`, Q2 alias 외 값은 기본 Q4로 조용히 대체된다. 지원 메서드에는 `weight_quantization`, `system_cpu_thread_binding`도 있으나 런타임 양자화나 CPU affinity binding은 수행하지 않는다. | 실제 소비 키와 실행 가능한 방법만 노출한다. 미지원 값은 명시적으로 거부하고, 사전 양자화 모델 선택은 런타임 양자화와 구별한다. |
| 5 | `src/ttakkari/research/adapters/llm_inference.py:475-489`; 실제 반환 `src/ttakkari/host_gpu.py:819-827` | HostGpu `bench()` 반환은 최상위 `swap`을 갖는다. 어댑터는 `raw.status.swap`에서 읽으므로 `swap_peak_growth_mb`를 만들지 못한다. evidence JSON의 swap 측정은 현재 adapter 변환 코드로 재현되지 않는다. | 실제 반환 구조에서 swap을 읽고 raw-output→measurement 변환 회귀 검사를 둔다. |
| 6 | TTS `src/ttakkari/research/adapters/tts_synthesis.py:408-425`; 기존 잠금 `src/ttakkari/tts/service.py:92-120`; LLM `src/ttakkari/research/adapters/llm_inference.py:359-372` | TTS adapter는 `service.budget()`을 통하지 않고 provider를 호출한다. 합성 중 event를 확인하지 않는다. LLM도 bench 진입 뒤 event 확인이나 취소 시 stop이 없다. | 기존 실행 예산·잠금을 재사용하고 실행 중 worker/host GPU 실행까지 취소 신호를 전달한다. |
| 7 | TTS 지원 키 `src/ttakkari/research/adapters/tts_synthesis.py:100-133`; 매핑 `:292-350`; synthesis `:420-425` | `split_on_comma`와 `total_steps`는 whitelist에만 있고 합성 요청에 전달되지 않는다. 지원 메서드 `tts_duration_prediction`은 생성 후 실제 길이를 기록할 뿐 예측하지 않는다. `tts_speaker_embedding_voice_consistency`도 embedding 비교가 없다. | 구현하지 않은 파라미터·method slug를 제거하거나 표시를 낮춘다. `english_acronym_mode`의 `as_word`·`spelled_out`도 같은 `read_latin` 경로이므로 별도 의미를 보장하지 않는다. |
| 8 | `src/ttakkari/research/adapters/retrieval_eval.py:150-182`; `tts_synthesis.py:135-185` | Retrieval의 hash/BM25 준비 플래그와 `ready=True`가 하드코딩되어 있다. E5 가용성은 details에만 기록한다. TTS는 Supertonic availability를 조회하나 모델이 없어도 adapter ready는 true다. readiness 계산기는 config mode별 준비 여부를 판단하지 않는다. | 기본 in-process 기능과 모델 의존 실행을 구분해 환경 상태를 반환한다. |
| 9 | `src/ttakkari/research/adapters/presets/ko_tech_docs_v1.json` 첫 5개 qrels와 corpus 직접 대조 | q-02의 doc-01은 KV 양자화 답이 아니고, q-03의 doc-03은 Flash Attention 원리가 아니라 KV cache이며, q-04의 doc-07은 Kokoro 대체 이유가 아닌 normalization 설명이다. q-05의 doc-08은 숫자·약어 변환 절차의 직접 근거가 아닌 사용자 사전이다. | qrels를 바로잡은 뒤 preset 점수를 다시 산출한다. |

## 통과한 정적 대조와 산출식

- Protocol dataclass/필드 및 `check_environment`, `validate_config`, `estimate_resources`, `execute_run`, `to_measurements` 메서드는 정의되어 있다. heavy model/runtime import는 실행 메서드 안에서 지연된다. Registry는 builtin adapter를 지연 등록한다.
- Retrieval Recall@k, MRR@k, graded NDCG@k는 adapter에 직접 구현되어 있다. 기존 `kb_eval/metrics.py`의 retrieval 평가 함수를 재사용하지는 않지만, 코드의 기본 수식은 표준식과 맞는다. 쿼리 지연은 각 query의 tokenization부터 scoring/rerank까지 monotonic clock으로 잰다. 실제 프리셋 실행 값과의 비교는 미검증이다.
- TTS RTF는 `synth_seconds / audio_seconds`로 계산된다. evidence JSON의 `1.899 / 12.673 = 0.1498`과 산술상 일치한다. 다만 원본 실행을 재현한 것은 아니다.
- Kokoro 요청은 `engine_requested` 및 Supertonic으로 대체한다는 경고와 함께 실제 `engine="supertonic"`으로 기록한다. 실행 엔진 대체 자체는 명시적이다.
- 별도 상시 데몬은 정적 검토에서 확인하지 못했다. LLM은 기존 HostGpu supervisor 경로를 호출한다.

## 실행 명령·출력 및 미검증 범위

기존 검증 실행 환경은 Codex command tool sandbox였다. 지정 테스트는 uv가 캐시를 초기화하는 단계에서 막혀 pytest 수집·실행으로 진행되지 않았다.

```text
$ uv run pytest tests/test_research_adapters.py
error: Failed to initialize cache at `/Users/winterholic/.cache/uv`
  cause: failed to open file `/Users/winterholic/.cache/uv/sdists-v9/.git`: Operation not permitted (os error 1)
```

같은 테스트를 다른 인터프리터나 캐시 경로로 우회하지 않았다. 아래 항목은 **미검증**이다.

- `retrieval_eval` preset 2개 설정 직접 실행 및 evidence JSON 수치 비교.
- `tts_synthesis`의 `normalize_only` 직접 실행과 Supertonic synthesize 1문장 실행.
- 어댑터를 통한 HostGpu 실측, 실제 정리 동작 및 HostGpu 출력→adapter measurement 비교. HostGpu bench 직접 반환 구조는 위 소스에서 정적으로 대조했으나 이 Run에서 host GPU MCP 도구를 실행하지 않았다.
- cancel event, 실패 반환 및 finally 경로의 동적 확인.

실행 증거가 아닌 정적 코드 판독은 정적 확인으로만 취급한다. 원본 실행 evidence JSON은 checkout의 `docs/research-lab/evidence/adapters-2026-10-25.json`이며, 이 follow-up에서는 이를 다시 열거나 수치 검증하지 않았다.

## 카탈로그 교정 후 재대조

`catalog_seed`는 당시 별도 교정 중이었다. 다음 키·slug 변경 여부와 의미를 교정 commit 이후 다시 대조해야 한다.

- LLM: `quant_type`, `quant_method`, `main_gpu`, `threads_batch`, logical/physical batch size 및 각 지원값.
- Retrieval: `dense_weight`, `normalize_embeddings`, `chunking_mode`, `top_k`, `rrf_k`.
- TTS: `split_on_comma`, `max_chunk_length`, `handle_measure_units`, `english_acronym_mode` 및 추가로 교정된 설정.
- 세 어댑터의 `supported_methods`, `supported_parameters`, 거부 규칙과 readiness의 일치.

이 문서는 다음 교정 worker가 참조할 기존 검증의 요약 기록이다. 교정 commit 뒤 fresh verifier가 별도로 재검증해야 한다.
