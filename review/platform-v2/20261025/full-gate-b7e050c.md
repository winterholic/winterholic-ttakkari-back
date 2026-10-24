# Full gate — platform V2 candidate b7e050c

- 측정일: 2026-10-25
- 판정: **FAIL — 전체 suite에 공통 실패 49건이 남음.** 다만 직전 부모 대비 후보 전용 실패는 0건이고, 부모에서 실패하던 목표 5건은 후보 전체 실행에서 통과했다.
- 후보: `b7e050cef2979cf87be1a3897814945b905319d4` (부모 `141d146a77866dc4fcc66a429f42e540373d1164`)
- 비교 기준: `origin/main` = `e4c810d6022968be4670564217153d49f81b3020`
- 세 트리 모두 별도 detached worktree, 공유 `back/.venv`를 가리키는 `.venv` symlink, 동일 명령을 사용했다:
  `PYTHONPATH=src:. .venv/bin/pytest -q -p no:cacheprovider unit_tests/ tests/`
- 측정 worktree 및 venv symlink는 실행 후 정리했다. 제품·테스트 코드 수정, push, main 반영, 배포, 운영 조작은 하지 않았다.

## 전체 suite

| 트리 | passed | failed | skipped | xfailed | subtests passed | 시간 |
|---|---:|---:|---:|---:|---:|---:|
| 부모 `141d146` | 4,709 | 54 | 19 | 2 | 5,020 | 910.04s |
| 후보 `b7e050c` | 4,715 | 49 | 19 | 2 | 5,020 | 907.01s |
| `origin/main` `e4c810d` | 4,539 | 49 | 19 | 2 | 5,020 | 870.86s |

부모와 후보 실패 ID 집합을 비교하면 부모 전용 5건, 후보 전용 0건, 공통 49건이다. 부모 전용은 아래 목표 테스트 5건이며 후보 전체 실행에서 모두 통과했다.

## 남은 실패 분류

- **후보 전용: 0건.**
- **공통·환경: 48건.**
  - 프로세스 테이블/감독자 경계 39건: `tests/test_host_gpu.py` 13, `unit_tests/test_host_resource_process_state.py` 1, `unit_tests/test_host_resource_recovery.py` 12, `unit_tests/test_host_resource_recovery_hardening.py` 13. 실행 환경에서 관측된 오류는 process table enumeration incomplete / owned process 상태 unknown이다. Codex sandbox의 process-list 제한 범위로 분류하며 호스트 전체 기능 부재를 뜻하지 않는다.
  - migration subprocess 9건: game 3, Google account 2, knowledge 2, meeting lineage 1, ClovaNote retirement 1. 실행 시 주입된 test DB는 인증된 loopback test DB였고, migration helper의 child URL 생성은 username/password 없이 localhost와 PGPORT만 넣는다(`tests/pg_sandbox.py:591-595`). 이 인증 경로 불일치가 원인이라는 **추론**이며, 자식 Alembic의 실제 stderr는 pytest 요약에 보존되지 않아 정확 원인은 미확인이다.
- **공통 assertion: 1건.** `tests/test_account_selection.py::test_choose_prefers_idle_account_then_priority`; 부모·후보·main에서 동일하게 실패했다. 이번 측정으로 환경 원인은 확인되지 않았다.

동일 링크 조건의 후보와 main은 실패 ID 49건이 완전히 일치했다. 따라서 남은 실패 중 후보에서만 새로 발생한 건은 없다.

## 목표 테스트와 diff 판단

부모 `141d146` 전체 실행에서 아래 다섯 건이 실패했고, 후보 `b7e050c` 전체 실행에서는 모두 통과했다.

- generation hardening 4건: `test_mismatched_recorded_release_is_actually_reprepared`, `test_record_active_failure_is_a_record_stage_error`, `test_slow_prepare_does_not_block_another_deployment`, `test_unreadable_contract_falls_through_to_prepare_in_one_pass`
- `unit_tests/test_fnd01_foundation.py::test_acc01_tts_drain_e2e_contract`: 부모 전체 실행 실패, 후보 전체 실행 통과
- 후보에서 위 5건과 새 rollback 회귀 테스트를 따로 실행: **6 passed in 3.64s**

