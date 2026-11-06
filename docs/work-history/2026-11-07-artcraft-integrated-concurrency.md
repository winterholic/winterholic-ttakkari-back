# ArtCraft 업데이트·제작 동시성 통합 (2026-11-07)

## 기준과 범위

- 전용 브랜치/worktree: `back-artcraft-integrated-20261107` / `projects/winterholic-ttakkari/back-artcraft-integrated-20261107`.
- 통합 시작점: fetch로 확인한 `origin/main` = `529022861120e86f80400ffaec53dd3f3f7f81c6`.
- 원본 `back/main`과 독립 검증·제작·업데이트 worktree는 수정하지 않았다. 원본 `back`에는 기존 사용자 변경이 남아 있고, 독립 검증 worktree는 clean이었다.
- 선택한 committed source revisions(순서대로, 중복 없음): `143146c775b3eef0b558a73bc07107a5ce4142ab`, `423a1b597b0734e5246c24f6f64c50ad7808d775`, `dda8563f0ca42d640869f1a0071e24d2ae269ced`, `dfdb5cc11d6fcfe300bfca08958cca57dff60452`(task engine/output 후속), `3ee76dc59f89061a2c07e85b16ebc382ff07b546`, `3cd4ae314f96fbd9182ac2854347092ae5a7f6ce`, `cff09c5c61c4a4ed4a1e94f324cf5e2b28eb76f0`, `2df5d9c8b0bcb957895aeaaa7f80b5ea1ed7ce1a`(재시작 복구 후속).
- 정정(2026-11-07): `8d794115`는 Git SHA가 아니라 작업자 ID다. 실행 보강의 실제 소스 커밋은 `d0a1c09297892d5e25810ce9b03bd6a97db89e54`이며, 앞서 `8d794115`를 객체 DB에서 찾지 못해 누락 소스로 적은 기록은 잘못된 식별이었다.

## 구현

- 앱 ID 순으로 잠금을 잡아 교착을 막는다. 프로세스 내부 `threading.Lock`과 세대/프로세스 간 `fcntl.flock`을 함께 사용한다. 따라서 같은 프로세스의 비동기 요청과 재시작이 겹친 프로세스 양쪽에서 등록 임계구역을 공유한다.
- 제작 생성/추가 라운드는 잠금 하에서 영속 updater 상태를 확인하고 DB 작업 및 감사 이벤트를 commit한다. 잠금 충돌 시 `queued/running`을 빠르게 재확인하고, 등록 순간에는 상태가 아직 기록되지 않을 수 있으므로 잠금 대기 중에도 상태를 주기적으로 재검사한다. 업데이트가 끝난 뒤 요청이 뒤늦게 통과하는 것을 막는다.
- 업데이트/롤백 route는 충돌검사, Job queued 등록, 감사 기록과 DB commit까지 앱 잠금을 유지한다. background runner는 동일 잠금을 전체 apply/rollback 수명 동안 잡는다. 프로세스별 `JOBS`에 없는 다른 generation의 작업은 strict shared state로 확인하며, 손상/접근 불가 상태는 fail-closed로 503 처리한다.
- 요청 DB 세션이 있는 API 호출은 runner에서 새로운 `sessionmaker()` 세션을 열어 DB 활성 작업을 재확인하고 닫는다. runner는 요청 세션을 캡처하지 않으며 apply/rollback에도 넘기지 않는다. 내부 직접 호출처럼 요청 DB가 없는 경로는 DB 세션을 만들지 않는다.

## 검증

통합 worktree에서 실행했다. pytest는 격리 테스트용 임시 PostgreSQL sandbox를 `tests/conftest.py`가 구동했다.

