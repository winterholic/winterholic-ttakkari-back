# Full gate — platform V2 execution integration

- 측정일: 2026-10-25
- 후보: `8503c4f862e93a8b099fcc05a019923410d88f13` (`integration/platform-v2-execution-all-20261025`, remote branch SHA 일치)
- 기준선: `e4c810d6022968be4670564217153d49f81b3020` (`origin/main`, `git ls-remote` 측정 당시 SHA)
- 원본 worktree는 입력 코드로만 사용했다. 이 파일만 요청된 untracked 보고서다. 제품 코드·테스트 코드 수정, push, main 반영, 배포, 운영 DB/프록시 조작은 없었다.

## 결과 요약

| 트리 | passed | failed | skipped | xfailed | subtests passed | 총 실행시간 |
|---|---:|---:|---:|---:|---:|---:|
| 후보 `8503c4f` | 4,692 | 71 | 19 | 2 | 5,020 | 889.97s |
| `origin/main` `e4c810d` | 4,523 | 65 | 19 | 2 | 5,020 | 855.26s |

명령 (각 worktree root):
```sh
PYTHONPATH=src:. /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest -q -p no:cacheprovider unit_tests/ tests/
```
후보와 main 모두 `tests/conftest.py`의 프로세스 전용 임시 PostgreSQL 경로로 DB 통합 테스트를 실행했다. 아래 분류는 정확한 실패 ID 집합 차이와 별도 격리 재실행을 반영한다.

## 실패 분류

### (a) 후보에서만 실패 — 4건

네 테스트는 동일 후보에서 단독 재실행해도 실패했고, main에서 같은 네 테스트는 `4 passed in 1.29s`였다. 공통 원인은 후보의 readiness 단계가 새 `ensure_executor()` 메서드를 호출하지만 테스트 `FakeSlots`가 이를 구현하지 않아, 예상했던 이후 단계 대신 rollback으로 빠지는 계약 불일치다. 정확한 실패 소유 테스트 파일은 `unit_tests/test_generation_hardening.py`; 호출 지점은 `src/ttakkari/generation_deployment.py:1272-1275` (`ensure_executor` 1274행)이다.

| 실패 테스트 ID | 첫 오류 줄 |
|---|---|
| `test_mismatched_recorded_release_is_actually_reprepared` | `AssertionError: assert 'rolling_back' == 'draining'` |
| `test_record_active_failure_is_a_record_stage_error` | `AssertionError: assert 'readiness' == 'record'` |
| `test_slow_prepare_does_not_block_another_deployment` | `AssertionError: assert 'rolling_back' == 'draining'` |
| `test_unreadable_contract_falls_through_to_prepare_in_one_pass` | `AssertionError: assert 'rolling_back' == 'draining'` |

### (b) 양쪽 실패 — 공통 65건

두 트리의 실패 ID 교집합은 65건이며, main에서만 실패한 ID는 0건이다. 확인된 공통 원인별 요약:

- **worktree Python 경로 환경 — 25건:** backup/restore 16건과 migration 자식 프로세스 9건. 대표 오류는 `프로젝트 Python 환경 없음: <worktree>/.venv/bin/python` 및 `FileNotFoundError: <worktree>/.venv/bin/alembic`. 실행 worktree에 `.venv`가 없는 환경 때문에 자식 CLI가 시작되지 않았다. 후보의 0069 단독 마이그레이션 테스트는 동일 공유 venv를 가리키는 임시 `.venv` 링크를 준 뒤 `1 passed in 2.24s`로 확인했다. 나머지 24개는 링크 제공 후 재검증하지 않았다.
- **호스트 프로세스/감독자 경계 — 39건:** `tests/test_host_gpu.py` 13, `unit_tests/test_host_resource_process_state.py` 1, `unit_tests/test_host_resource_recovery.py` 12, `unit_tests/test_host_resource_recovery_hardening.py` 13. 관측 오류 예: `process table ... enumeration was incomplete`, `owned server stop is not confirmed (observed unknown)`, `timed out waiting for ready`. Codex 실행환경은 `process_list`가 차단된 측정 환경이다. 환경 의존으로 분류하며 호스트 전체 불능으로 일반화하지 않는다.
- **양쪽 동일 assertion — 1건:** `tests/test_account_selection.py::test_choose_prefers_idle_account_then_priority`; 기대 계정 선택 결과와 실제 UUID map이 다르다. 양쪽 모두 재현됐으나 이번 측정에서 환경 요인은 확인되지 않았다.

