# 실행기 트랙 진행 기록 — 2026-10-21

기준 SHA: 054e04dd465d972cbc48701d773c95f722916983.
worktree: `/Users/winterholic/development/ttakkari-run-executor`, branch `feature/run-executor-slice`.
main.py와 tests/conftest.py는 변경하지 않았다. main 통합·push·배포 예약·운영 DB 쓰기 없음.

## 구현한 첫 slice

- 별도 `ttakkari executor --activate` 루프가 기존 DB Run 큐와 RunManager를 실행한다.
- API remote facade는 task/engine을 만들지 않고 명령·wake를 DB로 전달한다.
- 기존 generation_id/token fence와 전역 lane/session 직렬화를 유지하며, API active 포인터와 executor 포인터를 분리한다.
- capability2 registry·additive migration·DB claim/command/activation/rollback guard를 추가했다. API/감독 wire1은 유지한다.
- stale ORM autoflush·종료 기록·heartbeat를 fence한다. 취소 ack는 DB terminal 이후다.
- executor는 singleton OS lock만으로 과거 Run을 탈취하지 않는다. 잔여 lease가 있으면 재기동 거부한다.
- group leader reap 이후에도 process group이 남거나 조회 불능이면 lease를 보존한다. desk cold CLI도 handle에서 추적·reap한다.
- 실행 중 API 교체, 실제 SSE cursor, 취소 ack 재시도/reset, 중복 claim, 동시 executor 기동, protocol/rollback, idle restart/대기 재개, migration round-trip 테스트 경로를 작성했다.

## 검증 경계

TDD RED 로그는 이 Run outbox의 `executor-*-red*.log`에 보존했다. 단위 테스트는 실제 fake child spawn/취소/reap, SQLite ORM flush guard, Python fake 의존성 및 기존 supervisor 파일 경로를 실행한다. PostgreSQL row lock/trigger 보장을 SQLite 검사로 대체했다고 주장하지 않는다.

최종 실행: `.venv/bin/python -m pytest -q unit_tests` → `927 passed, 1 skipped, 6 warnings, 4946 subtests passed in 36.99s`; skip은 기존 host 전용 실DB 검사다. 변경 Python 파일 ruff는 `All checks passed!`, `git diff --check`는 exit0. Alembic `upgrade 0059_collector_intake:0060_run_executor --sql`은 offline SQL 생성 exit0이며 SQL 적용·PL/pgSQL 실행 검증은 아니다.

PostgreSQL/실 ASGI+executor 통합 8개는 collection만 실행했다. 기존 독립 검증 세션4ae15ca7의 Codex nested worker `initdb` → `shmget: Operation not permitted` 기록에 따라 같은 denial을 재시도하지 않았다. test fixture88269ca 및 재작업 중인 e5984236 변경은 복제·수정하지 않았다. collect-only는 명시적 미접속용 URL(port1)을 사용하며 DB를 만들거나 연결하지 않는다.

실모델 응답, provider 대화 resume, 실 MCP 실행은 각각 미실행이다. 운영 전환·executor supervisor 설치/readiness 연동·고아 reconcile 도구도 미완료다. DB 상태 재등록은 중단된 엔진 프로세스의 연속 실행 보장이 아니다.

## 부모 통합 시 처리

`0060_run_executor`는 동시 작업의 수집기0060·TTS0061과 충돌하므로 최종 순서로 재번호·down_revision·계약 schema/online 목록을 함께 맞춘다. config.py/models.py/generation_service.py/deploy 계약은 해당 작업들과 수동 통합·독립 검증이 필요하다. 이 분기의 main.py는 건드리지 않았다.

독립 환경에서 기존 private PG fixture로 다음을 실행한다:

```sh
TTAKKARI_NO_ENV_FILE=1 uv run pytest -q unit_tests/test_run_executor_unit.py unit_tests/test_generation_hardening.py
uv run pytest -q tests/test_run_executor.py tests/test_run_executor_processes.py tests/test_generations.py tests/test_generation_processes.py tests/test_generation_deployment.py
```

그 뒤 migration 왕복/동시성·전체 게이트와 실모델/MCP/resume를 각각 검사한다. 운영 활성화는 모든 기존 API Run drain, 모든 live API capability2 remote, 처리 가능한 명령만 존재, executor artifact/readiness 검증, orphan 처리 절차 확보 이후다. rollback은 Run·그룹·명령 drain 뒤 pointer 해제 순서다.

정확한 최종 명령·실제 출력·feature SHA·변경 manifest는 이 Run outbox의 보고서와 `executor-verification-receipt.json`에 저장한다. 부모가 독립 검증 후 통합·배포하며 이 트랙은 PMS를 중복 등록하지 않는다.
