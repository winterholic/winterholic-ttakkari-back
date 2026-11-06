# ArtCraft 배포 검사: precise evaluator 미등록 상태 표기 (2026-11-07)

## 실행 신원 및 기준

- 현재 Run: `c04c7904-0f02-4484-80df-d5b74d743bc1`. `/Users/winterholic/bin/ttk get /api/runs/{UUID}` → `status=running`, `engine=codex`, `model=gpt-6-luna`, `effort=high`, `profile=회사 코덱스 ai2 (ai2@hyperez.io)`.
- 지원 `ops_deploy_log(job="12cfd116-c2c4-4bb3-8a8c-87338b0abe6d", mode="summary")`: immutable release `2f14f897332e09e54676392d67eae32193c129f2`, `status=failed`, `stage=pytest`, `pytest -q -x --timeout=120`, 6162 bytes / 78 lines.

```text
cause: FAILED tests/test_collection_mission_stage2.py::test_validated_semantic_axis_rescues_a_lexical_hold_and_is_recorded_everywhere
assert tri["precise_eval"]["state"] == "pending" and tri["precise_eval"]["engine"]["status"] == "unavailable"
observed: state='pending', engine.status='waiting'
tests/test_collection_mission_stage2.py:63: AssertionError
1 failed, 931 passed, 8 skipped, 3 warnings, 6 subtests passed in 139.61s
```

## 환경 증명 및 단독 재현

- 검사 DB URL은 출력하지 않고 `loopback=127.0.0.1`, `database=ttakkari_test`, 명시 `port=58374`를 확인했다. 현재 Run에서 읽기 전용 `SELECT current_database(), current_user, inet_server_port(), current_setting('data_directory')` 결과는 `ttakkari_test / ttk_test / 58374 / /private/tmp/ttakkari-run-test-99596-a3512291/data`였다. 해당 반환 경로 부모의 단일 `run-test-cluster.marker`에서 Run UUID, port, `owner_pid=99596`, folder와 DB user 일치를 확인했다. 광역 `/tmp` 탐색 및 다른 Run DB는 사용하지 않았다.

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collection_mission_stage2.py::test_validated_semantic_axis_rescues_a_lexical_hold_and_is_recorded_everywhere -q -x --timeout=120 -p no:cacheprovider
F
E AssertionError: ('pending' == 'pending' and 'waiting' == 'unavailable')
1 failed in 1.06s
```

실패 상세를 출력한 재현은 `engine={'status': 'waiting', 'detail': 'subscription evaluator runtime was not registered'}`였다. 즉 이 실패는 같은 명령으로 단독 재현됐고 다른 파일 실행 순서나 이전 테스트의 전역 상태 누출이 필요하지 않다.

## 호출 경로와 판정

- `pipeline._admit_precise()`가 항목 admission 시 `precise_eval.view_engine_status()`를 저장한다. 테스트 프로세스에는 evaluator runtime이 등록되지 않아 값은 `unavailable`이다.
- `runner.consume_once()`는 job 처리 직후 `precise_eval.consume_pending_once(limit=1)`을 호출한다. `evaluate_document_guarded()`는 미등록 evaluator에서 `EvaluatorUnavailable("subscription evaluator runtime was not registered")`를 던진다.
- `consume_pending_once()`의 예외 처리 전에는 모든 `EvaluatorUnavailable`를 같은 `status="waiting"`으로 기록했다. 그래서 미등록 엔진의 admission-time `unavailable` 값이 덮였다. 이는 test-order/global-state 원인이 아니라 오류 분류의 제품 결함이다.
- 기존 [collection mission work-history](2026-10-25-collection-missions-progress.md)는 엔진 미연결 시 0 소비와 pending 보존, 그리고 busy/unavailable 호출을 pending 재시도 대기와 구분해 기록한다.
- 당시 reverse integration work-history는 이 호출 경로를 확인한 뒤, 미등록 evaluator 호출 결과는 per-item `waiting`, mission summary는 `unavailable`로 남기는 기존 동작을 기록하고 assertion을 `waiting`으로 바꾸는 별도 해결을 수용했다. 이번 작업 카드는 해당 assertion 완화를 명시적으로 금지했으므로, pending retry와 summary 상태는 유지하면서 미등록/비-ready 엔진의 per-item 상태를 `unavailable`로 보존하도록 좁혀 변경했다. ready 상태의 scope-capacity 대기는 음성 반례에서 계속 `waiting`이다.
- 새 원격 `origin/main=eafb7176fe748110791ad9770f28744f3c71b089`에는 stage2 파일의 process-global runtime 격리 fixture가 추가되어 있었다. 이를 merge에 보존했다. 원격 변경 중 실패 assertion을 `waiting`으로 바꾸는 부분은 작업 카드의 금지 조건과 충돌하므로 수용하지 않았고, 현재 수정이 보장하는 unavailable 기대를 명시했다.

## 수정 및 반례

- `src/ttakkari/collector/precise_eval.py`: `EvaluatorUnavailable` 처리에서 configured engine status가 `ready`일 때만 queue 상태 `waiting`으로 기록하고, readiness 자체가 미충족이면 `unavailable`로 기록한다. 두 경우 모두 `state="pending"` 및 retry 흐름은 유지한다.
- `tests/test_collection_mission_stage2.py`: 미등록 runtime의 정확한 unavailable/detail을 확인하고, `ready` engine에 scoped capacity가 없을 때는 `state=pending`, `engine.status=waiting`, `retry_after`가 유지되는 음성 반례를 추가했다.
- 수정 중 검토한 두 guard 변형은 기각했다. runtime 없는 경우 소비를 조기 종료하면 호출별 주입 evaluator 경합 검사를 깨고, configured status가 ready가 아닐 때 조기 종료하면 settled-result recovery를 막는다. 최종 구현은 consumer/claim/recovery 경로를 건드리지 않고 unavailable 예외의 status만 분류한다.

## 검증 출력

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collection_mission_stage2.py tests/test_collection_missions.py -q -x --timeout=120 -p no:cacheprovider
................................................                         [100%]
db reset: 48 selective (avg 13.1/132 tables), 0 forced full, 0 fell back to full
48 passed in 6.03s

$ PYTHONPATH=src ../back/.venv/bin/pytest unit_tests/test_collection_precise_subscription.py unit_tests/test_collection_mission_empty_result.py unit_tests/test_collection_triage_ml.py -q -x --timeout=120 -p no:cacheprovider
..................................................                       [100%]
50 passed in 1.13s

$ ../back/.venv/bin/ruff check src/ttakkari/collector/precise_eval.py tests/test_collection_mission_stage2.py
All checks passed!

$ git diff --check
[no output; exit 0]
```