- `PYTHONPATH=src:tests ../back/.venv/bin/pytest tests/test_artcraft.py tests/test_artcraft_tasks.py -q -p no:cacheprovider` → `58 passed in 12.36s`; DB reset: `58 selective`, `0 forced full`, `0 fell back to full`.
- `PYTHONPATH=src ../back/.venv/bin/pytest unit_tests/test_artcraft_update.py unit_tests/test_artcraft_tasks.py -q -p no:cacheprovider` → `64 passed in 7.18s`.
- `PYTHONPATH=src:/Users/winterholic/development/projects/winterholic-ttakkari/back-artcraft-independent-20261107/unit_tests ../back/.venv/bin/pytest /Users/winterholic/development/projects/winterholic-ttakkari/back-artcraft-independent-20261107/unit_tests/test_artcraft_update_path_probes.py -q -p no:cacheprovider` → `13 passed in 2.04s`.
- 집중 경쟁 회귀: active creation의 updater 409, creation이 먼저 DB commit한 경합, 실제 API Job→apply→rollback, 업데이트 중 create/add-round 거부 및 다른 앱 허용, foreign-generation 영속 상태 거부, 잠금 취소 회수, OS 프로세스 간 잠금, 상태가 뒤늦게 queued가 되는 반대 순서 재검사 → `4 passed in 2.74s` (API 세션/업데이트 전수) 및 관련 추가 단일 회귀는 전체 통합/단위 실행에 포함.
- 커밋 후 좁은 경쟁 회귀 재실행: 실제 API/DB 두 순서, 업데이트/롤백 Job, 상태 전환 재검사, 취소 회수 및 별도 프로세스 잠금 → `8 passed in 3.08s`; DB reset `8 selective`, 전체 초기화/fallback 없음.
- 실제 API 반대 순서 경쟁: updater route가 DB 검사 잠금을 먼저 잡은 동안 제작 API를 동시 시작, updater 등록 후 제작 409 및 updater Job 성공. 위 8개 회귀에 포함.
- 재시작/중단 회수 경계: `PYTHONPATH=src ../back/.venv/bin/pytest unit_tests/test_artcraft_update.py::test_interrupted_updates_reconciled_on_startup unit_tests/test_artcraft_update.py::test_restart_reconciles_real_interrupted_swap_from_journal -q -p no:cacheprovider` → `2 passed in 0.27s`.
- `git diff --check HEAD^ HEAD` → 출력 없음, 종료 코드 0.
- 독립 source의 전체 `test_artcraft_update.py` 실행은 기존 fake API가 `coordination_locked` 인자를 받지 않는 계약 차이로 `test_update_apply_and_rollback_api_success`에서 실패했고 뒤이어 장기 테스트를 중단했다. 독립 path probes 13개는 통과했다. 독립 테스트 원본은 수정하지 않았다.

## 배포/후속

- push와 배포는 하지 않는다. 독립 검증자 `06378389`가 위 전용 브랜치의 최종 SHA를 대상으로 교정 probe를 실행할 수 있도록 최종 SHA를 작업 보고에 함께 적는다.
- 안전 fetch 시점 이후 `origin/main` 추적 ref가 추가로 전진해 최종 상태에서 브랜치는 `origin/main`보다 2 commit 뒤로 표시된다. 검증 완료한 통합 기준은 fetch 시점의 `529022861120e86f80400ffaec53dd3f3f7f81c6`이며, 이 후속 변경을 검증 없이 재기반하지 않았다.

## 보충 기록 — d0a1c092 실행 최종 입력 통합 (2026-11-07)

- 사용자가 실행 입력 범위를 `dfdb5cc1`에서 `d0a1c09297892d5e25810ce9b03bd6a97db89e54`까지 확장했다. practical의 d0a는 두 파일만 변경하며, 활성 계정 중 `profiles.availability`에서 실제 실행 준비가 된 계정을 순서대로 선택하고 준비 계정이 없으면 `503 no_ready_engine`을 반환한다. `_expects_output_file`에 영어 조회·제작 표현을 추가해 성공한 정보조회에 결과 파일 누락 오류를 표시하지 않는다.
- 통합 worktree HEAD `e8e67c1f`에서 `git cherry-pick --no-commit d0a1c092...`를 실행했다. 적용 충돌은 없었고 staged 파일은 `src/ttakkari/artcraft_tasks.py`, `unit_tests/test_artcraft_tasks.py` 두 개뿐이다. 기존 `api/artcraft.py`/`artcraft.py` 공용 coordination/admission lock과 `add_round` 경쟁 회귀는 보존했다.
- 영향 검사: 준비·영어조회 unit 4개 → `4 passed in 0.42s`; task create/add-round 우선 경합 API 2개 → `2 passed in 2.40s`, DB reset `2 selective`, `0 forced full`, `0 fell back to full`. 두 동일 basename test 모듈을 한 pytest 호출로 수집하려던 첫 명령은 `import file mismatch`로 수집 단계에서 실패해 테스트가 실행되지 않았다. 파일군별 명령으로 분리 실행해 위 결과를 얻었다.
- practical worktree는 확인 시 QA evidence 두 파일(`EVIDENCE.md`, `quality_inspection.json`)만 미커밋 상태였고, 이들은 통합하지 않았다. d0의 독립 검증 worker `55fa65f8-2cb8-4f8d-8717-33d5a9e8ec98`가 검증을 시작했다는 것은 사용자 보고다. 현재 Run에서 `get_worker`와 기재된 세션의 `read_session`은 각각 404라 결과는 미확인으로 남긴다.
- 이전 전체 dfdb 병합은 무관한 alembic/deployment/runtime/test 충돌 때문에 abort했다. 그 시점부터 ArtCraft 실행 로직은 기존 `60bfa705` 통합본에 동등 반영돼 있었고, 이번에는 확정 d0 단일 커밋만 적용했다. push·배포는 이번 요청 범위에서 수행하지 않았다.

