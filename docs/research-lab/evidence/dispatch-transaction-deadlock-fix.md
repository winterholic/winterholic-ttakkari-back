# 재개 dispatch 트랜잭션과 실행 태스크의 잠금 교착 (2026-10-26)

기준 backend `a4d538ab42022ba0fe6123beae5042e3d46ad1dd`는 main
`a7e95cbf9fe5512fa887cfa6e61cca4751cd7db3`를 포함한다. 이번 변경은
`RunManager.dispatch`가 커밋한 뒤 실행 태스크를 시작하도록 순서를 바꾼다.
연구 코드·마이그레이션·배포 통과 기준은 변경하지 않는다.

## 관측한 배포 실패

지원 `backend_deploy_status(c6bedeba-f8d0-4816-9e6a-ab15b593ce51)` 1회와
`ops_deploy_log` summary/range를 직접 확인했다. release-stage pytest는
`1 failed, 393 passed, 2 skipped in 87.96s`였다(경고 3건 별도).
실패는 `test_restart_resume_uses_saved_engine_session_and_progress`이며
`resume_interrupted(force=True)` → `manager.dispatch` → commit의
`UPDATE runs SET resume_after=...`에서 `DeadlockDetectedError`가 났다.
로그 67~69행은 두 PostgreSQL transaction의 ShareLock 순환 대기를 표시한다.
기존 migration logging 격리·method shadow와 다른 실패다.

## 재현과 원인 경계

이 Run의 Codex 명령 환경에서 주입된 DB URL의 database/user를 단언하고
실제 `SELECT current_database(), current_user`가 `ttakkari_test / ttk_test`임을
확인했다. 접속 URL·비밀번호는 출력하지 않았다.

기존 대상 단독 실행은 `1 passed in 0.58s`였다. 재시도만으로 회복됐다고
판정하지 않았다. 기존 dispatch는 ChatSession을 FOR UPDATE로 잡고,
만료한 Run.resume_after를 변경한 뒤 `start()`로 태스크를 먼저 스케줄하고
마지막에 commit했다. 실행 태스크가 Run 잠금을 먼저 얻고 ChatSession을
기다리면 dispatch는 ChatSession→Run, 실행은 Run→ChatSession으로 교착한다.

신규 `tests/test_run_dispatch_transaction.py`는 엔진 subprocess 대신 좁은
실행 코루틴을 쓰되 두 연결의 실제 PostgreSQL 행 잠금을 유지한다.
dispatch commit 직전에 실행 태스크의 Run 잠금 획득을 Event로 기다려
확률적 타이밍을 고정했다. 이는 실제 배포 프로세스 전체의 재실행은 아니지만
배포 로그와 같은 잠금 순환을 실제 DB에서 재현한다. PostgreSQL 서버 로그나
운영 DB는 읽지 않았다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_run_dispatch_transaction.py -q --timeout=20 --tb=short
# RED: 3 failed in 1.90s
# 실제 PG deadlock 1건; commit 거절에도 start한 normal/desk 각 1건
```

## 최소 교정과 검증

normal/desk 두 분기 모두 `await db.commit()` 다음에 `start()`를 호출한다.
normal은 같은 이벤트 루프에서 commit 복귀와 동기 start 사이에 별도 await가
없으며 start의 `_handles` 중복 방지와 generation 실행의 소유권 claim을 유지한다.
커밋 실패 시 엔진을 시작하지 않고 만료 backoff도 롤백한다. DB 교착 재시도나
sleep을 제품 코드에 추가하지 않았다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_run_dispatch_transaction.py tests/test_api_runs.py::test_restart_resume_uses_saved_engine_session_and_progress -q --timeout=20 --tb=short
# GREEN: 4 passed in 0.82s

PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest tests/test_run_dispatch_transaction.py tests/test_api_runs.py tests/test_run_db_pool_budget.py tests/test_generations.py -q --timeout=30 --tb=short
# 83 passed in 16.62s
```

위 83건에는 새 normal/desk 동시 dispatch 검사가 포함된다. 이후 해당 검사의
`sleep(0)`을 명시적 Event로 바꾸고 desk lane도 직접 확인해 최종 5건을 실행했다.

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest tests/test_run_dispatch_transaction.py -m 'not contract' -q --timeout=20 --tb=short
# 5 passed in 0.73s
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -m contract -q --timeout=30 --tb=short
# 84 passed, 5317 deselected in 7.29s
.venv/bin/ruff check src/ttakkari/runs/manager.py tests/test_run_dispatch_transaction.py
# All checks passed!
git diff --check
# exit0
```

기존 Research/UI/B/C/D/runner 인수는 해당 소스가 그대로여서 유효 범위를
재사용한다. 이번 검증은 실제 LLM/TTS/Metal·운영 MCP·프론트 배포를 보장하지
않는다. 전체 회귀와 배포 활성은 필수 controller gate에서 별도로 확인한다.
알려진 Codex process/sysctl/preexec 거부를 반복하지 않고, 저장소에 명시된
`TTK_SKIP_GATE=1`로 로컬 pre-push 피드백만 생략한다. controller의 전체 테스트,
schema/readiness/ownership/drain 안전장치는 유지한다. 새 예약을 수락한 것만으로
배포 성공이라 하지 않으며 예약 후 대기 폴링하지 않는다.
