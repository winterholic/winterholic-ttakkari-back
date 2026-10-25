# Research Lab v2 남은 통합 인수 검증

- 검증 시각: 2026-10-25 (Asia/Seoul)
- 백엔드 기준 HEAD: `a564c05c197367aa267b33b05be168d051e64716`
- 프론트엔드 기준 HEAD: `d574b6ee39d8456dd0a4b2d17bdc81fe3e7cc32b`
- 범위: 출처 충돌 수정, retrieval metric 계약, 원문 UX 10문, 원문 §12 C/D. 전체 goal 완료 판정은 아니다.
- 안전 경계: 브라우저가 호출한 backend와 직접 API/DB probe는 `TTAKKARI_TEST_DB` 전용 `ttakkari_test` / `ttk_test` / `127.0.0.1`에 한정했다. 운영 DB 쓰기, 제품 소스 수정, push, deploy는 하지 않았다. 실제 TTS/Whisper/GPU 벤치도 하지 않았다.
- 선행 산출물: 요구 원문 §5/§12, `refactor-v2.md`, `integration-stage2-review-65de76a.md`, `backend-stage2-fix-review-c707aea.md`, `source-conflict-fix.md`를 읽었다. 과거의 전체 suite 및 이미 유효한 검증은 반복하지 않았다.

## 판정 요약

| 범위 | 판정 | 결과 |
|---|---|---|
| 출처 충돌 수정 | PASS | 실제 API 추가 18 조합 모두 422, Run/Measurement 0. 기존 nested locator 정규화·새 GET·별도 DB session 검증 재사용. 출처 없는 manual/import 허용. |
| Frontend metric payload | FAIL | `k=5, top_k=20` 설정이 실제 retrieval과 측정에서 k=20으로 실행됐다. UI가 표시한 k sweep의 k=5 조건은 실제 k=5 측정이 아니다. k=20 및 JSON legacy alias는 매칭된다. |
| §12 C RAG | PARTIAL / FAIL | 실제 local retrieval baseline과 개선 없는 primary 결과가 저장·reload·분석 화면에 남는다. 다음 실험의 이전 experiment/run/config 구조 참조는 API 계약에 없어 실패. |
| §12 D 새 방법 | PASS (합성 수치 한정) | 미지원 가설, 실행 전 422, source-free synthetic manual report, candidate/unverified 상태 reload, 카탈로그 검색 및 후속 method ID 연결을 확인했다. 합성 수치는 실측/외부 벤치가 아니다. |
| UX 10문 전체 | PARTIAL | 라이브러리·설계·실행·분석 UI와 저장 복원은 확인. 아래 원문 문항별 표에 C 재사용, 원인 설명, 재실행/최고 artifact의 한계를 분리했다. |

## 1. 출처 충돌과 저장 복원 — PASS

새 독립 probe가 canonical top-level source를 둔 채 `raw_output`, `environment_snapshot`, `meta` 각각에서 다음 6개 값을 충돌시켜 API를 호출했다: `source_title`, `source_url`, `source_locator`, `source_year`, `provenance`, `execution_origin` (총 18 조합). **18/18이 HTTP 422였고, 각 실험의 Run 0건·Measurement 0건**이었다. 응답에는 충돌 필드명이 포함됐다.

별도 clean 경로에서는 `raw_output.source_locator=doi:10.1234/synthetic`를 받아 외부 원문 locator를 정규화하고, canonical title/URL/year와 `external_benchmark` provenance를 fresh `GET /runs/{id}` 및 새 SQLAlchemy DB session의 Run/Measurement에서 확인했다. 이에 대한 기존 회귀 케이스는 아래의 제한 실행으로 재확인했다.

출처가 없는 별도 synthetic report는 `manual_evaluation`과 `imported_log` 모두 HTTP 201로 저장됐다. 각각 `execution_origin=external_report`, 요청 provenance가 복원됐다. 외부 출처 없는 값을 외부 벤치마크라고 주장하지 않았다.

