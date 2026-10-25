# C 구조적 재사용 독립 검증 보고 (backend 2e68f577 · 763dc03f / frontend e1158f0)

검증일 2026-10-25. 구현자 보고와 구현자 테스트를 근거로 삼지 않고, 이 검증자가 직접 실행한 결과로만 판정했다. 제품 코드는 수정하지 않았다.

- backend worktree `back-research-v2`, HEAD `9f087f07` (2e68f577, 763dc03f 모두 조상으로 확인)
- frontend worktree `front-research-v2`, HEAD `e1158f0`
- 실행 환경: 표준 주입 `TTAKKARI_TEST_DB`는 `host=127.0.0.1:60888 / database=ttakkari_test / user=ttk_test`로 확인한 뒤 사용했다. 운영 DB는 건드리지 않았다.
- 브라우저 검증은 별도 임시 PostgreSQL 클러스터(`tests/pg_sandbox`) + 이 worktree의 backend(포트 28799) + vite dev server(포트 25173) + Google Chrome headless(playwright-core)로 했다. 8799 포트는 다른 프로세스가 쓰고 있어 건드리지 않았다. 검증 후 내 프로세스와 클러스터는 모두 내렸다.
- 독립 프로브는 임시 파일 `tests/zz_probe_c_reuse_review.py`로 실행했고 커밋하지 않았다(사본은 outbox).

## 판정 요약

| # | 항목 | 판정 |
|---|---|---|
| 1 | 마이그레이션 0072→0073 upgrade/downgrade/upgrade, 행 보존, FK SET NULL, generation-contract 정합 | PASS (경미한 이름 차이 1건 기록) |
| 2 | 재사용 API 계약 반례 11종 + GET/resume/event 출처 보존 + 원본 불변 | PASS |
| 2b | 공유 계약에 없는 fail-open 탐색 | 계약 위반 fail-open은 못 찾음. 인접 결함 기록(아래 F1, F2) |
| 3 | shadowed k 판정 | 부분 FAIL. 기본 3종은 PASS, top_k도 sweep인 경우를 놓침(F1), 오탐(F2) |
| 4a | frontend vitest / tsc / eslint / build | PASS |
| 4b | 실제 backend + dev server + 브라우저 E2E | 출처 복제·배지·reload 유지·shadowed 차단은 PASS. **해석 노트 저장은 FAIL(F3)**. 재사용 충실도 FAIL(F4) |
| 5-7 | UX 7 성공/실패 이유 | 부분. 실패 원인 표시 PASS, 해석 기록 FAIL(F3) |
| 5-8 | UX 8 다시 실행 | PASS |
| 5-10 | UX 10 이전 연구 재사용 | 부분. 구조는 PASS, 충실도 FAIL(F4), 배지는 약함(F5) |

## FAIL 반례 (심각도 순)

### F3. 연구자 해석 노트 저장이 실제 backend에서 422 (높음, frontend 또는 backend 계약 불일치)
- 재현: 브라우저에서 결과 분석 → `해석 추가` → 제목·본문 입력 → `해석 저장`. 실패 Run의 `실패 원인 해석 남기기`도 같다.
- 관측: `POST /api/research/studies/{id}/nodes` 가 `422 {"loc":["body","epistemic_status"],"msg":"Extra inputs are not permitted"}`. UI에는 일반 문구 `요청 형식이 올바르지 않습니다.`만 뜨고 다이얼로그가 열린 채 남는다. reload 후에도 노트는 없다.
- 원인: frontend `ResultAnalysisView.tsx:73`이 `epistemic_status: 'unverified'`를 body로 보내는데, backend `NodeIn`(`api/research.py:86`)은 `Strict`(extra 금지)이고 해당 필드가 없다. 같은 body에서 `epistemic_status`만 빼면 201(직접 API 호출로 확인). vitest는 API를 모킹해서 이 불일치를 잡지 못했다.
- 보존 값: backend는 근거 없는 노드를 `unverified`로 저장한다(`research/service.py:159`, 코드 근거이며 저장된 값 자체는 이번 실행에서 응답 전체를 확인하지 않았다 확인 필요).
- 표시 쪽은 별도로 확인: 필드 없이 API로 넣은 `kind=interpretation` 노드는 reload 후 `연구자 해석 (미검증)` 카드로 `자동 수치 요약`과 분리되어 표시된다. 즉 저장 경로만 깨져 있다.
- 수리 방향 제안: frontend가 `epistemic_status`를 보내지 않거나, backend `NodeIn`이 허용값을 `unverified`로 한정해 받게 한다. 둘 중 하나를 리드가 정해야 한다.

