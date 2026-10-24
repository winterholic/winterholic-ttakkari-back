# FND1 프로토타입 검수와 부분 교정

2026-10-25 root GPT Sol, Codex command sandbox. 통합 기준 `3a49526e52a881953e90b5bcae2a5ced2e781af4`, 검수 대상 프로토타입 `e522187b454c6bdbcf5cc8f4748facb0410560e1`. FND1 전체는 미해결이며 main 병합·배포는 하지 않았다.

## 판정: 설계 미채택

`Caddy upstream num_requests=0 → API http_inflight=0 → DB queued/running=0`이라는 순차 관측은, 첫 관측 전에 업스트림 카운터에 진입하지 않은 구 라우트 요청을 제외한다. 그 요청은 나중에 API의 active 검사를 통과하고, 전환 뒤 commit하여 202를 받은 뒤 구 guard에서 failed가 될 수 있다. 고정 0.5초 sleep은 이 구간의 상한 증거가 아니다. 대기 시간이나 0 관측 횟수만 늘려서는 해소되지 않는다.

구현상 별도 결함도 있다. `close_tts_ingress()`가 복구용 try 바깥에서 호출돼 /load 성공 뒤 검증·영속화 실패 또는 취소 시 원 입구 복구가 실행되지 않는다.

공식 소스와 비교한 근거:

- [Caddy v2.11.7 reverseproxy.go](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/reverseproxy/reverseproxy.go): `countRequest(1)`은 `reverseProxy()` 내부의 시작에서 증가한다. 앞선 라우팅·업스트림 선택 구간까지 집계하지 않는다.
- [Caddy v2.11.7 app.go](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/app.go): reload는 shutdown의 시작을 기다리며, 완료 대기는 프로세스 종료 경로에 있다. reload 반환 자체로 기존 요청 완료를 확인하지 못한다.

## 직접 실행 증거

프로토타입 원본은 보존했다. 별도 worktree `../back-platform-v2-fnd1-proof-20261025`, 브랜치 `review/platform-v2-fnd1-proof-20261025`의 `3643a1f`에 안전성 회귀 테스트를 커밋했다.

```sh
PYTHONPATH=src:. ../back/.venv/bin/pytest -q -p no:cacheprovider --noconftest unit_tests/test_fnd1_proxy_barrier_proof.py
```

결과: **2 failed in 0.25s**. 실제 controller의 drain을 사용하고, DB·Caddy 요청의 스케줄링은 이벤트 모델로 제어한다. 첫 테스트는 세 관측이 통과한 뒤 요청이 202에 해당하는 commit을 하고 `failed/generation_draining`으로 바뀌는 반례다. 두 번째는 부분 차단 실패 후 `slots.closed=True`가 남는 반례다. 실제 Caddy 네트워크 측정으로 확대 해석하지 않는다. 이 브랜치는 의도적으로 실패하는 조사 증거이며 통합 대상이 아니다.

## 통합 후보의 최소 부분 교정

프로토타입 전체를 cherry-pick하지 않았다. 통합 후보에 아래만 반영했다.

1. 입구를 닫는 단계도 복구용 try 안으로 옮겼다. 부분 실패·CancelledError에서도 이전 슬롯의 열린 구성을 복원한다.
2. 복원 자체가 실패하면 예외 로그를 남기며 최초 오류를 유지한다. 복원 실패를 성공이라고 취급하지 않는다.
3. 구세대의 활성 TTS 조회에 queued뿐 아니라 running도 포함했다. 구세대의 퇴역 판정은 실행 중 TTS를 기다리지 않으므로 종료 시 running을 끊지 않으려면 필요하다.
4. 코드 설명에서 고정 sleep이 전송 계층 장벽이라는 잘못된 보장을 제거했다. 전송 계층 공백 자체는 아직 해결하지 않았다.

새 `unit_tests/test_tts_first_transition_failure_recovery.py` 4건은 수정 전 **4 failed in 1.01s**, 교정 후 residual 두 파일 포함 **28 passed in 1.11s**였다. running 조회 테스트는 실제 쿼리 빌더와 가짜 scalar 결과를 이용한 DB-free 계약 검사이며 PostgreSQL 실행 결과가 아니다.

대표 회귀 명령:

```sh
PYTHONPATH=src:. ../back/.venv/bin/pytest -q -p no:cacheprovider --noconftest \
  unit_tests/test_tts_first_transition_failure_recovery.py \
  unit_tests/test_generation_hardening.py \
  unit_tests/test_residual_drain_supervision.py \
  unit_tests/test_remote_api_residual_execution.py \
  unit_tests/test_tts_claim_stop_race.py \
  unit_tests/test_executor_integrated_drain.py \
  unit_tests/test_executor_safety.py \
  unit_tests/test_run_executor_drain.py \
  unit_tests/test_run_executor_services.py \
  unit_tests/test_executor_queued_consumers.py
```

결과: **100 passed in 11.60s**. 변경 Python 두 파일 Ruff: `All checks passed!`; `git diff --check` exit 0. 전체 gate 재실행은 아니며 이전 49건 실패 분류를 갱신하지 않는다.

## 최신 배포 기준 및 남은 일

MCP `ops_list_deploys`와 `backend_deploy_status(job_id=596947fc-f72d-4d68-b0e9-966826673f21)`는 `d56455c816cf286e3cf367d083f3affcb45b00fe`, succeeded, release_path `/Users/winterholic/.ttakkari/deploy/releases/d56455c816cf286e3cf367d083f3affcb45b00fe`를 반환했다. 이는 배포 영수증 범위의 확인이고 별도 모델 응답·MCP·재개 E2E 측정은 아니다. 저장소의 두 revision 비교에서 TTS·generation_service는 동일하지만 schema는 main `0068_library_intent`, 후보 `0070_background_consumer_queue`로 갈라졌다. main 통합 시 단일 head·온라인 이전 허용 목록을 다시 연결해야 한다.

다음 소유자는 root다. 프록시의 기존 요청 처리가 끝났다는 양의 확인을 얻을 수 있는지 또는 최초 호환 전환의 제약을 어떻게 지킬지 검토·구현하고 이 반례를 닫는다. 고정 시간, SSE가 없는 것으로 보이는 시점, 사람의 단순 idle 확인을 안전 증거로 바꾸지 않는다. 새 lease/fence 플랫폼은 만들지 않는다. 새 main 마이그레이션과 코드도 통합해야 한다.

Alembic 왕복·DB migration 9건·실제 Caddy·Chrome 정리·운영 E2E는 이번 범위에서 미실행이다. 기존 Codex initdb `shmget Operation not permitted`를 재시도하지 않았고, 발견된 MCP 중 승인된 일반 호스트 DB 테스트 실행 도구는 없었다. 호스트 전체가 실행 불가능하다는 뜻은 아니다.
