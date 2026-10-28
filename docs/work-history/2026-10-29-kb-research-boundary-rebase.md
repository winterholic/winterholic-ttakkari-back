# 2026-10-29 지식도서관 연구 맥락 분리 최신 main 리베이스 및 검증

## 1. 개요 및 배경
- 이전 세션(`d88fca52-be93-4f32-81c1-b53508d8b1c9`)에서 지식도서관(Knowledge Library)과 연구 맥락(Research Context)의 경계 분리(`91c376a3d886`)를 완료했으나, sandbox 내 loopback 접속 EPERM 제약 및 GitHub push 자격 문제로 main 통합이 보류됨.
- 최신 `origin/main`(`2e75876d9db9329cb525a835e062c2e9520261cf`) 상태를 확인하고 최신 커밋 위로 격리 브랜치 재구성 및 검증 수행.
- ttakkari main 통합/배포 단일 소유자(`INFRA 441ae34d-0d00-484b-b001-1a4b56da0d02`)에게 인계하기 위해 브랜치 푸시까지만 수행하고 main push 및 배포 요청은 건너뜀.

## 2. 최신 main 대조 및 리베이스
- `origin/main` ancestor 확인: `git merge-base --is-ancestor 91c376a3 origin/main` -> `NOT_ANCESTOR`. 최신 main에 미통합 상태 확인.
- 최신 `origin/main`(`2e75876d`) 기반 격리 브랜치 `fix/kb-context-boundary-20261029` 생성.
- `91c376a3` 체리픽 적용: `src/ttakkari/orchestrator/mcp_server.py` 자동 병합 성공, 충돌 없음.
- 변경 파일:
  - `docs/adr/0063-knowledge-library.md`
  - `docs/work-history/2026-10-28-kb-research-boundary.md`
  - `docs/work-history/evidence/2026-10-28-kb-context-inventory.json`
  - `src/ttakkari/api/research.py`
  - `src/ttakkari/kb_library.py`
  - `src/ttakkari/kb_retrieval.py`
  - `src/ttakkari/orchestrator/mcp_server.py`
  - `src/ttakkari/research/service.py`
  - `tests/test_kb_research_boundary.py`

## 3. 운영 환경 실측 Readback (지원 MCP `kb_documents`)
- 지원 MCP `kb_documents(limit=100)` 2회 호출을 통해 현재 운영 DB의 전체 지식 도서관 문서 121건 실측:
  - 총 문서 수: 121건
  - 종류별: `research_text` 105건, `web` 16건
  - Locator 분류:
    - 외부 웹 URL (`http://`, `https://`): 76건
    - 로컬 저장소/파일 (`repo:`, `file:`, `/`): 38건
    - 세션 기록 (`session:`): 2건
    - 기타 (`regna-ios/...` 등): 5건
  - 관측 결과: 과거 2026-10-28 조사 시점의 99건(외부 75 / 연구 24)에서 추가 연구 활동으로 인해 총 121건(외부 76 / 연구·작업 45)으로 증가함. 현재 배포된 백엔드에는 필터가 없어 non-HTTP 연구/작업 기록 45건이 지식도서관에 여전히 노출되고 있음. 본 패치 적용 시 45건이 지식 목록·검색·집계에서 정상 제외되고 직접 ID 조회/인용은 유지됨.

## 4. 검증 결과
- `ruff check`: All checks passed.
- `git diff --check`: 통과 (공백/포맷 이슈 없음).
- `unit_tests/test_knowledge_mcp.py`: 1 passed (0.63s).
- 인메모리 유닛 로직 검증:
  - `SourceIn`: `purpose` 기본값 `research_context`, 명시적 `knowledge_reference` 지정 지원 통과.
  - `research_source_purpose`: 레거시 URL 판별(`http://` -> `knowledge_reference`, 로컬/세션 -> `research_context`) 및 `origin_meta.library_purpose` 우선순위 통과.
- SQLAlchemy PostgreSQL Dialect 컴파일 검증:
  - `library_filter()` 및 `research_context_filter()`가 PostgreSQL 쿼리로 정상 컴파일됨을 확인 (`kb_sources.origin_meta ->> 'library_purpose'` JSONB 연산자 및 정규식 필터 확인).
- 미실행 사유:
  - `tests/test_kb_research_boundary.py`: 현재 Antigravity 샌드박스 환경에서 loopback TCP (`127.0.0.1:58229`) connect EPERM(Operation not permitted) 관측. 샌드박스 네트워크 격리 정책으로 인해 로컬 PostgreSQL 연결이 차단됨. 규정에 따라 환경 우회/재시도하지 않고 호스트/통합 환경 검증으로 인계.

## 5. 원격 브랜치 게시
- Mac의 활성 GitHub 계정을 MCP `github_switch_account(account="winterholic")`로 전환.
- 원격 브랜치 정상 푸시: `git push origin fix/kb-context-boundary-20261029` (성공).
- Push Commit SHA: `16a73437b7d0f3fbf6e16092499d159fe3bda4a9` (기록 커밋: `56d48793db9145894706edf7f38084f4126fce0a`)

