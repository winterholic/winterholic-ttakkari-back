# 통합 2단계 독립 cross-provider 검증

- 검토일: 2026-10-25
- 대상: back `65de76a8089d9c59755c8d0ebb35eaa6bb3fa85c`, front `f9ea05bcd65e95be92c6c94d280348b68c06781f`; 양쪽 `research-v2`
- 기준: [원래 요구사항](../refactor-v2-requirements.md), [설계 계약](../refactor-v2.md), [통합 1단계 증거](integration-stage1.md)
- 구현자: Gemini Flash (`20a0e2cf-ec25-44e6-bb7a-9666fa5ef316`). 이 보고서는 별도 검증자의 검토다.
- 제한: 제품 소스 수정, 운영 DB·launchd 접근, push·deploy, 운영 Research MCP 쓰기 작업은 하지 않았다.

## 판정 요약

**통합 2단계 인수 PASS는 승인할 수 없다.** 코드 결함 3건을 확인했고, A~D 실제 DB/API 영수증과 UI 상호작용을 독립 확인할 격리 스택이 현재 실행되지 않았다. 특히 B의 발음 오류 확인은 보고서와 화면에 근거가 없어 원래 인수 요구를 충족했다고 볼 수 없다.

| ID | 판정 | 근거 요약 |
|---|---|---|
| A — LLM host GPU | 미검증 | 저장된 화면·보고서만 있고 과거 `launch_id`, device/Metal 로그, raw receipt, lease 전후 상태를 대조할 수 없었다. 이번 host GPU 경로 확인은 `device_evidence=[]`라 bench 전에 정리했다. |
| B — TTS 발음·속도 | **FAIL** | 제공된 B 보고서/분석 화면은 합성 시간·RTF·정규화 문자 변화만 보여 준다. CER/WER 측정이나 청취 평가, 원본 WAV 보존·실제 artifact API 재생 증거가 없다. |
| C — RAG | 미검증 | 화면에는 3개 설정이 각각 n=1로 표시된다. raw API/DB 측정·negative control 재조회·후속 연구의 구조적 재사용을 확인할 수 없었다. |
| D — 새 가설·외부 결과 | **FAIL** | 보고서의 “논문 표 2”만으로 185.4 tok/s 출처를 특정할 수 없다. 현재 외부 어댑터는 `external_benchmark`에 제목/URL 없이도 검증 통과시킨다. 후보 승격 화면은 보이나 저장 상태는 미검증이다. |
| UX 10문 | **FAIL** | stage 2 §3은 원래 질문을 다른 체크리스트로 대체했다. 아래 원문 질문별 증거표를 기준으로 보면 확인 가능한 PASS가 없다. |
| 변경 코드 회귀 | **FAIL** | planner validator 예외와 미등록 adapter가 `valid=True`로 통과하는 것을 실제 함수 호출로 재현했다. 프론트 측정 지표 목록에도 실제 어댑터 키와 다른 항목이 있다. |

## 기준 SHA와 stage 1 이후 전체 변경 대조

실제 체크아웃 HEAD는 요청 SHA와 일치했다. `integration-stage1.md`의 stage 1 후속 기준(back `e61c4a1…`, front `2c6fa10…`)부터 현재까지 확인한 product 변경은 다음과 같다.

- **back:** `e289cf3` planner 검증 보완 1건, `65de76a` stage 2 보고서. 보고서의 4번 절은 frontend enum 정규화 및 지표·예산 동작을 열거하지 않았다.
- **front:** `5dec55e` enum `{value,label}` 정규화, `8aa0822` 실행 예산 상한 및 측정 지표 필터, `164dfef` LLM model 파라미터, `83a78ef` TTS 기본 프리셋, `7b1dddf` RAG 기본 프리셋, `f9ea05b` stage 2 스크린샷. 즉 backend 1건과 frontend 기능 변경 5건이 보고서의 “4건” 목록에 전부 대응하지 않는다.
- rebase 전 보고서 SHA에 대해 이 검토는 stage 1이 기록한 rebase 후 기준과 현재 브랜치의 ancestry/diff를 대조했다. 이 검토에서 rebase 또는 checkout은 하지 않았다.
- 첫 비교 시점의 로컬 back `origin/main`은 `da69e40`이었다. 검토 중 reflog에 `2026-10-25 20:19:45 +0900 update by push`가 기록되어 `origin/main`이 `44f88f8`로 이동했다. 이는 이 검토에서 실행한 fetch가 아니다. `da69e40..44f88f8`의 6개 first-parent commit은 host operations/backup·restore와 runner 종료 안전성 변경이며, 해당 범위의 `src/ttakkari/research`, `docs/research-lab`, migration 0072, research API/test 파일 diff는 없다. 새 tracking ref를 기준으로 back은 7 commits behind로 표시된다(당시 main 기준 1개 + 이후 6개). front의 로컬 `origin/main`은 `ad6149d`로 reflog상 검토 중 갱신되지 않았고, front main 추가 commit도 관찰되지 않았다. 원격 fetch·rebase·checkout은 하지 않았다.

