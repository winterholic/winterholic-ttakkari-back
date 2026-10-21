# ADR 0067. 세션 소유권과 배포 세대를 넘는 에이전트 전역 중지

- 상태: 재작업 수정 SHA `32dce03e7ec186d60da2211cb4aeafcb7bd6e42f`의 DB 통합 재검증 대기. feature 브랜치는 원격 push됨; main 반영 및 배포 예약은 미완료. 이 문서는 사용자 요청의 설계를 기록하며 별도 사용자 승인 기록을 뜻하지 않는다.
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
4. scope별 `AgentStopFence` 를 먼저 영속화한다. stop, enqueue, dispatch 는 동일 행을 정렬 순서로 잠근다. 중지 중 자동 접수는 409 로 거절하고, finally dispatch 와 재시작 재개는 paused fence 를 존중한다. Run 은 enqueue 당시 각 fence epoch 를 보관한다. 중지 전 대기 Run 과 구세대가 뒤늦게 넣은 epoch 미상 Run 은 fence 해제 뒤에도 시작하지 않고 취소한다.
5. 자동·시스템 입력은 fence 를 풀지 않는다. 중지가 끝난 뒤 사용자가 직접 새 Run 을 제출하면 요청된 개인 풀 또는 work 워크스페이스 범위의 fence 를 올려 해제하고 그 Run 을 현재 epoch 에 결합한다. 중지 집행 중에는 사용자 입력도 409 로 거절한다. 특정 세션 중지는 그 세션 fence만 세우며 형제 세션을 막지 않는다.
6. 다른 세대 소유 running Run 은 기존 `cancel` RuntimeCommand 를 lease 의 generation_id 로 보낸다. lease 가 없으면 `Run.pid`, 저장한 PID 시작 시각, 프로세스 그룹 ID, 엔진 명령을 모두 대조한다. 식별 조회가 막히거나 하나라도 일치하지 않으면 신호를 보내지 않고 Run 을 running/remaining 으로 남긴다. 종료 신호 뒤 프로세스 그룹 소멸도 확인하기 전에는 DB terminal 처리하지 않는다. `pid_started_at` 은 0062 migration 으로 추가하며 구세대 레코드는 식별 정보가 없어 안전 종료 대상이 아니다.
7. 오케스트레이터 호출자도 중지 대상이다. 요청 ID를 primary key로 한 `AgentStopReceipt` 를 요청 경계에서 먼저 기록하고, 별도 DB 세션의 task가 중지를 집행한다. 호출 HTTP 요청이 caller child 종료로 끊겨도 task는 이어진다. owner 인증 `GET /api/system/stop-receipts` 및 `GET /api/system/stop-receipts/{receipt_id}` 로 이후 결과를 확인한다. receipt 에는 running/claiming/partial/stopped/failed 상태와 전체 결과를 둔다.
8. 신세대 보고는 `stop_source_run_id` 와 그 원본 Run ID의 억제 표식만 정확히 연결한다. 구세대에는 원본 ID나 report/source FK가 없으므로 worker/time 근접성으로 취소 보고를 억제하지 않는다. 출처를 입증할 수 없는 구세대 취소 보고는 보존하고 자동 dispatch 를 보류하며 stop 결과에 remaining/unstopped 로 드러낸다. 중지 fence 이전/이후 dispatch와 merge도 이 경계를 지킨다.
9. 중지 결과는 `status=stopped|partial`, `stopped`, `remaining`, `unstopped[{run_id, session_id, reason}]`, `total`, `swept_reports` 를 반환한다. partial 메시지와 감사 로그에 남은 수·사유를 기록한다. 예약 action은 액션 집행을 마쳐도 대상이 남으면 `ScheduledAction.status=partial` 로 노출한다. `completed` 는 전부 중지 성공과 같은 뜻으로 쓰지 않는다.