관련 테스트는 한 pytest 프로세스에서 직렬 실행했다. 전체 suite 재실행은 하지 않았다.

## Git/evidence

- 코드·테스트 커밋: `88a79c4c` (`fix(collector): preserve unavailable precise engine status`).
- 원격 진행분을 포함한 merge commit: `616f2731`; source `88a79c4c`와 최신 fetch `eafb7176…`를 포함한다. 이 evidence 기록을 포함할 후속 문서 commit은 별도 SHA다.
- 예약 대상 `2f14f897…`은 이전 배포 시도이며, 이번 수정은 독립 검증 전이다. 이 작업에서는 push 및 새 배포 예약을 하지 않았다.

## 후속 원격 통합 및 최종 직렬 검증

- 작업 중 `origin/main`이 `eafb7176…`에서 `491312b8aaf7cec05674dfe90785f4f31d91495b`를 거쳐 `006130ab2c160d2672fc011560cf27d893a60645`로 더 전진했다. 새 변경에는 `precise_eval`의 분류를 다시 무조건 `waiting`으로 되돌리고 그 assertion을 `waiting`으로 완화하는 내용, 앞서 작성한 이 evidence 파일 삭제, 그리고 migration 기준을 `0077_artcraft_account_pin`으로 조정하는 `tests/test_collection_scope_merge.py`가 포함됐다.
- `git merge --no-commit origin/main` 후 source/test 상태는 이 작업의 unavailable 분류, unavailable 기대 및 음성 반례를 보존했고, incoming runtime 격리 fixture와 scope-merge 테스트·통합 문서는 포함했다. 삭제 대상으로 들어온 이 evidence 파일도 보존했다. `origin/main`의 latest full SHA `006130ab…`는 merge에 포함됐다.
- 최종 merge tree 직렬 회귀:

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collection_mission_stage2.py tests/test_collection_missions.py -q -x --timeout=120 -p no:cacheprovider
................................................                         [100%]
db reset: 48 selective (avg 13.1/132 tables), 0 forced full, 0 fell back to full
48 passed in 6.24s