`141d146..b7e050c` diff는 요청대로 테스트 파일 4개뿐이다. 기존 단계 기대값을 바꾸거나 기존 assert를 삭제한 변경은 없다. `FakeSlots.ensure_executor`와 실패 주입을 추가하고, DB 단계 테스트를 fake asyncpg 연결로 격리했으며, 새 rollback 테스트가 readiness 오류·이전 active/route 복귀를 확인한다.

## TTS 전역 상태 — 제품 결함 판단

`executor.serve()` 종료 정리(`src/ttakkari/executor.py:259-270`)는 `tts_jobs.drain()`을 기다리고 `request_stop()`을 호출한다. 이 경로 뒤에는 `tts_jobs.stop()`이 호출되지 않아 `_draining`이 True인 채 남는다(`src/ttakkari/tts/jobs.py:625-639`). 이는 프로세스 전역 상태 누수이며 해당 테스트들의 격리 정리는 유효하다.

다만 같은 프로세스 재시작 시 `start_background_services()`가 `tts_jobs.start()`를 호출하고(`src/ttakkari/executor.py:181-191`), local TTS가 활성화된 경우 `start()`가 consumer를 kick하기 전에 `_draining=False`로 되돌린다(`src/ttakkari/tts/jobs.py:604-622`). 종료된 consumer는 재시작 시 재생성된다. 따라서 요청에 든 “같은 프로세스 재시작 뒤 접수 차단”은 코드 경로상 재현되지 않으며 **제품 기능 결함으로 확정하지 않았다**. shutdown 후 다음 start 전 상태가 남는 점은 관측했지만 그 사이 실사용 접수 경로는 확인되지 않았다.

## disabled executor 호출 — 롤백 위험 판단

`GenerationController.process()`는 readiness에서 설정과 무관하게 `ensure_executor()`를 호출한다(`src/ttakkari/generation_deployment.py:1272-1275`). 그러나 `ensure_executor()`는 계약의 `run_executor.activation`이 enabled가 아니면 즉시 `{ready: false, activation: disabled}`를 반환한다(`src/ttakkari/generation_deployment.py:441-445`). 유효한 disabled 계약은 프로세스 중지·부트스트랩 경로로 진행하지 않으므로 호출 자체가 rollback을 유발하지 않는다. 계약 로딩 자체가 실패하는 잘못된 릴리스는 이 판단 범위 밖이다.

## Alembic 왕복

**미검증.** 사용자 요청에 따라 직접 initdb를 한 번 실행했지만 Codex command sandbox에서 실패했다. 정확한 명령과 관측 오류:

```text
/opt/homebrew/opt/postgresql@17/bin/initdb -D /private/tmp/platform-v2-pg.mghgN4/data -A trust -E UTF8 --locale=en_US.UTF-8 --no-sync
FATAL: could not create shared memory segment: Operation not permitted
DETAIL: Failed system call was shmget(key=358996456, size=56, 03600).
child process exited with exit code 1
initdb: removing data directory "/private/tmp/platform-v2-pg.mghgN4/data"
```

이 단계에서 중단되어 base→0070 업그레이드, 0067 다운그레이드, 재업그레이드는 실행하지 않았다. 위 sandbox 오류를 호스트 전체 PostgreSQL 불가로 일반화하지 않는다.

## Ruff / diff check 및 이전 수치

- 후보 변경 파일 4개 `ruff check`: **All checks passed!**
- `git diff --check 141d146 b7e050c`: exit 0, 출력 없음.

이전 문서 `full-gate-8503c4f.md`의 4,692/71 및 main 4,523/65는 이번 결과와 실행 조건이 다르다(당시 임시 worktree에 `.venv` 없음). 또한 해당 문서의 후보 `8503c4f` 부모는 `a8fc8fd`로, 이번 부모 `141d146`와 다르다. 이번 비교 표는 이 차이를 제거하기 위해 부모·후보·main을 공유 venv 링크 조건에서 다시 측정한 값이다.

