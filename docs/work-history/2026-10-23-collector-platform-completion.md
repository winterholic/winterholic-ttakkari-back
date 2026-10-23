# 범용 수집 플랫폼 완성화 — 2026-10-23

## 요청 범위
기존 S2 작업 범위를 넘어 collector platform 기능 자체를 최대한 완성하고 배포한다. 이전 S2 완료 세션이 쓰던 `back`/`front` 체크아웃을 건드리지 않고 `origin/main` 기준의 `back-collector-complete`, `front-collector-complete` worktree를 사용한다.

## 구현
- S2-06 보류 문서 재처리 API/job/UI를 추가했다. 재처리는 보존된 raw blob을 로컬에서 읽고 네트워크 fetch를 하지 않는다. 요청 및 실행 시점의 현재 store/store_raw 정책과 보존 만료, raw SHA를 검사한다. 같은 item/source/policy/profile/model processor 요청은 재사용한다. 재처리 결과에는 원 item lineage가 남는다.
- S3-07 quality utility model에 source-heldout train/validation/calibration 분할, 독립 출처와 표본 수 부족 시 calibration 미생성, 전용 calibration split Platt 보정, raw logit과 calibrated probability/hash 분리, label review·actor·시각/source policy hash·version/license/policy URL/train permission/text hash manifest를 구현했다. 모델은 계속 shadow-only다.
- WBS/readback, README, runbook, delivery validator를 부분 S3 착수를 반영하도록 갱신했다. PMS CP-S2-06은 review 90, CP-S3-07은 doing 55로 정합시켰다.

## 새 자체 검증
- 실행: `UV_CACHE_DIR=/private/tmp/collector-uv-cache uv run pytest -q unit_tests/test_collector_quality_learning.py tests/test_collector_manage.py::test_reprocess_uses_the_preserved_raw_blob_and_is_idempotent`
- 출력: `7 passed in 0.82s`.
- 실행: `UV_CACHE_DIR=/private/tmp/collector-uv-cache uv run ruff check src/ttakkari/collector/learning.py src/ttakkari/collector/quality_service.py`
- 출력: `All checks passed!`.
- 실행: `python docs/design/collector-platform/check_delivery.py`; 문서/WBS validator는 수정 중 링크 검증에서 실패했으므로 새 history 파일 생성 후 재실행해야 한다.
- S2-06 reprocess test는 다른 scope 404, 동작 멱등, 실행 중 outbound fetch 금지, raw hash와 reprocessed_from lineage를 확인한다.

## 진행 상태와 남은 외부 요건
- 품질 모델 registry의 persisted candidate/champion/rollback, 더 큰 S3 benchmark/holdout/retrieval suite는 구현되지 않았다.
- 실제로 `train` 허가가 확인된 source 5개와 최소 20 human labels, 독립 label audit 및 운영 canary가 없다. 기존 파일럿 `train=unknown`; 이를 만들거나 사람/권리를 추정하지 않았다.
- production UI 로그인, 운영 DB identity, 실제 자동 수집 tick과 최신 deploy 성공은 미확인이다. 이전 backend reservation은 `draining`으로 관찰됐고 그 상태는 성공으로 보고할 수 없다.
- 프론트 로컬 Browser는 격리된 profile에서 `/login`으로 이동했다. 로그인된 collector view는 브라우저 검증되지 않았다.
- 파일럿 권리·robots 원문 확인은 법률 검토가 아니다.

## 다음 단계
1. delivery validator + 변경된 백엔드/프론트 관련 테스트를 통과시킨다.
2. 새 변경을 origin/main 위로 rebase하고 pre-push 훅으로 검증 후 main에 푸시한다.
3. 백엔드 full SHA로 generation deploy를 예약하고 상태(reserved/testing/draining/succeeded)를 구분해 보고한다. 운영 상태는 별도 tools로 확인한다.
4. 완성도를 높이려면 persisted model registry와 S3 benchmark tooling을 이어서 구현하되, human labels/rights/independent review/run evidence가 준비되기 전 champion이나 canary를 승격하지 않는다.

## Full-gate 환경 한계
- 보안 단계: `UV_CACHE_DIR=/private/tmp/collector-uv-cache uv run pytest -q --timeout=120 -m security -o xfail_strict=false` → `595 passed, 2 skipped, 2 xfailed`; route inventory 분류 수정 후 통과.
- 비보안 단계: `UV_CACHE_DIR=/private/tmp/collector-uv-cache uv run pytest -q --timeout=120 -m 'not security' -o cache_dir=/private/tmp/collector-gate-20261023 --sw`는 Codex 실행 환경의 격리 pytest DB를 쓰는 `tests/test_game_migration.py::test_game_migration_is_additive_and_downgrades_cleanly`에서 Alembic 자식 연결이 `password authentication failed for user "winterholic"`로 실패했다. 1,130건이 먼저 통과했으며 배포 gate와 동일하게 해당 시험부터 재개했으나 같은 오류가 반복됐다.
- 테스트 helper들은 `origin/main`과 동일(`git diff origin/main -- tests/test_game_migration.py tests/test_meeting_lineage_migration.py tests/test_retire_clovanote_migration.py tests/pg_sandbox.py tests/conftest.py` 출력 없음). 실행 범위는 Codex nested worker sandbox/test PostgreSQL이며, 호스트나 배포 백엔드의 DB 연결 실패를 뜻하지 않는다. 배포 환경은 미검증. 이 때문에 전체 비보안 gate는 통과로 보고하지 않는다.
- 변경 관련 최종 집중 확인: route inventory + reprocess integration + quality learning `8 passed`; ruff clean; delivery validator PASS. Front collector tests `9 passed`, ESLint/tsc clean, `pnpm build` 성공(기존 큰 chunk advisory만 출력).
