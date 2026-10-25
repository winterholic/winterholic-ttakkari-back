# 수집 플랫폼 임무 중심 리팩토링 — 진행 중

## 현재 이어받을 자리 — 2026-10-25 15:38 KST
- 전체 C1–C7 진행 중. 이번 추가 구현은 이 절이 최신이다. 기존 back/front dirty checkout은 계속 보존한다.
- **C2 단일 지식 도서관 통합 및 프론트엔드 레거시 정리 완료**:
  1. **단일 도서관 규약 및 정규화 (`src/ttakkari/collector/library.py`)**:
     - `LIBRARY_SCOPE = "personal"`, `LEGACY_SCOPES = ("personal", "work", "shared")`.
     - `in_library(col)`: 이전 scope 값 전부를 같은 도서관으로 단일 조회.
     - `job_visible(pool)` / `effective_pool` / `narrowest_pool`: 회사 계정 엔진(`pool=work`)은 work pool 자료만 열람, 소유자 및 개인 엔진(`pool=personal`)은 전체 열람하는 보안 경계 보존.
     - `collector_doc_keys(tenant, source_key, url)`: scope 구획 없는 표준 단일 doc_key와 레거시 doc_key 목록 산출.
  2. **Additive/Online 마이그레이션 (`alembic/versions/0073_collection_single_library.py`)**:
     - `collector_jobs`, `collector_sources`, `kb_document_access`에 `pool VARCHAR(16) NOT NULL DEFAULT 'personal'` 컬럼 추가. 기존 `scope='work'` 행의 pool을 'work'로 백필.
     - `kb_document_aliases`: 지식 불변 원칙에 따라 중복 문서를 삭제하지 않고 canonical 하나로 연계 (`relation IN ('covered', 'variant')`).
     - `collection_scope_ledger`: 변경된 행의 이전/이후 상태를 JSONB로 남기는 감사 추적 테이블 생성.
     - `downgrade()`: rollback 시 pool='work' 정보를 바탕으로 legacy scope='work' 완벽 원복.
  3. **중복 병합 및 Ledger 멱등 스크립트 (`src/ttakkari/collector/scope_merge.py`)**:
     - `merge_scopes(db, run_id)`: 트랜잭션 advisory lock 하에 실행.
     - `collector_jobs`: 중복 (tenant, source_key, request_key) 발생 시 탈락 행의 request_key를 `~merged:work:{hex}`로 재명명하여 둘 다 보존 (FK/이력 보존).
     - `collector_sources`: 최신 설정, 짧은 주기, 좁은 pool(narrowest)로 통합하고 탈락 출처는 ledger에 기록 후 정리.
     - `collector_fetch_cache`: 최신 last_fetched_at 하나만 남기고 중복 제거 및 ledger 기록.
     - `collector_items` / `dedupe_bands`: `LIBRARY_SCOPE`로 일괄 정규화.
     - `kb_documents`: 동일 출처/내용 문서를 `kb_document_aliases`에 `covered`로 등록하고, 소유자 검색 시 canonical만 노출.
  4. **배포 계약 및 CLI (`deploy/generation-contract.json`, `src/ttakkari/cli.py`)**:
     - 계약 스키마를 `0073_collection_single_library`로 승격, `0072_collection_missions`를 `online_migration_from`에 등록.
     - `ttakkari collection-scope-merge` CLI 구현 (기본 dry-run, `--apply` 옵션 지원).
  5. **API 및 모델 교정**:
     - `src/ttakkari/api/collect.py`, `collect_control.py`: `scope` query param을 deprecated로 두고 무시, `in_library`를 통한 단일 조회.
     - `src/ttakkari/api/kb_library.py`: 소유자 단일 도서관 검색 지원, `PUT /api/assistant/kb/documents/{document_id}/engine-access` 엔드포인트 추가(회사 엔진 열람 권한 토글), `kb_register` 시 `pool_for_scope(body.scope)` 연동.
     - `src/ttakkari/api/kb_agent.py`: 세션 토큰의 `ctx.pool`을 기반으로 `kb_library`/`kb_retrieval` 격리 조회.
     - `src/ttakkari/collector/quality_service.py`: 도서관 전체 범위 학습 및 레거시 모델 digest 해석 지원.
     - `src/ttakkari/kb_library.py`: `access_filter`, `library_filter`, `document_aliases` 구현.
  6. **프론트엔드 레거시 scope 정리 (`front-collection-scope`)**:
     - `src/pages/KnowledgeLibraryPage.tsx`: 개인/회사 토글 버튼 제거, 단일 도서관 메타/요약 조회로 정리.
     - `src/pages/CollectPage.tsx`: `scope="personal"` 하드코딩 제거, 운영 설정 패널 prop 간소화.
     - `src/api/collector.ts` 및 하위 컴포넌트: 모든 수집 API의 `scope`를 optional로 변경하고 미전달 시 query에서 제외, `Overview`, `SourcesPanel`, `RunsPanel`, `Deliveries`, `Review`, `QualityPanel`의 `scope?: KbScope` 기본값 지원.

