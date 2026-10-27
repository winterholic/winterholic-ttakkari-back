# 2026-10-28 지식도서관 연구 맥락 혼입 수정

- 요청: 첨부한 “003~011 Mac CPU 통합 보고 최종 인수와 iPhone 미완료” 같은 작업 기록을 맥락 보존만을 이유로 지식도서관에 넣지 말 것.
- 기준 revision: 운영 배포와 origin/main 모두 `c70bce7ba8ab888ea4989f2ab3ef887ff41f0809` 확인. dirty 기본 checkout은 건드리지 않고 별도 worktree에서 구현.
- 원문 확인: `kb_document(90067a7c-bb85-4b70-8edf-d329a45659a7)` 본문은 SHA/검증 명령/부분 인수/미완료의 압축 기록이었다.
- 공통 원인: Research Lab `import_source`가 `research_text`로 KB 불변 저장소를 재사용하며, `library_filter`가 등록 목적을 구분하지 않아 일반 지식 목록·검색·집계에도 노출했다.
- 사전 운영 조회 `kb_documents(limit=100)`는 총 99건 전부 반환. research_text 84건 중 로컬/repo/session locator 24건과 외부 URL 참고자료 60건이 혼재했다. 웹 수집은 15건. source metadata 분류 기준의 예상 분리이며 전 문서 사실성 검토를 뜻하지 않는다.
- [사전 조회 메타데이터](evidence/2026-10-28-kb-context-inventory.json)에 문서 ID·locator·SHA와 예상 분류를 남겼다. 운영 반영 영수증은 별도 확인한다.

## 변경

- 새 research source의 purpose 기본값은 research_context. 지식 노출은 명시적으로 고른 knowledge_reference로 제한한다. 외부 URL뿐 아니라 별도로 정리한 로컬 참고자료도 등록할 수 있다. 사용자 요청은 작업 기록의 자동 혼입 금지이며 로컬 지식 자체를 금지하는 것은 아니다.
- source metadata 불변성과 재등록 멱등성을 지킨다. 기존 source_key의 등록 목적 변경은 409이며 묵시적 승격을 보고하지 않는다.
- legacy 외부 인용은 유지하고 로컬/repo/session 연구 기록은 목록·검색·집계에서 제외한다. 신규 semantic projection도 같은 discovery filter를 따른다. 이미 있던 vector 역시 검색 조건에서 걸러진다.
- source/revision/passage/evidence는 삭제·변경하지 않는다. 연구 resume과 직접 인용은 기존 접근 조건으로 계속 가능하다. schema migration이나 운영 DB 직접 수정이 필요 없다.
- MCP manage_research 설명에 작업 지시·handoff·인수 기록을 일반 지식으로 넣지 않는 경계를 추가했다. 사용자 선호는 remember로 저장했다.

## 검증 상태

- 테스트 먼저: 수정 전 새 경계 테스트 7 failed / 1 passed로 기존 목록·hybrid 노출과 purpose 미지원을 재현했다.
- 일차 PostgreSQL/HTTP 회귀: `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_kb_research_boundary.py tests/test_research_lab.py tests/test_kb_retrieval.py tests/test_kb_library.py` → 39 passed, 5 기존 deprecated-scope warnings, 2.03s.
- 확대 검증: KB HTTP/agent/검색/관측/불변성과 research source refresh/근거/재개/v2 API 및 route/MCP 계약을 실행했다. 1차 166 passed, 새 테스트의 오류 envelope key를 잘못 읽은 1 failed. 구현의 409 응답은 정상이며 테스트를 기존 오류 계약에 맞췄다.
- 최종 확대 검증: 아래 명령 → **168 passed, 20 기존 deprecated-scope warnings, 12.57s**. 실제 PostgreSQL/HTTP와 hash test embedder로 검증했으며 운영 DB는 건드리지 않았다.

  `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_kb_research_boundary.py tests/test_kb_library.py tests/test_kb_library_api.py tests/test_kb_agent_api.py tests/test_kb_retrieval.py tests/test_kb_observations.py tests/test_kb_defects_reproduce.py tests/test_research_lab.py tests/test_research_source_refresh_acceptance.py tests/test_research_selection_evidence.py tests/test_research_selection_context.py tests/test_research_resume_pagination.py tests/test_research_v2_api_contract.py tests/test_route_inventory.py unit_tests/test_knowledge_mcp.py`

- 변경 Python 6개 파일 `ruff check` → All checks passed. `git diff --check` → 출력 없음/exit 0.
- 배포 예약 후 job id와 revision은 이 lead의 manage_work에 보존한다. 실제 운영 적용 후 24건 제외·75건 보존 예상과 직접 인용/연구 resume을 MCP로 확인해야 한다. 배포 예약은 적용 성공이 아니며 이 문서만으로 운영 완료를 주장하지 않는다.
