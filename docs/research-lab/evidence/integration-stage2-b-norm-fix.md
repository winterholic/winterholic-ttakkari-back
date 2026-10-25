# Research Lab v2 통합 2단계 시나리오 B 정규화 분기 교정 및 재실측 보고서

- 작성일: 2026-10-25
- 대상 checkout: `back-research-v2`
- 기준: [원래 요구사항](../refactor-v2-requirements.md), [설계 계약](../refactor-v2.md), [통합 2단계 시나리오 B 보완 보고서](integration-stage2-b-supplement.md)
- 근거 데이터:
  - 증거 JSON: [`docs/research-lab/evidence/stage2-b-norm-fix-evidence.json`](stage2-b-norm-fix-evidence.json)
  - 실행 스크립트: [`docs/research-lab/evidence/run_scenario_b_norm_fix_execution.py`](run_scenario_b_norm_fix_execution.py)
  - 부정대조 테스트: [`tests/test_stage2_b_norm_contrast.py`](../../../tests/test_stage2_b_norm_contrast.py)
- 실행 환경:
  - 격리 데이터베이스: 표준 주입 `TTAKKARI_TEST_DB` (`ttakkari_test`, `ttk_test` 확인)
  - TTS 엔진: Supertonic 3 (로컬 ONNX 런타임, Apple Silicon CPU)
  - STT 엔진: Whisper (로컬 워커 서브프로세스, CPU, proxy 발음 평가)
  - HTTP 전송: FastAPI `httpx.ASGITransport(app=app)` 실제 ASGI 라우트 호출

> [!IMPORTANT]
> **STT 발음 지표의 한계 및 Proxy 명시**
> 본 검증의 CER(음절 오류율) 및 WER(어절 오류율)은 로컬 Whisper STT 음성 재인식을 통한 **발음 명료도 간접 대리 지표(proxy)**입니다. 이는 사람의 주관적 청취 음질이나 자연스러움(MOS, Mean Opinion Score) 평가가 아니며, Whisper 모델 자체의 역정규화(Inverse Text Normalization) 성향에 따라 표기 오차가 발생할 수 있습니다. 가설 검증 결과는 임의 조작 없이 실측치 그대로 기록되었습니다.

---

## 1. 판정 요약

| 항목 | 판정 | 핵심 검증 결과 |
|---|:---:|---|
| **정규화 분기 결함 교정** | **PASS** | `chunk_text(text, ..., normalize=False)` 명시 경로 추가로 전처리된 텍스트의 강제 재정규화 방지. 부정대조 단위 테스트(RED) 작성 후 통과(GREEN). |
| **청킹/취소/화자제거 회귀** | **PASS** | 청크 분할 상한, 취소 이벤트(`cancel_event`), STT 타임스탬프(`[00:01.20]`) 및 화자 접두사 정규식 교정 및 회귀 테스트 통과. 기존 TTS 테스트 98건 전원 통과. |
| **시나리오 B 실제 독립 재실행** | **수행 완료 (측정 사실 기록)** | Supertonic 3 + Whisper 실제 재실행 완료. Baseline과 Candidate의 provider 전달 청크 및 생성된 WAV SHA가 상이함을 실측 검증 (`chunks_equal=False`, `wav_shas_equal=False`). 원본 WAV 4건 및 증거 JSON Run outbox 보존 및 `share_file`. |

---

## 2. Systematic Debugging 조사 기록

### Phase 1: 콜스택 역추적 (Root Cause Tracing)
- **증상 발생 지점**: `TtsSynthesisAdapter.execute_run()`에서 `provider.synthesize(all_chunks, ...)`로 전달되는 텍스트 청크.
- **호출 경로**:
  1. `execute_run()` (`src/ttakkari/research/adapters/tts_synthesis.py:400`):
     - `_normalize_with_options(raw_s, use_convert_numbers=False, use_read_latin=False, ...)` 호출 결과: `"HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다."` (원문 유지됨).
  2. 직후 라인 `chunks = chunk_text(norm_s, max_chars=max_chunk_chars)` 호출.
  3. `chunk_text(text, max_chars)` (`src/ttakkari/tts/normalize.py:597`):
     - `for sentence in split_sentences(normalize_text(text)):`
     - `chunk_text` 내부에서 `normalize_text(text)`를 **무조건 강제 호출**.
  4. `normalize_text()`는 `convert_numbers()`, `read_latin()`, `_symbols()`를 기본 적용하므로, `norm_s`의 약어와 숫자가 `"에이치티티피 이백 오케이..."`로 강제 변환됨.
