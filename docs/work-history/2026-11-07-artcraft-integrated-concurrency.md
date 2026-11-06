# ArtCraft 업데이트·제작 동시성 통합 (2026-11-07)

## 기준과 범위

- 전용 브랜치/worktree: `back-artcraft-integrated-20261107` / `projects/winterholic-ttakkari/back-artcraft-integrated-20261107`.
- 통합 시작점: fetch로 확인한 `origin/main` = `529022861120e86f80400ffaec53dd3f3f7f81c6`.
- 원본 `back/main`과 독립 검증·제작·업데이트 worktree는 수정하지 않았다. 원본 `back`에는 기존 사용자 변경이 남아 있고, 독립 검증 worktree는 clean이었다.
- 선택한 committed source revisions(순서대로, 중복 없음): `143146c775b3eef0b558a73bc07107a5ce4142ab`, `423a1b597b0734e5246c24f6f64c50ad7808d775`, `dda8563f0ca42d640869f1a0071e24d2ae269ced`, `dfdb5cc11d6fcfe300bfca08958cca57dff60452`(task engine/output 후속), `3ee76dc59f89061a2c07e85b16ebc382ff07b546`, `3cd4ae314f96fbd9182ac2854347092ae5a7f6ce`, `cff09c5c61c4a4ed4a1e94f324cf5e2b28eb76f0`, `2df5d9c8b0bcb957895aeaaa7f80b5ea1ed7ce1a`(재시작 복구 후속).
- 요청된 실행 보강 후보 `8d794115`는 이 저장소 객체 DB에서 `git cat-file -t 8d794115` 결과 `fatal: Not a valid object name 8d794115`; source worktree의 미커밋 변경을 복사하지 않았다. 이 SHA가 요청한 다음 누락본이다.

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
