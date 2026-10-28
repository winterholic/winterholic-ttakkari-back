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
- Push Commit SHA: `16a73437b7d0f3fbf6e16092499d159fe3bda4a9`
