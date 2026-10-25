# Scenario B 최종 독립 인수 검토

- 검토일: 2026-10-25 (KST)
- 저장소 / checkout: `projects/winterholic-ttakkari/back-research-v2`
- 검토 시 HEAD: `acca0105fc55dfb1af2d97fe72fdf88f0405eb99`
- B 정규화 기준 commit: `28b6f6026338d01e93876629abdb7216ffc3e8f3`
- 비교 기준 runner artifact commit: `76d1dcfa13b3a6af1efbefde73c189699c28714f`
- 로컬 Python: 저장소 `.venv/bin/python` (Python 3.13)
- 제품 코드 변경: 없음. 추가 worktree 생성 없음.
- 검토 시작 시 `tests/test_research_v2_migration.py`가 dirty였으나 이 검토에서 읽거나 수정·stage하지 않았다. 최종 commit 전 status에서는 더 이상 dirty로 보이지 않았고, 원인 변경은 이 검토에서 하지 않았다.
- 새 실측 원본: [`scenario-b-final-review-76d1dcfa-execution.json`](scenario-b-final-review-76d1dcfa-execution.json)
- 추가 실패 경로 실측: Run outbox `scenario-b-adversarial-runtime-results.json`, `scenario-b-missing-second-runtime.json`

## 인수 범위와 판정

원문 요구사항 §12B는 발음 오류와 생성 속도를 비교하고 음성 파일·채택 설정을 결과물로 보존하는 시나리오다. 발음 품질 개선 자체를 필수 성공으로 규정하지 않는다. 본 검토는 지정 B 파일과 두 좁은 회귀 테스트 파일, 실제 PostgreSQL/ASGI TTS 실험, artifact 실패 경로를 확인했다. 전체 revision 인수나 배포 판정은 하지 않았다.

| 영역 | 판정 | 확인 결과 |
|---|---|---|
| B 소유 파일 동일성 | PASS | 지정 B 소스·테스트·기존 증거 파일을 `76d1dcfa13b3a6af1efbefde73c189699c28714f`와 diff 비교; 전부 동일. 현재 dirty 파일은 별도 migration test 1개뿐. |
| 지정 테스트 | PASS | 정규화 부정대조 4개 + Runner artifact 4개, 총 8개 통과. 영향 범위 기존 TTS adapter 회귀 1개 추가 통과. |
| 실제 모델·DB·ASGI 실험 | PASS (실행 경로) | 표준 `TTAKKARI_TEST_DB` identity가 `ttakkari_test` / `ttk_test` / loopback. Supertonic 3 CPU와 Whisper CPU를 2문장·2조건으로 실제 실행. 별도 생성 Run 2건 모두 `succeeded`, experiment `completed`. |
| 정규화 후보 가설 | FAIL (가설 수용성) | Candidate `stt_cer_vs_normalized=0.464`로 등록 목표 `<=0.05` 초과. 수치를 보정하거나 품질 개선으로 해석하지 않고 실패를 유지. |
| Artifact DB·API·파일 보존 | PASS | 4개 WAV 모두 Artifact와 ResearchArtifact 연결, 각 metadata/content 요청 200, 크기·SHA-256·RIFF/WAVE 일치. 비교 원본 4개를 이번 Run outbox에 보존. |
| 선택 설정 영구 보존 | FAIL | Analysis의 Pareto 결과는 Baseline을 가리키지만 `BestResult` row는 0개이고 evidence `best_result`도 null. 완료된 experiment에서 채택 설정으로 다시 열 수 있는 영구 BestResult는 확인되지 않음. |
| Missing-file 부분 실패 | FAIL (증거 보존 결함) | 두 번째 경로 누락을 실제 실행. Run은 `failed`가 되지만 rollback으로 adapter raw output, environment snapshot, 측정값과 DB 링크가 사라짐. 첫 파일의 원본은 바이트 동일하게 남고 DB 등록은 rollback됐으나, 이미 복사된 orphan 파일은 남음. |
| Legacy artifact ID 예외 | FAIL (Run 종료 상태) | 잘못된 UUID는 `failed`/`ValueError`로 끝나고 adapter 결과·측정이 보존됨. 형식상 유효하지만 없는 FK는 PostgreSQL `IntegrityError`를 일으켰고 Run이 `running`에 머물렀으며 output/context가 비어 있음. |
| 화면 reload·best/제약 표시 | 미검증 | backend 전용 검토 checkout이고 화면 주소/실행 frontend가 제공되지 않음. 구성된 browser에는 `about:blank`만 있어 UI 상태를 확인하지 않음. |