공통 실패 ID 전체:

#### `tests/test_account_selection.py` (1)

- `test_choose_prefers_idle_account_then_priority`

#### `tests/test_game_migration.py` (3)

- `test_0048_adds_only_nullable_columns_and_downgrades_cleanly`
- `test_game_migration_is_additive_and_downgrades_cleanly`
- `test_game_tables_enforce_uniqueness_and_cascade`

#### `tests/test_google_account.py` (2)

- `test_migration_copies_notebooklm_account_state_without_touching_the_old_table`
- `test_migration_is_idempotent_on_rerun_of_the_copy`

#### `tests/test_host_gpu.py` (13)

- `test_instance_stops_at_the_max_runtime`
- `test_only_one_supervisor_can_own_an_instance`
- `test_server_exit_is_recorded`
- `test_server_that_never_gets_healthy_is_stopped_at_the_start_timeout`
- `test_sigkilled_supervisor_leaves_no_unguarded_server`
- `test_start_status_bench_stop_through_a_detached_supervisor`
- `test_state_file_is_private_and_holds_no_secrets`
- `test_status_reveals_total_and_per_slot_context`
- `test_supervisor_refuses_to_run_the_server_without_a_working_watchdog`
- `test_supervisor_runs_until_stopped_and_cleans_up`
- `test_unconfirmed_server_stop_keeps_the_lease_until_hostgpu_observes_absence`
- `test_watchdog_exits_quietly_when_the_supervisor_stops_normally`
- `test_watchdog_only_matches_our_server_by_path_alias_port_and_start_time`

#### `tests/test_knowledge_migration.py` (2)

- `test_knowledge_migration_is_additive_and_guards_evidence`
- `test_populated_ledger_cannot_be_destroyed_by_application_rollback`

#### `tests/test_meeting_lineage_migration.py` (1)

- `test_lineage_migration_backfills_valid_full_jobs_and_preserves_sources`

#### `tests/test_retire_clovanote_migration.py` (1)

- `test_retirement_preserves_every_source_row_and_meetings_and_downgrades`

#### `unit_tests/test_backup_ops_remaining.py` (8)

- `test_backup_rejects_group_readable_passphrase_file`
- `test_backup_status_reports_latest`
- `test_backup_writes_latest_status_file`
- `test_data_snapshot_and_verify_roundtrip`
- `test_data_snapshot_encrypted_roundtrip`
- `test_data_snapshot_verify_detects_tamper`
- `test_encrypted_backup_then_rehearsal_row_counts_match`
- `test_encrypted_rehearsal_fails_with_wrong_passphrase`

#### `unit_tests/test_backup_restore.py` (8)

- `test_backup_artifacts_are_private_and_manifest_has_no_credentials`
- `test_backup_failure_leaves_no_success_artifacts`
- `test_backup_then_rehearsal_row_counts_match`
- `test_rehearsal_detects_row_count_mismatch`
- `test_rehearsal_fails_without_backup_artifact`
- `test_rehearsal_refuses_non_isolated_target`
- `test_rehearsal_rejects_remote_database_url_before_database_tools`
- `test_retention_cleanup_failure_does_not_misreport_completed_backup`

#### `unit_tests/test_host_resource_process_state.py` (1)

- `test_watchdog_keeps_guarding_a_server_whose_lookup_is_unknown`

#### `unit_tests/test_host_resource_recovery.py` (12)

- `test_automatic_recovery_refuses_legacy_state_and_force_release_clears_it_after_verification`
- `test_cli_force_release_requires_confirmation_and_reports_refusals`
- `test_force_release_clears_a_live_lease_whose_owner_evidence_is_missing`
- `test_force_release_refuses_while_any_test_server_process_is_alive_and_never_signals`
- `test_orphan_lease_owner_pid_reuse_is_identity_mismatch_and_leaves_that_process_alone`
- `test_orphan_lease_with_another_launchs_stopped_state_is_reclaimed`
- `test_orphan_lease_without_any_state_is_reclaimed_when_owner_is_confirmed_absent`
- `test_pending_spawn_with_a_live_server_is_stopped_then_reclaimed`
- `test_pending_spawn_without_any_server_is_reclaimed`
- `test_recorded_pid_of_an_unrelated_live_process_needs_explicit_acceptance`
- `test_recorded_supervisor_or_owner_that_is_still_alive_cannot_be_forced_even_with_acceptance`
- `test_supervisor_writes_a_durable_state_before_it_spawns_the_server`