### 이번 검증 범위 (C2)
- **Backend 단위·통합 검증**:
  - `tests/test_collection_scope_merge.py`: 0073 마이그레이션 왕복·ORM 일치, dry-run 롤백 격리, apply 시 중복 twin job request_key 보존, sources narrowest pool 통합, fetch cache 중복 정리, kb_document_aliases 생성 및 canonical 노출, ledger 추적, downgrade 복원, CLI 파서 등록 등 4개 종합 테스트 완료.
  - 전체 수집·KB 회귀 테스트: `pytest tests/test_collection_scope_merge.py tests/test_collection_missions.py tests/test_collection_mission_migration.py tests/test_collector_jobs.py tests/test_collector_pipeline.py tests/test_collector_fetch.py tests/test_collector_product_flow.py tests/test_kb_library.py tests/test_kb_library_api.py tests/test_kb_agent_api.py tests/test_kb_retrieval.py tests/test_route_inventory.py tests/test_security_gate_inventory.py unit_tests/test_schema_head_admission.py unit_tests/test_collection_adapters.py unit_tests/test_collector_policy.py unit_tests/test_collector_parser.py unit_tests/test_collection_plan.py`
    → **270 passed, 66 subtests passed in 12.26s**.
  - `ruff check`: 28개 변경 파일 대상 0 errors (clean).
- **통합 순서 주의 (2026-10-25 15:38 KST 재확인)**:
  - C2 커밋 `104c9b0`의 `0073_collection_single_library`와 C1 커밋 `90c326a`의 `0073_collection_mission_runs`가 각각 `0072_collection_missions`를 `down_revision`으로 선언한다. 두 커밋을 그대로 합치면 Alembic revision 중복/다중 head가 된다.
  - 이 worktree는 C1 migration 및 모델 변경을 포함하지 않는 C2 전용 브랜치이므로 revision을 여기서 임의 변경하지 않았다. 통합 시 순서를 `0073_collection_mission_runs` → `0074_collection_single_library`로 재번호화하고, generation-contract 및 schema-head admission 기준을 `0074`로 함께 갱신해야 한다. 이 순서로 적용하는 것은 통합 검증 전제이며 아직 검증된 통합 결과가 아니다.
  - C1 진행 기록의 broad 회귀 실행은 migration 테스트와 `unit_tests/test_schema_head_admission.py`를 포함해 **247 passed, 66 subtests passed**로 기록되어 있다. C1 migration을 현재 C2 전용 브랜치에 가져오거나 그 검증을 중복 실행하지 않았다.
  - 이번 C2 재검증: `tests/test_collection_scope_merge.py`, KB agent/retrieval, collector pipeline/product flow, route inventory, schema-head admission 선택 실행 → **58 passed, 6 deprecation warnings**. 변경 파일 `ruff check`와 `git diff --check` 통과. 운영 DB 데이터 분포는 조회하지 않았고 미검증이다.
- **Frontend 단위·빌드 검증**:
  - `vitest run src/pages/KnowledgeLibraryPage.test.tsx src/pages/CollectPage.test.tsx`: 19 passed in 903ms.
  - `vitest run src/features/kb/Collection.test.tsx src/features/collector/Collector.test.tsx`: 12 passed in 660ms.
  - 전체 vitest: **146 test files passed, 2 skipped (1379 tests passed, 10 skipped in 11.23s)**.
  - `tsc -b`: 0 errors.
  - `npm run build`: Vite 및 PWA Service Worker 프로덕션 빌드 성공.

