# 리버스 엔지니어링 기능 (2026-10-18)

사용자가 화제의 `morluto/rea`를 보고 "나도 해보고 싶다"고 요청했다. 따까리에 저장소 뜯어보기 기능과 페이지를 새로 만들었다. 격리 작업 디렉터리 back-reverse / front-reverse에서 구현했다. 원래 back·front의 다른 작업 변경(미커밋)은 보존한다.

## 설계(ADR 0038)

- rea에서 차용한 것은 증거 우선·한계 명시, 단계별 안내 구조, 읽기만 하고 실행하지 않기다. 바이너리·런타임 분석은 범위 밖이다.
- 파이프라인 6단계: request → collect(shallow clone) → inventory → structure → flows → summarize. 단계마다 evidence·notes를 DB에 저장한다.
- 실행은 백엔드 백그라운드 asyncio 작업 + `reverse_jobs` 테이블이다. 작업 세션(worker) 인프라는 쓰지 않았다. 결정적 정적 검사라 모델 쿼터 없이 끝나고 pytest로 검증할 수 있기 때문이다. 기획 세션은 MCP 3종(`reverse_start`, `reverse_list`, `reverse_get`)으로 대화에서 시작·조회한다.
- 보안 불변: 대상 코드 절대 실행 금지, 비밀 파일은 개수만 세고 이름도 결과에 싣지 않음, https URL만 수집(`git@`·http·file·자격 포함 URL 거부).

## 백엔드 변경(back)

- 신규: `src/ttakkari/reverse.py`(정규화·분석·파이프라인), `src/ttakkari/api/reverse.py`(소유자·개인 세션 라우터), `alembic/versions/0041_reverse_jobs.py`, `docs/adr/0038-reverse-engineer.md`, `tests/test_reverse.py`.
- 수정: `models.py`(ReverseJob), `config.py`(reverse_root), `main.py`(라우터 2개), `orchestrator/mcp_server.py`(도구 3종), `orchestrator/tool_policy.py`(READ_ONLY 2종 + REVERSE_TOOLS 자동 승인), `api/assistant.py`(카탈로그 3종), `tests/test_orchestrator.py`(도구 목록·읽기전용 집합).
- 마이그레이션 체인 단일 헤드 `0041_reverse_jobs` 확인.

## 프론트 변경(front)

- 신규: `src/api/reverseTypes.ts`, `src/pages/ReversePage.tsx`(목록·요청), `src/pages/ReverseDetailPage.tsx`(타임라인·결과), `src/pages/reverse.css`, `src/pages/ReversePage.test.tsx`(4건).
- 수정: `src/api/client.ts`(4 함수), `src/shell/features.ts`(더보기·부가 기능 등록), `src/App.tsx`(라우트 2개), `src/features/markdown/linkTarget.ts`(APP_ROUTE_ROOTS), `src/shell/AppShell.test.tsx`(목록 기대치), `src/pages/design-system.css`(미정의 토큰 1건을 snug로 교체. origin/main에 있던 기존 위반이라 lint 게이트를 위해 같이 고침).

## 검증

- `ruff check src tests alembic`: 내 파일 통과. `deploy_gate.py:30` RUF012 1건은 기존 위반(변경 stash 후에도 동일)으로 손대지 않음.
- 오프라인 검증: 순수 파이프라인·모델 DDL 컴파일·마이그레이션 단위·라우터/MCP/정책 배선을 실행해 확인. 상세는 세션 로그.
- 프론트: `tsc -b` 통과, `vite build` 성공, `eslint . && node scripts/ui-lint.mjs` 통과, `vitest run` **591 passed, 1 skipped**.
- rea 실측: `morluto/rea` rev `3ad1032b` shallow clone(75M) 후 동일 함수로 분석. 0.5초에 파일 4266개·hub `EvidenceMcpServer.ts` 등 확인. 결과 JSON `/tmp/rea-result.json`, 사용자 요약은 outbox `reverse-engineer-rea.md`.
- 미검증(DB 필요): `tests/test_reverse.py` API 종단 2건 + 카탈로그·MCP 계약 테스트. 이 실행 환경 샌드박스가 loopback TCP·유닉스 소켓을 막아(EPERM) Postgres에 닿지 못한다. 실행하면 conftest 세션 픽스처에서 `PermissionError`로 전체가 ERROR가 된다(확인함). 배포 전 운영 파이프라인에서 pytest 전체를 돌려야 한다.

## 배포

- 백엔드 main 병합·푸시 후 `request_backend_deploy`로 전체 SHA 예약. 프론트는 별도 저장소 main 반영으로 배포(기존 방식).
- 주의: canonical `back/`·`front/` 체크아웃에 다른 세션의 미커밋 변경이 있다. 이 작업은 건드리지 않았고, main 갱신은 격리 워크트리에서 fast-forward로만 했다.