## 최신 main 통합·배포 후보 (2026-11-07)

- 새 격리 후보 worktree: `back-artcraft-release-20261107`, branch `release/artcraft-main-20261107`. 최종 통합 소스 커밋 `c11f2d6b294b64648978dcf7f86c69aa490c7def`는 최신 fetch `origin/main=98bea2f253cc8642d884602ebf0da082942f9ea1`을 포함한다. 직전 fetch의 `4d09c79f` 뒤 원격 변경은 research integration 기록 문서 1개뿐이었고, ArtCraft 코드·테스트·계약 파일은 바뀌지 않았다.
- 원격과의 직접 ArtCraft 파일 대조에서 upstream 추가 변경은 없었다. 932d7102 독립 검증 후 ArtCraft 코드 차이는 `src/ttakkari/artcraft_tasks.py`에 한정되며 이는 검증된 d0a1 readiness 및 영어 정보 조회 동작이다. 8d794115는 worker ID, 소스 커밋은 `d0a1c09297892d5e25810ce9b03bd6a97db89e54`로 정정되어 있다. 기존 shared lock/API/updater 코드는 독립 932d7102 검증 이후 변경되지 않았다.
- `docs/artcraft/update-api.md`의 Busy Guard를 앱별 shared lock의 DB commit 및 updater 전체 수명 경계에 맞췄고, 상태 목록에 `failed`를 추가했다. update/rollback 응답이 `{job: ...}` wrapper 없이 `job.public()`을 반환하고 `ArtcraftJob` 필드 형식에 `kind=update`, `mode=apply|rollback`을 쓰는 점도 기록했다. 현재 별도 프론트 `ArtcraftJob` 타입은 update 변형을 표현하지 않으므로 프론트 변경은 owner 범위로 남겼다.
- 후보 영향 회귀:
  - `PYTHONPATH=src ../back/.venv/bin/pytest unit_tests/test_artcraft_tasks.py -q -p no:cacheprovider -k 'resolve_task_engine or english_information_lookup'` → `4 passed, 21 deselected in 0.61s`.
  - `PYTHONPATH=src ../back/.venv/bin/pytest tests/test_artcraft_tasks.py::test_task_runs_end_to_end_with_mcp_note_and_outputs -q -p no:cacheprovider` → `1 passed in 0.90s`; DB reset `1 selective`, `0 forced full`, `0 fell back to full`.
  - 공유 잠금/API 경쟁 6개와 updater 4개는 immutable 932d7102 독립 snapshot에서 각각 `6 passed` 및 `4 passed`; 이후 통합 브랜치 집중 경쟁 회귀는 앞선 기록의 `8 passed` 결과를 재사용했다. 해당 코드 경로는 이번 원격 통합에서 바뀌지 않아 반복하지 않았다.
- 후보 검사: `git diff --check HEAD^ HEAD` 출력 없음, exit 0. worktree는 `c11f2d6b` 시점에 clean이고 `origin/main`보다 ahead 14였다. 이 문서의 후속 증거 commit은 기록만 추가한다.
- 배포 전 상태: push와 backend deploy reservation은 아직 실행하지 않았다. 최종 pushed SHA 및 reservation ID/state는 배포 후 실행 기록에 추가한다.

### 배포 게이트 보완 검증