### 다음 실행 (원래 요구에서 남은 것)
1. 경량 ML 실제 보수적 선별·독립 평가(C4): 입력 목적 관련성 평가 및 보수적 필터링.
2. 원래 시나리오 A–E E2E 인수 기준 검증.
3. clean main 통합·push·운영 배포 및 실제 기능 확인 (현재는 feature branch 커밋만 수행).

## 이전 이어받을 자리 — 2026-10-25 15:00 KST
- 전체 C1–C7는 여전히 진행 중. 아래 `2026-10-25 14:24 KST`와 `이번 코드`는 이전 이력이다. 이번 추가 구현은 이 절이 최신이다. 기존 back/front dirty checkout은 계속 보존한다.
- **C1/C6 임무 제어, 자동 분할, 공정 할당, 오류 격리 및 프론트엔드 UI 완료**:
  1. **임무 제어 API**: `pause`, `resume`, `cancel`, `extend`(추가 수집: 보존된 frontier 이어가기 + 예산 추가), `rerun`, `update_schedule` 엔드포인트 구현.
     - `CollectorJob`의 `claim_batch`에서 일시정지된 임무(`mission.status == "paused"`)의 작업을 claim에서 배제.
     - `cancel` 시 진행 중/대기 중인 작업을 취소하고 후속 자식 생성을 방지.
     - `extend` 시 frontier가 보존된 상태에서 추가 예산을 배정하여 즉시 다음 자식 작업을 생성·재개.
     - `rerun` 시 기존 `CollectorSource`를 멱등하게 재사용하며 새 임무 주기를 시작.
     - `route_inventory`에 6개 엔드포인트를 `owner-plain`으로 등록 완료.
  2. **자동 하위 작업 분할 및 재개 (Auto-split on Policy Page Limit)**:
     - 한 source/job의 정책 한도(`max_pages_per_job`)에 도달하여 작업이 partial로 종료될 때, 보존된 frontier(`cursor["frontier"]`)를 seed로 삼아 다음 child job을 자동 생성하고 인큐.
     - `runner.py`의 `finish` 직후 mission job인 경우 `reconcile_mission`을 자동 호출하여 즉각적인 분할 재개 트리거.
     - 출처별 최신 job(`latest_jobs`)을 기준으로 출처 완료 여부 및 임무 전체 완료(`succeeded`)를 판정하도록 뷰 로직 교정.
  3. **실제 사용량 기반 공정 예산 재할당 (Fair Share Budget Allocation)**:
     - 단순 예약 페이지 기준 선점 결함을 해소. 종료된 job의 실제 수집 페이지(`cursor["processed"]`)를 집계하여 미사용 예약분을 회수하고, 남은 출처들에게 공정하게 분배(`fair_share`).
  4. **준비 단계 독립 출처 오류 격리**:
     - 복수 출처 준비 단계에서 GitHub API 실패 등 단일 출처의 메타데이터 수집 예외 발생 시 `gaps`에 `reason="github_metadata_failed"`로 기록하고 후속 출처 준비를 중단 없이 계속 진행.
  5. **프론트엔드 UI 제어 컨트롤 및 반응형 디자인**:
     - 임무 상세 화면에 일시정지/재개/취소/더 수집하기(쪽수 입력 인라인 폼)/다시 실행/주기 설정 버튼 및 패널 구현.
     - 320px 모바일 환경부터 데스크톱까지 일관된 버튼 래핑 및 배치(`collector.css`).
     - 상태 뱃지("일시 중지됨", "수집 종료" 등) 및 한국어 에러 메시지 노출.

### 이번 검증 범위 (C1/C6)
- **Backend 단위·통합 검증**:
  - `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py tests/test_collector_jobs.py tests/test_collector_pipeline.py tests/test_collector_fetch.py unit_tests/test_collection_adapters.py unit_tests/test_collector_policy.py unit_tests/test_collector_parser.py --tb=short` → **208 passed, 66 subtests passed in 11.49s**.
  - `tests/test_collection_missions.py`: 8개 신규 제어·분할·공정할당·격리 검증 테스트 추가 (20 passed in 2.95s).
  - `tests/test_route_inventory.py`: 7 passed in 1.35s.
  - `ruff check`: 0 errors (clean).