## 환경 및 증거 경계

기존 `/tmp/rv2-stage2`를 먼저 확인했다. `pgdata`는 없었고 PG17 클러스터 `/tmp/rv2-stage2/pg1`은 `shut down` 상태였다. 지정 포트 58731, backend 18793, frontend 5195 모두 연결되지 않았다. 설정 파일의 값은 읽지 않았다.

격리 DB 복원 명령은 Codex 명령 샌드박스에서 실패했다. 로그의 정확한 오류는 `FATAL: shmat(...) failed: Operation not permitted`였다. 이 실패는 해당 명령 샌드박스 범위에만 적용한다. 다른 실행기나 쉘 브라우저로 우회하지 않았다. 설정된 `ttakkari_browser`로 `http://127.0.0.1:5195`를 열었지만 `net::ERR_CONNECTION_REFUSED`였고 브라우저를 닫았다. 따라서 HTTP snapshot, PostgreSQL 원본 행, UI 실제 동작·reload 확인은 미실행이다.

요청한 PG 경로와 기존 실제 경로는 서로 달랐다. 시도한 복원은 기존 `pg1`만 대상으로 했으며 새 DB나 연구를 만들지 않았다. production DB와 `manage_research` 운영 쓰기 호출은 사용하지 않았다. 별도 격리 대상으로 라우팅하는 Research MCP 인자는 확인되지 않아 실제 MCP 전송은 미검증이다.

### host GPU 최소 점검

기존 `host_gpu_status`는 `stopped`였다. 요청에 허용된 최소 host GPU 경로 점검으로 qwen3.5-4b-q4_k_m, `cache_type_k/v=q8_0`, `flash_attn=on`, ctx 4096을 시작해 ready 상태까지 확인하고, `device_evidence=[]` 및 launch ID 미제공 때문에 bench 없이 내가 시작한 인스턴스를 정리했다. 종료 응답은 `running=false`, `server_exit_code=0`, `stopped_now=true`였다. 이는 host GPU에서 속도를 측정한 결과도, 연구 어댑터의 `RuntimeLease` 전후 증거도 아니다. 과거 A의 수치나 설정을 확인한 것으로 간주하지 않는다.

환경 상태·실패 출력·host GPU 경로 결과는 [sanitized evidence JSON](stage2-review-evidence.json)에 기록했다.

## A~D 인수 증거 상세

### A — LLM host GPU

보고서는 3개 run의 129.6~133.4 tok/s, 58.05~58.71 ms proxy, swap 0 MB, BestResult 1건을 주장한다. 하지만 요구된 DB 스냅샷의 definition/config/run/measurement/raw_output/environment/analysis/best-result/artifact 행과 실행 명령 로그를 현재 격리 DB에서 대조하지 못했다. 스크린샷은 원시 측정 영수증이나 `launch_id`/device evidence가 아니다. 이번 host tool 응답도 Metal offload 증거를 주지 않았다. 따라서 속도값, 실제 KV/Flash 설정, cleanup, `RuntimeLease` 전후 상태는 전부 **미검증**이다. 샌드박스의 Metal 제한을 host 불가로 일반화하지 않는다.

### B — TTS 발음 오류와 생성 속도

