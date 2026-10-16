# 2026-10-16 무중단 배포 이후 적극 배포 방침

- 사용자 요청: 다른 작업이 있으면 배포를 보류하던 규칙을 정리하고 적극 배포한다. 개인 따까리 변경은 관련 검사·clean commit·push 후 바로 예약하며, 명시적인 보류 지시가 있으면 따른다.
- 현재 구조 확인: activation marker가 generations를 선택한다. `Controller.process`는 generation 예약을 `wait_idle` 이전에 분기한다. 실제 조사 중에도 기존 예약은 `testing`에서 `draining`으로 전환됐다. 동시 Run의 존재 자체는 현재 배포기 차단 조건이 아니었다.
- 정리한 잔존 안내: AGENT_NOTE의 전체 작업 종료 대기·별도 배포 지시 조건, MCP 도구 설명의 Only-use-on-request, API 도구 목록의 작업 종료 후 재시작, README와 shell/helper 설명. 새 정책과 슬롯 자동 대기·draining/succeeded 의미를 맞췄다. 사용자 하네스 파일에서는 배포 차단 문구가 검색되지 않았으며 수정하지 않았다.
- 유지: legacy idle/최초 bootstrap 안전 대기, 동시 두 세대 제한, schema/protocol 호환성, readiness, 작업 소유권·fencing, 구세대 drain, 직접 launchctl/재시작 금지, 회사 세션 배포 거절. 진행 중 작업을 없애려고 취소·강제 종료하지 않는다.
- 회귀 확인: 실행 중 가짜 Run과 기존 draining 예약이 있어도 API가 후속 generations 예약을 202로 받는다. 외부 Controller가 owned_runs=2인 세대에서 legacy wait/count/restart를 호출하지 않고 신세대로 전환한다.
- 조사 중 실제 불일치 발견: 알림함 migration head는 `0031_notifications`인데 배포 계약이 `0030_runtime_generations`에 머물러 필요한 migration을 건너뛸 수 있었다. 계약/head 비교 테스트가 먼저 실패한 것을 확인하고, 목표를 0031로 고쳤다. 새 테이블·인덱스만 추가하므로 0030에서의 online migration만 명시 허용한다. 운영 DB를 Run에서 직접 변경하지 않는다.
- 검증: 격리 DB에서 `pytest -q tests/test_deployment.py tests/test_generation_deployment.py tests/test_generation_bootstrap.py tests/test_generation_processes.py tests/test_orchestrator.py tests/test_notifications.py tests/test_push.py -x` → **121 passed in 25.45s**. 실제 두 ASGI/Caddy·가짜 CLI 포함. `ruff check src tests alembic`·`git diff --check` 통과.
- 별도 격리 PostgreSQL DB에서 0030을 설치하고, 기존 `runs` 조회 트랜잭션이 열린 채 0031로 upgrade해 새 알림 테이블과 기존 조회를 확인했다. 이후 downgrade0030 → upgrade0031도 확인하고 임시 DB를 삭제했다. 운영 downgrade는 수행하지 않는다.
- 산출물·원시 로그: `/Users/winterholic/.ttakkari/data/outbox/f577ae90-f836-4b9a-99bb-20bc159ad39d/`. 실제 배포 완료는 외부 예약의 상태·공개 SHA로 별도 판정한다. 이 기록의 검증 결과는 운영 전환 성공을 뜻하지 않는다.