- **Frontend 단위·빌드 검증**:
  - `vitest run src/pages/CollectPage.test.tsx src/features/collector/ --reporter=dot` → **16 passed in 1.05s** (제어 액션 및 스케줄 갱신 테스트 4건 추가).
  - `tsc -b`: 0 errors.
  - `eslint`: 0 errors.
  - `npm run build`: Vite 및 PWA Service Worker(`dist/sw.js`, `dist/workbox-e217238d.js`) 프로덕션 빌드 성공.

### 다음 실행 (원래 요구에서 남은 것)
1. personal/work 실제 데이터·참조·모델 통합(C2): 맥락 문서 및 파일 연계, immutable provenance 보존.
2. 경량 ML 실제 보수적 선별·독립 평가(C4): 입력 목적 관련성 평가 및 보수적 필터링.
3. 원래 시나리오 A–E E2E 인수 기준 검증.
4. clean main 통합·push·운영 배포 및 실제 기능 확인 (현재는 feature branch 커밋만 수행).

## 이전 이어받을 자리 — 2026-10-25 14:24 KST
- 전체 C1–C7는 여전히 진행 중. 아래 `이번 코드`와 `실행 근거`는 첫 기능 묶음 f33c9be의 이력이다. 이번 추가 구현은 이 절이 최신이다. 기존 back/front dirty checkout은 계속 보존한다.
- GitHub/URL 없는 연구 임무를 내부 `mission_prepare` CollectorJob으로 영속 큐에 연결했다. API에서는 네트워크·모델을 실행하지 않는다. managed claim에 임무 관계를 포함하며 기존 attempt fence·orphan recovery·cancel·generation drain을 재사용한다. 준비 취소가 먼저 기록되면 새 자식 수집을 만들지 않는 재현 검사도 통과했다.
- 연구 준비는 DuckDuckGo HTML 검색→후보 URL/검색어/제공자/순위 보존→기존 정책-backed acquisition을 연결한다. 검색 진전은 매 쿼리마다 원자적으로 보존하고 새 후보가 두 번 연속 없으면 종료한다. 현재 쿼리는 기존 결정적 시작안이며, **하위 주제 의미 분해·문서에서 새 개념을 읽어 후속 검색하는 자율 연구는 아직 미완성**이다. 알려지지 않은 권리의 후보는 gap으로 남으므로 URL 없는 외부 임무 전체 성공을 아직 검증하지 않았다.
- 실제 Bing RSS 실험은 질의와 무관한 Google/YouTube 결과 및 RSS 표시 외 이용 제한을 확인하여 폐기했다. 현재 코드에 Bing 경로는 없다. DuckDuckGo 외부 제한 실험은 `"harness engineering" research paper` 질의에서 arXiv 논문, 원문 사이트, 논문 모음 등 5개 URL의 관련 제목을 확인했다. 이는 후보 탐색 검증이며 본문 적재·신뢰도 검증이 아니다.
- GitHub API repo/commit/tree inventory를 실제 임무 자식 수집에 연결했다. 하위 LICENSE 우선 적용, MIT/Apache 전체 표준 본문 대조, 별도 제한 문구가 앞뒤에 있으면 unknown, 여러 LICENSE가 있으면 unknown을 유지한다. 하위 LICENSE는 commit-pinned raw로 읽고 Git blob SHA와 대조하여 파일별 API 호출을 피한다. license notice와 원본 path/revision/blob SHA는 임무 provenance를 통해 KB origin_meta로 전달된다. LICENSE 자체는 지식 문서 후보에서 제외했다.
- 자동 확인한 라이선스 정책은 `allowed_path_prefixes`로 해당 commit/file에 한정한다. 빈 필드는 기존 policy hash를 바꾸지 않도록 직렬화에서 생략한다. 파일 정책이 `/robots.txt`도 막아 `robots_unreachable`이 되던 결함을 로컬 재현 후 교정: origin의 `/robots.txt` 제어 파일만 별도로 허용하며 실제 문서의 host/path 제한은 그대로다.
- Python 공식 문서의 PSF-2.0 근거를 공식 license 페이지에서 확인하고 서버 정책으로 등록한다. 수집에는 기존 robots/pacing/SSRF/권리 검사가 적용된다. 문서 본문(main/article)의 링크를 사이트 공통 메뉴보다 먼저 탐색하고, PDF 링크도 보존한다.
- 임무 문서 작업은 25건 단위로 정상 yield한다. retry 횟수를 소비하지 않고 다음 tick에서 기존 cursor를 이어간다. 전체 요청 page budget에 닿으면 frontier를 버리지 않고 partial + `mission_page_budget`/남은 URL 수로 종료한다. **한 source/job의 정책 한도를 넘는 자동 하위 작업 분할·임무 확장/재개 API는 아직 없다.**

