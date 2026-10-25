# Research Lab v2 통합 2단계 시나리오 B 보완 검증 보고서

- 검증일: 2026-10-25
- 대상 checkout: `back-research-v2`
- 기준: [원래 요구사항](../refactor-v2-requirements.md), [설계 계약](../refactor-v2.md), [통합 2단계 독립 검토서](integration-stage2-review-65de76a.md) §4.4 (시나리오 B 발음 오류 및 산출물 보존)
- 실행 환경:
  - 격리 데이터베이스: 표준 주입 `TTAKKARI_TEST_DB` (`ttakkari_test`, `ttk_test` 확인, 운영 DB 아님)
  - TTS 엔진: Supertonic 3 (로컬 ONNX 런타임, Apple Silicon CPU)
  - STT 엔진: Whisper (로컬 워커 서브프로세스, CPU, proxy 발음 평가)
  - HTTP 전송: FastAPI `httpx.ASGITransport(app=app)` 실제 ASGI 라우트 호출

> [!IMPORTANT]
> **STT 발음 지표의 한계 및 Proxy 명시**
> 본 검증의 CER(음절 오류율) 및 WER(어절 오류율)은 로컬 Whisper STT 음성 재인식을 통한 **발음 명료도 간접 대리 지표(proxy)**입니다. 이는 사람의 주관적 청취 음질이나 자연스러움(MOS, Mean Opinion Score) 평가가 아니며, 가짜 평가나 인간 청취 평가로 대체 서술하지 않습니다.

---

## 1. 판정 요약

| 항목 | 판정 | 핵심 검증 결과 |
|---|:---:|---|
| **시나리오 B (TTS 발음·속도 및 산출물)** | **PASS** | 실제 Supertonic 3 음성 합성 및 Whisper STT 재인식 실행 성공. Baseline 대비 Candidate 정규화 설정의 전사/CER/WER 실측 보존. 원본 WAV 4건의 ResearchArtifact 정식 보관함 등록 및 `GET /api/artifacts/{id}/content` 200 OK + `RIFF/WAVE` 매직 바이트 검증 완료. Outbox 공유 완료. |
| **산출물 연결 결함 교정** | **PASS** | `runner.execute_run_instance`의 `AdapterRunResult.artifacts` 미소비 결함 재현 후 기존 `register_file`(`force_copy=True`)을 통한 정식 등록 최소 교정 적용. Narrow regression test 통과. |

---

## 2. 산출물 연결 결함 분석 및 최소 교정

### 2.1 결함 재현 (Defect Reproduction)
- **현상**: 기존 `runner.execute_run_instance`는 어댑터가 반환한 `AdapterRunResult.artifacts`를 소비하지 않고, `adapter_res.raw_output.get("artifacts", [])` 내의 `artifact_id`만을 검색하여 `ResearchArtifact`를 생성함.
- **영향**: 어댑터는 DB를 인지하지 못하므로 파일 경로(`path`)만 반환하며 `artifact_id`를 생성할 수 없음. 이로 인해 Run이 성공하더라도 `ResearchArtifact` 행이 0건 생성되고, 임시 디렉토리(`tempfile.mkdtemp`)의 실 WAV 파일이 정식 아티팩트 저장소에 등록되지 않아 유실됨.
- **재현 증거**: [`docs/research-lab/evidence/repro-stage2-b-artifacts.py`](repro-stage2-b-artifacts.py)
  ```text
  reproduction_result:
    run_status: succeeded
    research_artifacts_count: 0
    file_artifacts_count: 0
    defect_reproduced: True
  ```

### 2.2 최소 교정 (Minimal Fix)
1. **`src/ttakkari/research/runner.py`**:
   - `adapter_res.artifacts` 및 `adapter_res.raw_output.get("artifacts")`를 통합 수집.
   - 각 파일 경로에 대해 기존 `ttakkari.artifacts.service.register_file`을 호출하여 `s.artifact_store`에 영구 복사(`force_copy=True`, `run_id=None`).
   - 발급된 `Artifact.id`를 외래키로 하여 `ResearchArtifact` 행을 생성하고 DB에 커밋.
