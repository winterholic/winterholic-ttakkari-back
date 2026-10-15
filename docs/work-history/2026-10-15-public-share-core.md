# 2026-10-15 · 공개 공유 핵심 권한·회수 경합

최신 상태: core 기록 이후 [TXT/MD/JSON/CSV 및 owner export 후속](2026-10-15-public-share-exports.md)을 구현했다. 아래 “미구현” 절은 그 시점의 상태를 보존한 이력이다.

범위는 승인 스냅샷과 권한·DB·최종 응답 인가다. engine/model/effort 변경·worker 생성 없이 이어갔다. 먼저 recall → 같은 날짜 설계 작업기록 → ADR0030 → 상세 계약 → 현재 diff/코드 순으로 복원했다. dev-experts(인증/ASGI 경계), TDD, verification-before-completion, working-discipline을 적용했다. 별도 handoff는 만들지 않았다.

## 구현

- 공개 strict DTO를 allowlist로 조립하고 선택한 Meeting 컬럼·분석 item만 읽는다. 원음/Artifact/부모·파생·프로젝트·Run·세션·proposal/private metadata를 펼치지 않는다. summary-only는 전사·job을 읽지 않는다. local 전사는 기존 adopted_job 규칙과 원래 발화 ID·job별 이름을 보존한다. people/evidence opt-in, 내부 서명 링크 게시 거절, snapshot 4MiB/발화10000/분석항목5000 한도를 검사한다.
- PublicShare와 migration0028, active root partial unique index, SHA-256 capability hash, RR preview/게시 후보 hash, owner source revision/publication CAS, idempotent revoke, 기본 off를 추가했다. 기존 owner API는 별도 capability를 받아들이지 않으며 api JWT도 owner subject를 확인한다.
- migration의 6개 source trigger가 삭제·scope·선택 binding의 부모/kind/job status 변경을 같은 DB 트랜잭션에서 비가역 revoke하고 hash/snapshot/bindings를 제거한다. 기존 meetings.py나 분석 handler 수정은 필요 없게 했다. gate는 trigger 누락과 독립적으로 존재·부모·scope를 검사한다.
- 공개 GET/HEAD 및 후속 download wrapper는 승인 DTO만 사용한다. ASGI Response가 생성 완료 후 root→선택 binding→share FOR SHARE를 짧게 잡고 DB clock_timestamp로 만료/revision/권한을 다시 확인한다. 생성 중 회수/삭제/scope 변경404, 재게시409이며 생성 바이트를 버린다. 인가 뒤 이미 시작한 전송은 완료될 수 있다.
- 모든 share 응답의 보안 헤더, 정확한 CORS HEAD/Retry-After 추가, raw public Uvicorn access log 차단과 template/status/request ID 로그, allowlist audit, bounded rate bucket·동시 생성2개·최대8MiB를 구현했다. maintenance 실패는 본문/SQL/비밀 없이 경고하며 gate를 통과시키지 않는다.

## 후속 통합 계약과 미구현 (core 완료 시점 상태)

core 완료 당시 `sharing/delivery.py::FORMATTERS`는 빈 상태였다. 그 시점에는 실제 formatter·owner `/api/exports`·public UI가 미구현이고 download 형식도 모두 비활성이었다. 이후 TXT/MD/JSON/CSV 및 owner export/public generation download가 구현되었으며, 현재 UI와 SRT/VTT/DOCX/PDF는 여전히 미구현이다. 당시 테스트에서는 합성 async formatter만 등록했다.

후속 formatter는 `async (MeetingDocumentV1, part: str) -> bytes`만 구현한다. 공통 wrapper가 승인 DTO deep copy와 검증한 part를 주며 DB·원본·capability·파일 ticket은 넘기지 않는다. formatter가 자체 Response/경로/공개 URL을 반환하지 않도록 하고 `AuthorizedResponse` 최종 gate를 유지한다. 형식 권한403과 선택 데이터 부적합409(미분석은 analysis_not_generated)를 구분한다. 형식별 검증 뒤 등록하고도 public UI 준비 전 공개 기능은 켜지 않는다. PMS/WBS/Artifact는 미지원 상태다.

## RED와 검증

