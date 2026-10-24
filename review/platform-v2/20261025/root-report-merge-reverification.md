# Report 병합 P1 교정 재검증

- 판정: **passed (지정된 병합/claim 범위)**
- 고정 후보: `a371677b7ea273bff07c0242ad8ac80d73928c09`
- 브랜치: `fix/orchestrator-report-merge-queue-20261025`
- 기준: `917ea4b2fbe402efc3932885e3fe22c864057580`
- 이전 실패 SHA: `91fe1300f03b946c5b4868e574a32b9c814c075d`

이번 턴은 같은 프로젝트 worktree에서 실행했고 HEAD는 고정 SHA와 일치했다. 제품 코드, 일반 테스트, conftest, 기대값은 수정하지 않았다. 별도 `/private/tmp/ttakkari-report-race-a371677` detached worktree에 검증 전용 테스트 2개만 추가해 정상 `tests/conftest.py`의 임시 PostgreSQL DB로 경쟁 순서를 재현했다. 해당 검증 테스트 원문은 이 폴더의 `test_report_merge_real_db_race.py`에 보존했다.

## 실행 결과

1. 지정된 단위 테스트
   - 명령: `UV_CACHE_DIR=/Users/winterholic/development/.cache/ttakkari-v2-report-verification-a371677 ./scripts/pytest_slots.py -- uv run pytest -q unit_tests/test_orchestrator_report_merge.py`
   - 출력: `........ [100%]` / `8 passed in 0.63s`
   - 이는 8개 mock/unit 검증이다. 단독으로 실제 DB 직렬화를 증명한다고 보지 않았다.
2. 기존 통합 회귀
   - 명령: `UV_CACHE_DIR=/Users/winterholic/development/.cache/ttakkari-v2-report-verification-a371677 ./scripts/pytest_slots.py -- uv run pytest -q tests/test_orchestrator.py::test_reports_queued_behind_busy_orchestrator_are_merged`
   - 출력: `. [100%]` / `1 passed in 0.97s` / `db reset: 1 selective ...`
   - 정상 통합 conftest가 격리 임시 PostgreSQL을 기동했고, 테스트 reset 통계가 출력됐다.
3. 실제 두 트랜잭션 경쟁 (별도 detached worktree, 격리 PostgreSQL)
   - 명령: `UV_CACHE_DIR=/Users/winterholic/development/.cache/ttakkari-v2-report-verification-a371677 ./scripts/pytest_slots.py -- uv run pytest -q tests/test_report_merge_real_db_race.py`
   - 출력: `.. [100%]` / `db reset: 2 selective ...` / `2 passed in 0.60s`
   - merge가 먼저 부모 세션·대기 report 잠금을 잡고 미커밋 상태일 때 claim을 시작했다. claim의 최초 Run 조회 후에도 claim 완료가 막혀 있음을 확인하고 merge를 commit했다. 그 다음 claim이 유효 token을 반환했고 같은 단일 report에 기존 본문과 새 본문이 모두 보존됐다.
   - claim이 먼저 세션·Run 잠금을 잡고 lease insert 후 commit 직전에 멈춘 동안 enqueue가 완료되지 않음을 확인했다. claim commit 후 enqueue는 기존 report를 수정하지 않고 두 번째 queued report를 만들었고, 최초 prompt가 그대로였다.
   - 두 순서 모두 3초 timeout 안에 끝났고 유효 claim이 반환됐다. 테스트 실행 실패나 무기한 대기는 없었다.
4. 검증 harness 작성 중 첫 시도는 두 케이스 모두 seed 단계에서 Workspace보다 Session을 먼저 INSERT해 `ForeignKeyViolationError`가 났다. 이는 테스트 데이터 준비 오류였고 제품 코드/경쟁 구간 실행 전 실패했다. harness에서 Workspace를 먼저 flush한 뒤 위 명령을 재실행해 2건 통과했다. 이 최초 harness 오류는 제품 실패로 분류하지 않았다.

## 코드/계약 판정

고정 SHA에서 claim과 enqueue는 같은 `ChatSession → queued Run` 잠금 순서를 사용한다. merge 선행에서 report prompt 전체가 보존된 채 claim되고, claim 선행에서 lease된 report는 불변이며 새 보고가 분리 생성되는 것을 실 PostgreSQL 경쟁 실행으로 확인했다. 기존 통합 회귀도 재현 환경에서 통과했다. 추가 lease/fence/recovery 기능은 요구하거나 구현하지 않았다.

## 범위 밖 / 미검증

- Flash가 이전에 언급한 별도 `test_db_create_schema_record_failures_carry_their_stage` baseline 실패는 이번 관련 회귀에서 드러나지 않아 비교하지 않았다. 원인 미확인으로 남긴다.
- “환경 실패 13건은 호스트에서 정상”은 호스트 실행 근거를 확인하지 않았으므로 미확인이다.
- FND 후보 `86d63945` 및 계획 worker `ff3871f9`는 재검증하거나 새 지시하지 않았다.
- work-history `git pull --ff-only`는 이 경로가 git repository가 아니라는 오류가 앞선 확인에서 관측됐다. 이 경로에 다른 방법으로 접근하지 않았다.

## 사이드 이펙트 및 다음 전달

테스트는 `tests/conftest.py`의 격리 PostgreSQL에서만 실행됐다. 테스트 검증용 detached worktree는 이 보고와 fixture를 복사한 뒤 제거한다. uv의 다운로드/빌드 캐시는 사용자가 지정한 workspace 캐시 경로 아래에 생성됐다. 운영 DB, 서비스, 프로세스, GPU/모델, secrets, push, 배포는 건드리지 않았다.

통합기에 전달할 `verification_outcome=passed`이며 적용 범위는 report 병합/claim 직렬화다. FND 최종 독립 검증과 통합/배포는 통합 루트가 이어간다.
