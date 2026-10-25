# FND1: 구세대 앱 접수 계약을 경계로 쓰는 최초 local→remote 전환

2026-10-25 · Claude worker, 호스트 Mac Studio의 Claude Code 실행 환경 · 부모 `8174a26655005c7d672e0ab2a31e5703b26c4678`

운영 proxy·서비스·DB·GPU·launchctl은 건드리지 않았다. 아래 검증은 모두 격리 환경 결과이며 운영 전환 증거가 아니다.

## 결정

**accepted work**는 구세대가 `tts_jobs` 행을 커밋한 접수다. 커밋 전에 503을 받았거나 앱에 닿지 못한 요청은 accepted work가 아니며 원문은 클라이언트에 남는다.

FND1의 손실 원인은 917/17212d9 계열 구세대였다. 원문을 메모리에만 두고 draining이 되면 queued를 실패시켰기 때문에 activate 전에 전송 계층까지 비워야 했다. 9847022 이후 코드(현재 운영 23e0cbe 포함, `git diff 23e0cbe origin/main -- src/ttakkari/tts src/ttakkari/generation*.py src/ttakkari/api/tts.py` 차이 없음)는 의미가 다르다.

1. `accept`는 HTTP 집계(`InflightMiddleware`) 안에서 커밋 전에 DB 상태를 매번 다시 읽는다. active가 아니면 커밋하지 않고 503을 낸다.
2. 원문을 `chunks_json`에 커밋한다. draining 세대의 consumer는 자기 소유 queued/running을 끝낸다.
3. `can_retire`는 진행 HTTP와 소유 활성 TTS job이 모두 0일 때만 True다.

따라서 닫아야 할 admission 경계는 프록시 전송이 아니라 **구세대 앱의 상태 확인과 커밋**이다. `activate`(구세대 active→draining CAS)가 그 경계를 닫는다. pre-counter·slow-header·body-before-commit 요청은 모두 앱의 상태 확인을 거친다. activate 전에 확인을 통과하면 커밋 후 draining 구세대가 완료하고, 퇴역은 그때까지 막힌다. 확인이 activate 뒤라면 503이다. Caddy 전송 ack가 없어도 이 경계는 성립한다.

## 결속과 fail-closed

- 구세대 프로세스가 `/api/ready`에 `tts_admission="generation-state-v1"`(`tts/jobs.ADMISSION_CONTRACT`)을 보고한다.
- controller는 같은 응답의 generation이 `previous_generation`과, revision이 `previous_revision`과 같을 때만 전송 ack 대신 이 계약을 쓴다. 이 경로는 프록시를 조작하지 않는다.
- 계약 값 없음·다른 값·revision 불일치·기록 revision 없음이면 기존 stock 거절(close→`StageError(tts-drain)`→route 복원)을 그대로 적용한다. generation 불일치·응답 없음은 프록시 조작 전에 거절한다.
- 일반 local→local은 551c354의 좁은 예외를 그대로 쓴다. 이미 remote+executor_protocol2인 구세대 생략도 그대로다.
- 유휴 여부는 판정 근거가 아니다. 계약 경로도 activate 뒤 같은 identity의 `drain_ready`(retire)가 완료를 확인한다.

새 lease/fence/MSA/번들/schema는 추가하지 않았다. 변경은 상수 1개, ready 필드 1개, controller 분기 1개다.

## 실제 전환 순서

기존 릴리스는 바꿀 수 없다. 23e0cbe는 계약 값을 보고하지 않으므로 직접 remote 전환을 받으면 **거절**된다(아래 음성 대조).

1. 이 커밋을 포함한 **local** 릴리스를 배포한다(run_executor activation disabled). 23e0cbe controller의 일반 local→local 경로를 탄다.
2. 배포 controller(supervisor)가 이 커밋 이상의 코드인지 확인한다. supervisor가 옛 코드면 remote 시도는 stock 거절로 멈춘다. 손실이 아니라 진행 불가다.
3. remote activation 릴리스를 배포한다. 구세대(1단계 릴리스)가 계약을 보고하므로 프록시 조작 없이 activate한다. 구세대는 draining 중 자기 소유 TTS를 끝낸 뒤 `drain_ready`가 되어 retire된다.
4. rollback은 기존 `activate(previous, expected=candidate)`를 쓴다. 구세대가 다시 active가 되어 접수를 재개하고 QUEUE_OWNER도 adopt한다.

