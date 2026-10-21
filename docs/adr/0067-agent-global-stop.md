# ADR 0067. 세션 소유권과 배포 세대를 넘는 에이전트 전역 중지

- 상태: DB 통합 재작업을 반영한 코드 SHA `19936155273127e52adc38c6320f3916dfcd00c4`에서 stop 통합 테스트는 통과했으나 전체 gate는 미통과(§7). main 병합과 배포는 하지 않았고 독립 검증 뒤 기획이 진행한다. 이 문서는 사용자 요청의 설계를 기록하며 별도 사용자 승인 기록을 뜻하지 않는다.
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

## 6. DB 통합 재작업 (2026-10-21, 코드 SHA `1993615`)
`2cbe1f1` 은 비교 기준 `fe0386a` 에서 통과하던 stop 통합 테스트가 11 failed / 1 error 였다. 원인과 조치:

1. `POST /api/orchestrator/stop-all` 500: `_stop_with_durable_receipt` 의 필수 키워드 `session_id` 를 호출부가 넘기지 않아 TypeError. `session_id=None` 전달.
2. work 풀 fence 과확장: `fence_keys("work", ws)` 가 `pool:work` 와 `workspace:<id>` 를 함께 반환해 한 워크스페이스 stop 이 모든 회사 워크스페이스의 후속 Run 을 409 로 막았다. work 범위는 `workspace:<id>`(+세션 키)만 쓴다. 풀 전체 정지는 `pool:all` 뿐이다. 단위 테스트가 `pool:work` 를 기대하던 것도 경계 분리 단언으로 바꿨다.
3. 락 순서 교착: `queue_run` 은 fence 행 → (request_key 가 있으면) 세션 행, `dispatch` 는 세션 행 → fence 행 순으로 잠갔다. 같은 세션에 대한 request_key 제출과 dispatch 가 겹치면 서로를 기다린다. `lock_fences` 호출 지점(queue_run, dispatch, resume_interrupted, stop, `_complete_stop`)과 세션 행 잠금 지점(agent.py share, desk `_delegate`, lead_archive, `_collect_artifacts`)을 코드에서 추적해 "세션 행 먼저, fence 행은 키 정렬 순서" 로 통일했다. queue_run 이 세션 행을 먼저 잠그고, 같은 세션 제출을 직렬화하므로 request_key 중복 판정도 이 락 아래에서 한다. 회귀 테스트 `test_same_session_submit_and_dispatch_lock_session_before_fences` 는 queue_run 이 fence 를 잡은 시점에 dispatch 를 끼워 교차를 결정적으로 만든다. 이전 순서로 되돌리면 `deadlock detected` 로 실패하고 수정 후 통과하는 것을 둘 다 실행해 확인했다.
4. 새 라우트 `GET /api/system/stop-receipts`, `/{receipt_id}` 를 `ROUTE_SCOPE` 에 `owner-plain` 으로 분류했다(`User` 의존성, 소유자 전용).
5. Run 시작 경로의 `ps` 조회: antigravity 종료 테스트가 `asyncio.create_subprocess_exec` 를 가짜 spawn 으로 바꿔 둬서 PID 식별용 `ps` 호출이 가짜 proc 을 받는다. 기능 의도(시작 시각 기록)는 유지하되 `_process_identity` 가 별도 스레드의 `subprocess.run` 을 쓰도록 바꿔 asyncio spawn 패치와 분리하고, `RunManager._record_process_identity` 주입 지점을 둬 가짜 pid 를 쓰는 테스트가 명시적으로 끊게 했다.
6. 테스트 격리: 예약 루프 경합으로 `stop_all` 예약 테스트 2건이 다음 테스트의 TRUNCATE 와 교착하던 문제는 `due_at=+3600s` 로 해결했다(`ccaae83`, 구현 변경 없음).
7. 진단성: `tests/pg_sandbox._run` 이 `CalledProcessError` 하위 `ClusterCommandError` 로 stderr/stdout 꼬리를 예외 메시지에 싣는다. 기존 `except CalledProcessError` 처리와 호환된다.

범위 밖·미검증: 0062 는 빈 DB 에서 `0061 → 0062 → 0061 → 0062` 왕복만 확인했다(기존 데이터 보존, 세대 공존은 별도 검증 필요). 교차세대 lease 실집행과 운영 중지는 실행하지 않았다.

## 7. 전체 게이트 결과 및 후속 조치 (2026-10-21)
`scripts/verify_deploy_gate.sh --full` 을 코드 SHA `19936155273127e52adc38c6320f3916dfcd00c4` 에서 실행했다. `/private/tmp/stop-rework-gate-full.log` 기준 `13 failed, 3354 passed, 15 skipped, 2 xfailed, 3 errors, 4952 subtests passed in 3435.87s`. 전체 게이트는 **실패**이며 병합·배포 준비 완료로 판정하지 않는다.