- **결과**: Baseline과 Candidate가 동일한 한글 발음 청크를 `provider.synthesize`에 전달하여 동일한 WAV 파일(`baseline_candidate_synthesis_equal=True`)이 생성됨.

### Phase 2: Business Logic 검증
- 일반 서비스 경로: 원문이 들어오면 `chunk_text(text)`를 통해 정규화 + 문장 분할 + 상한 청킹을 일괄 수행.
- 리서치 어댑터 경로: 정규화 각 단계(숫자 변환, 라틴 변환, 사전 치환)의 대조 실험을 위해 이미 세밀하게 전처리된 텍스트(`norm_s`)를 넘기므로, **재정규화 없이 문장 분할 및 길이 상한 분할만 수행하는 경로**가 필수적이나 누락되어 있었음.

### Phase 3: Environment 격리
- OS/버전/아티팩트 환경 문제가 아니며, 순수 텍스트 전처리·청킹 파이프라인의 하드코딩된 호출 결함임.

### Phase 4: Logging 및 가설 검증
- **가설**: `chunk_text(text, max_chars=120, *, normalize=True)`에 호환 optional flag `normalize=False`를 추가하고, 어댑터에서 이를 호출하면 Baseline의 원문 약어/숫자가 provider까지 유지되고 Candidate 청크와 달라질 것이다.
- **근본 원인**: `ttakkari.tts.normalize.chunk_text`가 인자로 전달받은 텍스트에 대해 `normalize_text`를 강제 실행함으로써 전처리 제어 옵션을 무력화함.

---

## 3. TDD 사이클 및 최소 교정

### 3.1 RED (부정대조 실패 재현)
- [`tests/test_stage2_b_norm_contrast.py`](../../../tests/test_stage2_b_norm_contrast.py) 작성:
  - `test_negative_contrast_baseline_raw_preserved_to_provider_chunks`: Baseline과 Candidate의 `provider.synthesize` 인자 청크를 캡처하여 `b_chunks != c_chunks` 및 `"HTTP 200" in b_chunks[0]` 검증.
  - `test_chunk_text_supports_normalize_false_and_preserves_limits`: `chunk_text(..., normalize=False)` 키워드 인자 및 분할 상한 검증.
  - `test_stt_speaker_removal_and_text_override`: 타임스탬프(`[00:01.20]`) 및 화자 태그 정제 검증.
- **RED 실행 결과**:
  ```text
  FAILED tests/test_stage2_b_norm_contrast.py::test_negative_contrast_baseline_raw_preserved_to_provider_chunks
  AssertionError: Baseline chunks did not preserve raw text: ['에이치티티피 이백 오케이 응답과 함께 십오점오 메가바이트의 제이슨 데이터가 성공적으로 반환되었습니다.']
  
  FAILED tests/test_stage2_b_norm_contrast.py::test_chunk_text_supports_normalize_false_and_preserves_limits
  TypeError: chunk_text() got an unexpected keyword argument 'normalize'
  
  FAILED tests/test_stage2_b_norm_contrast.py::test_stt_speaker_removal_and_text_override
  AssertionError: assert '[00:01.20] 화...단일 테스트 문장입니다.' == '오버라이드된 단일 테스트 문장입니다.'
  ```

