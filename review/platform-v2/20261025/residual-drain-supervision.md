# API 잔여 작업 drain 예외 복구 교정

2026-10-25, GPT Codex root 직접 실행. 시작 revision `86b90aabb14e47e04c17687afc76e15c9e660c68`, integration/platform-v2-execution-all-20261025. 최종 구현 revision은 이 기록을 포함한 커밋으로 식별한다. 운영 적용은 하지 않았다.

## 원인과 범위

`GenerationController.retire()`는 구세대 snapshot의 `drain_ready`가 참이어야 슬롯을 정지한다. `ready()`는 ArtCraft/로그인 inflight가 남으면 거짓이다. 그런데 `coordinate()`는 draining 진입 때만 감시 task를 생성했다. `drain_residual_jobs()`가 drain 예외를 False로 기록하고 반환하면 감시가 끝나고, 같은 draining 상태에서는 다시 생성되지 않았다.

기존 GPT 검증자의 `api_residual=1 / drain_ready=False / watcher_alive=0` 보고를 바탕으로, 현재 후보에서도 예외·False 반환 및 감시 자체 예외의 재시도 부재를 직접 재현했다. 이 문제는 DB/Chrome/프로세스 목록 조회 없이 재현된다. 별도로 rollback 취소 후 감시의 finally가 끝나기 전에 다음 전이로 진행하는 경계도 재현했다.

## 변경

- 상태 전이의 초기화와 매 tick 감시를 분리했다. 종료된 감시는 예외를 회수·기록하고, 실제 inflight가 남으면 다음 tick에서 재시도한다.
- 최초 draining 진입 시각을 monotonic clock으로 보존한다. 재시도의 자연 완료 대기 시간은 `max(0, 최초 상한 - 경과 시간)`이다. 재시도마다 30초/20초가 다시 생기지 않는다.
- timeout 취소 후 False가 반환돼도 실제 inflight가 0이면 감시를 반복 생성하지 않는다.
- rollback과 coordinator 종료는 감시를 취소한 뒤 완료까지 회수한다. 재진입은 새 시간 예산을 받는다. 동시에 두 감시가 남지 않는다.
- `ready()`는 수정하지 않았다. 정리가 영구 실패하면 준비 완료로 속이지 않고 매 tick 오류를 기록·재시도한다. 이 경우 유한 시간 내 퇴역까지 보장하는 것은 아니다.
- 기존 remote lifespan 테스트가 가짜 DB로 실제 profiles 조회를 실행해 만들던 미대기 coroutine 경고는 setup에서 조회 함수를 올바르게 대체해 제거했다. 테스트 기대값은 약화하지 않았다.

## 실행 증거

공통 명령 prefix: `PYTHONPATH=src:. ../back/.venv/bin/pytest -q -p no:cacheprovider`.

1. 구현 전 `unit_tests/test_residual_drain_supervision.py --tb=short`: **6 failed in 1.43s**. 예외/False × 두 서비스 4건, 감시 자체 예외 1건, rollback 취소 회수 1건. [원문](residual-supervision-red.txt).
2. 교정 후 신규 파일 9건 + 기존 remote 파일 15건: **24 passed**. `-W error::RuntimeWarning` 포함. [원문](residual-supervision-green.txt).
3. 실행부·로컬 퇴역·TTS·색인 등 지정 17개 파일: **193 passed in 58.43s**. [전체 명령과 원문](residual-supervision-regression.txt). 전체 repository gate 재실행은 아니며 기존 49건 실패 분류를 갱신하지 않는다.
4. 실제 `artcraft.drain` / `LoginManager.drain`에 첫 호출만 RuntimeError를 넣고 실제 asyncio 작업을 정리했다. 두 경우 모두 첫 retire는 중지하지 않고, 재시도 후 `api_residual=0`과 실제 `GenerationController.retire()`의 이전 슬롯 stop 호출·succeeded 전이를 확인했다. Chrome은 실행하지 않았다.
5. 변이는 subprocess의 메모리 내 module 교체로 검사해 원본 파일을 수정하지 않았다. 재시도 제거 4 failed, 예산 초기화 4 failed, 취소 join 제거 1 failed. [요약](residual-mutations-summary.txt). 모두 pytest exit 1이며 해당 회귀 assertion이 실패했다.
6. 변경 Python 3개 Ruff: `All checks passed!`; `git diff --check` exit 0. dev-python AST pitfall scan `total: 0 finding(s)`. mypy는 이 환경에서 명령이 없어 미실행.

17개 파일 회귀 실행 이후 rollback 테스트에 새 진입의 예산 초기화 assertion만 보강했다. 이 보강을 포함한 두 파일 24건은 다시 실행했다. 구현은 회귀 실행 이후 변경하지 않았다.

## 미완료와 다음 단계

- `6d342dd` 원 작업자 da122f97는 기존 검증 결과에 따라 failed 기록을 유지한다. 이번 root 교정의 별도 작업자 독립검증을 수행했다고 주장하지 않는다.
- FND1 첫 혼합 세대 TTS 전환 손실 해소 설계는 기존 작업자 `2b831ab9-5033-4d57-bed7-270dc2f34df8`가 맡고 있다. 설계를 검토하고 구현·반례 검증으로 이어가야 한다.
- Alembic 전체 왕복 및 DB-backed migration 9건은 미검증. 기존 Codex initdb shmget 권한 거부는 재시도하지 않았다. 승인된 호스트 실행 수단이 필요하다.
- 실제 Chrome 프로세스 트리/프로필 락, 운영 Run·모델 응답·MCP·재개 E2E는 미검증이다.
- main 병합·운영 전환은 보류한다. 전체 원래 요구 P0~P6/FND1/INT 완료를 뜻하지 않는다. 최신 목표와 다음 실행 소유자는 `manage_work`에 보존한다.
