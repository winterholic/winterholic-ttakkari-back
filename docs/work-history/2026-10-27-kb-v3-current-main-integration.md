# 최신 origin/main(1083a0ce)과 KB v3 통합 및 0076 마이그레이션 수렴

## 1. 개요 및 작업 트리
- **기준 merge**: `3789807a` (보존된 `back-kb-v3-final-5787f6de`의 중간 merge SHA, `77c1238a` + KB `1b91fa4bc46be8227187ea30bf860f631450b82f`)
- **최신 main**: `1083a0cee98879da6869a6b224f5a11282ec290d` (`origin/main`)
- **작업 worktree**: `/Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-current-20261027`
- **브랜치**: `integ/kb-v3-current-20261027`
- **소유권 및 경계**: 기존 `back-kb-v3-final-5787f6de` 트리는 리드 전용으로 일체 불변 유지. `main` 머지/push 및 generation 배포는 리드 소유이므로 본 worktree의 통합 커밋까지가 수행 범위.

## 2. 충돌 파일 해소 및 통합
`git merge --no-commit --no-ff origin/main`으로 발생한 9개 충돌 파일 및 신규 마이그레이션을 완벽하게 해소하고 양측의 기능을 손실 없이 결합했다:

1. `pyproject.toml` & `uv.lock`:
   - `pypdf>=6.18,<7` 유지 및 KB v3 파서 의존성(`openpyxl>=3.1.0`, `python-docx>=1.1.0`, `python-pptx>=1.0.0`) 통합.
   - `pg` 마커 선언 추가.
2. `alembic/versions/0076_kb_v3_collection_merge.py`:
   - `0074_kb_v3_research_merge`와 `0075_collection_research_merge`를 두 부모로 수렴하는 no-op 머지 마이그레이션 신설.
   - 기존의 모든 마이그레이션 ID 및 DDL을 단 1글자도 변경하지 않고 단일 head로 병합.
3. `deploy/generation-contract.json`:
   - `schema_revision: "0076_kb_v3_collection_merge"`
   - `online_migration_from`에 양측 누적 리비전(`0072_kb_v3_schema_and_scope`, `0073_kb_l2_budget_ledger`, `0074_kb_v3_research_merge`, `0075_collection_research_merge` 등) 모두 등록.
4. `src/ttakkari/collector/control_models.py`:
   - `KbDocumentAccess`에 `scope`, `pool`, `origin_scope`, `visibility`, `hidden` 필드 및 work 스코프의 global 누출을 차단하는 fail-closed `__init__` 검증 가드 유지.
   - `KbDocumentAlias` 및 `CollectionScopeLedger` 모델 유지.
5. `src/ttakkari/kb_delivery.py`:
   - `origin_meta`의 v3 메타데이터(parser/chunker/lineage/sha256)와 main의 pool/mission 필드 통합.
   - `enqueue_pipeline_job` 호출 보존.
6. `src/ttakkari/kb_library.py`:
   - `access_filter`, `library_filter`, `resolve_doc_key`, `require_document_access`, `set_engine_access`, `document_aliases` 보존.
   - `scope_filter`, `require_document_scope`, `_current_revision_filter`와 결합.
7. `src/ttakkari/api/kb_agent.py`:
   - `agent_kb_access(ctx)`를 `ctx.pool`/`ctx.scope` 기반으로 통합.
   - `search`, `documents`, `document`, `evidence` 엔드포인트에 `access_context`와 `pool` 양방향 인자 지원.
8. `src/ttakkari/api/kb_library.py`:
   - v3 온톨로지(그래프, 엔터티, 별칭)·파이프라인·충돌 라우트 전체 유지.
   - origin/main의 `PUT /documents/{document_id}/engine-access`(`EngineAccessIn`, `kb_engine_access`) 엔드포인트 통합 보존.
9. `src/ttakkari/kb_retrieval.py`:
   - `status` 및 `search`에 `pool` 매개변수 지원.
   - 5대 경로 후보 생성, RRF(k=60), 다양성 캡, 결정적 정렬 결합 유지.

## 3. 인벤토리 및 계약 테스트 갱신
1. `scripts/platform_v2_inventory.py`:
   - `0074`, `0075`, `0076` 머지 리비전을 `core.generation_deploy`로 분류.
   - `src/ttakkari/common/` 및 `kb_` 신규 v3 모듈 54개를 `domain.knowledge_rag`로 등록.
2. `tests/test_kb_v3_schema_migration.py`:
   - head 단언을 `0076_kb_v3_collection_merge`로 갱신하고 부모 계보 단언 반영.
3. `unit_tests/test_schema_head_admission.py`:
   - `0075`를 online 허용 목록으로, `0076`을 current schema head로 갱신.
4. `tests/test_collection_combined_migration_contract.py` & `tests/test_kb_v3_generation_coexistence.py`:
   - generation-contract 검증 기대값을 0076 및 양측 online 목록으로 갱신.

## 4. 검증 결과 및 실행 근거
- `unit_tests/test_platform_v2_inventory.py`: 57 passed (exit 0, `review/kb-v3/current-main-20261027/inventory-green.log`)
- `unit_tests/test_collection_research_migration_merge.py` & `unit_tests/test_schema_head_admission.py`: 21 passed (exit 0, `review/kb-v3/current-main-20261027/admission-green.log`)
- `tests/test_kb_v3_schema_migration.py` (16개 전수 함수): ALL 16 PASSED (`review/kb-v3/current-main-20261027/schema-green.log`)
- `unit_tests/test_collection_adapters.py`, `test_collection_plan.py`, `test_collection_research.py`, `test_collector_parser.py`: 68 passed (exit 0, `review/kb-v3/current-main-20261027/collection-units-green.log`)
- `test_c1_and_c2_form_a_serial_migration_path_and_generation_contract`: PASSED (exit 0)
- `test_contract_declares_head_and_both_intermediate_revisions_online`: PASSED (exit 0)

## 5. 미검증 항목 및 환경 제약
- **미검증 항목**: `tests/conftest.py` 기반의 실DB 통합 fixture(`_schema_and_app`)를 요구하는 E2E / API 실 live 쿼리 pytest.
- **사유**: `미검증: Antigravity sandbox loopback TCP connect EPERM`. Run 소유 DB 마커(`run_id=e4dcc92d-8b6d-482e-a9e7-714867e3f0c5`, port 49782) 및 DB 프로세스는 정상 실행 중이나, 본 에이전트 명령 샌드박스의 loopback TCP 소켓 접근 거부(`PermissionError: [Errno 1] Operation not permitted at sock.connect(('127.0.0.1', 49782))`) 발생.
- 지침에 따라 샌드박스 우회를 시도하지 않고 격리하여 기록함.