### 이번 검증 범위
- **최종 추가 확인:** 코드 revision `02b1510610faf10cef265b4b8f796c98d78b8983`에서 `COLLECTION_LIVE=1 PYTHONPATH=src ../back/.venv/bin/python -m pytest -q -s tests/test_collection_live.py --tb=short` → **2 passed in 8.02s**. GitHub 15개 eligible 중 제한한 2개(SKill.md + references/_sections.md) 실제 적재·ASP.NET 검색 확인. Python 본문 링크 우선 탐색으로 tutorial/builtins/library **3개 모두 실제 적재·Python 검색**, 29개 frontier 보존. 두 임무는 의도한 제한 실험이므로 partial이며 B/D 전체 커버리지 완료가 아니다. 실행 stdout은 이 Run outbox `collection-live-check.txt`로 보존하고 share_file 한다. 아래 첫 외부 실험의 '최종 파일 선택/탐색 순서 재검증 필요'는 이 관측으로 해소했다. 서버/브라우저/작업 세션 잔류 없음, 시험 PG는 fixture teardown으로 정리됐다.
- Codex 명령 환경, 격리 PG/로컬 HTTP: `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py tests/test_collector_jobs.py tests/test_collector_pipeline.py tests/test_collector_fetch.py unit_tests/test_collection_adapters.py unit_tests/test_collector_policy.py unit_tests/test_collector_parser.py --tb=short` → **199 passed, 66 subtests passed in 9.50s**. 그 뒤 MIT 앞부분의 추가 제한 문구 반례를 추가/교정하고 `unit_tests/test_collection_adapters.py` → **11 passed**. 같은 범위 중복 수를 합산하지 않는다. 변경 ruff와 diff check clean.
- opt-in `tests/test_collection_live.py`: 실제 외부 GitHub openai/skills(aspnet-core 하위 경로) 및 Python tutorial→격리 KB 적재/검색, **2 passed in 7.87s**. GitHub commit `49f948faa9258a0c61caceaf225e179651397431`, SKILL.md 포함 2건 적재; 당시 두 번째는 LICENSE였으며 이후 후보에서 제외했으므로 최종 파일 선택은 재검증 필요. Python 3건 budget에서 2건 적재/검색, 1건 changelog response_too_large 보류, 29개 frontier 보존. 이후 본문 링크 우선순위를 변경했으므로 최종 탐색 순서는 재검증 필요. **운영 DB/배포 검증이 아니다.** 이 두 제한 실험으로 E2E-1~5 전체 완료를 주장하지 않는다.
- 초기 외부 GitHub 시험의 `robots_unreachable`은 위 path 정책 제어 파일 결함이었다. raw host의 실제 robots는 404(no rules), 수정 후 실제 acquisition이 통과했다. 호스트/운영 전체 장애로 일반화하지 않는다.

### 다음 실행 (원래 요구에서 남은 것)
1. 위 최종 외부 검증과 현재 git 상태/영속 계약을 복원한다. GitHub 파일 선택/raw license·Python 본문 링크 순서는 재확인했으므로 같은 검사를 무조건 반복하지 않는다.
2. 임무 pause/resume/cancel/확장/주기 갱신을 API/UI에 연결하고, source/job page 한도를 넘는 자동 분할·재개를 구현한다. **현재 allocation은 예약 페이지 기준이라 먼저 만난 출처가 예산을 선점하거나 미사용 예약이 남을 수 있다.** 복수 레포/혼합/연구 임무에 실제 사용량 기반의 공정한 하위 작업 할당을 연결해야 한다. 한 출처 metadata 실패가 뒤의 독립 출처를 막지 않도록 준비 작업도 분리/기록한다.
3. personal/work 실제 데이터·참조·모델 통합(C2), 경량 ML 실제 보수적 선별·독립 평가(C4), 사용자 맥락 자체 적재와 자료 관계, 논문 metadata/브라우저 경로를 이어간다. 원래 시나리오 A–E 인수 기준은 manage_work.original_request로 복원한다.
4. C1–C7 전체 증거와 미완료를 대조한 뒤 clean main 통합·push·운영 배포 및 실제 기능 확인. 현재 frontend는 e5afcc6 clean, 이번 Run에서 변경하지 않았다. 아직 main merge/push/배포 없음. 별도 worker/외부 서버 없음.