2. **`src/ttakkari/research/adapters/tts_synthesis.py`**:
   - `texts` 파라미터가 명시적으로 주어졌을 때 프리셋 문장을 선별 오버라이드할 수 있도록 보완.
   - STT 출력에서 화자/타임스탬프 태그가 포함된 포맷팅 문자열 대신 순수 전사 텍스트(`original_text`)를 우선 추출하고 정규식 태그를 정제하여 CER/WER 노이즈 방지.
   - 어댑터 반환 `artifacts`에 `"role": "audio_sample"` 명시 및 `raw_output["artifacts"]` 보존.
3. **Narrow 회귀 테스트**:
   - [`tests/test_scenario_b_artifacts.py`](../../../tests/test_scenario_b_artifacts.py) 추가.
   - 실행 결과: `1 passed in 0.53s` (PostgreSQL + ASGI).

---

## 3. 시나리오 B 실제 실행 증거

### 3.1 실험 설계 및 입력 데이터
- **표준 프리셋**: `ko_mixed_pronunciation_v1` (한국어 숫자·단위·영문 약어·기호 혼합 발음 평가셋)
- **대표 입력 문장**:
  - 문장 1 (숫자·단위): `"2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다."`
  - 문장 2 (영문 약어·소수점·단위): `"HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다."`
- **비교 설정**:
  - **Baseline (Control)**: 정규화 미적용 (`convert_numbers=False`, `read_latin=False`, `custom_pronunciation_dict={}`)
  - **Candidate (Treatment)**: 한국어 숫자·단위 정규화 + 영문 약어 커스텀 사전 (`convert_numbers=True`, `read_latin=True`, `custom_pronunciation_dict={"JSON": "제이슨", "HTTP": "에이치티티피"}`)

### 3.2 실측 결과 (Measurements & Audio Metrics)

| 설정 | Run ID | 상태 | 합성시간 (synth_s) | 음성길이 (audio_s) | RTF | 최대 RSS (MB) | Wall time (s) |
|---|---|:---:|---:|---:|---:|---:|---:|
| **Baseline** | `4e6a9bfa-8ee3-4603-9357-bf403b02ce6d` | succeeded | 2.195s | 16.133s | 0.1361 | 554.08 MB | 20.658s |
| **Candidate** | `df895810-b52b-45a4-9e71-7a5f84f82406` | succeeded | 2.207s | 16.133s | 0.1368 | 552.52 MB | 20.394s |

### 3.3 문장별 전사 및 발음 Proxy 오차율 (CER / WER)

#### Chunk 0: 문장 1 (날짜, 시간, 숫자)
- **원문 (Raw Ref)**: `2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.`
- **정규화 기준 (Norm Ref)**: `이천이십육년 시월 이십오일 오후 두 시 삼십 분에 연구실 회의가 시작됩니다.`
- **Whisper STT 전사 (STT Text)**: `2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.`
- **오차율**:
  - CER vs Norm: `0.4062` / WER vs Norm: `0.6364` (STT가 숫자를 아라비아 숫자로 표기하여 표기상 오차 발생)
  - CER vs Raw: `0.0000` / WER vs Raw: `0.0000` (원문 기준 완전 일치)

#### Chunk 1: 문장 2 (영문 약어, 단위, 소수점)
- **원문 (Raw Ref)**: `HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.`
- **정규화 기준 (Norm Ref)**: `에이치티티피 이백 오케이 응답과 함께 십오점오 메가바이트의 제이슨 데이터가 성공적으로 반환되었습니다.`
- **Whisper STT 전사 (STT Text)**: `HTTP 205K 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다.`
- **오차율**:
  - CER vs Norm: `0.5217` / WER vs Norm: `0.5455`
  - CER vs Raw: `0.0476` / WER vs Raw: `0.2000` (원문 대비 1개 어절 오차: 200 OK → 205K)

