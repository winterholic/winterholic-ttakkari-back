# QWAIT: DB 재개 시각과 메모리 타이머의 불일치

## 현재 운영에서 확인한 정지 지점

2026-10-27 08:35 KST `backend_deploy_status(50d4c86e)`는 78c4a2e7 배포 succeeded였다.
같은 응답의 updated_at은 2026-10-26T23:32:48Z. 공식 deploy log121행은
5853 passed/19 skipped/2 xfailed/5020 subtests passed in1401.77s를 기록했다.
이전 백업·복원·스냅샷 검사는 재실행하지 않았다.

`read_session(ecc176d9-1bdd-4f04-b6bf-6432477e51ee,last=1)`의 23:35:52Z 관측:

- observer와 claim owner 모두 f84f2b63-79aa-480a-bb39-c0efea420f2b.
- local_coordinator=true, remote=false, shutting_down=false, draining=false.
- worker lane leases=2/limit=16, head Run=8cba5845-d62c-4adb-b2a2-4d0b6e63d586.
- 같은 Run은 queued/resume_after=null/lease_owned=false, task_exists=false.
- **메모리 armed_until=2026-10-30T04:45:56Z.** strict_engine=true와 verification 역할 유지.

따라서 현재 dispatch를 막는 것은 lane 부족이나 실행 중 태스크가 아니라 DB에서 해제된 옛 메모리 타이머다.
이 불일치가 운영에서 처음 생긴 정확한 시점은 아직 미확정이다.

## 같은 모양을 만드는 소스 경로와 교정

dispatch는 DB resume_after와 별도로 메모리 타이머를 거절 조건으로 사용했다.
_wait_for_reset은 같은 reset/마지막 waiting 이벤트이면 알림을 중복 제거하면서 **commit도 건너뛰었다**.
DB wait를 해제한 뒤 같은 reset을 다시 기다리면 새 시각은 rollback되고 메모리만 armed가 된다.
resume_queued의 이전 조회 결과가 재평가 후 늦게 _arm하는 경합도 같은 불일치를 만들 수 있다.
두 생성 경로 중 무엇이 위 운영 불일치를 만든 것인지는 추론이며, 운영 관측과 구분한다.

1. dispatch는 **저장된 미래 시각**만 대기 조건으로 삼는다. DB가 null/만료면 옛 메모리 타이머를 해제한다.
2. 반복 알림을 생략해도 새 wait는 commit한 뒤 타이머를 건다. 이벤트·알림 중복 제거는 유지한다.

동일 Run/진행 handoff/strict/개인 범위/독립 검증 역할과 미래 DB backoff를 유지한다.
global release_waiting 호출, 대상 Run 직접 변경, 새 중복 Run 생성은 수행하지 않았다.

## 이번 실행 증거

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q tests/test_worker_quota_recovery.py \
  -k 'committed_wait or repeated_wait_commits' --tb=short
```

변경 전 3 failed/1 passed in0.98s: DB null/만료인데 dispatch=None 두 건,
반복 wait 뒤 saved.resume_after=None 한 건. 음성 대조 미래 DB backoff는 그대로 대기했다.

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q tests/test_worker_quota_recovery.py \
  tests/test_run_dispatch_transaction.py tests/test_run_db_pool_budget.py \
  tests/test_agent_scope_auth.py --tb=short
```

변경 후 49 passed in11.36s. 실제 격리 PostgreSQL을 사용했고, 독립 initdb/known OS 거부는 재시도하지 않았다.
변경 Python2파일 Ruff All checks passed!, git diff --check exit0.
정확 후보의 운영 배포 및 대상 Run의 실제 엔진 재개/완료는 아직 미검증이다.
원 V2 remote 모드 배선/activation/혼합세대/lossless/rollback/실모델MCPresume 및 전체 PMS 대조도 미완이다.