### 계속 지킬 결정과 함정
- 최종 목표는 실제 유용한 지식의 수집·검색이며 테스트/Gate/예약은 전체 완료가 아니다. 추가 승인 없이 계속한다.
- 외부 SKILL 지시는 끝까지 데이터. source rights는 서버 검증 근거만 사용한다. client가 license/rights를 주장해 자동 허용시키지 않는다.
- collector는 KB 저장소/RAG를 중복 구현하지 않는다. 실제 delivery receipt와 검색을 확인하고 export를 적재로 세지 않는다.
- protected harness/config.env/accounts/logs는 열지 않는다. 운영 조회/배포는 지원 MCP를 사용한다.

## 원래 목표와 작업 위치
- 사용자 2026-10-25 요청: 자연어·URL·GitHub·논문/PDF·사용자 맥락을 임무 하나로 처리하고, 자율 탐색·저비용 ML 선별·통합 지식 도서관·임무 UI·A–E 실제 E2E·운영 배포까지 수행한다. 중간 구현/테스트/예약은 전체 완료가 아니다.
- backend: `back-collection-missions`, branch `feat/collection-missions-20261025`, 시작 `551c354b49b5e3a9ac40424e2361c558c857743a`.
- frontend: `front-collection-missions`, 같은 branch, 시작 `da9d98d`.
- 기존 `back`/`front`는 크게 뒤처진 dirty checkout이다. 해당 변경은 보존했다. 프로젝트에 적용되는 AGENTS.md/CLAUDE.md는 발견되지 않았다.
- 전체 계약은 기획 세션 manage_work C1–C7에 active로 보존. 아직 필수 요구 중 어느 묶음도 전체 done이 아니다.

## 이번 코드
- 임무 계획/영속 모델/인증 owner API: 목표·URL·사용자 맥락·예산·종료 조건·참고 자료 유형을 보존한다. URL 분류에는 모델 호출이 없다. URL 없는 요청의 현재 계획은 결정적 시작안이며 지능형 자율 연구가 구현된 상태는 아니다.
- 기존 정책-backed 수집 source/job/frontier/SSRF/robots/raw/dedupe/KB outbox를 재사용한다. 임무 provenance가 job→item envelope→KbSource origin_meta까지 이어진다.
- 결과는 실제 KB delivery와 document/revision ID를 집계한다. 단순 보관/export를 KB 적재로 세지 않는다. 의미·온톨로지 처리는 unverified로 구별한다. 목록은 본문을 읽지 않고 집계하고 상세 메타데이터는 SQL에서 100건으로 제한한다.
- additive `0072_collection_missions`와 기존 세대 계약 online migration 선언을 연결했다. 수집/KB 기존 이력을 재작성하지 않는다.
- 직접 지정한 PDF는 pypdf 6.21.2의 별도 프로세스에서 추출한다. input/page/decompression/CPU/wall 제한, 암호화·스캔 PDF 보류, 페이지 경계·실제 parser version을 남긴다. macOS RLIMIT_AS는 ValueError라 적용하지 않으며 Linux만 VM soft limit을 둔다. 추출기가 자른 본문을 완전한 문서로 표시하던 기존 분석 경로도 교정했다.
- GitHub API 어댑터는 repo→commit→tree→SKILL/문서/관련 코드 목록을 조회하고 raw URL을 commit SHA로 고정한다. 실행 경로에 연결하는 작업은 남아 있다. 미확인 라이선스를 allowed로 추정하지 않는다. 저장소/하위 파일 라이선스 자동 해석을 더 연결해야 한다.
- 한국어 임무 composer, 맥락/텍스트 파일, 선택적 계획 preview, 임무 목록/성과/결과/KB 링크. 운영 패널은 접고 열 때만 mount하여 불필요한 요청을 피한다. UI에서 범위 토글을 뺐지만 실제 scope 데이터 통합은 아직 전혀 완료되지 않았고 고급 패널은 현재 personal legacy 경로다.