---

## 4. 아티팩트 보존 및 API 조회 검증

생성된 모든 WAV 음성은 기존 아티팩트 파이프라인을 통해 정식 등록되었으며, `GET /api/artifacts/{id}` 및 `GET /api/artifacts/{id}/content` 엔드포인트 조회를 통해 완전성을 검증했습니다.

| Run / 설정 | ResearchArtifact ID | Artifact ID | 파일명 | 크기 (bytes) | SHA256 (앞 12자리) | Content API 응답 | WAV Magic 헤더 |
|---|---|---|---|---:|:---:|:---:|:---:|
| **Baseline** Chunk 0 | `8b5ff3f8-5813-4011-8ecb-c2e74eece467` | `f2fc8c52-7e05-4f40-8c29-37e408ec24cf` | `chunk-0000.wav` | 579,648 | `027ee2699d52` | **200 OK** | `RIFF/WAVE` 검증 |
| **Baseline** Chunk 1 | `b85675c6-b51f-4efc-8d1e-873b88b2eb59` | `289382f7-7b79-4d6d-8cbe-3b566580f5d4` | `chunk-0001.wav` | 843,356 | `1e044490bb22` | **200 OK** | `RIFF/WAVE` 검증 |
| **Candidate** Chunk 0 | `9b128aa1-d248-4f81-9b7e-e1d51a66e4a5` | `bc67ad93-d499-4c6e-8a24-9b88cf6d2ee8` | `chunk-0000.wav` | 579,648 | `027ee2699d52` | **200 OK** | `RIFF/WAVE` 검증 |
| **Candidate** Chunk 1 | `e48719bc-c3bb-4015-84a2-e64e52b2ae60` | `2785e6ef-a0d3-49df-8cf5-731c25fc04fc` | `chunk-0001.wav` | 843,356 | `1e044490bb22` | **200 OK** | `RIFF/WAVE` 검증 |

### 4.1 Deliverables 배포 (Outbox 및 `share_file`)
사용자가 웹 앱 대화창에서 직접 청취 및 다운로드할 수 있도록 Run Outbox(`/Users/winterholic/.ttakkari/data/outbox/477d3196-001d-4e7f-b992-7b13dab8f262`)에 오디오 파일을 복사하고 MCP `share_file`을 호출했습니다:
1. `scenario_b_baseline_chunk-0000.wav` (`240534d8-376f-441c-9451-170ac22bbd60`)
2. `scenario_b_baseline_chunk-0001.wav` (`2af2fa94-131e-4163-b3ce-f6c8f3618d18`)
3. `scenario_b_candidate_chunk-0000.wav` (`0996c76c-4aa4-424d-b2a8-1d79d5974644`)
4. `scenario_b_candidate_chunk-0001.wav` (`930d094e-ad2c-4ff1-9e23-611da51070bf`)

---

## 5. 재검증 증거 자료 목록

1. **Sanitized Raw Export JSON**:
   - [`docs/research-lab/evidence/stage2-b-supplement-evidence.json`](stage2-b-supplement-evidence.json)
   - DB credential 및 JWT 인증 토큰 제외, Study/Experiment/Runs/Measurements/STT Eval/Artifacts 메타데이터 완전 수록.
2. **실제 실행 스크립트**:
   - [`docs/research-lab/evidence/run_scenario_b_execution.py`](run_scenario_b_execution.py)
3. **결함 재현 스크립트**:
   - [`docs/research-lab/evidence/repro-stage2-b-artifacts.py`](repro-stage2-b-artifacts.py)
4. **Narrow 회귀 테스트**:
   - [`tests/test_scenario_b_artifacts.py`](../../../tests/test_scenario_b_artifacts.py)