- 첫 pre-push 시도는 uv 기본 캐시(`/Users/winterholic/.cache/uv`) 쓰기에서 command sandbox `Operation not permitted`로 중단됐다. 허용된 `/private/tmp/ttakkari-uv-cache-453208be`를 지정해 게이트를 다시 실행했다.
- 게이트에서 Ruff 26개 lint 오류와 `tests/test_artcraft_tasks.py`/`unit_tests/test_artcraft_tasks.py` 동일 basename 수집 충돌이 드러났다. ArtCraft 범위에서 자동 import/style 정리와 의도된 기존 예외 처리 lint 명시만 적용했다. 락/업데이터 실행 동작은 바꾸지 않았다.
- 세 pytest 명령을 동시에 돌린 첫 집중 실행에서는 7개 중 1개가 race cleanup의 `/api/runs/{id}/cancel` 뒤 SQLAlchemy `Could not refresh instance 'Run'`로 실패했다. 당시 `TTAKKARI_TEST_DB`가 명시 설정되어 있었고 `tests/conftest.py`는 명시 DB를 사용하며 테스트마다 dirty table reset을 수행한다. 따라서 병렬 프로세스의 reset 경합으로 서로 테스트 데이터를 지운 것이 원인이다. 동일 테스트 단독 재실행은 `1 passed`; 이후 같은 공유 DB 검사는 직렬 실행으로 모두 통과했다.
- 직렬 최종 영향 회귀: 실제 DB/API 경쟁·세션·프로세스 잠금과 추가 라운드 포함 7개 → `7 passed in 3.81s`, reset `7 selective`, `0 forced full`, `0 fell back to full`; updater 4개 → `4 passed in 0.76s`; d0 readiness/영어 조회 unit 4개 → `4 passed, 21 deselected in 0.54s`; fake-engine API 1개 → `1 passed in 1.02s`, reset `1 selective`, `0 forced full`, `0 fell back to full`.
- 최종 게이트는 임시 uv cache와 `PYTEST_ADDOPTS=--import-mode=importlib`를 사용해 재실행한다. 일반 pytest import mode에서 두 동명 테스트 파일을 한 호출에 수집하는 게이트 오류를 피하면서 테스트 파일을 모두 검사하기 위함이다.

### Pre-push 실패 원인 분리·교정 (2026-11-07)