## 실행 근거와 범위
- Codex 명령 환경, 격리 테스트 PostgreSQL/로컬 HTTP fixture:
  `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py tests/test_collection_mission_migration.py tests/test_collector_product_flow.py tests/test_collector_pipeline.py tests/test_route_inventory.py tests/test_security_gate_inventory.py unit_tests/test_collection_plan.py unit_tests/test_collection_adapters.py unit_tests/test_collector_parser.py unit_tests/test_executor_migration_contract.py unit_tests/test_schema_head_admission.py --tb=short`
  → **100 passed, 21 subtests passed in 5.62s**. 임무→HTML/PDF 실제 추출→KB receipt→검색, provenance, 멱등·권한·export 구분, schema roundtrip, 라우트와 배포 계약 포함. 외부/운영 E2E를 뜻하지 않는다.
- frontend `vitest run src/pages/CollectPage.test.tsx src/features/collector/ --reporter=dot` → **12 passed**. `tsc -b`, 변경 파일 ESLint, Vite build 성공. 기존 큰 chunk advisory 유지. 변경 Python ruff 성공.
- 외부 GitHub 제한 실험: `openai/skills` commit `49f948faa9258a0c61caceaf225e179651397431`, eligible files 700, limit 1; `skills/.curated/aspnet-core/SKILL.md` commit-pinned raw 200, 5544 bytes, frontmatter 확인. repo root license는 unknown. 이는 inventory/raw 획득만 검증했으며 KB 적재나 B 시나리오 완료가 아니다. 첫 smoke script의 SafeFetcher user_agent 누락은 수정한 검증 명령으로 재확인했다.
- configured ttakkari_browser 개발 fixture(실제 사용자 데이터 아님): 320/390/1440px snapshot bounds와 화면 확인. screenshot `page-2026-10-25T05-02-33-626Z.png`는 시험 데이터이며 Run outbox에서 share_file 했다. 임시 UI harness와 서버는 제거/종료했고 브라우저도 닫았다. 운영 로그인/모바일 실기기 확인 아님.
- main merge/push/배포는 아직 하지 않았다. 운영 최근 성공은 MCP에서 `94a4963c`, 최신 main `551c354b`의 이전 배포는 다른 플랫폼 테스트에서 failed로 관찰했다. 현 작업 때문에 그 원인을 추정하거나 검증 완료로 바꾸지 않는다.

## 다음 실행 — 승인 추가 없이 계속
1. 현재 작업 공간의 실제 git 상태와 manage_work를 복원한다. 유효한 검증을 처음부터 반복하지 않는다.
2. GitHub 준비·검색·정책 해석을 API 프로세스 밖의 기존 durable job claim/fence/재시도 경로에 연결한다. URL 없는 임무를 단순 discovery_pending에 두는 현재 임시 상태를 해소한다. 외부 SKILL 지시문은 끝까지 데이터로 취급한다.
3. GitHub 하위 LICENSE, 공식 Python 문서 라이선스, 공개 검색/논문 provenance를 자동 검증하고, 검증 불가 권리만 정확한 gap으로 남긴다. .pdf 링크 탐색은 아직 legacy parser가 제외하므로 함께 연결한다.
4. 임무별 batch yield·자동 분할·frontier 보존·재개·추가 수집·주기 갱신과 상태 제어. 현재 기존 파이프라인은 page cap에서 frontier를 버리므로 그대로 두면 D 인수 불가다.
5. personal/work 실제 데이터/모델/참조 통합은 별도 필수 작업이다. immutable evidence와 payload hash를 무작정 덮지 않는다. 현재 프론트 변경만으로 C2가 충족됐다고 쓰지 않는다.
6. 경량 ML 후보를 최신 공식 근거로 좁히고 대표 실험/독립 평가를 통해 실제 선별에 연결한다. 현재 ML은 기존 shadow 그대로다. 입력 목적 관련성, 보수적 자동 처리, 보류/복구와 미검증 표시가 필요하다.
7. 사용자 맥락의 KB 적재와 외부 자료 관계, PDF/파일 입력, 시나리오 A–E 제한 외부 E2E·필수 회귀 후 clean main 통합·push·배포 예약을 진행하고 운영 실동작을 별도 확인한다.