재검증 명령:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_v2_api_contract.py tests/test_research_v2_e2e.py -k 'external_benchmark_requires_source_and_persists_it_across_requests or external_report_rejects_conflicting_source_before_persistence or external_report_canonical_source_survives_fresh_read or external_report_only_hypothesis_can_validate_save_and_accept_report or non_executable_local_method_is_rejected_before_run_dispatch or vertical_e2e_rag_research_flow' -q --tb=short
13 passed, 35 deselected in 1.39s
```

이는 전체 suite 재실행이 아니다. fixture의 선택 reset 로그는 `13 selective … 0 forced full, 0 fallback`이었다. 신규 독립 API/DB 결과, 각 18건의 HTTP 상태 및 row 수는 outbox raw JSON에 저장했다.

## 2. Frontend metric payload — FAIL

기존 리드 검증의 전체 frontend `65 tests`, type/lint/build는 반복하지 않았다. 이 경로만 독립 실행했다.

```text
pnpm exec vitest run src/pages/research/research.test.tsx -t 'retrieval_eval 기본 k=5|retrieval_eval k=20 설정|retrieval_eval k 가 변경|JSON 고급 탭에서 입력된 legacy alias'
Tests 4 passed | 57 skipped (61)
Test Files 1 passed
```

테스트 payload에서는 기본 k=5가 `recall_at_5`, k=20이 `recall_at_20`, secondary가 `mrr_at_20`/`ndcg_at_20`으로 정규화된다. JSON advanced의 legacy alias도 k=20 지표명으로 정규화된다. 그러나 이 payload 단위 검증은 backend의 `top_k` 우선 규칙까지 증명하지 않는다.

실제 d574b6e 화면에서 `top_k=20`과 `k=[5,20]` sweep을 만들고 즉시 실행한 결과를 새 GET 및 전용 DB에서 확인했다. k=5 설정도 저장된 config에 `top_k=20`을 포함했고, 두 설정의 Run `raw_output.k`가 모두 `20`, Measurement 키도 둘 다 `recall_at_20`, `mrr_at_20`, `ndcg_at_20`이었다. 실험의 primary metric은 `recall_at_20`으로 해당 키의 값 자체는 존재했지만, **k=5 sweep arm은 실제 k=5 retrieval/평가가 아니었다.** 화면상 k=5 arm을 실제 k=5 결과로 읽을 수 있는 반례다.

- 실제 실험 ID: `48fa3edd-0249-448c-b59e-3811c25084dc`
- k=5 config: `{"k":5,"top_k":20,...}` → Run `raw_output.k=20` → metric keys 모두 `*_at_20`
- k=20 config: `{"k":20,"top_k":20,...}` → Run `raw_output.k=20` → metric keys 모두 `*_at_20`
- backend 구현 근거: `src/ttakkari/research/adapters/retrieval_eval.py:305`와 `:386`에서 `top_k`가 있으면 `k`보다 우선해 실행 cutoff로 쓰고, `:675-701`에서 그 실행 k로 measurement key를 생성한다.

최소 교정 범위: 검색 후보 수(`top_k`)와 평가 cutoff(`k`)를 분리해 계약을 정의하고, UI의 k sweep이 의도한 변수와 backend 입력 필드를 일치시킨다. 대안은 UI에서 sweep 변수를 `top_k`로 명시하는 것이지만, 그 경우에도 평가 cutoff metric의 정의가 의도와 일치하는지 명시해야 한다. 이번 검증에서는 제품 소스를 수정하지 않았다.

## 3. §12 C — 저장/복원 PASS, 구조적 후속 재사용 FAIL

새 실제 backend API가 `retrieval_eval`의 synthetic built-in preset `ko_tech_docs_v1`를 CPU에서 실행했다. 따라서 이는 **Research Lab adapter의 local measured fixture 데이터**이며, 외부 일반 벤치·GPU 측정이 아니다. 실험은 `strategy=ab`, 1회씩 두 설정이었다.

| 설정 | Run 상태/origin | Recall@20 | MRR@20 | NDCG@20 |
|---|---|---:|---:|---:|
| lexical_overlap (baseline) | succeeded / local_measured | 0.9479 | 0.9688 | 0.9631 |
| BM25 (treatment) | succeeded / local_measured | 0.9479 | 1.0000 | 0.9808 |

Primary Recall@20 개선 없음(절대 변화 0.0)을 보존하면서 MRR@20 +0.0312, NDCG@20 +0.0177을 비교 화면에 표시했다. 새 study 생성 → 두 설정 계획 → run dispatch → 완료 poll → fresh run GET 및 DB 측정 → `compare-series` → 새 study resume → 브라우저 결과 분석 화면까지 연결했다. 비교 UI는 `따까리 실측`, 2 settings/2 runs, 0.0% 요약과 함께 데이터셋 버전이 기록되지 않았다는 비교 주의도 표시했다.

그러나 §12 C의 다음 연구에서 이전 결과를 **구조적으로** 재참조하는 경로는 PASS가 아니다. 후속 Experiment 요청에 `source_study_id`, `source_experiment_id`, `source_run_id`, `source_configuration_id`를 전달한 실 API 요청은 HTTP 422 `Extra inputs are not permitted`를 반환했다. Study 내부에 실험을 함께 보존하는 것은 가능하지만, 그 자체는 앞선 결과/config 재사용이 아니다. ID를 자유 문구나 임의 JSON에 복사해 저장하는 것도 구조적 참조로 간주하지 않았다.

최소 교정 범위: 후속 실험이 이전 study/experiment/run/configuration을 typed relation으로 참조하거나, 원본 설정을 relation을 유지한 채 복제하는 API/UI 경로를 추가하고 reload 후 연결을 검증한다.

## 4. §12 D — PASS (합성 수치·후보 상태 한정)

새 개인 가설 `GPU 메모리 접근 레이아웃 가설`을 `hypothesis`/`unverified`로 등록했다. `llm_inference` 자동 실행 요청은 dispatch 전에 HTTP 422로 막혔고, 메시지는 로컬 자동 실행 미지원 및 프로토콜 구체화 필요를 명시했다. Run은 생성되지 않았다.

그 다음 **합성 명시** manual report를 source 없이 저장했다. 측정값 `decode_tps=1.0`의 meta/environment에 `fixture=synthetic`, `device=not measured`, `provenance=manual_evaluation`, `execution_origin=external_report`를 기록했다. 실제 모델·GPU 성능으로 주장하지 않았다. 이 결과에서 method를 `candidate`로 승격했고, 새 GET에서 candidate와 `evidence_status=unverified`가 유지됐다. 카탈로그 검색에서도 같은 method ID가 반환됐다. 후속 실험 생성/reload가 그 `method_id`를 직접 참조했다. 별도 출처 없는 `imported_log` 저장도 §1에서 통과했다.

실패한 여정/환경 분리: 지원되지 않는 kernel은 실행 실패 Run으로 조작하지 않고 API 422 상태로 확인했다. 이 결과는 수치 벤치가 아니며, worker B의 TTS/Whisper/artifact 교정 및 A host-GPU 측정과 별개다.

## 5. 원문 UX 10문 검증

아래 문항은 요구사항 원문의 순서와 내용을 유지했다. 화면 근거에는 실제 browser screenshot/snapshot이 있다. `부분`은 해당 UI/자료가 보였으나 전체 질문에 대한 증명은 부족한 경우다.

| 원문 UX 질문 | 판정 | 화면·조작·저장복원 증거 |
|---|---|---|
| 시도 가능한 방법? | PASS | 방법론 화면에서 검색·필터 사용. 113개 중 페이지 표시, 분야별 설명·층위·실행 상태를 확인했다. hybrid retrieval은 즉시 실행 가능 표시. `browser/page-2026-10-25T12-08-38-014Z.png`. |
| 현재 실행 가능한 방법? | PASS | hybrid retrieval의 즉시 실행 가능과 새 GPU memory hypothesis의 `어댑터 필요 / 후보(외부 결과만)` 상태를 실제 목록에서 확인. 가설은 API 실행 시 422. `browser/page-2026-10-25T12-15-09-935Z.png`. |
| 이전 시도? | PASS | 실제로 생성한 실험 2개 Run이 완료되고 monitor에서 2/2 성공을 확인. 결과는 reload 후에도 유지. `browser/page-2026-10-25T12-10-42-830Z.png`. 이 케이스에는 과거 실패 Run은 없음. |
| 적용 설정? | PASS | 실제 designer에서 hybrid retrieval, k 설정, Recall/MRR/NDCG metric 및 두 조건을 확인하고, 저장된 DB config와 run 원문에서 재대조했다. 다만 k=5 arm이 backend에서 k=20으로 실행된 불일치가 있어 설정 적용 정확성 자체는 §2 FAIL. 브라우저 snapshot `browser/page-2026-10-25T12-09-18-566Z.yml`, `browser/page-2026-10-25T12-10-11-329Z.yml`. |
| 조합? | PARTIAL | 방법 선택·A/B 조건 생성·화면상 두 조건 비교를 확인했다. 수치 결과는 lexical_overlap 대 BM25 두 retriever 조건이다. 임의의 다중 기법 상호작용 조합 전체가 실행됨을 뜻하지 않는다. |
| 개선/악화 폭? | PASS | primary 무개선 0.0%와 secondary 수치를 실제 분석 UI/compare API에서 확인. `browser/page-2026-10-25T12-16-26-994Z.png`; 전체 측정은 raw JSON. |
| 성공 이유·실패 원인? | FAIL | 분석 화면에 실패/취소 영역은 있지만 이 케이스는 실패 Run이 없다. 자동 수치 요약은 원인을 해석한 결과가 아니라고 UI가 밝힌다. 미지원 422 이유는 API 응답에는 있으나, UI가 원인 분석까지 저장/표시한다는 증거는 없다. |
| 재실행 가능한 실험? | PARTIAL | 브라우저에서 새 실험을 즉시 실행했고 재진입/저장복원은 됐다. 이번 acceptance에서는 완료된 같은 실험을 UI 조작으로 다시 실행하는 동작 자체를 반복하지 않았다. |
| 현재 최고 결과물? | PARTIAL | 분석에 최선 결과 채택 영역과 비교 후보는 있다. 이 probe에서는 파일/모델 artifact를 만들거나 최선 결과물을 실제 채택하지 않았다. current best artifact 관리의 완료 증거로 보지 않는다. |
| 이전 연구 재사용 방법? | FAIL | D 후보 method를 후속 실험에서 `method_id`로 재참조한 것은 확인. 하지만 원문 §12 C의 기존 결과/config를 다음 실험이 구조적으로 참조하는 요청은 422. §3의 반례가 남는다. |

추가 화면 근거: 대시보드 `browser/page-2026-10-25T12-08-21-607Z.png`, 방법론 `browser/page-2026-10-25T12-08-38-014Z.png`, 실행 monitor `browser/page-2026-10-25T12-10-42-830Z.png`, 이전 화면 분석 `browser/page-2026-10-25T12-11-49-219Z.png`, D 후보 상세 `browser/page-2026-10-25T12-15-09-935Z.png`, C 실제 API run 분석 `browser/page-2026-10-25T12-16-26-994Z.png`.

## 실행 경로, 제한, 정리

- Backend는 `back-research-v2` checkout에서 `127.0.0.1:18793`, frontend는 `front-research-v2` checkout에서 `127.0.0.1:5174`로 실제 listener를 확인하고 브라우저를 연결했다. 5173에는 다른 checkout listener가 있어 사용하지 않았다.
- 실제 검색 호출은 `retrieval_eval` adapter의 built-in preset 경로였고 Run `execution_origin=local_measured`; 실제 모델 라우팅은 없었다. `qwen` alias가 들어간 D 자동 실행은 허용되지 않아 422에서 끝났고 모델을 실행하지 않았다. 185.4/FlashInfer는 외부 측정으로 취급하지 않았다.
- 데이터베이스 연결 전에 URL에서 database/user/host만 파싱해 확인하고, 연결 후에도 `current_database()/current_user`를 재확인했다. URL/password/JWT는 raw artifact에 넣지 않았다. 초기 빈 전용 테스트 DB에는 해당 repo migration head까지 적용했다. 이전 `/tmp/rv2-stage2/pg1` 복원이나 initdb는 시도하지 않았다.
- Codex command sandbox에서 초기 기본 lifespan backend 기동은 앱 startup 단계에서 보호된 `creative-studio/renderer.lock` 접근 `PermissionError: [Errno 1] Operation not permitted`가 발생해 중단했다. 같은 기본 lifespan 경로의 Qwen service는 보호된 로그 파일 열기에서 실패했고, GPU 벤치를 시작하지 않았다. 이 사실은 그 sandbox 기동 시도에만 해당한다. 독립 UI/API 검증은 worker startup을 끈 `--lifespan off` backend에서 통과했다.
- ttakkari_browser로 검증 후 browser를 닫았다. 콘솔에는 `/sw.js`가 `text/html`로 응답해 ServiceWorker 등록이 실패하는 화면 환경 오류가 남았다. 로그인 이후 Research API 화면은 로드되고 조작됐다.
- 실행한 임시 Vite/backend 프로세스를 종료했다. 종료 결과는 Vite session exit 130 (Ctrl-C), backend `Shutting down / Finished server process [95283]`였다.
- 검증 시작 시 backend HEAD는 `a564c05…`, frontend HEAD는 `d574b6e…`였다. 검증이 진행되는 동안 동시 worker B가 backend HEAD를 `f9e28aa8cc2a6461f22d34f086be14c481992b98`로 이동시키는 별도 commit을 만들었다 (`fix(research): integrate runner adapter artifacts into storage and verify scenario B TTS/STT`). 이 commit은 worker B 소유이며, 본 probe/report commit과 분리되어 있다. Frontend HEAD는 `d574b6e…` 그대로다. 본 검증은 시작 시점 a564 목표에 대해 수행됐다. backend API process는 그 전에 시작해 중단 시점까지 같은 process를 사용했다. 동시 worker의 dirty `runner.py`/`tts_synthesis.py` 변경은 직접 읽거나 수정하지 않았다. `retrieval_eval.py`는 a564와 f9e 사이 변경되지 않았음을 확인했다 (`git diff --quiet a564..f9e -- src/ttakkari/research/adapters/retrieval_eval.py`, exit 0). 실제 실행 경로의 adapter attribution은 API response (`adapter_id=retrieval_eval`, `execution_origin=local_measured`)와 이 unchanged module에 근거한다. 이것은 당시 전체 backend worktree가 clean했다는 주장이 아니다.
- A host-GPU 측정: 미검증. B Supertonic/Whisper/WAV/artifact: 해당 worker 범위이며 여기서는 실행하지 않았다. 전체 frontend suite, 배포, production behavior: 이번 범위 밖/미실행.

## 산출물

- 독립 재현 probe: `docs/research-lab/evidence/repro-remaining-integration-a564c05.py`
- 비밀 없는 raw JSON 및 브라우저 스크린샷: 이번 Run outbox의 `remaining-integration-raw.json` 및 `browser/` 아래 screenshot 파일. API ID/합성 built-in dataset 예시가 있으며 credential/URL secret은 포함하지 않는다.

이 리뷰는 관찰된 현재 상태만 판정한다. 위 FAIL/PARTIAL 항목은 제품 교정 후 새 독립 verification worker의 재검증이 필요하다.