## 검증 (격리, 임시 PostgreSQL 클러스터, 실제 Caddy v2.11.7)

Python `back/.venv/bin/python`, `PYTHONPATH=src:tests:unit_tests`, conftest의 per-process 임시 PostgreSQL.

| 단계 | 명령/조건 | 결과 |
|---|---|---|
| red | `pytest tests/test_tts_admission_handoff.py` (구현 전) | 9 failed, 7 passed. 원인은 `KeyError: 'tts_admission'`, stock `StageError` (이후 테스트 쪽 `lookup_many(touch=)` 서명, 1xx 이중 응답, 평탄화된 error body를 교정함) |
| green | 같은 파일 | **16 passed / 2.68s**, 3회 반복 16/16 |
| mutation M1 | accept가 non-active도 접수 | 4 failed (draining 거절, 실제 Caddy pre-counter·slow-header·rollback) |
| mutation M2 | revision 결속 제거 | 1 failed (`revision-mismatch`) |
| mutation M3 | `can_retire`가 소유 TTS 무시 | 1 failed (커밋·activate 경쟁 후 퇴역 차단) |
| 회귀 | TTS·세대·route inventory 14파일 | **257 passed / 22.33s** |
| 음성 대조 | `fnd1_pre_counter_probe.py` (현 controller, 계약 없는 모델 snapshot) | `refused`, 붙잡힌 요청 202 → `succeeded` |
| 음성 대조 | 같은 probe `--baseline` (1cc48b6) | `allowed` → `failed/generation_draining` (반례 유지) |
| 정적 | ruff 4파일, `git diff --check` | pass |

실제 Caddy 시험은 `main.app`(InflightMiddleware·`/api/ready`·TTS route)을 uvicorn으로 띄우고, 격리 Caddy의 `request_buffers=16`과 `Expect: 100-continue`로 요청을 upstream 카운터 이전에 붙잡는다(Caddy 100 Continue, upstream `num_requests=0`, `http_inflight=0`, job 0 확인 후 판정).

- pre-counter POST를 activate 뒤 풀면 503 `generation_not_active`, job 0. `drain_ready` True.
- 상태 확인 통과 후 커밋 전에 activate하면 202, 구세대 소유 queued. activate 직후와 커밋 직후 `drain_ready` False, 구세대 tick 후 `succeeded`, 이후 True.
- 헤더 미완성 연결을 activate 뒤 완성하면 503, job 0.
- 계약 없는 구세대(23e0cbe 형태)는 stock 거절, Caddy `/load` 기록이 `closed→open`, 최종 route 열림, 붙잡힌 요청은 active 구세대에서 202 → `succeeded`.
- rollback(`activate(a, expected=b)`) 뒤 같은 구세대가 다시 202 → `succeeded`.
- 판정 중 취소 시 프록시 쓰기 없음, 구세대 active 유지.

`fnd1_slow_headers_probe.py`는 e522187 prototype 전용 `Slots(admin_url=)`에 의존한다. 현재 코드에서는 `TypeError`로 기동 전에 멈추므로 재실행하지 않았다. 같은 경우는 위 실제 Caddy 시험이 다룬다.

## 한계

- 운영 2단계 전환, supervisor 코드 버전 확인, 운영 rollback/restart는 **미검증**이다. 운영 실증은 lead가 순차로 조율한다.
- 구세대가 draining 중 crash하면 소유 queued는 기존 reaper가 `owner_gone`으로 명시 실패시킨다. `chunks_json`은 남지만 자동 재큐잉은 없다. 이는 crash 계약이며 이번 범위에서 바꾸지 않았다.
- HTTP/2·HTTP/3·WebSocket, 운영 인증/tunnel, 장시간 SSE, 실제 TTS 엔진/GPU는 이번 시험 범위 밖이다.
- 전체 pytest와 deploy gate는 실행하지 않았다(지시에 따라 lead integration에서 한 번에 실행).