$ PYTHONPATH=src ../back/.venv/bin/pytest unit_tests/test_collection_precise_subscription.py unit_tests/test_collection_mission_empty_result.py unit_tests/test_collection_triage_ml.py -q -x --timeout=120 -p no:cacheprovider
..................................................                       [100%]
50 passed in 1.13s

$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collection_scope_merge.py -q -x --timeout=120 -p no:cacheprovider
.............                                                            [100%]
db reset: 13 selective (avg 4.3/132 tables), 0 forced full, 0 fell back to full
13 passed in 1.16s

$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collection_mission_stage2.py::test_validated_semantic_axis_rescues_a_lexical_hold_and_is_recorded_everywhere tests/test_collection_mission_stage2.py::test_ready_engine_with_unavailable_scope_stays_waiting -q -x --timeout=120 -p no:cacheprovider
..                                                                       [100%]
db reset: 2 selective (avg 7.5/132 tables), 0 forced full, 0 fell back to full
2 passed in 1.07s

$ ../back/.venv/bin/ruff check src/ttakkari/collector/precise_eval.py tests/test_collection_mission_stage2.py
All checks passed!

$ git diff --check
[no output; exit 0]
```

- Final source/test commit remains `88a79c4c…`; cumulative local merge commit is `2f8b5644811521c1e5cc4dbc0b2fcce5bb40dda4`, with latest origin `006130ab…` included. This evidence update creates a separate documentation tip. Both local commits remain unpushed as requested.

## 독립 검증 수용 후 누적 source push 및 예약 (2026-11-07)

- 현재 Run API `/api/runs/36b1a2fb-cb9c-4a08-8ebd-efeb77434a26`: `status=running`, `engine=codex`, `model=gpt-6-luna`, `effort=high`, `profile=회사 코덱스 ai2 (ai2@hyperez.io)`.
- 시작 시 checkout은 `8afea8c6faffcc7f8da44638b1fcd7d7170c9be6`, clean, `origin/main`보다 6 commit ahead였다. `git fetch origin main` 결과 `006130ab2c160d2672fc011560cf27d893a60645`; 이는 이미 HEAD의 조상이어서 `git merge --no-edit origin/main`은 `Already up to date.`였다. 원격 추가 diff/충돌은 없고 독립 감사의 의미 판정이 무효화되지 않았다. ArtCraft account pin·lookup·updater와 catalog 수정 파일은 이 누적 source에서 변경되지 않아 동작과 검증 상태를 보존했다.
- 독립 감사 `2026-11-07-artcraft-precise-status-independent-audit.md`를 수용해 `docs/api-contract.md`의 수집 mission 계약에 item `pending`=재시도 대기열, evaluator 미등록/not-ready=`unavailable`, ready evaluator의 용량 대기=`waiting`, mission summary=현재 전역 readiness를 명시했다. 과거 작업 기록은 덮어쓰지 않았다. 계약 추가 커밋 `f75ba35242deb22d755f54388b0a00c723315f28` (`docs(collector): define precise evaluator status meanings`).
- 테스트 DB 사전 증명: 주입 URL 원문을 출력하지 않고 host `127.0.0.1`, database `ttakkari_test`, port `55022`, user `ttk_test`를 확인했다. read-only `SELECT current_database(), current_user, inet_server_port(), current_setting('data_directory')` 결과도 `ttakkari_test/ttk_test/55022`; 반환 data directory의 정확한 부모 marker에서 현재 Run UUID, port, `owner_pid=99596`이 일치했다. DB reset은 hook이 선택한 현재 Run test DB에서만 수행됐다.
- 첫 정상 `git push origin HEAD:refs/heads/main`은 설치된 pre-push hook을 실행했으나 기본 `/Users/winterholic/.cache/uv`에 대한 `uv sync` 쓰기에서 `Operation not permitted`로 검사 초기화가 실패해 push가 거절됐다. 우회하지 않고 허용된 `/private/tmp/ttakkari-uv-cache-36b1a2fb-cb9c-4a08-8ebd-efeb77434a26`를 지정해 같은 정상 push를 재실행했다. hook 출력은 변경 파일 4개, Ruff 변경 Python 2개 통과, contract 87 통과, 변경 테스트 파일 1개(5개 테스트) 통과, 총 33초, exit 0이었다. DB 출력: contract `87 selective`, 변경 테스트 `5 selective`, forced full/fallback 각각 0.
- 성공 push 출력 `006130ab..f75ba352 HEAD -> main`. 직후 `git ls-remote origin refs/heads/main`과 local `git rev-parse HEAD` 모두 full SHA `f75ba35242deb22d755f54388b0a00c723315f28`; `git status --short --branch`는 clean이며 ahead/behind가 없었다. `git diff --check HEAD^ HEAD`도 출력 없이 exit 0.
- `request_backend_deploy(revision="f75ba35242deb22d755f54388b0a00c723315f28")` receipt: id `c9babac4-8435-4069-876e-da7c0322cb3d`, `status=requested`, `skip_tests=false`, `strategy=generations`, `error=null`. 예약 접수만 확인했고 배포 상태 polling은 하지 않았다. 기존 실패 예약 `12cfd116-c2c4-4bb3-8a8c-87338b0abe6d`는 별도 과거 기록으로 유지한다.
- 필수 pre-push 범위와 집중 검사는 통과했다. 전체 release pytest gate는 이 Run에서 실행하지 않았으며, 예약 controller의 독립 검사와 실제 active 확인, 운영 제작, PhotoCraft 적용 및 복구가 다음 운영 단계다. 이 문서 갱신은 source SHA 뒤의 별도 evidence-only 커밋으로 남긴다.

## Collector hold-audit DB session factory 교정 (2026-11-07)

### Run / checkout / 승인 DB 증명

- Run API: `/Users/winterholic/bin/ttk get /api/runs/e287bee8-642f-4754-8375-0f56417d4969` 출력에서 `status=running`, `engine=codex`, `model=gpt-6-luna`, `effort=high`, `profile=회사 코덱스 ai2 (ai2@hyperez.io)`를 확인했다. 조건과 맞아 작업을 진행했다.
- 시작 상태: `release/artcraft-main-20261107`, HEAD `91239dcd99821c89bfe2ac4795766a092e760a19`, clean, `origin/main`보다 1 commit ahead. `git pull --ff-only` → `Already up to date.`. `origin/main=f75ba35242deb22d755f54388b0a00c723315f28`이며 해당 revision과 수정 전 `src/ttakkari/collector_precise_runtime.py`, `tests/test_collector_manage.py`가 동일했다.
- `TTAKKARI_TEST_DB` present. URL 원문은 출력하지 않았다. read-only 연결의 SQL은 `SELECT current_database(), current_user, inet_server_port(), current_setting('data_directory')`; 결과 `ttakkari_test / ttk_test / 52684 / /tmp/ttakkari-run-test-99596-866ce185/data`.
- SQL 반환 경로의 정확한 부모 `/tmp/ttakkari-run-test-99596-866ce185/run-test-cluster.marker`에서 `run_id=e287bee8-642f-4754-8375-0f56417d4969`, `port=52684`, `owner_pid=99596`, `folder=ttakkari-run-test-99596-866ce185`를 읽었다. Run ID, URL/SQL port, marker folder와 owner PID가 맞고 SQL user `ttk_test`도 URL user와 일치했다. 다른 경로 검색이나 타 Run DB 사용은 없었다.

### Immutable 실패 경계와 원인

- 먼저 unmodified target을 단독 실행했을 때 통과했다. 원인은 test conftest가 `TTAKKARI_KB_L2_ADAPTER=disabled`로 격리하고 target이 실제 collector runtime을 설치하지 않아 해당 composition 경계를 밟지 않았기 때문이다. 그래서 target에서 `collector_precise_runtime.install()`을 명시하고, 이전 전역 runtime은 `monkeypatch`가 복원하도록 했다. 이는 제품 동작을 우회하지 않고 운영 runtime 경계를 테스트에 드러낸다.
- immutable `f75ba352…`의 production source 상태에서 강화한 집중 red:

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collector_manage.py::test_hold_audit_is_a_stable_stratified_sample_per_source_and_reason -q -x --timeout=120 -p no:cacheprovider
F
E TypeError: 'async_sessionmaker' object does not support the asynchronous context manager protocol
src/ttakkari/kb_pipeline/l2_adapters.py:173
1 failed in 1.05s
```