## 3. 취소 보고의 재작업 경로 차단
- 취소 전에 Run 단위 억제 표식을 남긴다. `RuntimeCommand(kind="suppress_report", key="suppress-report:<run_id>", generation_id=NULL)` 이며 어느 세대도 명령으로 읽지 않는 영속 표식이다. `_enqueue_report` 가 이를 보고 보고 Run 을 만들지 않는다. `Run.usage` 플래그는 소유 세대 태스크가 `usage` 를 통째로 덮어써 사라질 수 있어 쓰지 않았다.
- 구세대가 이 표식을 모르면 취소 보고에 원본 ID가 빠진다. DB 스키마와 구세대 `_enqueue_report`를 대조했으나 report row에는 원본 Run FK/key가 없고 RuntimeCommand marker는 취소 원본 Run만 가리킨다. 따라서 구세대 취소 보고를 특정 원본 Run에 입증할 수 없다. 이 보고는 marker로 삭제하지 않고 queued 상태로 보존하며 자동 dispatch를 보류한다. stop 중 발견하면 partial/remaining/unstopped로 결과에 포함한다. 신세대의 정확한 source ID chunk만 marker와 매칭한다.
- 한계: 구세대가 제한 시간(기본 3초) 안에 끝내지 못한 Run 은 `remaining` 으로 보인다. lease 가 없고 process identity 를 확인할 수 없는 Run 도 안전을 우선해 `remaining` 으로 둔다.

## 4. 남은 위험
- 소유 세대가 cancel 명령 집행 전에 죽어 Run 이 interrupted 로 남은 경우: 신세대 `resume_interrupted` 가 미집행 cancel 명령을 회수하고(`_consume_pending_cancel`) 재개하지 않고 취소로 마감한다. 이 경우는 세대가 죽은 뒤 새 코드가 뜰 때만 생긴다(살아 있는 구세대는 cancel 명령을 직접 집행한다). 롤백으로 구코드가 다시 뜨면 가드 없이 재개할 수 있다.
- `ScheduledAction` 목록 조회는 풀 구분이 없다(기존 동작, 이번 범위 밖).

### Legacy 보고 연결 경계 보강 (2026-10-21)
- 독립 재검증 SHA `a2a16624b07661a64221a077331dbd74ef953b31`에서 worker+1시간 fallback 오탐 반례를 확인했다. 수정은 `fix/agent-global-stop-audit-followup-20261021`에 먼저 보존하고 DB 통합 재검증 예정.
- 코드/모델 추적: 구세대 `manager._enqueue_report`는 worker ID·상태만 prompt에 기록한다. `Run`에는 report source FK/key가 없고, `RuntimeCommand` suppression marker는 취소 원본 Run ID만 저장한다. 시간/worker로는 지연된 구세대 취소 보고의 원본을 입증할 수 없다.
- 계약: 신세대 source ID가 있는 merged report는 각 보고 chunk를 분리해 marker가 일치하는 chunk만 제거하고 나머지는 유지한다. source ID가 없는 구세대 취소 보고는 DB에서 보존한다. stop 대상 조회/마지막 sweep는 이를 취소하지 않고 `remaining`/`unstopped` partial로 표시한다. 이후 dispatch는 해당 queued row를 자동 실행하지 않고 보류하며, 다른 정상 queued 작업은 진행할 수 있다. `_enqueue_report`는 보류 대상과 새 보고를 합치지 않는다.
- 테스트: DB 비의존 반례에서 현재 fallback이 같은 worker의 두 source-less cancel report를 모두 삭제함을 먼저 관측했고(실패), fallback 제거 후 보존을 확인했다(통과). A(stop 후 지연 보고 유지/자동 실행 보류), B(이후 별개 취소 보고 유지), merged exact-source chunk, 기존 queue-merge gate 실패 node를 DB 통합으로 재검증해야 한다.
- `d767a2cc-abcc-4f87-8309-6f0de67e4167` (`a042f509`) 전체 gate는 `test_reports_queued_behind_busy_orchestrator_are_merged`에서 report 2개가 남아 실패했다. 해당 revision에는 이번 전역 stop source-ID/fence 구현이 없고 merge 조건도 동일했으므로 선행/무관 실패로 분류한다. 수정 SHA에서 해당 node를 별도로 실행해 회귀 여부를 확인한다.
- 추가 반례: source-less legacy queued report는 프로세스가 없어도 실행 대기 중이므로 stop 결과에서 `stopped`가 아니라 `remaining`/`unstopped`여야 한다. `_record_target_outcome` 단위 회귀가 이를 검사하고, stop API 통합 테스트에서도 양쪽 목록이 겹치지 않는지 단언한다.
- 커밋 `f4a70911cbc66543b65710dc5c6cdacfe50eacd1`은 feature 원격에 push했다. 최신 DB 비의존 검사 `pytest --noconftest -q tests/test_agent_stop_contract_unit.py`는 `6 passed in 0.30s`, Ruff `All checks passed!`, compileall 및 `git diff --check` 통과.
- DB fixture 포함 단위 실행은 pytest conftest의 고유 `initdb` 단계에서 exit 1로 실패했으나 traceback에 subprocess stderr가 없어 원인 불명이다. 이전 shmget 오류라고 단정하거나 같은 초기화 명령을 재시도하지 않는다. generation gate `512181be-6876-46f9-8a5c-082b661a7b74`는 이후 failed 상태가 됐다. 로그의 pytest 요약은 `3360 passed, 8 skipped, 2 xfailed, 4952 subtests passed in 1356.95s`지만 reservation은 `schema contract requires an idle migration/bootstrap; active work preserved`로 실패했다. 종료 뒤에도 `server_resources`에서 별도 python/Postgres 부하가 관측돼 DB 통합은 대기 중이다.
- 같은 수정 SHA에서 contract/head 및 migration-online allowlist 검사 3건은 `pytest --noconftest`로 `3 passed in 0.44s`였다. 이는 0062 upgrade/downgrade, 기존 데이터 보존, 세대 공존 동작 검증이 아니다.