### F4. "이 설정으로 후속 실험"이 원본 설정을 그대로 재현하지 않음 (중간, frontend)
- 재현: 원본 `{preset_id, retriever_mode:bm25, k:5}`(방법 hybrid_retrieval) 설정 #2 → 후속 실험 → 설계실 → `실험만 만들기`.
- 관측: 전송 payload의 `search_space`가 `{dense_weight:0.6, top_k:20, retriever_mode:bm25, k:5}`, `primary_metric`이 `recall_at_20`. 생성된 설정 parameters는 `{k:5, top_k:20, dense_weight:0.6, ...}`. 어댑터는 `top_k`가 있으면 `k`를 무시하므로(`retrieval_eval.py:386`) 후속 실험의 실제 cutoff는 5가 아니라 20이다. 실패 Run 재사용(원본 k=4)도 동일하게 top_k=20이 붙었다.
- UI는 `검색 수(top_k) 우선 적용 안내`만 표시하고 막지 않는다. 사용자가 원본과 같은 설정이라고 오해하기 쉽다.
- 코드 위치: `ExperimentDesignerView.tsx:64-88`에서 복제 값 위에 방법 기본값(`d.params.values`)이 병합된다. 겹치는 키(예 retriever_mode)에서 어느 쪽이 이기는지는 이번에 따로 시험하지 않았다(확인 필요).

### F1. shadowed k 판정이 top_k도 sweep인 경우를 놓침 (중간, backend)
- 재현: `strategy=grid`, `search_space={"k":[5,20],"top_k":[5,20]}`, `fixed_inputs`에 top_k 없음.
- 관측: `POST /experiments/validate` → `valid=true`(preset 지정 시), 생성 201, 설정 4개 `(k,top_k)=(5,5),(5,20),(20,5),(20,20)`. top_k가 항상 있으므로 k는 무의미하고 (5,5)와 (20,5)는 같은 실행이 된다. 763dc03f가 막으려던 "의도하지 않은 동일 arm"이 그대로 남는다.
- 오류 문구의 해결 안내(`top_k를 sweep 변수로 지정하세요`)를 따르면 이 상태가 된다. frontend `hasShadowedKSweep`은 top_k가 sweep이어도 막으므로 frontend와 backend 판정이 서로 다르다.
- 원인: `planner.check_shadowed_k_sweep`(`planner.py:92`)이 top_k가 스칼라일 때만 shadowed로 본다.

### F2. shadowed 오탐 (낮음~중간, backend)
- `fixed_inputs={"top_k":10}` + `search_space={"k":[5,20],"top_k":[5,20]}` → 422. top_k가 sweep으로 덮어쓰이는데도 고정값만 보고 막고, 메시지는 이미 한 일을 하라고 안내한다.
- 재사용에서 같은 문제: 원본 설정이 top_k를 갖고 있으면 상속된 top_k 때문에 `search_space`의 k sweep이 422(`top_k` sweep만 허용). 상속 키를 지울 방법도 없다. `fixed_inputs={"top_k":null}`은 201이지만 저장 parameters에 `top_k:None`이 들어가고, 어댑터 `validate_config`는 정수가 아니면 오류를 낸다(`retrieval_eval.py:305-307`, 코드 근거이며 이 값으로 실제 Run을 돌려 보지는 않았다 확인 필요).
- `strategy=single`(sweep이 전개되지 않음)에서 k 리스트+top_k 고정도 422, `adapter_id=external_report`에서 k/top_k 조합도 422. 어댑터와 무관하게 적용된다.

### F5. 출처 배지가 약함 (낮음, frontend)
- reload 후에도 결과 분석 화면에 `후속 실험: 원본 설정 31920180 기반`이 유지된다(PASS). 다만 설정 ID 앞 8자만 보이고 원본 실험 이름·링크·실패 Run 출처가 없다. 실행 모니터 화면에는 출처 표시가 없다. UX 10을 한눈에 답하기엔 약하다.

