# 회의록 개인·회사 분류 수정

사용자가 회사 회의록을 개인으로 잘못 올린 뒤 옮길 수 없다고 요청했다. 기존 PATCH는 scope를 받지 않아 HTTP 200이어도 변경되지 않았다.

- 상세 상단 범위 선택과 변경 확인을 추가했다. 저장 응답의 실제 scope를 검증하며, 전사·분석 중에는 변경하지 않는다.
- PATCH scope personal/work를 지원한다. 내용·녹음·분석·항목·제안은 보존하며, 생략된 프로젝트는 새 범위와 맞지 않는 연결만 해제한다. 명시한 프로젝트는 새 범위로 원자적으로 검증한다.
- 기존 공개 링크는 기존 DB trigger로 해제한다. 이전 범위 검색 조각은 변경 트랜잭션에서 삭제하고 재색인하며, 옛 scope로 진행하던 색인의 지연 쓰기를 차단한다.
- 승인한 도서관 기억은 재분류하지 않는다. 이동 전 범위의 기억·액션을 수정하는 제안 승인은 stale로 막아 재분석을 안내한다.
- 별도 DB `ttk_scope_integrated_20261016`: `PYTHONPATH=src TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_scope_integrated_20261016 <back .venv>/bin/python -m pytest tests/test_meeting_scope.py tests/test_meetings.py tests/test_public_sharing.py tests/test_meeting_exports.py -q --tb=short` → **85 passed in 20.24s**.
- 변경 Python ruff 통과. DB migration 추가 없음. 기존 배포·알림 변경 39d6632를 보존해 통합했다.
- 프론트 전체 검사 469 passed / 1 skipped, TypeScript·Vite build·변경 ESLint 통과. 실제 컴포넌트와 합성 응답으로 브라우저 390×844/1280×900 계측, 모바일 취소·변경과 회사/프로젝트 해제 표시 확인. 운영 사용자 데이터 변경은 하지 않았다.
- 화면 증거와 검사 로그: outbox/2a4cb554-3cac-4163-8710-cfccd0584f2c. 배포는 clean commit/push 후 외부 감독에 예약한다. 예약을 운영 반영 완료로 표기하지 않는다.