## 5. 재작업 독립 검증 상태
- 감사 보고서 `agent-global-stop-audit-9afe3b2.md` 의 5개 반례를 수용 기준으로 고정했다. 기준 SHA `9afe3b2701383f2d606ec868e8f25ddad3c3bc87` 의 구현 경로 검토와 실제 실행 결과를 구분한다.
- DB 없는 검사: `pytest --noconftest -q tests/test_agent_stop_contract_unit.py` — 3 passed. 범위 fence 분리, 이전 epoch stale 차단, PID identity 없는 살아 있는 fake child에 signal 미전송을 검사한다.
- 구문·스타일: `python3 -m compileall -q ...`, 변경 파일 `ruff check` 통과, `git diff --check` 통과.
- DB 통합 반례 5개는 `tests/test_agent_stop_all.py` 에 추가했으나 미실행이다. 이 실행 환경에는 `TTAKKARI_TEST_DB` 가 설정되지 않았고 `/tmp/ttakkari-test-*` test cluster도 없다. 기존 동일 fixture의 `initdb` 는 `shmget: No space left on device` 로 실패한 이력이 있어 같은 경로를 반복하지 않았다.
- 기준 SHA 예약 `2c6afdaa-619a-4a5e-9f74-1910cb69763a` 는 최종 `failed` (`schema contract requires an idle migration/bootstrap; active work preserved`). 수정 SHA 독립 gate는 이 ADR 갱신 시점에 아직 예약하지 않았다.
- 운영 전체 중지는 실행하지 않았다. 05:00 취소 예약을 조회·변경·재활성화·실행하지 않았다.


### 후속 검증 상태 (2026-10-21)
- 수정 SHA `32dce03e7ec186d60da2211cb4aeafcb7bd6e42f`에서 DB 비의존 테스트 `4 passed`, 변경 대상 Ruff `All checks passed!`, compileall, diff check 및 Alembic head 조회를 다시 실행했다.
- 원격 feature branch push 성공. 원격 main push의 pre-push gate는 Codex 명령 sandbox에서 uv cache 경로 접근 `Operation not permitted (os error 1)`로 `uv sync` 단계에서 멈췄다. 테스트 우회는 하지 않았다.
- 기존 다른 SHA 예약 `d767a2cc-abcc-4f87-8309-6f0de67e4167`가 조회 시 `testing`이어서 통합 DB 테스트나 무거운 게이트를 겹쳐 시작하지 않았다. 수정 SHA 게이트/배포는 예약되지 않았다.
- 이 수정 SHA의 DB 통합 반례 5건, 교차세대 lease 실제 집행, scope/token, catch-up/중복 방지 동적 회귀는 미확인이다. 운영 전체 중지와 취소된 05:00 예약은 실행/변경하지 않았다. 위 근거 전까지 최종 독립 통과 판정을 내리지 않는다.