### 3.2 GREEN (최소 코드 수정)
1. **`src/ttakkari/tts/normalize.py`**:
   - `chunk_text(text, max_chars=120, *, normalize: bool = True) -> list[str]` 수정: `source = normalize_text(text) if normalize else text`.
   - `chunk_preprocessed_text(text, max_chars=120) -> list[str]` 편의 함수 추가.
2. **`src/ttakkari/research/adapters/tts_synthesis.py`**:
   - `chunks = chunk_text(norm_s, max_chars=max_chunk_chars, normalize=False)`로 변경하여 기 전처리문 재정규화 방지.
   - STT 화자 정제 정규식 `r"^\[[\d\.\:\–\- ]+\]\s*(화자\s*\d+:)?\s*"`로 콜론(`:`) 지원 추가.

### 3.3 회귀 검증
- `tests/test_stage2_b_norm_contrast.py`: `4 passed in 0.65s`
- `tests/test_scenario_b_artifacts.py`: `1 passed in 0.53s`
- `tests/test_research_adapters.py` (TTS/STT 관련 8건): `8 passed in 0.29s`
- `tests/test_tts_*.py` (기존 TTS 전 범위 98건): `98 passed in 12.24s`

---

## 4. 실제 재실행 증거 및 실측 비교 (Supertonic 3 + Whisper)

### 4.1 실험 식별자
- **Study ID**: `c595ce9b-1993-4dd4-bb4d-aa0d0db14ac4`
- **Experiment ID**: `dac79c06-42f5-42f5-9851-9b39fb8f7183`
- **실행 시각**: 2026-10-25 12:30:16 UTC
- **입력 문장 2건**:
  1. `2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.`
  2. `HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.`

### 4.2 합성 청크 및 음성 대조 검증 (Contrast Verification)

| 검증 항목 | Baseline (정규화 미적용) | Candidate (정규화+발음사전) | 일치 여부 (equal?) | 판정 |
|---|---|---|:---:|:---:|
| **Chunk 0 합성 입력** | `2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.` | `이천이십육년 시월 이십오일 오후 두 시 삼십 분에 연구실 회의가 시작됩니다.` | **False** | 정상 분기 확인 |
| **Chunk 1 합성 입력** | `HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.` | `에이치티티피 이백 오케이 응답과 함께 십오점오 메가바이트의 제이슨 데이터가 성공적으로 반환되었습니다.` | **False** | 정상 분기 확인 |
| **Chunk 0 WAV SHA** | `7397692665e1a67f2b9a160609ae9cc19d9b17470168228607157efd19b7f338` | `027ee2699d5235e0b0dbde8bb3d2151a795d2bb74b0c7fa7679cb288fa1562e3` | **False** | 독립 음성 확인 |
| **Chunk 1 WAV SHA** | `68ff55169f484619be619fca9e9098efb5222c305f2ea591194ffb54f66521ff` | `1e044490bb2267d86d14819fa285b5668d8ee5d5a8aca7e162c3a800177...` | **False** | 독립 음성 확인 |

### 4.3 실측 지표 요약

| 지표 (Metric) | Baseline (`05194bae`) | Candidate (`1af41960`) | 단위 / 특성 |
|---|---:|---:|:---:|
| **합성 소요 시간 (synth_seconds)** | 2.112s | 2.199s | s (lower is better) |
| **생성 오디오 길이 (audio_seconds)** | 15.493s | 16.133s | s |
| **실시간 계수 (rtf)** | **0.1363** | **0.1363** | ratio (lower is better, 수용기준 `<= 1.0` 달성) |
| **전체 Wall 시간** | 21.876s | 19.918s | s |
| **최대 RSS 메모리** | 550.78 MB | 552.69 MB | MB |
| **STT CER vs Normalized (주요 지표)** | **0.0238** | **0.4640** | ratio (대리 지표, 수용기준 `<= 0.05`) |
| **STT CER vs Raw** | **0.0238** | **0.0238** | ratio (원문 대조 동일) |
| **STT WER vs Raw** | 0.1500 | **0.1000** | ratio (Candidate에서 어절 인식 개선) |

### 4.4 문장별 Whisper 전사 상세 및 가설 평가

