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
| **Runner 아티팩트 잠재 결함 교정** | **PASS** | (1) 누락 파일 선언 시 침묵 skip 대신 명확한 Run 실패(`artifact_file_missing`) 및 에러 메시지 보존, (2) normalized path 기준 영구 파일 중복 등록 및 중복 ResearchArtifact 방지, (3) 정상 legacy artifact_id 연결 및 중복 연결 방지. 실PG 좁은 회귀 테스트 4건 통과. |
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
- `tests/test_scenario_b_artifacts.py`: `4 passed in 0.69s` (아티팩트 잠재 결함 3건 포함)
- `tests/test_research_adapters.py` (TTS/STT 관련 8건): `8 passed in 0.29s`
- `tests/test_tts_*.py` (기존 TTS 전 범위 98건): `98 passed in 12.24s`

### 3.4 Runner 아티팩트 정적 잠재 결함 분석 및 TDD 교정

리드 독립 검증 보고서(`scenario-b-artifacts-review-f9e28aa.md`)에서 지적된 Runner 영역의 3대 정적 잠재 결함을 실제 PostgreSQL 기반 좁은 회귀 테스트로 재현(RED)하고 최소 교정(GREEN)을 완료했습니다.

1. **선언 파일 부재 시 침묵 건너뛰기 결함**:
   - **결함 내용**: 어댑터가 성공(`succeeded`)을 반환했으나 `artifacts`에 선언된 파일 경로가 디스크에 없을 때, `if p.is_file():` 조건으로 인해 아무런 경고나 오류 없이 다음 항목으로 넘어가 Run이 성공하고 아티팩트만 0건 생성됨.
   - **TDD RED 재현**: `test_runner_fails_when_declared_artifact_file_is_missing`에서 디스크에 없는 파일 선언 시 Run이 `failed`가 아닌 `succeeded`로 끝나는 결함 확인.
   - **최소 교정**: `not raw_p.is_file()` 감지 시 `db.rollback()`으로 중간 상태를 정리하고 Run을 `failed`로 전이하며 `error_code="artifact_file_missing"`, 누락 경로가 포함된 `error_message`를 명확히 보존.

2. **완전 동일 dict 비교의 한계 및 중복 생성 결함**:
   - **결함 내용**: 동일한 파일 경로라도 `artifacts`와 `raw_output` 간 딕셔너리 키·순서나 메타데이터(label, role 등)가 조금만 다르면 단순 dict 비교(`if a not in all_artifacts`)를 통과하여 `register_file`이 중복 호출되고 `Artifact` 및 `ResearchArtifact`가 N개 생성됨.
   - **TDD RED 재현**: `test_runner_deduplicates_artifacts_by_normalized_path`에서 동일 파일에 다른 메타데이터 3건 전달 시 `assert len(res_arts) == 1`에 대해 3건이 생성되는 결함 확인 (`assert 3 == 1`).
   - **최소 교정**: 파일 경로를 `raw_p.resolve()`로 정규화(normalized path)하여 `registered_paths: dict[Path, uuid.UUID]`로 관리. 이미 등록된 정규화 경로에 대해서는 중복 `register_file` 호출을 방지하고 기 발급 ID를 재사용. 또한 `linked_artifact_ids: set[uuid.UUID]`를 두어 동일 실행에서 동일 아티팩트의 `ResearchArtifact` 행 중복 생성을 원천 차단.

3. **정상 Legacy `artifact_id` 유지 및 중복 방지**:
   - **결함 내용**: 기 발급된 `artifact_id`가 지정되어 들어올 때 UUID 변환 후 연결되지만, 중복 전달 시 ResearchArtifact가 중복 생성될 수 있음.
   - **TDD RED 재현 및 교정**: `test_runner_preserves_legacy_artifact_id_and_deduplicates`를 통해 기존 `Artifact` 행과의 정상 외래키 연결을 유지하면서 `linked_artifact_ids`를 통해 중복 연결을 방지(`len(res_arts) == 1`).

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

## 5. 아티팩트 보존 및 식별자 완전 일치 대조

본 실행에서 생성된 모든 오디오 산출물은 `runner.py`를 통해 영구 아티팩트 저장소에 등록되고 `ResearchArtifact`로 연결되었으며, `GET /api/artifacts/{id}` 및 `GET /api/artifacts/{id}/content` 엔드포인트를 통해 정상 조회 및 `RIFF/WAVE` 매직 헤더 검증이 완료되었습니다.

과거 초안 보고서의 식별자 혼용 문제를 방지하기 위해, 아래 표는 원본 증거 JSON([`stage2-b-norm-fix-evidence.json`](stage2-b-norm-fix-evidence.json))의 `verified_artifacts`로부터 직접 자동 산출되어 100% 교차 일치합니다.

| Run / 설정 | ResearchArtifact ID | Artifact ID | 파일명 (Outbox 배포명) | 크기 (bytes) | SHA256 (앞 12자리) | Content API 응답 | WAV Magic 헤더 |
|---|---|---|---|---:|:---:|:---:|:---:|
| **Baseline** Chunk 0 | `4b7a1aa6-5b22-444b-8637-981855fc393f` | `919a6c62-2e38-436a-894f-28313eecd572` | `stage2_b_norm_fix_baseline_chunk-0000.wav` | 592,314 | `7397692665e1` | **200 OK** | `RIFF/WAVE` 검증 |
| **Baseline** Chunk 1 | `484cc9c3-bc3e-41ad-8f35-c35e5a712a00` | `c49427dc-1b46-4325-b834-1392449fff49` | `stage2_b_norm_fix_baseline_chunk-0001.wav` | 774,278 | `68ff55169f48` | **200 OK** | `RIFF/WAVE` 검증 |
| **Candidate** Chunk 0 | `1a6cddad-7632-46e8-833f-8e2638a79ae1` | `9a8a12bb-cc2a-42e7-98e3-8925566e5c9d` | `stage2_b_norm_fix_candidate_chunk-0000.wav` | 579,648 | `027ee2699d52` | **200 OK** | `RIFF/WAVE` 검증 |
| **Candidate** Chunk 1 | `7437000f-2c99-4265-a2db-88a1444331db` | `38d54364-10b2-48a9-b9cb-addab2e6fc84` | `stage2_b_norm_fix_candidate_chunk-0001.wav` | 843,356 | `1e044490bb22` | **200 OK** | `RIFF/WAVE` 검증 |

### 5.1 Deliverables 배포 (Outbox 및 `share_file`)

본 실행의 검증 산출물은 현재 Run outbox(`/Users/winterholic/.ttakkari/data/outbox/681fed40-fd59-47e3-bde5-891cb4d1e3e7`)에 저장되었으며, MCP `share_file`을 통해 대화 세션에 공유되었습니다:
1. `integration-stage2-b-norm-fix.md` (본 검증 보고서)
2. `stage2-b-norm-fix-evidence.json` (전체 실행 세부 메타데이터 및 지표 원본)
3. `stage2_b_norm_fix_baseline_chunk-0000.wav`
4. `stage2_b_norm_fix_baseline_chunk-0001.wav`
5. `stage2_b_norm_fix_candidate_chunk-0000.wav`
6. `stage2_b_norm_fix_candidate_chunk-0001.wav`