### 기타 관찰 (판정 영향 없음)
- 마이그레이션 인덱스 이름이 ORM `create_all`(`ix_research_experiments_source_configuration_id` 등)과 Alembic(`ix_research_experiments_source_cfg` 등)에서 서로 다르다. 새로 만든 DB와 마이그레이션한 DB의 스키마 이름이 갈린다. 동작 영향은 없다.
- 외부 결과 등록(external_report) 원본을 재사용할 때 body에 `adapter_id`가 없으면 기본값 `retrieval_eval`과 비교해 422(`source_adapter_mismatch`). `research.py:529`의 "source 어댑터로 기본값" 분기는 도달하지 못한다.
- source ID 존재 판정: `source_configuration_id` 없이 존재하지 않는 experiment/study/run ID만 주면 404가 아니라 422(`source_configuration_id_required`). cfg가 있고 experiment/study ID가 틀리면 404가 아니라 422 mismatch. 공유 계약 문구("존재하지 않으면 404")가 cfg 기준이라면 PASS, ID별이라면 불일치다. 리드 확인 필요.
- 방법 없이 API로 만든 원본 실험을 UI에서 재사용하면 `방법을 하나 이상 선택하세요`로 막힌다(방법이 있는 원본은 방법이 미리 선택됨).
- 새 실험 생성 후 이동한 모니터 URL에 `reuse_*` 쿼리가 그대로 남는다.

## 1. 마이그레이션 (PASS)

명령: 임시 프로브에서 `alembic command.downgrade(0072_research_lab_v2)` → `upgrade(0073_research_reuse_source)`를 2회 반복. 표준 DB는 `TTAKKARI_TEST_DB`.

| 확인 | 결과 |
|---|---|
| 0073에서 컬럼 4개 | `source_configuration_id, source_experiment_id, source_run_id, source_study_id` |
| downgrade 후 | source 컬럼 0개, `alembic_version=0072_research_lab_v2`, 실험 행(`mig exp`, config `{"k":5}`)과 설정 행 1개 보존 (2회 모두) |
| re-upgrade 후 | 컬럼 4개, 버전 0073, 기존 행 보존, source_* 모두 NULL (2회 모두) |
| FK / 인덱스 | FK 4개 `confdeltype='n'`(SET NULL), 인덱스 4개 |
| ON DELETE SET NULL 동작 | 출처 설정 행 삭제 후 참조 컬럼만 NULL, 나머지 출처 ID는 유지 |
| `alembic heads` | 단일 head `0073_research_reuse_source` |
| generation-contract | `schema_revision=0073_research_reuse_source`, `online_migration_from`에 `0072_research_lab_v2` 포함, 0073은 미포함(현재 head이므로 정합) |
| 기존 테스트 | `tests/test_research_v2_migration.py tests/test_research_v2_api_contract.py` 59 passed (4.83s). `tests/test_generation_deployment.py tests/test_generation_bootstrap.py unit_tests/test_generation_hardening.py unit_tests/test_collector_migration_chain.py unit_tests/test_executor_migration_contract.py` 107 passed (9.41s) |

## 2. 실제 ASGI + PostgreSQL 재사용 반례 (PASS)

모든 케이스를 별도 프로브로 새로 작성해 실행했다. 원본 불변은 새 DB session으로 원본 experiment와 configuration 행 전체를 JSON 비교했다.

| 반례 | 기대 | 관측 |
|---|---|---|
| run만 (cfg 없음) / experiment ID만 / study ID만 | 422 | 422 `source_configuration_id_required` ×3 |
| experiment ID 불일치(없는 ID, 실재하는 다른 실험) | 422 | 422 `source_experiment_id_mismatch` ×2 |
| study ID 불일치(없는 ID, 실재하는 다른 연구) | 422 | 422 `source_study_id_mismatch` ×2 |
| 타 scope (personal 원본 → work 대상), run 포함 | 422 | 422 `source_scope_mismatch` ×2 |
| 존재하지 않는 cfg / 유효 cfg+없는 run / 전부 없는 ID | 404 | 404 `source_configuration_not_found`, `source_run_not_found`, `source_configuration_not_found` |
| UUID 형식 오류 / 없는 대상 연구 | 422 / 404 | 422 validation / 404 `research_study_not_found` |
| 실패 run 재사용 | 201 | 201, 응답·GET(평면+wrapper)·resume·`experiment_planned` event에 source 4종 보존 |
| 다른 설정의 run / 다른 실험·연구의 run | 422 | 422 `source_run_lineage_mismatch` ×2 |
| fixed_inputs override | 원본 값 복제 + 명시값 우선 | `{"k":5,"retriever_mode":"lexical","extra":1}` |
| search_space 확장(비 k) / k sweep(원본에 top_k 없음) | 201, 설정 수만큼 | 설정 2개 / `k=[3,8,13]` 3개 |
| 어댑터 불일치 / 예산 초과 grid | 422 | 422 `source_adapter_mismatch` / 422 `experiment_plan_invalid`(조합 25 > 5) |
| 원본 experiment·configuration 불변 | 동일 | 새 session 재조회에서 JSON 완전 동일 |

