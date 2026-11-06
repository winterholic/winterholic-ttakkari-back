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