- 현재 Run 확인: `read_session(session_id='ca12109d-f5c6-4a8a-8e25-cbb31785585f', last=1)`의 `execution.head_run_id=a1e3f335-5304-49f7-9f94-9b6d59f8254d`; `/Users/winterholic/bin/ttk get /api/runs/a1e3f335-5304-49f7-9f94-9b6d59f8254d` → `status=running`, `engine=codex`, `profile=회사 코덱스 ai2 (ai2@hyperez.io)`. 이전 Run `24407a0b`는 현재 프로필 근거로 사용하지 않았다.
- 보존 후보 첫 gate: `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-453208be ./scripts/verify_deploy_gate.sh --commit 27507965fec7b30d06fd0149ac1af171363df607 --base 40e88581cc1010287ef2b82e13188e2d6c6e8df9` → Ruff 통과, pytest 수집 오류 `import file mismatch`: `tests/test_artcraft_tasks.py`와 `unit_tests/test_artcraft_tasks.py`가 같은 `test_artcraft_tasks` 모듈명으로 충돌 (`6811 deselected, 1 error`).
- 임시 importlib 조정 `PYTEST_ADDOPTS=--import-mode=importlib`만 추가한 시도는 `ModuleNotFoundError: No module named 'pg_sandbox'`로 conftest 수집 전에 종료됐다.
- `PYTHONPATH=tests PYTEST_ADDOPTS=--import-mode=importlib`까지 넣은 시도는 계약 수집에서 `unit_tests/kb_eval`의 `test_runner`, `test_oracles`를 못 찾아 4 errors가 났고, 변경 테스트에서 18 failed/80 passed가 났다. 첫 실패는 `test_catalog_endpoint_lists_sections_categories_and_workspace`에서 `workspace_id`가 None인 것이었다. 이 실행은 `tests/conftest.py`를 pytest plugin과 top-level `import conftest` 두 번 로드해 서로 다른 임시 `allowed_roots`를 썼다. 제한 출력 진단은 `DIAG_WORKSPACE_ERROR: ArtCraft 작업 폴더를 쓸 수 없습니다: 허용된 상위 경로(allowed_roots) 밖입니다.`였다. 기본 표준 pytest로 같은 단일 테스트는 `1 passed in 0.62s`; 따라서 이전 18개는 제품 실패 증거가 아니라 잘못된 import-mode/PYTHONPATH 환경의 cascade다.
- 이전 18개 실패 목록: `test_catalog_endpoint_lists_sections_categories_and_workspace`; `test_task_runs_end_to_end_with_mcp_note_and_outputs`; `test_task_session_stays_out_of_session_lists`; `test_task_create_validates_inputs`; `test_photo_task_uses_template_apps_and_options`; `test_unconfined_app_gets_no_root_flags_and_all_category_apps_by_default`; `test_follow_up_round_gets_previous_outputs_and_sessions_resume`; `test_follow_up_can_skip_previous_and_attach_a_chosen_output`; `test_failed_run_is_reported_with_error`; `test_list_filters_by_category_state_and_archive`; `test_task_can_be_found_from_its_session_even_when_archived`; `test_active_task_is_listed_as_active`; `test_update_api_queries_real_db_and_rejects_active_creation_run`; `test_create_commit_wins_same_app_update_race`; `test_add_round_commit_wins_same_app_update_race`; `test_update_and_rollback_api_jobs_use_fresh_db_sessions_and_hold_admission_lock`; `test_foreign_generation_persisted_update_blocks_create_and_add_round`; `test_outputs_gallery_filters_and_pages`.
- 기본 importer를 유지하면서 중복을 없애도록 `unit_tests/test_artcraft_tasks.py`를 `unit_tests/test_artcraft_tasks_unit.py`로 rename했다 (100% rename). 이후 표준 mode gate에서 첫 실제 계약 실패는 `test_route_inventory_matches_manifest`: update API GET/POST/rollback/status 4개 route가 `ROUTE_SCOPE`에서 누락됐다. 변경 테스트에서는 `test_create_task_persists_the_resolved_active_engine`가 `app_updating`으로 실패했다.
- persisted 상태 테스트 누수를 직렬 재현: `PYTHONPATH=src:tests:unit_tests ../back/.venv/bin/pytest tests/test_artcraft_tasks.py::test_foreign_generation_persisted_update_blocks_create_and_add_round unit_tests/test_artcraft_tasks_unit.py::test_create_task_persists_the_resolved_active_engine -q -p no:cacheprovider -vv` → `1 passed, 1 failed`; 앞 테스트가 `photocraft=running` 공유 fixture 상태를 지우지 않아 뒤 단위 테스트에 남긴 것이다. 단위 테스트 단독은 `1 passed in 0.34s`.
- 최소 수정: update API 네 경로를 owner 인증 경계대로 `owner-plain`으로 `ROUTE_SCOPE`에 추가했고, `test_foreign_generation_persisted_update_blocks_create_and_add_round`의 생성/추가 라운드 단언을 `try/finally`로 감싸 persisted updates를 정리하게 했다. 제품 source 동작은 변경하지 않았다. 두 수정 커밋: `607d909443d12beae1ae588f2ee130d3403b22f8` (rename), `b56cda9cdb2a6cc5888a2f8f74162d4cbcd428a3` (manifest 및 상태 cleanup).
- 수정 후 표준 gate: `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-453208be ./scripts/verify_deploy_gate.sh --commit b56cda9cdb2a6cc5888a2f8f74162d4cbcd428a3 --base 40e88581cc1010287ef2b82e13188e2d6c6e8df9` → exit 0; `ruff (변경된 파이썬 9개)` 통과, 계약 `85 passed, 6836 deselected in 18.33s`, 변경 테스트 `105 passed in 15.12s` (4개 파일), DB reset `38 selective`, `0 forced full`, `0 fell back to full`. 이는 pre-push 영향 검사이며 전체 suite 통과를 뜻하지 않는다.

### Push hook 재실행과 background consumer 격리 (2026-11-07)

