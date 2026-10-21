# 2026-10-21 에이전트 전역 중지 보강 (ADR 0067)

## 배경
새벽 4시 "에이전트 전부 중지"에서 AIP 작업 세션만 "다른 세션이 끼어들 수 없다"며 남았다. (1) 재배포 타이머 유실은 f086be1(ScheduledAction)이 다뤘고, 이번에는 (2)와 주변 구멍을 고쳤다.

## 확인된 원인
- `stop_worker` 는 호출한 오케스트레이터 소유 작업자만 찾는다.
- 기존 전역 중지는 `manager.cancel` 만 불러, lease 도 프로세스도 없는 running Run 과 재개 대기 interrupted Run 을 놓쳤다.
- 취소된 작업자의 보고 Run 이 부모 오케스트레이터를 깨워 새 작업을 만드는 경로가 있었다(이전 중단 시도에서 발견한 것을 재현·차단).

## 변경
- `runs/agent_control.py` 신규: `stop_all_agents`, 범위 필터, 보고 억제 표식, 구세대 보고 폐기, 보고 스윕.
- `POST /api/orchestrator/stop-all`, `POST /api/orchestrator/agents/{session_id}/stop`, MCP `stop_agents`. 일반 `stop_worker` 제한은 유지.
- `/api/system/stop-all`, 예약 `stop_all` 이 같은 서비스를 사용. 예약 payload 범위는 예약한 오케스트레이터 풀로 고정.
- `manager.dispatch` 에 구세대가 만든 취소 보고 폐기 가드.
- `resume_interrupted` 가 미집행 cancel 명령을 회수하고 재개 대신 취소로 마감(`_consume_pending_cancel`). 소유 세대 사망 후 부활 경로 차단.
- 스키마 변경 없음. alembic head `0059_collector_intake`, `deploy/generation-contract.json` 불변.

## 검증
- `tests/test_agent_stop_all.py` 14건. 수정 전 코드에서 12건 실패, 1건 통과(예약 stop_all). 수정 후 14건 통과(배포 게이트에서 확인).
- 재현 3종: 다른 오케스트레이터 소유 작업자, 다른 세대 소유 running Run(실제 두 세대 런타임 + cancel 명령 집행), 한도 대기 Run.
