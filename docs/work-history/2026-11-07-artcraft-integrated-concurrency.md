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