## 실행 증거

### 지정 테스트

명령:

```sh
.venv/bin/python -m pytest -q tests/test_stage2_b_norm_contrast.py tests/test_scenario_b_artifacts.py
.venv/bin/python -m pytest -q tests/test_research_adapters.py::test_tts_synthesis_adapter_catalog_mapping_and_execution
```

출력:

```text
8 passed in 0.87s
1 passed in 0.08s
```

98개 전체 TTS 테스트는 이전 구현자 보고가 있어 무조건 재실행하지 않았다. 이번 범위에서는 provider 입력 부정대조, 청크 상한, 취소, STT override·speaker 제거, artifact 연결 회귀 및 해당 adapter regression을 확인했다.

### 실제 Supertonic·Whisper·PostgreSQL·ASGI 실행

스크립트는 `importlib`로 불러 `RUN_OUTBOX_DIR`와 `EVIDENCE_JSON_PATH`를 본 Run 경로로 덮어쓴 뒤 호출했다. 첫 두 번은 sandbox가 기본 artifact 경로 `/Users/winterholic/.ttakkari/data/artifacts` 쓰기를 거부해 아티팩트 등록에서 실패했다. 그 경로를 재시도하지 않았다. 성공 실행은 실행 프로세스에서만 설정 home을 본 Run outbox 아래로 바꿔 동일한 `register_file`/ASGI 경로를 확인했다. 실제 설정 form 검증으로 사용하지 않았고, run 생성 뒤 시험용 `Configuration.parameters`를 직접 지정한 사실도 그대로 한정한다.

실행 명령의 핵심:

```text
.venv/bin/python - <<'PY'
spec_from_file_location(..., "docs/research-lab/evidence/run_scenario_b_norm_fix_execution.py")
module.RUN_OUTBOX_DIR = <이번 Run outbox>
module.EVIDENCE_JSON_PATH = <고유 repo evidence JSON>
asyncio.run(module.run_scenario_b_norm_fix())
PY
```

실측 요약 (Whisper 전사 기반 CER/WER는 proxy이며 청취 평가·발음 정답 판정이 아님):

| 측정 | Baseline raw | Candidate 정규화 |
|---|---:|---:|
| provider 입력 chunk | 원문 숫자·약어 유지 | 한글 숫자·약어 표기 |
| `stt_cer_vs_normalized` (proxy) | 0.0238 | **0.4640** |
| `stt_wer_vs_normalized` (proxy) | 0.3000 | **0.5909** |
| `stt_cer_vs_raw` (proxy) | 0.0238 | 0.0238 |
| `stt_wer_vs_raw` (proxy) | 0.1500 | 0.1000 |
| RTF | 0.1365 | 0.1452 |
| WAV | 2 files | 2 files |

새 설정 파라미터 전체는 execution JSON의 `configuration_parameters_observed`에 저장했다. 입력 chunk 목록과 생성 WAV SHA는 서로 달랐다 (`chunks_equal=false`, `wav_shas_equal=false`). 실제 사람 청취는 수행하지 않았다.