- 최초 `/api/shares/preview` 404 → 기대200 실패를 실제 관측. 핵심 초기19개 RED 후 구현했다.
- 추가 RED: 품질 차단 metadata를200으로 수용하던 경우, 손상 snapshot의 잘못된404 분류, part enum의 잘못된403, audit 누락, Uvicorn query sentinel 누출, 향후 CSV 권한/적합성 혼동, off 반복 요청429. 해당 반례 뒤 좁은 수정으로 GREEN을 확인했다.
- 실제 별도 PostgreSQL 트랜잭션과 `pg_stat_activity.wait_event_type='Lock'`으로 FOR SHARE 대기를 관측했다. 잠금 대기 중 scope 변경·만료 커밋, transaction now()가 고정된 상태에서 실제 만료시각을 넘기는 경우도 바이트 차단을 검사했다. 인가 후 revoke 커밋과 이미 인가한 전송의 완료도 별도 확인했다.
- 격리 migration DB `ttk_share_migration_20261015_3c660b8b`에서 `alembic upgrade head → downgrade 0027 → upgrade head`:0028, trigger6개, active root unique index 존재. downgrade 때 table/function/trigger 제거 확인. pytest에도 실제 transactional DDL upgrade/downgrade/upgrade 회귀를 남겼다.
- 관련 기존 회귀: `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_share_20261015_3c660b8b uv run pytest tests/test_public_sharing.py tests/test_api_auth.py tests/test_api_artifacts.py tests/test_meetings.py tests/test_meeting_stt_ui_api.py tests/test_stt_utterances_api.py tests/test_meeting_retranscribe.py tests/test_transfers.py -q` →155 passed in33.28s (이후 추가 반례는 아래 최종 검사에 포함).
- 검증 실행 실수: 첫 전체 검사 중 같은 테스트 DB로 추가 pytest를 겹쳐 테이블/가상 회의가 충돌했다. 해당 전체 결과 4failed/731passed/2xfailed 및 추가 검사2failed/35passed/1error는 **실행 충돌로 무효**, 제품 결함/통과 근거로 사용하지 않는다. 이후 실행별 별도 DB로 분리했다. 운영 DB는 접근하지 않았다.
- 분리한 전체 중간 검사 `ttk_share_full_20261015_3c660b8b`:738 passed,2xfailed,6subtests in146.76s. 이후 경계 반례·마지막 수정은 아래 최종 코드 검사로 다시 확인한다.

## 변경 소유권·운영 경계

본 작업 신규 파일은 sharing 패키지, meetings/document.py, api/shares.py, api/public_shares.py, migration0028, 공유 테스트2개, 이 기록이다. 공통 파일에는 config의 off/origin 설정, models의 PublicShare/text import, main router/middleware/cleanup/CORS, auth subject 검사만 추가했다. ADR0030과 공유 계약의 현재 상태를 갱신했다.

시작부터 타 세션 engine/model/API/문서 변경이 있었고 실행 중에도 concurrency 설정·문서·HEAD가 바뀌었다. 해당 변경을 stash/reset/삭제/임의 commit하지 않았다. 본 변경도 stage/commit하지 않았으며 전체 clean 배포 SHA를 제공하지 않는다. `.env.example`, README, 다른 engine/model/concurrency diff는 본 작업 소유가 아니다.

공개 기능은 기본 off. 실제 사용자 자료의 공유 발급·외부 전송·운영 DB migration/변경·모델 실행·프론트 변경·운영 배포/재시작은 미실행이다. 테스트의 모델/agent 경로는 기존 fake/monkeypatch만 사용한다. 외부 reverse proxy/CDN·프론트 CSP/캐시·실기기 전달은 미검증이다.


## 최종 코드 검증

- 신규 경계·migration: `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_share_boundary_20261015_3c660b8b uv run pytest tests/test_public_share_migration.py tests/test_public_sharing.py -q` → **40 passed in3.63s**.
- 격리 전체: `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_share_final_20261015_3c660b8b uv run pytest -q` → **740 passed,2xfailed,6subtests passed in146.04s**. 본인 원인의 잔여 실패0. 실행 충돌로 무효인 앞선 결과와 구분한다.
- `UV_CACHE_DIR=/private/tmp/ttk-uv-cache uv run ruff check src/ttakkari/sharing src/ttakkari/meetings/document.py src/ttakkari/api/shares.py src/ttakkari/api/public_shares.py src/ttakkari/main.py src/ttakkari/models.py src/ttakkari/config.py src/ttakkari/security/auth.py tests/test_public_sharing.py tests/test_public_share_migration.py alembic/versions/0028_public_shares.py` → **All checks passed!**
- `git diff --check` → 출력 없음/exit0. `git diff --cached --stat` → 출력 없음. 본 변경은 미stage·미commit 상태이며 공통 파일에는 타 세션 diff도 포함된다. 확인 시점 HEAD f89709a93c008a447d19c9410a7ee155ab182073은 본 공유 구현을 담은 clean 배포 SHA가 아니다.
- migration 전용 DB 재확인: `psql -d ttk_share_migration_20261015_3c660b8b -Atc "SELECT version_num FROM alembic_version; SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'public_share_%'"` → `0028` / `6`.

- 기존 xfail2건은 `tests/test_guard.py:105`(변수 확장 민감 경로 읽기), `:124`(일반 Bash 워크스페이스 밖 쓰기)다. 이번 공유 기능의 실패로 숨기거나 해결됐다고 주장하지 않는다.

## 최종 통합 결과

이 기록 이후 TXT/MD/JSON/CSV 및 owner export는 [후속 기록](2026-10-15-public-share-exports.md)에 구현·검증을 남겼다. 그 뒤의 통합 마감에서도 export formatter와 core의 `final_gate`, 회수·만료·삭제·scope/선택 binding·재게시 경합, owner revision/공개 off, migration 0028을 합친 좁은 세트가 새 격리 DB에서 **57 passed in 4.32s**로 통과했다. 이번 기록의 최초 구현 시점에 있던 미stage 상태는 당시 기록이며, 최종 선별 stage/commit/push 결과는 후속 기록의 마감 항목이다.