요구 §12-B는 발음 오류와 생성 속도를 확인하도록 한다. stage 2 보고서는 `synthesize`와 RTF/합성 시간, `normalized_char_delta`를 기록하며 CER/WER나 청취 평가를 기록하지 않는다. 제공된 [B08 분석 화면](../../../../front-research-v2/docs/research-v2-screens/stage2/B08-analysis-desktop.png)에도 설정별 n=1, 정규화 문자 수와 RTF만 보이고 CER/WER는 없다. 정규화 전후 문자열 차이는 TTS 출력의 발음 정확도 측정이 아니다.

따라서 B의 발음 정확도 주장은 **FAIL**이다. 이 판정은 STT를 새 필수 요건으로 추가한 것이 아니다. 실제 `mode`, CER/WER 원문과 정규화 기준, 원본 WAV, diff, 청취 평가 여부가 보고서에 없으므로 발음 오류 확인 자체가 증명되지 않았다는 뜻이다. 파일 보관함 참조 보존, artifact API의 파일 조회·재생도 API/UI에서 확인하지 못해 미검증이다.

### C — RAG 결과 및 후속 연구

제공된 [C08 분석 화면](../../../../front-research-v2/docs/research-v2-screens/stage2/C08-analysis-desktop.png)은 Control 0.969, hybrid 1.000 Recall@20, nDCG@20 0.985→0.988, 각 설정 성공 n=1로 표시한다. 이는 한 내장 코퍼스에서 관찰된 한 회 결과이며 일반 성능 개선 근거가 아니다. 보고된 latency 0.93→0.73 ms와 raw 설정·측정치 일치는 DB가 없어 확인하지 못했다.

negative/no-improvement 실험의 새로고침 후 보존도 미검증이다. [C11 후속 연구 화면](../../../../front-research-v2/docs/research-v2-screens/stage2/C11-followup-study-desktop.png)은 연구 목표/환경 자유 텍스트에서 이전 C 연구와 채택 결과를 언급하지만, 선행 study/result ID나 재사용된 configuration을 보여 주지 않는다. 제목·문구 복사와 구조적 참조를 구분할 수 없어 재사용 주장은 **미검증**이다.

### D — 새 아이디어, 외부 보고, 후보 승격

스크린샷 [D02](../../../../front-research-v2/docs/research-v2-screens/stage2/D02-method-created-badge-desktop.png)/[D06](../../../../front-research-v2/docs/research-v2-screens/stage2/D06-promoted-status-desktop.png)/[D07](../../../../front-research-v2/docs/research-v2-screens/stage2/D07-catalog-reusable-desktop.png)은 `adapter_required` 안내, `candidate (외부 결과만)` 상태, 검색 결과 카드를 보여 준다. 이는 화면 정황일 뿐 refresh 후 DB 저장, 새 실험 payload, 후속 실험의 method/config 참조 증명은 아니다.

보고서에는 A100, FlashInfer, “논문 표 2” 설명과 185.4 tok/s가 있으나 정확한 논문 제목/URL/연도 또는 원문 인용·해시가 없다. 값을 사람이 입력했을 가능성과 출처를 독립적으로 판별할 수 없다. 외부 `external_benchmark` 어댑터는 출처가 없는 값도 받아들이는 코드 결함이 별도로 재현됐다. 따라서 stage 2의 외부 값은 실제 외부 벤치마크로 인정하지 않으며, D의 출처 요구는 **FAIL**이다. 후보 승격이 자동 `validated`로 바뀌지 않았는지, 화면 재조회·검색·후속 실행의 DB 연결은 **미검증**이다.

## UX 원문 10문 커버리지

stage 2 §3의 체크리스트는 비교 설정·지표 방향·통계·실패 보존·proxy·환경·Pareto·맥락·신규 아이디어·모바일 질문이었다. 이는 원래 요구 §5.2 질문을 바꾸어 대체할 수 없다. 아래는 원문 질문에 직접 대응한 판정이다.