- 실패 13건 중 10건은 Phaser 게임 빌드/격리/서빙 경로에서 120초 pytest timeout 또는 게임 서버 연결 실패였다. 2건은 generation process 테스트에서 readiness 로그가 비어 있었다. 1건은 `unit_tests/test_remote_desktop.py::test_binary_transport_and_socket_cleanup` 에서 `ConnectionResetError` 였다. 공통 원인은 이 로그만으로 확인되지 않았다. 게이트 뒤 호스트 자원 측정에서 OrbStack Helper CPU 101.6%, swap 사용 97.6%였지만 게이트 시각의 측정이 아니므로 원인으로 단정하지 않는다.
- 오류 3건은 `test_meeting_uses_ordered_models_and_falls_back_on_runtime_limit[3]`, `test_reset_my_context_during_turn_applies_after_it`, `test_reroute_cannot_escalate_without_new_reason` 의 DB setup에서 `_clean` TRUNCATE deadlock이었다. `tests/conftest.py` 에서 이 특정 deadlock만 최대 6회 재시도하는 변경이 작업트리에 추가됐으나, 아래 실행 환경 제약으로 DB 회귀 확인은 못 했다.
- 재검증 시도 `.venv/bin/python -m pytest -q --timeout=180 tests/test_agent_stop_all.py ...` 는 Codex 명령 샌드박스의 `initdb` 단계에서 `shmget ... Operation not permitted` 로 중단됐다. 이는 해당 샌드박스의 결과이며 호스트 실행 불가를 뜻하지 않는다. 같은 조건으로 반복하거나 샌드박스를 우회하지 않았다.
- 정적 확인에서 변경 Python 파일 Ruff는 `All checks passed!`, `compileall` 은 성공했다. `--noconftest` 묶음은 `51 passed, 2 errors`; 두 오류는 `tests/conftest.py` 가 제공하는 `make_git_dir` fixture가 의도적으로 제외되어 생긴 실행 방식 한계이며 기능 실패로 세지 않는다. stop DB 통합/API tests와 cleanup retry는 재실행이 필요하다.

다음 검증 단계: cleanup 재시도 변경을 포함한 코드에서 stop DB 통합/API 테스트와 전체 게이트를 허용된 호스트 테스트 환경에서 다시 실행하고, 앞선 13건이 재현되는지 실패별로 확인한다. 전체 게이트가 모두 통과하기 전까지 이 브랜치를 병합·배포 후보로 표시하지 않는다.

## 8. Fixture cleanup bounded-drain 후속 (2026-10-21, SHA \`535fc2d\` 이후)
- 조사 경로: \`RunManager.start()\` 는 \`_handles\` 에 Run task를 등록한다. \`_spawn()\` 는 \`_background\` 에 task를 등록한다. \`_execute()\` finally 는 아직 종료 중이 아니면 \`_spawn(self.dispatch(sid))\` 를 만들어 다음 queued Run을 시작할 수 있다. 따라서 teardown의 cancel/dispatch 연쇄가 고정 반복 횟수를 넘는 경우를 코드상 배제할 수 없다.
- 결정적 DB-free 재현은 기존 20회 drain 알고리즘과 같은 registry·await 순서를 fake task로 실행했다. 완료 callback이 다음 dispatch task를 등록하는 연쇄를 21개로 만들자 이전 teardown은 \`handles=0\`, \`background=1\` 인데도 정상 반환했다. 두 번째 fake task는 \`RuntimeError("fixture dispatch failure")\` 를 던졌고 이전 \`except BaseException: pass\` 경로는 이를 성공처럼 삼켰다. 이는 fixture 코드 경로의 bounded-drain postcondition 및 오류 은폐 결함을 입증한다. 실제 테스트에서 task 21개가 남았거나 DB deadlock을 일으켰다는 증거는 아니다.
- 최소 수정: \`tests/task_cleanup.py::drain_registered_tasks\` 로 registry drain을 분리했다. 최대 20회 한도는 유지하되 마지막에도 등록 작업이 남으면 handle ID와 background 잔여 수를 담아 \`AssertionError\` 로 실패한다. Run 및 background task의 일반 예외는 전파하고, 정상 취소 계약인 \`asyncio.CancelledError\` 만 소비한다. fake Run 취소가 completion dispatch를 등록하는 정상 경로도 drain됨을 검사한다.
- TRUNCATE 재시도는 이전 \`"deadlock detected"\` 문자열 검색에서 PostgreSQL SQLSTATE \`40P01\` 확인으로 좁혔다. 각 시도는 loop 안에서 새 \`AsyncSession\` context를 열고 닫으므로 실패 트랜잭션은 context 종료 시 rollback/close 된 뒤 다음 시도를 연다. 재시도는 warning log에 다음 attempt 번호와 \`SQLSTATE 40P01\` 을 남긴다. 다른 SQLSTATE, 메시지 문자열만 있는 오류, 6번째 실패는 재시도하지 않고 원래 예외를 전파한다.
- TDD/무DB 실행: 변경 전 fake 재현 명령 \`.venv/bin/python -m pytest --noconftest -q tests/test_fixture_cleanup.py\` 는 기존 알고리즘에서 두 테스트가 실패했다(잔여 background 1개, unexpected RuntimeError 은폐). 수정 뒤 같은 경계 테스트와 stop contract unit을 함께 실행해 \`10 passed in 0.30s\` 를 확인했다. Ruff \`All checks passed!\`, compileall, \`git diff --check\` 도 통과했다.
- 실행하지 않은 것: Codex nested command sandbox의 이전 \`initdb shmget Operation not permitted\` 제한을 그대로 적용해 DB fixture, DB 통합, cleanup deadlock 실재 재현 및 full gate를 실행하지 않았다. 따라서 이 fixture 수정이 기존 DB deadlock을 해결했다는 결론은 아니다. stop DB 반례와 full gate도 여전히 미검증이며 독립 판정은 partial이다. main 병합/배포 금지 상태를 유지한다.
- 독립 reviewer 입력: 고정 SHA는 이 항목을 포함한 후속 commit에서 갱신한다. 최소 재현/검증 명령은 \`PYTHONPATH=src .venv/bin/python -m pytest --noconftest -q tests/test_fixture_cleanup.py tests/test_agent_stop_contract_unit.py\`; 별도 DB-free 정적 명령은 \`.venv/bin/ruff check tests/conftest.py tests/task_cleanup.py tests/test_fixture_cleanup.py\` 및 \`.venv/bin/python -m compileall -q tests/conftest.py tests/task_cleanup.py tests/test_fixture_cleanup.py\` 이다. 이 결과만으로 DB/full gate 통과를 주장하지 않는다.
