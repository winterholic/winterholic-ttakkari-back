# 2026-10-15 · 공개 공유 TXT/MD/JSON/CSV 및 owner export

## 목표와 경계

기존 public share core에 사용자 선택 회의록의 네 F1 formatter와 owner `POST /api/exports`를 연결했다. 앞선 NUL 오류 작업은 범위에서 제외했고 다시 실행하거나 수정하지 않았다. 기존 core의 DTO allowlist, 독립 capability, download wrapper, 8 MiB/동시 2개/분당 10회/share, 승인 직전 `final_gate`, 기본 off를 유지했다. SRT/VTT/DOCX/PDF·PMS/WBS/Artifact·public UI·프론트는 구현하지 않았다.

formatter는 `async (MeetingDocumentV1, part: str) -> bytes`만 받으며 DB/경로/토큰/원본을 받지 않는다. TXT/MD는 선택 section만, JSON은 projected DTO만, CSV는 단일 적용 part만 생성한다. CSV는 고정 컬럼, UTF-8 BOM, CRLF, quoting, null/boolean/숫자 보존, 위험 문자열의 apostrophe 방어를 사용한다. owner export는 User 필수, source revision과 part 적용성을 확인하고, public sharing feature flag와 무관하게 개인 export를 허용한다. owner/public filename은 사용자 제목과 분리된 고정 이름이다.

summary-only는 전사/job lookup 없이 DTO를 만든다. 미분석 분석 CSV part는 `analysis_not_generated`, 선택하지 않은 part와 미지원 데이터는 적용 불가 오류를 낸다. 분석 완료된 빈 CSV 목록은 header-only다. JSON/TXT/MD/CSV 출력은 선택되지 않은 섹션과 DTO에 없는 내부 metadata를 재조회하지 않는다.

## RED → GREEN

- `tests/test_meeting_exports.py`를 먼저 추가해 빈 `FORMATTERS`에서 네 형식 등록/동작이 실패하는 것을 관측했다. 첫 수집은 새 모듈이 없어서 import error였고 테스트 import를 기존 delivery registry 기준으로 바꾼 뒤, 새 DB에서 `assert set(FORMATTERS) == {}` 대 네 형식 기대치 불일치 등 기능 부재 실패를 확인했다.
- `/api/exports` owner 통합 테스트는 새 DB에서 인증 없는 POST가 404인 RED를 관측한 뒤 route를 추가했다.
- 구현 후 새 격리 DB `ttk_exports_green2_20261015_luna`에서 `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_exports_green2_20261015_luna uv run pytest tests/test_meeting_exports.py -q` → **12 passed in 0.83s**.
- 새 격리 DB `ttk_owner_export_green_20261015_luna`에서 `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_owner_export_green_20261015_luna uv run pytest tests/test_public_sharing.py::test_owner_export_is_private_authenticated_revision_bound_and_not_feature_flagged -q` → **1 passed in 0.40s**.
- 새 격리 DB `ttk_exports_integration2_20261015_luna`에서 `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_exports_integration2_20261015_luna uv run pytest tests/test_meeting_exports.py tests/test_public_sharing.py -q` → **56 passed in 4.26s**. 실제 ASGI route, headers, 네 다운로드, 미분석/미선택 part, owner 인증/revision, summary-only lookup 부재, existing generation/revoke/expiry/final-gate 회귀를 포함한다.
- fresh DB `ttk_share_exports_full_20261015_luna`에서 `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_share_exports_full_20261015_luna uv run pytest -q` → **757 passed, 2 xfailed, 6 subtests passed in 139.10s**. 앞선 core 기준 740 passed보다 새 formatter/API 테스트 17개가 추가됐다. 남은 xfail은 `tests/test_guard.py`의 기존 shell 경계 두 건이며 export 변경과 무관하다.
- 정적 검사: `UV_CACHE_DIR=/private/tmp/ttk-uv-cache uv run ruff check src/ttakkari/sharing/exports.py src/ttakkari/sharing/delivery.py src/ttakkari/sharing/service.py src/ttakkari/sharing/schemas.py src/ttakkari/sharing/middleware.py src/ttakkari/api/shares.py src/ttakkari/main.py tests/test_meeting_exports.py tests/test_public_sharing.py` → **All checks passed!**
- 마감 검사: `git diff --check` → **exit 0, 출력 없음**. `git diff --cached --stat` → **출력 없음**, stage 변경 없음.