| # | 원문 질문 | 판정 | 실제 제공 화면/동작 증거와 공백 |
|---:|---|---|---|
| 1 | 시도 가능한 방법 | 미검증 | A04/B03/C03 라이브러리 화면은 스크린샷으로 존재한다. 검색·필터가 실제 API 응답 및 대상/계층에 맞는지 이 검토에서 실행하지 못했다. |
| 2 | 지금 실행 가능한 방법 | 미검증 | D02 스크린샷은 `adapter_required` 안내를 보인다. 실제 환경 readiness 조회와 local dispatch 차단은 API/DB로 확인하지 못했다. |
| 3 | 이전 시도 | 미검증 | A11/B10/C10 기록 화면 캡처는 있으나 실패 결과의 reload 후 복원은 확인하지 못했다. |
| 4 | 적용 설정 | 미검증 | A07/B05/C05 설계 화면 캡처는 있으나 입력 설정과 DB configuration/raw run config 일치 여부를 확인하지 못했다. |
| 5 | 방법 조합 | 미검증 | A 시나리오가 KV 양자화+Flash Attention 조합을 주장하지만 expanded config/조합 조건의 저장값을 볼 수 없었다. |
| 6 | 개선·악화 폭 | 미검증 | C08은 변화율을 표시하지만 raw measurement 대조가 안 된다. 화면상 각 설정 n=1이라 일반화할 수 없다. |
| 7 | 성공 이유·실패 원인 | **FAIL** | 결과 분석의 자동 요약은 규칙 기반 요약이며 원인을 해석하지 않는다고 스크린샷에 명시돼 있다. 성공/실패 원인 기록을 확인할 증거가 없다. |
| 8 | 다시 실행 가능한 실험 | 미검증 | 기록 화면에서 새 실행이 저장 config를 실제 재사용하는지 확인하지 못했다. |
| 9 | 현재 최선 결과물 | 미검증 | A10/B09 채택 결과 화면 캡처는 있으나 artifact API의 파일/보관함 참조 및 재현성 metadata는 대조하지 못했다. |
| 10 | 과거 방법·결과 재사용 | **FAIL (증거 부족)** | C11은 자유 텍스트에 선행 연구를 적은 화면이다. 구조적 parent/result/config ID 연결 증거가 없어 단순 복사와 구분되지 않는다. |

Desktop 1280×900 및 mobile 390×844의 기존 스크린샷은 보조 자료다. 이번 요구된 대표 흐름 수행, 새로고침 검증, live API payload 확인으로 계산하지 않았다. 브라우저 서버가 없어 실제 UI 검증은 **미검증**이다.

## 코드 검증 및 결함

### 1. Planner가 adapter 오류를 삼킴 — 코드 결함, 재현됨

`planner.py`의 `except Exception: pass`는 validator 예외를 오류로 바꾸지 않고 계획 검증을 계속한다. 실제 `validate_experiment_plan`을 in-memory DB stub로 호출한 결과:

```text
missing_required: valid=False, errors=['required input missing: model']
invalid_sweep_member: valid=False, errors=['n_ctx must be positive']
validator_exception: valid=True, errors=[]
unknown_adapter: valid=True, errors=[]
```

즉 일반 validator가 응답하면 required 값과 sweep 오류는 거부되나 validator 예외와 미등록 adapter는 통과한다. 최소 필수 수정은 adapter 조회 실패 및 예외를 명시적 `errors`로 변환하고, 예외/unknown adapter/유효 sweep/무효 sweep negative control을 회귀 테스트로 두는 것이다. 재현 스크립트는 [planner reproduction](repro-stage2-planner-validation.py)이며 DB 연결 없이 대상 모듈을 직접 import해 호출한다.

### 2. 외부 benchmark 출처가 선택사항 — 코드 결함, 재현됨

`ExternalReportAdapter.validate_config`는 `provenance=external_benchmark`와 숫자 measurement만 주고 `source_title`, `source_url`, `source_year`를 생략해도 오류가 없다. 재현 출력:

```text
config_has_source: {'source_title': False, 'source_url': False, 'source_year': False}
validate_config_errors: []
```

실제 외부 출처를 입증해야 하는 `external_benchmark`에는 최소한 제목과 locator를 요구하고, 없으면 `manual_evaluation` 또는 `imported_log`처럼 실제 성격을 선택하도록 해야 한다. [외부 출처 reproduction](repro-stage2-external-source.py)은 실행/저장 없이 어댑터 검증만 호출한다.