#### Chunk 0 (날짜·시간·숫자)
- **Baseline**:
  - 합성 텍스트: `2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.`
  - Whisper 전사: `2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.`
  - CER vs Norm: `0.0000` / CER vs Raw: `0.0000` / WER: `0.0000`
- **Candidate**:
  - 합성 텍스트: `이천이십육년 시월 이십오일 오후 두 시 삼십 분에 연구실 회의가 시작됩니다.`
  - Whisper 전사: `2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.`
  - CER vs Norm: `0.4063` (Whisper ITN으로 인한 표기상 차이) / CER vs Raw: `0.0000`

#### Chunk 1 (영문 약어·소수점·단위)
- **Baseline**:
  - 합성 텍스트: `HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.`
  - Whisper 전사: `HTTP205K 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.`
  - CER vs Norm: `0.0476` / CER vs Raw: `0.0476` / WER: `0.3000` (HTTP 200 OK 발음 왜곡으로 HTTP205K 오인식)
- **Candidate**:
  - 합성 텍스트: `에이치티티피 이백 오케이 응답과 함께 십오점오 메가바이트의 제이슨 데이터가 성공적으로 반환되었습니다.`
  - Whisper 전사: `HTTP 205K 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.`
  - CER vs Norm: `0.5217` / CER vs Raw: `0.0476` / WER: `0.2000` (공백 복원: HTTP 205K)

#### 연구 수용 기준 vs 실제 제품 실행 분석
1. **수용 기준과 지표의 차이**:
   - 가설은 Candidate가 `stt_cer_vs_normalized <= 0.05`를 달성할 것으로 예상했으나, 실제 Candidate의 `stt_cer_vs_normalized`는 `0.4640`으로 측정됨.
   - **이유**: Whisper STT 모델은 음성을 인식한 후 자체적인 역정규화(ITN)를 적용하여 날짜/숫자를 아라비아 숫자로, 영문 약어를 알파벳 대문자로 출력함. 따라서 한글 발음 풀이문(Norm Ref)과 대조할 경우, 음성이 정확하게 발음되었음에도 표기 불일치로 인해 CER이 40~50% 수준으로 왜곡됨.
   - **원문 대조 지표(Raw Ref)**: 원문 기준 CER(`stt_cer_vs_raw`)은 두 설정 모두 `0.0238`로 우수하며, WER은 Candidate가 `0.1000`으로 Baseline(`0.1500`) 대비 개선됨.
   - **판정 원칙 준수**: 가설이 예측한 대로 나오지 않았으나 수치를 조작하거나 임계값을 임의 수정하지 않고, **측정 결과 사실 그대로 보존**함.

---

## 5. 산출물 보존 및 Outbox 배포

본 실행에서 생성된 산출물은 Run outbox에 저장되었으며 `share_file`을 통해 배포되었습니다.

- **Outbox 디렉토리**: `/Users/winterholic/.ttakkari/data/outbox/b4ad5289-227d-404a-b9eb-ffa79a9d5bf1`
- **보존 파일 목록**:
  1. `stage2-b-norm-fix-evidence.json` (전체 실행 세부 메타데이터 및 지표 원본)
  2. `stage2_b_norm_fix_baseline_chunk-0000.wav` (Baseline Chunk 0 WAV, 592,314 bytes, SHA256=`7397692665e1...`)
  3. `stage2_b_norm_fix_baseline_chunk-0001.wav` (Baseline Chunk 1 WAV, 774,278 bytes, SHA256=`68ff55169f48...`)
  4. `stage2_b_norm_fix_candidate_chunk-0000.wav` (Candidate Chunk 0 WAV, 579,648 bytes, SHA256=`027ee2699d52...`)
  5. `stage2_b_norm_fix_candidate_chunk-0001.wav` (Candidate Chunk 1 WAV, 843,356 bytes, SHA256=`1e044490bb22...`)
  6. `integration-stage2-b-norm-fix.md` (본 검증 보고서)