- 최종 기록 commit `111acfc22da6f5461e4ef5b153e1815f4e7f8bca`를 대상으로 normal `git push`를 시도했다. pre-push hook은 Ruff 및 계약 `85 passed, 6836 deselected in 15.39s`를 통과했으나 변경 테스트가 `104 passed, 1 failed`여서 push를 거절했다. 원격 write는 발생하지 않았다.
- 실패: `unit_tests/test_artcraft_update.py::test_start_update_job_actual_path_self_exclusion`, assertion `db_calls[1] is runner_sessions[0] ...` 실패. 동시 background log는 `background consumer failed: reverse`와 `AttributeError: 'RunnerSession' object has no attribute 'execute'`를 남겼다.
- 콜스택 대조: 이 테스트는 `ttakkari.db.sessionmaker`를 monkeypatch하지만, `tests/conftest.py`의 app-lifespan은 background consumer를 실행한다. reverse consumer도 같은 global factory를 읽어 mock `RunnerSession`으로 DB 접근을 시도했고, 그 추가 세션이 runner-session 순서 단언을 깨뜨렸다. 테스트 코드만 실행할 때는 `1 passed in 0.97s`로 재현되지 않아, 공유 test process의 background consumer 교차가 원인임을 확인했다.
- 최소 수정: `test_start_update_job_actual_path_self_exclusion`에서 background consumer claiming을 잠시 멈추고 이미 진행 중인 pump가 끝날 때까지 기다린다. 테스트 teardown은 원래 `sessionmaker`를 복원한 뒤 이전에 paused가 아니었던 경우에만 consumer claiming을 재개한다. 변경은 단위 테스트만이며 제품 source는 바꾸지 않았다.
- 수정 후 좁은 검증: `PYTHONPATH=src:tests:unit_tests ../back/.venv/bin/pytest unit_tests/test_artcraft_update.py::test_start_update_job_actual_path_self_exclusion -q -p no:cacheprovider -vv` → `1 passed in 0.97s`. 최종 candidate에 대한 필수 gate와 실제 push hook은 이 기록 commit 뒤 다시 실행할 예정이다.

### 직렬 격리 수정 후 gate 결과 (2026-11-07)

- 첫 수정 포함 commit `9b0262a8af2fd32e0a5f08b422c918787e735ef6`의 impacted gate는 계약 `85 passed, 6836 deselected in 15.36s`, 변경 테스트 `105 passed in 11.65s`였으나 새 테스트의 `from ttakkari import ...` 결합 import가 Ruff `I001`로 실패(exit 1)했다.
- import를 `from ttakkari import artcraft_tasks, background_consumers`와 `from ttakkari import db as db_module`로 분리했다. 이후 latest fetch `origin/main=40e88581cc1010287ef2b82e13188e2d6c6e8df9`를 포함한 기준으로 `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-453208be ./scripts/verify_deploy_gate.sh --commit 11bb047a894bb3d50cacd2af3b03095e91d037ae --base 40e88581cc1010287ef2b82e13188e2d6c6e8df9` 실행 → exit 0; Ruff 9 Python files 통과, 계약 `85 passed, 6836 deselected in 15.35s`, 변경 테스트 4 files `105 passed in 10.01s`, DB reset `38 selective`, `0 forced full`, `0 fell back to full`.
- 이 검사는 필수 pre-push impacted 범위만 다룬다. 전체 suite는 반복하지 않았다. 이 기록 자체는 문서 변경이며, push hook은 최종 기록 commit SHA에서 다시 실행한다.

### ArtCraft release push 및 backend 예약 (2026-11-07)

- 배포 후보 `55f2a40aefb642f7ac5ceef9f7ca8055fcdd1fd3`, push 직전 fetch한 `origin/main=40e88581cc1010287ef2b82e13188e2d6c6e8df9`; merge-base 동일, ahead 23, worktree clean이었다. 원격은 후보에 이미 포함된 `40e88581` 이후 추가 전진이 없었다.
- 계정별 인증: `github_accounts`는 `winterholic` active/success를 반환했다. `gh auth switch` 없이 per-command credential helper로 `git push origin HEAD:main`을 실행했다.
- 실제 push의 pre-push hook → exit 0: Ruff(변경된 Python 9개) 통과, 계약 `85 passed, 6836 deselected in 15.56s`, 변경 테스트 4개 `105 passed in 10.13s`; DB reset `38 selective`, `0 forced full`, `0 fell back to full`.
- push 결과: `40e88581..55f2a40a HEAD -> main`. 직후 `git fetch origin main`/`git rev-parse HEAD origin/main` → 둘 다 `55f2a40aefb642f7ac5ceef9f7ca8055fcdd1fd3`; branch `release/artcraft-main-20261107...origin/main` clean; `git diff --check origin/main^..HEAD` 출력 없음(exit 0).
- `request_backend_deploy(revision='55f2a40aefb642f7ac5ceef9f7ca8055fcdd1fd3')` → reservation `09a13bbe-b69f-4efa-859f-d6d7101075ea`, `status=requested`, `strategy=generations`, `skip_tests=false`, `error=null`. 이는 접수이며 실제 배포 완료를 뜻하지 않는다. 요청된 대로 deploy 상태는 polling하지 않았다.
- 이 추가 문단을 담는 후속 commit은 work-history만 바꾸며, release source와 예약 대상 SHA는 위 `55f2a40a…` 그대로다.
