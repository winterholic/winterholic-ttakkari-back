# ADR0036 실행기 첫 slice (2026-10-21)

이번 사용자의 구현 지시가 ADR0036의 과거 구현 보류를 대체한다. 운영 활성화·main 통합·push·배포는 이 작업의 범위 밖이다. ADR0066 통합기는 별도 작업이며 여기에는 별도 큐를 만들지 않는다.

## 관측과 범위

18:24 KST 운영 `/api/health` ok, `/api/version` b1a3d162d9c2749d344d6b9a37783c6f456fc02e PID29900. 8791 readiness protocol1 active, owned_runs13, drain_ready=false. 해당 SHA의 immutable release 경로 존재 확인. 감독 runtime 기록은 별도 e817fc19471bdc48054158cada4a99466f7eaa6f이며 앱 revision과 혼동하지 않는다. 054e04dd 배포 예약15b05872는 당시 testing(성공 아님).
읽기 전용 운영 SQL: active 세대1, lease13, 최장 실행33분. 최근3일 rolling 표본의 날짜별 30분 초과/전체는 10-18 8/439, 10-19 23/536, 10-20 29/562, 10-21 12/241(18:25 현재, 진행 Run 포함). 두 세대 경합의 빈도는 이 단일 시점으로 판정 불가. ADR0036의 10-18 8792 장기 Run3개 draining 사례를 보존한다.

## 경계·소유권

API는 등록·조회·기존 DB cursor SSE·명령 발행만 수행하는 remote 모드. executor는 기존 RunManager 실행·대기 재개·명령 소비·fence·DB 결과 기록을 소유한다. 별도 Run 큐 없음. 감독은 artifact/readiness/cutover/drain/rollback을 계속 소유한다.
`RuntimeGeneration`에 role/protocol/execution_mode, `RuntimeControl`에 executor_generation FK를 additive로 넣는다. lease의 generation_id는 API 또는 executor incarnation의 소유자 id로 계속 쓰며 token fence를 유지한다. API active_generation과 executor_generation은 독립이다. 모든 claim은 기존 control 행 잠금을 공유한다.
DB trigger는 remote 활성 시 구버전의 API lease INSERT를 거부한다. protocol2 remote API만 있는 idle 전환에서 executor pointer를 설정한다. 기존 lease는 이관하지 않는다. 최초 활성화 이전 API 대상 미처리 명령은 lease가 빈 경계에서 executor로 원자적으로 옮긴다. 알 수 없는 명령 protocol은 활성화를 거부한다. 포인터 교체/해제/스키마 downgrade는 lease 또는 미처리 명령이 있으면 거부한다. executor 교체는 singleton OS lock + control 잠금 + 기존 lease 없음 조건을 모두 만족해야 한다. heartbeat timeout은 소유자 사망 증거가 아니다.

## 실패·명령·복구

취소·reset·release_waiting은 기존 RuntimeCommand를 사용한다. lease가 있으면 그 소유자, 없으면 executor로 간다. ack 전 장애는 재전달되며 취소/reset은 멱등 처리한다. 프로세스 종료가 확인되지 않으면 lease를 반환하지 않는다. group leader의 reap만으로 충분하다고 보지 않고 process group이 남거나 조회가 거부되면 lease를 보존한다. 접수 cold CLI도 attempt handle로 추적하며 취소 시 reap한다. stale token은 결과·이벤트 flush 전에 검사한다.
정상 executor 종료 후 빈 lease에서 재기동 가능. 비정상 종료의 남은 lease는 quarantine(기동 거부)이며 자동 탈취·자동 provider 재실행 없음. 이 slice에는 고아 프로세스가 모두 죽었다는 운영 증명과 수동 reconcile 도구가 아직 없다. 후속 독립 검증 전 운영 전환 금지.
DB의 상태 복원은 실행 중 프로세스의 중단 없는 복원이 아니다. provider 대화 resume는 별도 검사 대상이다.

## 혼합 버전·rollback

schema 먼저(기능 off) → executor 소유권 protocol2 지원 API 배포 → API 모두 remote 모드이며 이전 API Run drain 완료 → 독립 executor readiness/실행 검증 → 명시 activation. protocol1 API가 남으면 activation 거부. API/감독 wire protocol1은 보존한다. 별도 ownership protocol2를 registry·계약 run_executor에 명시한다. 기존 감독은 기능 off additive schema 배포만 가능하며 executor activation/readiness·교체 감독은 후속 검증 대상이다. 운영에서 이 절차 실행 안 함.
rollback은 신규 실행 유입 정지 → executor Run·프로세스 그룹·명령 drain → pointer 해제 → API local로 전환 → 필요하면 schema downgrade. 실행 중 pointer 해제·구 protocol로 active 변경은 DB에서도 거부한다. supervisor drain 공식의 기존 API 자식/HTTP/finalizer 검사는 보존한다.

## 범위 밖·통합

Qwen 접수 상주 자식·preview·STT·TTS는 API 소유를 유지한다. remote executor는 Qwen warm pool을 시작하지 않고 CLI fallback을 쓴다. 이 자식들의 독립 worker 이행과 readiness 집계는 후속. API 종료 시 remote RunManager에 엔진 handle이 없어 executor 자식을 종료하지 않는다.
main.py 변경 없이 generation lifespan에 opt-in remote 경로를 연결한다. config/models/generation_service 및 migration head/배포 계약은 수집기·TTS·예약 작업과 통합 충돌 예상. 현재 분기 기준054e04dd, 다른 작업 미커밋은 포함하지 않는다. 0060 migration 번호는 부모 통합 때 재번호·down_revision·계약을 함께 조정해야 한다.

## 검증 환경

88269ca 기존 임시 PG fixture를 수정하지 않는다. 독립 검증 세션4ae15ca7은 Codex sandbox initdb shmget EPERM을 기록했으며 e5984236은 fixture 별도 재작업 중. 같은 denial 재시도·공유 PostgreSQL CREATE DATABASE로 우회하지 않는다. 이 환경에서는 DB 통합·실프로세스 E2E를 미검증으로 남기고 순수 unit/fake 의존성 검사와 독립 환경용 실제 경로 테스트를 제공한다. 실모델 응답·MCP 실행·provider resume는 각각 별도 미실행으로 기록한다.