fail-open 탐색: 위 계약 위반 fail-open은 발견하지 못했다. 인접 결함은 위 F1, F2와 "기타 관찰"에 적었다.

## 3. shadowed k (부분 FAIL)

| 입력 | 관측 |
|---|---|
| `search_space k=[5,20]` + `fixed_inputs top_k=10` | `/validate` `valid=false` + shadowed 메시지, 생성 422 (PASS) |
| `fixed_inputs {k:5, top_k:20}` 단일 설정 | 201 (PASS) |
| k 리스트 + `search_space top_k=[10]`(길이 1) / strategy ab / 레거시 `config.top_k` | 모두 422 (PASS) |
| `fixed_inputs top_k=None` + k sweep | 201 (저장 값이 None이 되는 점은 F2) |
| `fixed_inputs top_k=[10]`(리스트) + k sweep | 201이지만 `/validate`는 `valid=false`(어댑터가 정수 아님을 지적) |
| **k sweep + top_k sweep** | `/validate` `valid=true`, 생성 201, 4개 설정 (F1 FAIL) |
| fixed top_k + k sweep + top_k sweep | 422 (F2 오탐) |
| strategy single + k 리스트 + top_k, adapter external_report + k 리스트 + top_k | 422 (F2 오탐) |

## 4. frontend

| 명령 | 결과 |
|---|---|
| `npx vitest run src/pages/research` | 2 files, 74 passed |
| `npx tsc -b` | exit 0 |
| `npx eslint .` | exit 0 |
| `node scripts/ui-lint.mjs` | `ui-lint ok · 위반 138건(기준선 138건 이하, 새로 늘지 않음)` |
| `npx vite build` | 성공 (PWA precache 93 entries) |

### 브라우저 E2E (실제 backend + dev server, 1280×1100, 스크린샷은 outbox `c-reuse-review-shots/`)

환경: 임시 PG 클러스터, `ko_tech_docs_v1` 프리셋으로 실제 `retrieval_eval` Run을 돌린 성공 실험(k=[3,5])과, 실패 Run 행을 직접 넣은 실험(`corpus_missing`, 합성 실패)을 시드했다.

| 단계 | 결과 | 스크린샷 |
|---|---|---|
| 결과 화면 설정 행마다 `이 설정으로 후속 실험` 버튼 | PASS | `01-analysis-result.png`, `01b-followup-modal.png` |
| 모달: 대상 연구 선택(현재 연구/새 연구) → `설계실로 이동` | PASS | `01b-followup-modal.png` |
| 설계실에 `출처 설정 기반 후속 실험` 배너, 출처 실험·설정 배지, 방법 자동 선택, 이름 `(후속)` | PASS | `02-designer-reuse.png` |
| 생성 payload에 `source_study/experiment/configuration_id` 전달, 서버 201, 응답에 출처 보존 | PASS | `03-after-create.png` |
| reload 후 결과 분석 화면에 `후속 실험: 원본 설정 31920180 기반` 유지 | PASS(약함, F5) | `04-analysis-reload.png` |
| 실패 Run `이 실행 설정으로 후속 실험` → payload에 `source_run_id` 포함 → API에서 4종 출처 모두 저장 확인 | PASS | `12-designer-from-failed-run.png`, `14-failed-followup-analysis-reload.png` |
| 원본 실험·설정·Run 수 불변 | PASS (실패 원본 1설정·1run, 성공 원본 2설정·2run) | |
| top_k 우선 적용 안내 표시 | 표시됨(그러나 F4) | `02b-designer-params.png` |
| 평가 k `값 여러 개 탐색` 켜기 → 검증 패널에 `top_k가 설정된 상태에서는 k 탐색(sweep)이 ... 무시됩니다` 오류, `만들고 바로 실행`·`실험만 만들기` 모두 비활성 | PASS | `05-shadowed-k-block.png`, `05b-shadowed-k-viewport.png` |
| 해석 노트 저장(성공 노트) | **FAIL (F3)** | `07-interpretation-filled.png`, `08-interpretation-saved.png` |
| 해석 노트 저장(실패 Run) | **FAIL (F3)** | `13-failed-run-interp-save.png` |
| API로 넣은 해석 노트 reload 후 표시, 자동 수치 요약과 분리 | PASS | `10-interpretation-api-seeded-display.png` |
| 실패 Run이 `실패·취소한 실행` 섹션에 원인 코드·메시지와 함께 유지 | PASS | `11-failed-run-section.png` |