- 경로는 `runner.consume_once → precise_eval.consume_pending_once → evaluate_document_guarded → collector_precise_runtime.availability → _adapter → SubscriptionCliL2Adapter.availability`다. `db.sessionmaker()` 선언은 `async_sessionmaker[AsyncSession]`를 반환한다. runtime이 함수 `sessionmaker` 자체를 adapter에 넘기면 adapter의 `self.session_factory()`가 `async_sessionmaker` 객체를 반환하고 이를 곧장 `async with`에 넣어 실패한다. `call_json()`도 같은 `self.session_factory()` 계약을 쓰므로 두 경로가 영향을 받는다.
- 운영 composition은 `generation_service._collector_control()`과 `_collector_consume()`에서 `collector_precise_runtime.install()`을 호출한다. 따라서 fixture만의 오주입이 아니라 runtime 조립부의 실제 생산 코드 오류다.

### 최소 수정과 집중 검증

- `src/ttakkari/collector_precise_runtime.py`: adapter 생성 시 `sessionmaker` 대신 `sessionmaker()`를 전달한다. 이에 따라 adapter 호출 결과가 실제 `AsyncSession`이고 SQLAlchemy async context protocol을 만족한다. consumer·adapter 내부 쿼리 계약은 변경하지 않았다.
- `tests/test_collector_manage.py`: hold-audit 통합 테스트가 production runtime을 명시 등록하고, 전역 `_runtime` 상태를 테스트 종료 때 복원하게 했다. 새 provider 호출, skip, 테스트 우회는 없다.
- source/test 커밋: `548df81697be1bb4e87e6efb145f68a6fb03ee55` (`fix(collector): pass async session factory to precise runtime`). 이 커밋은 지정 source/test 경로 두 개만 포함한다.

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collector_manage.py::test_hold_audit_is_a_stable_stratified_sample_per_source_and_reason -q -x --timeout=120 -p no:cacheprovider
.                                                                        [100%]
1 passed in 1.05s