## 6. 독립 검증(Luna `d9a009a9` / Run `38abb759`) FAILED 분석 및 P2 결함
- 판정: FAILED (`verification_outcome=failed`).
- 결함 내용 (P2):
  - 기존 `research_context_filter()`가 문서에 속한 모든 과거 revision 중 `research_context`가 하나라도 존재(`exists()`)하면 문서 전체를 숨김.
  - 이로 인해 한 문서의 계보에서 과거 로컬 작업 기록(context) 후 HTTP/명시적 참고자료(reference)로 업데이트된 경우(혼합 이력), 현재 유효한 참고자료까지 목록·검색·집계에서 은폐됨.
  - 주의: 단순히 HTTP 이력이 하나라도 있으면 문서 전체를 노출시키는 역방향 수정을 취할 경우 최신 작업 본문(context)이 일반 검색에 누출될 위험이 있으므로, current/canonical/revision 모델 계약에 따라 단위별 경계를 정립해야 함.

## 7. P2 결함 교정 설계 및 구현
1. **문서(`KbDocument`) 노출 기준 교정**:
   - `research_context_filter()`가 문서의 **CURRENT revision**(`_current_revision_filter()`)만을 평가하도록 수정.
   - 현재 유효 리비전이 `knowledge_reference`(또는 비-research_text)인 문서는 일반 지식도서관 목록(`list_documents`)에 정상 노출.
   - 현재 유효 리비전이 `research_context`인 문서는 문서 전체가 지식 목록에서 제외됨 (역방향 누출 방지).
2. **리비전/패시지(`KbRevision`/`KbPassage`) 단위 경계 분리**:
   - `research_context_revision_filter(KbRevision)` 신설.
   - `search_passages`: `current_only=False` 검색 시에도 과거 `research_context` 리비전의 패시지가 일반 검색에 노출되지 않도록 `~research_context_revision_filter(KbRevision)` 적용.
   - `summary`: 유효 지식 문서에 속하더라도 `research_context`인 리비전/패시지/근거/소스는 도서관 통계에 집계되지 않도록 분리.
   - `kb_retrieval.index_pending` / `status` / `search`: 자동 임베딩 대상 및 시맨틱/하이브리드 검색 풀에서 `research_context` 패시지를 일관되게 제외.
3. **직접 ID / 인용 / 원문 바이트 / 연구 재개 보존**:
   - `get_document`, `get_evidence`, 연구 세션 재개는 `library_filter`가 아닌 `access_filter` / 직접 조회를 따르므로 원문 바이트, 해시 검증, 슬라이스, 노드 인용이 100% 온전하게 보존됨.

## 8. 교정 검증 결과
- 정적 검사 및 린트:
  - `uv run ruff check src/ttakkari/kb_library.py src/ttakkari/kb_retrieval.py tests/test_kb_research_boundary.py unit_tests/test_kb_research_boundary_unit.py`: All checks passed.
  - `git diff --check`: 통과.
- 단위/인메모리/PostgreSQL 컴파일 검증 (`unit_tests/`):
  - `unit_tests/test_kb_research_boundary_unit.py`: 4 passed (0.25s).
    - `test_research_source_purpose_classification`: 명시적/레거시/HTTP/로컬/세션 locator 분류 검증.
    - `test_source_is_research_context_sql_logic`: SQL 식 평가 검증.
    - `test_p2_defect_demonstration_and_correction_logic`: 혼합 이력(context->reference: 문서 노출 & context 본문 은폐, reference->context: 문서 은폐) 논리 검증.
    - `test_postgresql_dialect_compilation_all_library_queries`: `library_filter`, `research_context_revision_filter`, `search_passages`, `summary`, `index_pending` 쿼리의 PostgreSQL dialect 컴파일 및 서브쿼리 correlate 검증.
  - `unit_tests/test_knowledge_mcp.py`: 1 passed (0.62s).
- 실제 DB 통합 회귀 테스트 (`tests/test_kb_research_boundary.py`):
  - 추가 테스트 케이스:
    - `test_mixed_history_context_then_reference_appears_in_library`: context -> reference 혼합 이력에서 문서 노출, reference 검색, context 검색 방지, 요약 집계 정합성, 직접 ID 인용 검증.
    - `test_mixed_history_reference_then_context_hidden_from_library`: reference -> context 역순 혼합에서 문서 숨김 및 본문 미노출 검증.
    - `test_explicit_knowledge_reference_with_local_and_session_locators`: 로컬/세션 locator의 명시적 reference 보존 검증.
    - `test_canonical_and_covered_alias_with_research_boundary`: alias(covered) 중복 배제 및 canonical 문서 유지 검증.
  - 미실행 사유: Antigravity 샌드박스 loopback TCP(`127.0.0.1:53401`) connect `PermissionError: [Errno 1] Operation not permitted`.
  - 배포/CI 게이트 검증 조건: 독립 게이트 또는 호스트 PostgreSQL 환경에서 `uv run pytest tests/test_kb_research_boundary.py` 전수 통과 확인 필요.

## 9. 원격 게시 및 인계
- 본 교정 커밋은 기존 SHA(`56d48793`) 위에 추가 커밋으로 생성하여 리베이스/force push 없이 안전하게 게시.
- 기획(`af14105d-230f-4ab2-908d-8a1fcae39d00`) 및 독립 검증자 Luna(`d9a009a9`)에게 재검증 배정용으로 새 HEAD SHA 보고.