브라우저에서 시험하지 않은 것: 고급(JSON) 탭에서 `extra.k` 리스트로 shadowed를 만드는 경로(vitest에만 있음), 대상 연구를 다른 연구/새 연구로 바꿔 생성, 320~390px 폭, WebKit. 이들은 미실행이다.

### 인계 후 추가 확인 요청: 채택 → reload → 최고 설정/연결 artifact

| 경로 | 판정 | 근거 |
|---|---|---|
| 현재 화면에서 결과를 채택한 뒤 reload | 미실행 | 기존 브라우저 검증은 분석 결과에서 후속 실험을 생성하고 출처 배지가 reload 뒤 유지되는지만 확인했다. 채택 동작은 수행하지 않았다. |
| reload 후 최고 설정 및 연결 artifact 표시 | 미실행 | 기존 검증에서 최고 설정의 지속성이나 연결 artifact 링크/관계를 검사하지 않았다. |

따라서 이 보고서의 브라우저 PASS는 **후속 실험 생성·출처 유지** 범위에만 해당하며, 채택 상태 및 artifact 표시를 보증하지 않는다.

## 5. 원문 UX 질문 판정 (`refactor-v2-requirements.md:62`)

- 7 성공 이유·실패 원인: **부분**. 실패 원인(`error_code: error_message`)은 결과 분석과 실행 모니터에 표시된다(PASS). `자동 수치 요약`은 측정이 부족하면 `측정 데이터가 충분하지 않습니다`만 말한다. 사용자가 이유를 직접 남기는 해석 노트는 저장 단계에서 깨진다(F3). F3가 고쳐지기 전에는 "답해진다"고 할 수 없다.
- 8 재실행 가능한 실험: **PASS**. 실행 모니터에서 실패 Run 행의 `다시 실행`을 눌러 새 Run이 생기고 성공했으며 기존 실패 행은 그대로 남았다(`15-monitor-failed.png`, `16-monitor-after-rerun.png`). 이 버튼은 e1158f0 이전부터 있던 기능이고 `local_measured`/`migrated_v1` 출처에만 보인다.
- 10 이전 연구 재사용: **부분**. 설정·실패 Run에서 후속 실험으로 이동, 다른 연구 선택, 출처 저장·reload 유지는 PASS. 그러나 방법 기본값이 덧씌워져 원본과 다른 설정이 만들어지고(F4), 출처 표시는 짧은 ID뿐이다(F5).

## 재현 방법

- backend 프로브: 임시 `tests/zz_probe_c_reuse_review.py`(사본은 outbox `c-reuse-review-shots/`)를 `scripts/pytest_slots.py --timeout 300 -- uv run pytest -q tests/zz_probe_c_reuse_review.py`로 실행. 표준 `TTAKKARI_TEST_DB` 사용.
- 브라우저: `tests/pg_sandbox`로 임시 클러스터를 띄우고 `TTAKKARI_CORS_ORIGINS`에 dev server 출처를 넣어 `uvicorn`(포트 28799) 실행, `VITE_API_BASE=http://127.0.0.1:28799 npx vite --port 25173`, 로그인 후 playwright-core로 구동.

## 인계 후 보고서 보완

2026-10-26 인계 요청에 따라 위 채택/reload/artifact 경로를 원래 실행 기록과 대조했다. 기존 시험에 포함되지 않아 미실행으로 남겼다. 이 보완은 보고서만 수정했으며 제품 코드는 변경하지 않았다. 보완 시 worktree HEAD는 이 보고서 커밋 `0f675591`이었고, 그 부모 제품 revision은 `9f087f07`이었다. 해당 revision의 runner/artifact 관련 파일 및 테스트는 수정하지 않았다.