$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collector_manage.py -q -x --timeout=120 -p no:cacheprovider
..........                                                               [100%]
10 passed in 2.08s

$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_collection_mission_stage2.py::test_validated_semantic_axis_rescues_a_lexical_hold_and_is_recorded_everywhere tests/test_collection_mission_stage2.py::test_ready_engine_with_unavailable_scope_stays_waiting tests/test_kb_v3_default_activation.py tests/test_kb_v3_subscription_cli_contracts.py -q -x --timeout=120 -p no:cacheprovider
......................                                                   [100%]
22 passed in 4.56s

$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_artcraft_tasks.py::test_account_id_is_persisted_and_used_for_all_artcraft_rounds tests/test_artcraft_tasks.py::test_deleted_pinned_account_does_not_fall_back_to_peer tests/test_artcraft_tasks.py::test_limited_pinned_account_waits_without_account_or_emergency_fallback tests/test_artcraft_tasks.py::test_follow_up_round_gets_previous_outputs_and_sessions_resume tests/test_artcraft_tasks.py::test_update_api_queries_real_db_and_rejects_active_creation_run tests/test_artcraft_tasks.py::test_follow_up_report_lookup_does_not_require_a_file -q -x --timeout=120 -p no:cacheprovider
.......                                                                  [100%]
7 passed in 1.51s