## 격리 실행 실수와 신뢰 가능한 결과

초기 테스트 작성 도중 `ttk_export_red_20261015_luna` DB에 연속 두 pytest 명령을 실행했다(첫 명령 collection import error, 다음 명령 RED 확인). 사용자가 정한 실행마다 새 DB 규칙에 어긋나므로 이 두 실행은 완료 검증 근거로 삼지 않는다. 그 뒤의 GREEN 통합 실행은 각 pytest 실행마다 이름이 다른 새 DB를 만들었고 위 최종 narrow 결과가 당시의 신뢰 가능한 검증 근거다. 아래 최종 통합 검사가 현재 checkout의 추가 확인이다.

## 소유권·상태

공유 backend에는 이 세션 시작 전부터 공통 `main.py`, models/config/auth, orchestrator 및 문서·migration·core 변경이 미커밋으로 공존했다. 본 세션은 `.env.example`, README, account/orchestrator/STT/NUL 수정이나 기존 migration/core 코드를 stage/reset/stash/delete/commit하지 않았다. 본 세션의 코드 변경은 `sharing/exports.py`, `delivery.py`, `service.py`, `schemas.py`, `middleware.py`, `api/shares.py`, `main.py` router 등록, 신규 formatter 테스트와 public sharing 회귀 테스트다. `main.py` 변경도 export router 등록 한 줄이며, 파일의 나머지 diff는 선행 세션 변경이다. 최종 `ruff check`와 `git diff --check` 결과는 마감 검증 후 보충한다.

운영 DB/migration·실회의/녹음/전사·외부 전송·모델 실행·실제 공개 링크 발급·배포/재시작은 수행하지 않았다. `public_sharing_enabled=false` 기본 설정은 유지했다.

## 최종 core + formatter 통합 마감

- 프로젝트 규칙 파일은 상위·프로젝트 경로에서 찾지 못했다. 원격은 `git pull --ff-only` 결과 `Already up to date`; 시작 HEAD는 `12b3a94`였고 기존 타 세션의 7개 선행 커밋과 dirty 변경을 확인했다.
- API 연결 계약을 [docs/api-contract.md](../api-contract.md#회의록-외부-공유와-내보내기)에 추가했다. preview의 `source_revision`, format/part capability, 일회성 fragment 발급, owner 인증 export, revoke/republish, public read/download의 capability header와 응답 직전 최종 인가를 UI 연결 순서와 함께 고정했다. 상세 계약과 ADR에서 formatter 활성/기본 off 상태를 같은 내용으로 갱신했다.
- 처음 지정한 격리 DB `ttk_share_join_20261015_codex`가 없어서 테스트가 DB setup 단계에서 57 errors로 끝났다. 로컬 전용 빈 격리 DB를 생성하고 같은 대상 세트를 다시 실행: `UV_CACHE_DIR=/private/tmp/ttk-uv-cache TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_share_join_20261015_codex uv run pytest tests/test_meeting_exports.py tests/test_public_sharing.py tests/test_public_share_migration.py -q` → **57 passed in 4.32s**. 여기에는 CSV 수식 방어, owner 인증·revision·public off, migration 왕복, final gate 뒤 revoke/expiry/delete/scope/item 변경의 404 및 republish의 409가 포함된다.
- 이 검사와 기존 narrow 결과가 통합 계약 확인 근거다. 이번 마감에서 757개 전체 suite를 반복하지 않았다. 마감 정적 검사와 선택 stage/commit/push 결과는 같은 기록의 마지막 상태로 갱신한다.