ASGI 실행은 새 `GET /api/artifacts/{id}`와 `GET /api/artifacts/{id}/content` 요청을 각 산출물마다 별도로 수행했다. 모든 응답이 HTTP 200이었고 content bytes SHA-256이 DB metadata와 일치하며 `RIFF/WAVE` 헤더를 가졌다. API 요청은 각 요청별 DB dependency session 경로로 조회됐다. 새 Study ID `bde6ca20-a1d0-44b6-a6dc-e384a80456d3`, Experiment ID `3728f5b2-84fb-4aaf-9a5a-04ccef891945`; 최종 성공 실행의 두 Run ID와 artifact ID는 execution JSON에 기록했다.

완료 후 별도 새 `GET /api/research/experiments/3728f5b2-84fb-4aaf-9a5a-04ccef891945` 요청은 HTTP 200, `state=completed`, 2 runs와 `latest_analysis`를 돌려줬다. Analysis의 Pareto 항목은 Baseline config `a36e2b40-e872-4817-bbe3-afe5b3432ec9`를 best point로 표시했지만 DB `BestResult` row는 0개였다. 따라서 실행·비교·파일 저장은 확인했으나 §12B의 채택 설정 영구 보존은 확인하지 못했다.

## 추가 반례 실측

1. **두 번째 파일 누락**: 첫 파일을 실제 등록/복사하게 하고 다음 선언 경로를 없앴다. 결과는 `failed`, `artifact_file_missing`, `measurement_count=0`, `research_artifact_count=0`, source Artifact DB row 0이다. adapter의 raw output과 environment snapshot도 빈 객체로 남았고, 파일 원본 SHA는 실행 전·후 동일했다. 첫 복사 파일은 DB rollback 뒤 orphan으로 남았다. 따라서 성공 위장은 없지만 부분 실패의 adapter 결과·측정 맥락은 보존되지 않는다.
2. **잘못된 legacy UUID**: 결과 Run은 `failed`, `error_code=ValueError`; `raw_output`, `environment_snapshot`, 측정 1건이 유지됐다.
3. **유효하지만 없는 legacy FK**: DB insert/commit에서 `research_artifacts.artifact_id` foreign-key 위반. 실행 후 Run은 `running`, `error_code` 없음, output/context/measurement/artifact link 없음으로 조회됐다. 정상적인 terminal failure로 정리되지 않는 재현이다.

추가 반례 결과 JSON은 이 Run outbox에 보관했다. 첫 두 번의 실험 실패와 이 실패 경로 결과는 성공 실행 증거와 섞지 않았다. outbox의 orphan 복사 파일은 rollback 부작용 증거로 유지했다.

## 기존 증거 파일의 내부 일치 대조

기존 `integration-stage2-b-norm-fix.md`와 `stage2-b-norm-fix-evidence.json`을 비교했다. 네 artifact의 ResearchArtifact ID·Artifact ID는 전부 JSON과 일치하고, 크기(천 단위 구분자 제거)·SHA-256 앞 12자도 일치했다. baseline/candidate 두 provider chunk 문자열과 요약 CER/WER·RTF 측정도 원본 JSON 값과 일치한다. 이번에 새로 수집한 결과는 오래된 보고서/JSON을 덮어쓰지 않고 새 고유 JSON에 저장했다.

## 남은 범위

- UI에서 experiment reload 후 best result, 제약 목표, 원본 파일 API 접근성을 확인하지 못했다.
- Whisper proxy는 표기 역정규화 영향이 있어 사람의 발음 정답·자연스러움 평가를 대체하지 않는다.
- `artifact_file_missing` rollback은 원본·environment·측정 컨텍스트를 버리고 복사 파일을 남긴다. 이 관측은 코드 추측이 아니라 실제 DB/file 실험 결과다.
- 유효하지만 존재하지 않는 legacy `artifact_id`에서는 Run이 running에 고착됐다. 별도 교정 구현·제품 변경은 이 인수에서 하지 않았다.
- 배포 및 전체 checkout/revision 통합 인수는 범위 밖이다.