$ ../back/.venv/bin/ruff check src/ttakkari/collector_precise_runtime.py tests/test_collector_manage.py
All checks passed!

$ git diff --check
[no output; exit 0]
```

- 검사는 기존 `../back/.venv`, `PYTHONPATH=src`, cache provider 비활성으로 직렬 실행했다. unavailable evaluator의 `pending/unavailable`과 ready evaluator의 capacity 대기 `pending/waiting` 검사를 그대로 통과했다. ArtCraft pin, lookup, updater 관련 변경 파일은 source diff에 없고, pin·resume·lookup·update 경계 테스트도 통과했다. 전체 release suite는 실행하지 않았다.
- source/test 커밋 직후 local branch는 `origin/main`보다 2 commits ahead였다. work-history는 별도 경로 지정 문서 커밋으로 추가한다. push, provider 실행, 배포 조회/예약, service 변경은 하지 않았다. 배포 전 남은 일은 독립 리뷰와 요구된 release gate이며, 이번 범위에서 이를 실행하거나 배포하지 않았다.

## 누적 ArtCraft + collector 교정 release gate — sandbox 실패로 push 보류 (2026-11-07)

- 실제 Run API `/api/runs/55d30431-5dab-4789-b25c-9683cbae2ab9`: `running`, `codex`, `gpt-6-luna`, `high`, `회사 코덱스 ai2 (ai2@hyperez.io)`. GitHub 계정 목록은 `winterholic` 활성, `daeseon-dev` 비활성이었다. 다른 계정 전환은 하지 않았다.
- checkout `release/artcraft-main-20261107` 시작 HEAD=`2cef9c1ada00c3fe38b00744928b1a6aa22b2299`, clean, `origin/main=f75ba35242deb22d755f54388b0a00c723315f28`, ahead 3. `git fetch origin main` 성공, `git merge --no-edit origin/main` → `Already up to date.`. merge-base 조상 검사 exit 0. source 커밋 `548df81697be1bb4e87e6efb145f68a6fb03ee55`는 포함됐다. 독립 수용 evidence commit `187eee4ed4df655e485498fbfb25bfaae4f2757e` 객체는 보이나 `git merge-base --is-ancestor … HEAD` exit 1이므로 release branch에는 포함되지 않았다. 수용 결과 원문은 지정된 독립 RESULT.md에서 확인했다. source diff는 collector runtime/test 2개 파일이며 총 변경 파일은 해당 두 파일과 이 work-history 문서다.
- ArtCraft 누적 경계는 현재 tree에서 `api/artcraft.py`의 update/rollback/status와 account_id 입력, `artcraft_tasks.py`의 account pin resolve/immutable follow-up, `tests/test_artcraft_tasks.py`의 pin, lookup, updater 테스트로 확인했다. 해당 코드 파일은 `origin/main...HEAD` diff에는 없고 이전 기준점부터 누적된 source다. 수용된 548 evidence의 동일 source focused ArtCraft 경계 테스트 7개 결과를 재사용했다. 확인한 테스트 이름은 `test_account_id_is_persisted_and_used_for_all_artcraft_rounds`, `test_deleted_pinned_account_does_not_fall_back_to_peer`, `test_limited_pinned_account_waits_without_account_or_emergency_fallback`, `test_follow_up_round_gets_previous_outputs_and_sessions_resume`, `test_update_api_queries_real_db_and_rejects_active_creation_run`, `test_follow_up_report_lookup_does_not_require_a_file`다.
- DB URL 원문은 출력하지 않았다. read-only `SELECT current_database(), current_user, inet_server_port(), current_setting('data_directory')`는 `ttakkari_test / ttk_test / 56078 / /tmp/ttakkari-run-test-99596-5471ec7a/data`를 반환했다. URL database/user/port가 SQL identity와 일치했다. 정확한 부모 marker `/tmp/ttakkari-run-test-99596-5471ec7a/run-test-cluster.marker`에서 `run_id=55d30431-5dab-4789-b25c-9683cbae2ab9`, port `56078`, folder `ttakkari-run-test-99596-5471ec7a`, `owner_pid=99596`이 일치했다. 새 DB/initdb, 별도 경로의 DB 탐색은 하지 않았다.
- controller와 같은 검사 순서는 release archive에서 `uv sync --frozen --quiet` 후 `build_security_gate_command`의 보안 묶음, 통과 시 전체 `pytest -q --timeout=120 -m 'not security' -o cache_dir=<job cache>/main --sw`다. archive는 `/private/tmp/ttakkari-run-55d30431-artcraft-release/release`에 만들고 `UV_CACHE_DIR=/private/tmp/ttakkari-run-55d30431-artcraft-release/uv-cache`를 사용했다. `uv sync --frozen --quiet` 출력은 exit 0이었다.
- 실제 보안 gate 명령: `/private/tmp/ttakkari-run-55d30431-artcraft-release/release/.venv/bin/python -m pytest -q -m security -o xfail_strict=false --timeout=120` → `10 failed, 913 passed, 3 skipped, 2 xfailed, 6112 deselected in 103.39s`, exit 1. 실패 10건은 전부 `tests/test_host_operations.py` (inline report/performance baseline, child process timeout/cancel/identity, backup/restore와 cleanup 상태)였다. 예: `test_runner_sigkill_before_child_identity_is_recorded_is_recovered_from_child_pidfile`에서 `PermissionError: [Errno 1] Operation not permitted (originated from sysctl() malloc 1/3)`.
- 원인 조사: 현재 Run Codex probe 명령 `python3 /Users/winterholic/development/projects/winterholic-ttakkari/back/src/ttakkari/runs/env_probe.py --json --challenge …` 후 API 등록된 측정에서 `process_list`와 `sysctl_read`가 `Operation not permitted`였다. 별도 자식 probe `os.setpriority(os.PRIO_PROCESS, 0, 10)`도 `PermissionError: [Errno 1] Operation not permitted`로 실패했다. `src/ttakkari/operations/runner.py::_child_setup`이 해당 `setpriority`를 preexec에서 호출하며, pytest의 여러 child process 예외 및 psutil 프로세스 열거 EPERM과 일치한다. 이 원인은 현재 Codex sandbox에서의 gate 실패를 설명하는 강한 환경 증거이며 production host의 실패라고 단정하지 않는다.
- controller 순서상 security gate가 exit 1로 끝나 전체 비보안 pytest 단계는 실행하지 않았다. 실제 gate는 실패로 남겼고 재실행/우회하지 않았다.
- 보조 검사: `git diff --check` → 출력 없음, exit 0. `../back/.venv/bin/ruff check src/ttakkari/collector_precise_runtime.py tests/test_collector_manage.py` → `All checks passed!`. release archive의 `python -m pytest -q --timeout=120 -m contract` → `87 passed, 6953 deselected in 9.96s`. release archive의 `python -m pytest -q --timeout=120 tests/test_collector_manage.py` → `10 passed in 2.29s` (DB reset: 10 selective, forced full/fallback 0).
- push는 실행하지 않았다. 미실행 명령 `git push origin HEAD:refs/heads/main`; 사유: controller 보안 gate 실패. 따라서 `git ls-remote` 원격 full SHA 비교도 하지 않았고 request_backend_deploy를 호출하지 않았다. 예약 ID/상태 없음. `skip_tests=false` 예약, 독립 controller test, active generation 전환 확인, 운영 제작 시험은 미실행이다. 원 목표 R1–R4 review도 계속 남아 있다.
- source SHA=`548df81697be1bb4e87e6efb145f68a6fb03ee55`; 이 검사에 사용한 source/document checkout SHA=`2cef9c1ada00c3fe38b00744928b1a6aa22b2299` (gate 이후 별도 문서 수정 전). 이 기록은 gate 실패와 보류 이유를 보존하며 push/배포 승인을 의미하지 않는다.

## 수용된 ArtCraft 수정 push 및 controller 배포 예약 (2026-11-07)

- Run API `/api/runs/43980791-25d8-426d-8ceb-a44c95fd830a`: `running`, `engine=codex`, `model=gpt-6-luna`, `effort=high`, `profile=회사 코덱스 ai2 (ai2@hyperez.io)`. GitHub 계정 목록은 `winterholic` active, `daeseon-dev` inactive였다. 계정 변경은 하지 않았다.
- 시작 checkout은 `release/artcraft-main-20261107`, clean, HEAD=`63e17f8485530f959c9fee5399199f70c76401b4`, `origin/main=f75ba35242deb22d755f54388b0a00c723315f28`, ahead 4였다. `git fetch origin main`은 `f75ba352…`를 가져왔고 `git merge --no-edit origin/main`은 `Already up to date.`였다. source `548df81697be1bb4e87e6efb145f68a6fb03ee55`와 pin/lookup/updater 누적 source를 보존했다. 직전 Codex Run의 보안 gate 실패 `10 failed, 913 passed`는 재실행하지 않았고 권한 probe도 반복하지 않았다.
- 현재 Run 주입 DB를 read-only로 확인했다. SQL `SELECT current_database(), current_user, inet_server_port(), current_setting('data_directory')` 결과는 `ttakkari_test / ttk_test / 51097 / /tmp/ttakkari-run-test-99596-c7e72490/data`; database/user/port는 주입 URL identity와 일치했다. 정확한 부모 marker `/tmp/ttakkari-run-test-99596-c7e72490/run-test-cluster.marker`의 `run_id=43980791-25d8-426d-8ceb-a44c95fd830a`, `port=51097`, `folder=ttakkari-run-test-99596-c7e72490`, `owner_pid=99596`이 일치했다. DB URL은 출력하지 않았고 다른 Run DB/new initdb는 사용하지 않았다.
- 정상 push 명령: `env -u TTK_SKIP_GATE UV_CACHE_DIR=/private/tmp/ttakkari-run-43980791-artcraft-push/uv-cache TMPDIR=/private/tmp/ttakkari-run-43980791-artcraft-push git push origin HEAD:refs/heads/main`. 현재 Run 전용 tmp cache를 사용했고 pre-push hook을 그대로 실행했다. hook은 대상 `63e17f848553`, 기준 `f75ba35242de`, 변경 파일 3개를 검사했다.

```text
ruff (변경된 파이썬 2개): 통과
계약 묶음: 87 passed, 6953 deselected; DB reset 87 selective, forced full/fallback 0
바뀐 테스트 파일 1개: 10 passed; DB reset 10 selective, forced full/fallback 0
총 36초; 사전 검증 통과; push exit 0
To https://github.com/winterholic/winterholic-ttakkari-back.git
   f75ba352..63e17f84  HEAD -> main
