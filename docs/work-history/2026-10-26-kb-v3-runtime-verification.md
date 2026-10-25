# KB v3 실제 PostgreSQL 검증 재개

## 대상과 자원 소유

- 앞선 코드 기준: `27ea2f44d24b84e0a5e57b05cacb626571376a76`.
- 후속 교정 기준: `3a6d69a524d0f86399cf496d34e77676d50d9d9f`.
- 프론트: `14d09f5a2ca35b816490792bfcf4a573a8b689f4`.
- 실행은 리드 Codex 명령 환경이며, 런타임이 이미 제공한 현재 Run
  `b585d875-e284-4d8e-8408-9392d1805e5d` 전용 PostgreSQL을 사용했다.
  `/tmp/ttakkari-run-test-6372-5bd08299/run-test-cluster.marker`의 Run ID·port가
  제공된 `TTAKKARI_TEST_DB`와 일치함을 확인했다. loopback port 59455,
  `ttk_test` 역할, DB 이름 `ttakkari_test`이고 공유 65499 또는 운영 DB가 아니다.
  비밀번호나 URL은 기록하지 않는다. 새 initdb를 실행하지 않았고 소유 클러스터 정리는
  런타임에 맡긴다. 시험은 이 DB에서 직렬 실행했다.
- 검증 worktree는 `back-kb-v3-runtime-check-27ea`이다. 처음에는 27ea 고정,
  후속에는 동일 소유 worktree를 3a6로 명시적으로 전환했다. 디렉터리 이름을 SHA 근거로 쓰지 않는다.
  앱 import는 해당 worktree의 `src`로 고정했다.

## 실행 결과

1. **27ea 관련 회귀**: `tests/test_kb_v3_*`, `unit_tests/test_kb_v3_*`, 접근·백필 순수 시험,
   검색·세대 배포·라우트 inventory·라이브러리 API·관측 시험.
   최초 `1 failed, 401 passed, 1 skipped in 47.03s`, exit 1.
   실패는 오프라인 SQL 생성 자식 `uv`가 허용되지 않은 기본 캐시
   `/Users/winterholic/.cache/uv/sdists-v9/.git`에 접근한 것이다.
2. **실패 항목만 재확인**: `UV_CACHE_DIR`을 검증 worktree의 `.cache/uv`로 설정하고
   `tests/test_kb_v3_schema_migration.py::test_migration_0072_offline_sql_generation`을 실행했다.
   `1 passed in 5.44s`, exit 0. 거부된 기본 캐시는 다시 읽거나 수정하지 않았다.
3. **생략 항목 별도 실행**: 동일 소유 DB를 `KB3_PG_URL`로 전달해
   `test_postgres_roundtrip_resolution_idempotency_and_visibility`를 실행했다.
   `1 passed in 0.48s`, exit 0. 해당 fixture의 스키마 재생성은 이 Run 전용 DB 안에서만 수행했다.
4. **3a6 델타**: 신규 독립 실PG 회귀 2개, 새 순수 회귀, 기존 접근 백필·구조 백필·API/MCP·관측 시험.
   `62 passed in 10.26s`, exit 0.
   현재 관측 rev1 회사 / 늦게 도착한 rev2 개인 출처에서도 회사로 분류함,
   dry-run 무쓰기·apply·2회차 무변경, 실제 충돌 해결 API 왕복 뒤 동일 쌍 재탐지 없음,
   문서 상세 resolved_by/note와 HTTP entity_id 필터를 확인했다.
5. **프론트 기록 응답·상태 시험**: `vitest run src/features/kb/kbLive.test.tsx
   src/features/kb/kbV3Flows.test.tsx src/features/kb/kbRules.test.ts` → `32 passed`, exit 0.
   이것은 기록 응답을 fetch 더블로 재생하는 클라이언트/컴포넌트 검증이며 새 라이브 브라우저 시험은 아니다.

명령 인자와 출력 원문(임시 접속 자격은 가림)은
`review/kb-v3/runtime-27ea-20261026/` 및 `review/kb-v3/runtime-3a6d69a-20261026/`에 있다.
재사용한 회귀와 델타는 겹치므로 개수를 합산해 전체 시험 수라고 쓰지 않는다.

## 남은 실행과 소유자

- 원요구 E2E 10개 및 실 E5 제품 검색 품질: 작업자
  `610327e9-8787-4ca5-b671-2fe2cfadef5a`에 고정 backend 3a6 / front 14d09 기준으로 배정했다.
  이전 프론트 26단계 Chrome/WebKit 보고와 원요구 10개를 같은 것으로 취급하지 않는다.
- 해시 임베딩·가짜 L2 어댑터로 통과한 배선 시험은 실제 의미 검색 품질이나 실 구독 CLI 검증이 아니다.
- 운영 데이터 보존/마이그레이션/백필 실반영, main 병합·push·배포 예약/성공은 아직 이뤄지지 않았다.
- 이전 c111 보고의 350/351 및 잘못된 파일별 개수는 이 기록의 근거가 아니다.
  이번 실행으로 새로 관측한 결과만 판정에 사용한다.