#### `unit_tests/test_host_resource_recovery_hardening.py` (13)

- `test_comfy_lease_whose_runtime_pid_was_never_recorded_needs_the_explicit_flag`
- `test_comfy_lease_with_a_confirmed_stopped_runtime_is_released_with_consumer_evidence`
- `test_force_release_succeeds_when_a_setuid_login_started_after_the_lease`
- `test_orphan_lease_is_reclaimed_when_enumeration_is_complete_and_empty`
- `test_orphan_lease_with_another_launchs_state_is_also_checked_by_enumeration`
- `test_orphan_lease_without_state_is_kept_while_an_alias_server_still_runs`
- `test_pending_server_is_found_even_when_the_clock_stepped_back[120.0]`
- `test_pending_server_is_found_even_when_the_clock_stepped_back[250.0]`
- `test_pending_server_is_found_even_when_the_clock_stepped_back[3.0]`
- `test_pending_spawn_recovery_tolerates_a_setuid_login_started_after_the_supervisor`
- `test_setuid_foreign_binary_rule`
- `test_strict_enumeration_skips_unreadable_setuid_binaries_but_not_other_unreadable_processes`
- `test_the_real_process_table_does_not_block_force_release_of_an_old_lease`

### (c) 환경·실행 순서 의존 — 후보 실패 중 2건

- `unit_tests/test_fnd01_foundation.py::test_acc01_tts_drain_e2e_contract`: 전체 실행에서는 첫 오류 `assert False is True`; 동일 후보에서 단독 실행은 `1 passed in 0.24s`. 전체 실행 상태/순서 의존으로 분류하며, 단독 PASS가 전체 실패를 지우지는 않는다.
- `tests/test_game_build_handoff_migration.py::test_0069_adds_only_nullable_columns_and_downgrades_cleanly`: 전체 실행은 `.venv/bin/alembic` 부재로 실패. 임시 `.venv` 링크 후 단독 테스트는 `1 passed in 2.24s`; 코드 실패가 아닌 자식 실행 경로 문제다.

후보 전용 네 generation hardening 실패는 (a)에 포함하고, 위 2건은 (c)로 분리했다. 이 분류 후 후보 실질 통합 gate 실패는 4건이다.

## 마이그레이션 및 배포 계약

| 트리 | `alembic heads` | `generation-contract.json`의 `schema_revision` | `online_migration_from` 끝부분 | 판정 |
|---|---|---|---|---|
| 후보 | `0070_background_consumer_queue` | `0070_background_consumer_queue` | `0067`, `0068`, `0069` 포함 (39개) | 단일 head와 일치 |
| origin/main | `0067_collector_outbox_retry` | `0067_collector_outbox_retry` | `0064`, `0065`, `0066` (36개) | 단일 head와 일치 |

직접 실행 명령: `/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m alembic heads` (각 worktree root).

요청한 전체 DB 왕복(`base → head → origin/main head 0067 downgrade → head 재upgrade`)은 **미검증**이다. 별도 임시 클러스터 생성 단계가 실패해 중단했다. 실패 단계와 관측 오류:
```text
/opt/homebrew/opt/postgresql@17/bin/initdb -D /tmp/ttakkari-test-52015-9c5b509b/data -A trust -E UTF8 --locale=en_US.UTF-8 --no-sync
CalledProcessError: exit status 1. pg_sandbox wrapper가 initdb stderr를 예외 출력에 보존하지 않아 더 구체적인 stderr는 미관측.
```
전체 pytest의 내부 임시 DB fixture 및 후보 `0069` 단독 migration test는 별도로 실행됐지만, 위 실패한 클러스터의 전체 Alembic 왕복을 대신하지 않는다. 운영 DB는 접속하지 않았다.

## 원격 API에 남은 실행 경로 (코드 기준, runtime 실측 아님)

### Remote API에서도 API 프로세스에서 실행됨

- `src/ttakkari/api/artcraft.py:49-60,69-78` → `/remote-check`, `/smoke`, `/apps/{app}/gui`가 `artcraft.start_*`를 부른다. `src/ttakkari/artcraft.py:461`은 메모리 `JOBS` 작업을 `asyncio.create_task(run())`으로 실행한다. remote 역할/모드 가드나 durable executor queue가 코드 경로에 없다. 작업은 프로세스 메모리의 `JOBS`에만 남으며, 재시작 시 기록이 지워진다는 오류 설명은 `src/ttakkari/artcraft.py:465-468`이다.
- `src/ttakkari/api/google_accounts.py:72-75` → `src/ttakkari/google_account/login.py:323`에서 Chrome 로그인 작업을 `asyncio.create_task`로 실행한다. 사용자 대면 로그인 흐름이 API 로컬 수명주기에 남는다.

