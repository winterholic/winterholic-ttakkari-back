# ADR 0067. 세션 소유권과 배포 세대를 넘는 에이전트 전역 중지

- 상태: 구현(2026-10-21). 사용자 요청에 따른 에이전트의 구현 설계이며 별도 사용자 승인 기록을 뜻하지 않는다.
- 요청: 새벽에 "에이전트 전부 중지"를 시켰을 때 AIP 작업 세션만 "다른 세션이 끼어들 수 없다"며 남았다. 소유 오케스트레이터와 무관하게 모든 활성·대기 Run 을 멈춘다.

## 1. 원인
- `stop_worker` 는 `_worker(db, sid, worker_id)` 로 호출한 오케스트레이터 소유 작업자만 찾는다. 다른 기획 세션의 작업자는 멈출 방법이 없었다.
- 기존 전역 중지(`/api/system/stop-all`, 예약 `stop_all`)는 `manager.cancel` 만 불렀다. 소유 세대 lease 가 없고 프로세스도 없는 running Run 은 `False` 를 돌려주고 조용히 남았다. 재배포 중단으로 재개를 기다리는 interrupted Run 은 조회 대상도 아니라 중지 뒤 되살아날 수 있었다.
- 취소된 작업자는 `_finish` 에서 부모 오케스트레이터에게 보고 Run 을 만든다. 중지 직후 부모가 깨어나 새 작업을 시작하는 경로였다.

## 2. 결정
1. 일반 `stop_worker` 의 직속 소유권 제한은 유지한다. 전역/관리 중지는 별도 경로로 분리한다.
   - `POST /api/orchestrator/stop-all`, `POST /api/orchestrator/agents/{session_id}/stop`, MCP 도구 `stop_agents`(오케스트레이터 전용, `WORKER_MANAGEMENT_TOOLS`).
   - 소유자 전용 `POST /api/system/stop-all` 과 예약 `stop_all` 도 같은 서비스(`runs/agent_control.py`)를 쓴다.
2. 범위 경계: 개인 오케스트레이터는 개인 풀 전체, 회사 오케스트레이터는 같은 워크스페이스만 대상이다. 범위 밖 세션을 지정하면 0건으로 끝내지 않고 403 이다. 예약 `stop_all` 은 생성 시 예약한 오케스트레이터의 풀·워크스페이스로 payload 를 덮어써 범위를 넓힐 수 없다. 소유자 전용 `system/stop-all` 만 `all` 이다.
3. 대상: queued, running, 한도 대기(queued + resume_after), 재시작 재개 대기(interrupted, 오류 "서버 종료로 중단").
4. 순서: 대기 Run 을 먼저 `manager.cancel_queued`(= `_finish`)로 마감하고, 실행 중 Run 은 `manager.cancel` 로 넘긴다. 실행 중인 Run 이 끝나는 순간 다음 Run 이 시작되지 않게 한다.
5. 다른 세대 소유 running Run: 기존 `cancel` RuntimeCommand 를 lease 의 generation_id 로 보낸다. 소유 세대의 coordinator 가 `runtime_poll_seconds` 주기와 NOTIFY 로 집행한다. 구세대도 이미 이해하는 명령이라 새 컬럼과 마이그레이션이 없다(`schema_revision` 불변, `deploy/generation-contract.json` 변경 없음).
6. lease 도 프로세스도 없는 고아 running Run 은 소유자가 없으므로 DB 에서 취소로 마감한다.
   - 2026-10-28 보완: lease 가 남아 있지만 소유 세대가 종료된 경우에도 명시 중지 요청을 처리한다. `manager.cancel` 이 영속 cancel 명령을 기록한 뒤 해당 Run 하나에만 `executor_reconcile.cancel_orphan` 을 적용한다. 소유 PID·시작 시각, lease token, 전체 process-group 원장을 잠금 안에서 재확인하며, 살아 있는 자식은 기존 검증된 그룹 종료 절차를 사용한다. 모든 그룹의 종료가 확인돼야 cancelled 이벤트·명령 확인·lease 해제를 한 트랜잭션으로 기록한다. 소유자 생존/불명, 그룹 증거 누락/불명은 보존한다. 운영자 전용 일반 orphan 복구 CLI의 제한은 유지한다.
7. 결과는 `stopped`, `remaining`, `unstopped[{run_id, session_id, reason}]`, `total`, `swept_reports` 를 모두 돌려준다. 다른 세대가 아직 집행하지 않은 Run 은 멈춘 것으로 세지 않고 이유를 적는다.

## 3. 취소 보고의 재작업 경로 차단
- 취소 전에 Run 단위 억제 표식을 남긴다. `RuntimeCommand(kind="suppress_report", key="suppress-report:<run_id>", generation_id=NULL)` 이며 어느 세대도 명령으로 읽지 않는 영속 표식이다. `_enqueue_report` 가 이를 보고 보고 Run 을 만들지 않는다. `Run.usage` 플래그는 소유 세대 태스크가 `usage` 를 통째로 덮어써 사라질 수 있어 쓰지 않았다.
- 구세대가 이 표식을 모르면: 취소를 마감하면서 보고 Run 을 평소처럼 만든다. 구세대는 draining 이라 dispatch 하지 않아 그 보고는 queued 로 남는다. 신세대 `dispatch` 가 시작 전에 "worker_id=… 취소." 보고를 표식과 대조해(1시간 창, 표식 시각 이후 생성) 버린다. 중지 마지막에도 범위 안의 대기 보고 Run 을 한 번 더 쓸어 취소한다.
- 한계: 이 가드는 신세대 코드가 활성화된 뒤에만 동작한다. 소유 세대가 제한 시간(기본 3초) 안에 끝내지 못한 Run 은 `remaining` 으로 보인다.

## 4. 남은 위험
- 소유 세대가 cancel 명령 집행 전에 죽어 Run 이 interrupted 로 남은 경우: 신세대 `resume_interrupted` 가 미집행 cancel 명령을 회수하고(`_consume_pending_cancel`) 재개하지 않고 취소로 마감한다. Run 이 running 으로 남고 lease 도 잔존한 경우는 위의 명시 중지 경로로 처리한다. 롤백으로 구코드가 다시 뜨면 해당 보완 없이 이전 동작을 따른다.
- `ScheduledAction` 목록 조회는 풀 구분이 없다(기존 동작, 이번 범위 밖).