```

- push 후 `git rev-parse HEAD`와 `git ls-remote origin refs/heads/main`은 모두 full SHA `63e17f8485530f959c9fee5399199f70c76401b4`였다. `git status --short --branch`는 clean/ahead-behind 없음, `git diff --check HEAD^ HEAD`는 출력 없이 exit 0이었다.
- 지원 배포 API 요청 `request_backend_deploy(revision="63e17f8485530f959c9fee5399199f70c76401b4")`의 receipt는 `id=28000cc3-93f6-438e-a469-27646c4b082e`, `revision=63e17f8485530f959c9fee5399199f70c76401b4`, `skip_tests=false`, `status=requested`, `strategy=generations`, `error=null`이었다. 이 요청은 예약 접수다. 상태 polling이나 추가 상태 조회는 하지 않았다.
- 범위 분리: 직전 Codex 로컬 full security gate 실패는 과거 기록 그대로 유지한다. 이번 정상 pre-push 검사는 계약 87건과 변경 collector 테스트 10건만 통과했고 전체 release suite 통과를 뜻하지 않는다. 호스트 controller의 필수 security gate 및 전체 테스트는 예약 controller가 수행할 미완료 단계다. 배포 active 확인과 실제 ArtCraft 제작·업데이트/rollback 운영 시험, R1–R4 review도 남아 있다. provider 호출·서비스 재시작은 하지 않았다.
- source SHA=`548df81697be1bb4e87e6efb145f68a6fb03ee55`; main에 push한 SHA=`63e17f8485530f959c9fee5399199f70c76401b4`. 이 단락은 push/예약 후의 경로 지정 문서 증거이며, 아래 문서 전용 커밋은 제품 변경이 아니다.