### Remote API에서 남지만 작업 실행이 아닌 제어/감시

- `src/ttakkari/generation_service.py:378`의 `coordinate(runtime, manager)` task는 remote에서도 기동한다. generation 제어/상태 조정 루프다.
- `src/ttakkari/runs/desk_qwen_service.py:220-229`는 외부/관리 Qwen endpoint 감시 task를 실행한다. remote에서는 `owner=False`여서 llama-server를 직접 띄우지 않고 health watch만 한다.

### Remote에서 실행부로 이관된 경로 / remote가 아닌 legacy 조건

- Games: `src/ttakkari/api/games.py:426-432`; queued 행은 `_spawn_build`가 반환하고, 요청은 `queued_work.delegated()`로 enqueue한다.
- Creative assets: `src/ttakkari/api/creative_v2.py:408-420`; remote는 durable enqueue 후 반환하고 in-process task는 local fallback이다.
- Context indexing: `src/ttakkari/api/assistant.py:467-480`; `deferred()`가 remote에서 request row만 남기고 direct `create_task`는 local fallback이다.
- TTS: `src/ttakkari/tts/jobs.py:66-69,226-254,595-601`; remote API는 `QUEUE_OWNER` row + `NOTIFY`로 넘기고 consumer `kick()`은 반환한다.
- `src/ttakkari/generation_service.py:246-253,379-381`에서 preview/STT/TTS/background consumers와 maintenance tasks는 local일 때만 시작한다. `src/ttakkari/background_consumers.py:30-33`의 remote 판별도 API+remote로 제한한다.
- `src/ttakkari/main.py:167-171`에서 generation runtime 활성 시 위 legacy lifespan을 건너뛴다. 따라서 `main.py:226-240`의 retention/reminder/collector/schedule 등의 상주 task는 legacy runtime 경로이며, remote generation API에서 실행되는 것으로 계산하지 않았다.

## Ruff 전체 결과

| 트리 | 결과 | fixable | 집계 방식 |
|---|---:|---:|---|
| 후보 | 100 errors | 67 | `ruff check .` + 파일별 JSON 결과 합산 |
| origin/main | 100 errors | 66 | 동일 |

명령: `/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check .` (전체 repo, 변경 파일 한정 아님). Ruff 출력 한 번의 100건 표시 상한과 관계없이 `ruff check --show-files .`가 반환한 파일을 파일별 JSON으로 합산해 총 100건을 확인했다. 규칙별 차이는 후보 `F841` 2건 / main 1건, main `RUF012` 1건 / 후보 0건이며, 나머지 rule count는 동일하다. 수정은 하지 않았다.

## `scripts/platform_v2_inventory.py` 대조

- 후보 SHA에서 실행, exit 2 / `coverage_partially_unverified`; 추출 성공·분류기는 collected 항목을 모두 분류했다.
- 수집: routes 580, tools 134, tables 106, migrations 70, configs 232, source files 390 (총 96,928 lines). 독립 대조: migrations/source_files reconciled; routes/tools/tables/configs unknown; mismatch 없음.
- 이 scanner는 `claims_product_inventory=false`; ORM 88/106 tables 기반이며 실제 DB나 런타임 전수성을 입증하지 않는다. 선언 baseline `607f8bda…`의 src tree가 측정 후보와 다르다고 기록(`src_tree_matches_scanned=false`). 따라서 API task 목록은 scanner가 아닌 위 소스 참조 확인으로 대조했다.

## 미검증 / 제한

- 전체 Alembic base→head/down→up 왕복: initdb 실패로 미검증.
- `/remote-check`/ArtCraft/Google login 등 remote API 기능은 코드 경로 조사만 했고, API runtime을 remote mode로 기동해 관측하지 않았다.
- Codex engine에서 process_list 차단된 환경에 한해 host process/supervisor 테스트를 환경 의존으로 분류했다. 이는 Mac Studio host 전체 capability의 부재를 의미하지 않는다.
- Ruff/pytest 결과는 고정 후보 SHA와 측정 당시 origin/main SHA에 한정된다.