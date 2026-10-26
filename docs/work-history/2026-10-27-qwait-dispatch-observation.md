# QWAIT 운영 대기 관측 보강

목적: 적합 계정이 있어도 같은 Run이 queued인 원인을 실제 coordinator에서 관측한다. 재개 정책을 추측으로 바꾸지 않는다.

## 이번 운영 증거

- `read_session(ecc176d9-1bdd-4f04-b6bf-6432477e51ee,last=1)` (07:59 KST):
  Run `8cba5845-d62c-4adb-b2a2-4d0b6e63d586` queued, resume_after=null, lease_owned=false,
  last_event_type=run.waiting. Antigravity Claude Sonnet / verification / strict_engine=true 유지.
- `account_usage` 07:59 응답: 개인 Antigravity Claude 계열 여유 100%인 계정은 실행 0,
  그 관측은 301초 전이다. 다른 두 계정의 Claude pool은 0%. 이것만으로 실행 성공을 보증하지 않는다.
- 이전 관측: 같은 세션에 Run은 하나뿐이고, runtime02430ac6는 local active/3 leases였다.
  target Run 72시간/배포 process 2시간 ops 검색은 0건이었다(오류 부재 증명 아님).
- 현재 API는 메모리 handles/waiting 및 lane 수/상한을 반환하지 않아 정확한 dispatch 정지 지점은 미확정이다.

## 변경과 검증

`runs/diagnostics.py`는 coordinator identity, lane별 lease 수와 설정 상한, 세션 선두 Run을 읽는다.
현재 API가 claim owner일 때만 local 태스크/armed wait/대기 코루틴 이름을 반환한다.
다른 coordinator/remote이면 local 관측은 null이며, “태스크 없음”으로 잘못 판정하지 않는다.
코루틴은 최대 8개 코드 이름(각 80자)뿐이며 소스·경로·인자·지역 변수·진행 인계·예외 repr는 반환하지 않는다.
조회는 claim/dispatch/타이머 해제/라우팅 변경을 하지 않는다.

새 API 검사 두 개는 먼저 `KeyError: execution`으로 2 failed/0.77s를 확인했다.
첫 RED에서 remote 테스트 더미의 http_inflight 누락은 검사 준비 오류라 보강한 뒤 위 RED를 재확인했다.

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q \
  tests/test_agent_scope_auth.py tests/test_worker_quota_recovery.py --tb=short
```

23 passed in 2.07s. 실제 suspended task의 관측·지역 변수 비노출·대기 유지,
legacy/실제 generation owner, stale observer/remote의 미관측, 깊이 상한,
기존 회사/개인 범위 차단과 quota 재개 회귀를 포함한다.
변경 Python 3파일 Ruff `All checks passed!`, git diff --check exit0.
배포 뒤 실제 대상의 local_dispatch를 읽어야 운영 원인을 판정할 수 있다. 재개/완료는 아직 미검증이다.

## P0 운영 검증 진전

이번 `host_operation_status(5be35341-0b7c-4ed0-959b-dd1b59f5c93a)`:
data_snapshot succeeded, exit0, 710.86s, encrypted=true, 파일 97776개/11885835404바이트.
archive SHA256 `6a077efb7a8357d15c12da144e7bf093234b2ca426253ff4e9bb0df5b14fdc96`.
그 manifest의 snapshot_verify `f45fe91c-fbb2-4c35-a921-ba854071ebcb`를 실제 시작했고,
이번 상태 조회에서 succeeded/exit0/236.38s/files_verified=97776/audit 정상이다.
앞선 운영 DB backup/격리 restore123테이블 일치 증거는 재실행하지 않았다.

remote activation·혼합세대·lossless·rollback·실제 모델/MCP/재개·전체 V2/PMS 대조는 미완이다.
보호 로그/키 접근, known Codex setpriority/MachIPC/initdb 거부 재시도, global release_waiting,
strict 해제, 타 기획 worker 변경, 새 중복 Run 생성은 수행하지 않았다.