### 3. 프론트 측정 지표 키가 어댑터 출력과 다름 — 코드 결함

- front `labels.ts`는 retrieval 측정 가능 키를 `recall_at_k`, `mrr_at_k`, `ndcg_at_k`로 제한하고 `designerModel.buildPayload`는 그 키를 그대로 `primary_metric`으로 전송한다.
- back retrieval adapter는 `k` 값으로 `recall_at_{k}`, `mrr_at_{k}`, `ndcg_at_{k}`를 기록한다. C08도 헤더에 주요 지표 `recall_at_k`라고 표시하면서 표에는 Recall@20의 동적 키 측정값이 나온다. 선택한 주요 지표와 실제 측정 키가 일치하지 않아 분석 화면은 첫 측정 키로 fallback할 수 있고 header에는 원시 alias가 남는다.
- TTS 측정 whitelist는 `stt_wer_vs_raw`를 누락한다. 반대로 CER/WER 키는 실행 모드(`synthesize_and_stt`)와 STT 결과 존재 여부를 고려하지 않고 항상 제시한다. 실제 backend는 `stt_eval`이 반환된 경우에만 해당 measurement를 만든다.

최소 필수 수정은 실제 adapter measurement contract에 맞춘 key 매핑(특히 `k` 동적 metric), TTS mode/가용성에 따른 선택지, 누락된 raw WER 키 보완 및 payload 단위 테스트다. 현 프론트 연구 테스트는 이 불일치 모델을 검출하지 않고 통과했다.

## 변경 테스트와 남은 검증 범위

실행한 검증:

```text
pnpm exec vitest run src/pages/research/research.test.tsx
Test Files 1 passed; Tests 49 passed

pnpm build
tsc -b && vite build; PWA files generated
```

빌드에는 500 kB 초과 chunk advisory가 있었고 실패는 없었다. 프론트 연구 테스트는 객체 enum 정규화, 기본 model/preset, 예산 12, metric 목록을 확인한다. 그러나 실 API request/응답 및 동적 metric key의 일치를 검증하는 테스트는 아니다.

Backend DB 기반 catalog/planner/API/adapters pytest 묶음은 격리 PG를 기동하지 못한 동일 샌드박스 공유메모리 실패 때문에 실행하지 않았다. stage 1 전체 회귀는 이전 stage 1 SHA 대상 evidence로 재사용했으며 현재 `e289cf3` planner 변경의 DB/API 회귀를 대신하지 않는다. 별도 Python reproduction 두 건은 실행했다. A~D HTTP snapshot, 새 연구 생성, artifact endpoint, D 승격 이후 reload, MCP 전송, desktop/mobile 브라우저 흐름은 **미실행/미검증**이다.

## 필수 수정 목록

1. Planner adapter 예외·미등록 adapter fail-closed 및 exception/unknown/sweep negative-control 테스트.
2. `external_benchmark` 출처 식별자를 검증하고 D 결과의 원문 citation/URL을 저장·표시.
3. Frontend adapter metric 선택 키를 backend output과 일치시키고 TTS STT 지표를 mode-aware하게 구성.
4. B 재검증에서 실제 발음 오류 평가(CER/WER를 선택하면 기준 문자열·정규화 기준·원본 WAV/diff를 보존하거나 청취 평가를 기록)와 artifact API 파일 조회/재생·보관함 참조를 확인. STT만을 새로운 필수 경로로 고정할 필요는 없다.
5. C는 negative run을 reload 후 확인하고, 후속 실험에서 선행 study/result/config를 구조적으로 참조하는지 입증한다. n=1 결과는 해당 코퍼스의 단일 관찰로만 표시한다.
6. D는 실측 API/DB snapshot으로 external_report → candidate 승격 → 검색/reload/후속 참조를 검증하고 자동 `validated` 승격이 없음을 확인한다.
7. 최종 보고서를 원문 UX 10문으로 다시 매핑하고, 실제 API 및 desktop/mobile reload 시나리오의 fresh evidence를 붙인다.

제품 소스는 변경하지 않았다. 이 보고서, sanitized evidence JSON, 두 reproduction script만 검토 증거로 추가했다.
