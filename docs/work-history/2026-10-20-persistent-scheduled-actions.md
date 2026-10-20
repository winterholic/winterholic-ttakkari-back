# 재배포·세대 전환에도 살아남는 영속 예약 작업(ScheduledAction) 구현

## 배경
- 2026-10-20 사용자가 "04:30에 따까리 에이전트 전부 중지"를 요청했을 때, 재배포로 앱 프로세스 메모리 안의 타이머가 유실될 위험이 있어 프로젝트 밖 1회성 launchd 예약으로 우회했다.
- 무중단 세대 전환(최대 3슬롯) 배포 중 구세대와 신세대가 동시에 실행되는 환경에서 타이머·지연 실행·리마인더·재시도 대기가 유실되지 않고, 정확히 한 번(exactly-once) 실행되도록 영속화했다.
- 인프라 추가 최소화 및 낮은 자원 부하 원칙(PostgreSQL 17 이미 상주)에 따라 Redis 대신 PostgreSQL 테이블 + `SELECT FOR UPDATE SKIP LOCKED` 기반 분산 리스를 채택했다.

## 작업 내용
1. **타이머·예약 전수 조사 및 분류**:
   - `manager._arm` / `_waiting` / `_wakers`: 한도 대기(run.waiting) 및 슬롯 대기 시 `run.resume_after`를 DB에 영속 기록해 재기동 후 `resume_queued()`에서 복원하도록 보강.
   - `ScheduledAction` (신규): 특정 시각 Run 중지(stop_all), 단일 Run 취소(cancel_run), 세션 메시지 전송(send_message) 등 영속 예약 작업 지원.
   - `meetings/vectors.schedule`: 디바운스 sleep 중 프로세스 종료 시에도 `vector_status`가 유지되어 세대 주기 색인 작업이 자동 복구하도록 유지.
   - 캘린더 알림(`CalendarEvent.remind_at`), 체크리스트 알림(`ChecklistReminder`), 날씨 브리핑(`WeatherBriefing.scheduled_at`)은 이미 DB 기반 스캔으로 확인됨.
   - 데스크 웜 풀, subprocess 하트비트, 유지보수 주기 루프는 프로세스/OS 수명주기에 종속된 휘발 캐시 및 주기 루프로 분류.
2. **영속 예약 테이블 및 마이그레이션**:
   - `models.py`: `ScheduledAction` 모델 추가 (`id`, `action_type`, `payload`, `due_at`, `status`, `owner_id`, `leased_until`, `attempts`, `max_attempts`, `description`, `last_error`, `result`, `created_at`, `updated_at`, `executed_at`).
   - 복합 인덱스 `(status, due_at)`로 도래 작업 조회 최적화.
   - Alembic 마이그레이션 `0057_scheduled_actions.py` 작성 (additive 신규 테이블, online-safe, down_revision: `0056_research_lab`).
   - `deploy/generation-contract.json`: `schema_revision`을 `0057_scheduled_actions`로 갱신하고 `online_migration_from`에 `0056_research_lab` 추가.
3. **스케줄 서비스 (`ttakkari/schedules/service.py`)**:
   - `parse_due_at`: 상대 시각(`+10m`, `-10s`, `+300s`, `+1h`, `+2d`), 타임스탬프, ISO 8601(UTC/KST 오프셋) 파싱.
   - `create_schedule`: 작업 유효성 검사, DB 저장 및 활성 세대 `generation.wake(db)` NOTIFY 신호 발송.
   - `acquire_due_schedules`: `SELECT FOR UPDATE SKIP LOCKED`로 복수 세대/폴러 동시 조회 시 중복 없이 단 1곳만 리스 획득.
   - `execute_action`: `stop_all`(전체 활성/대기 에이전트 중지), `cancel_run`(특정 Run 취소), `send_message`(세션에 Run 큐잉), 지각 만료 정책(`late_limit_seconds`) 지원.
   - `process_due_schedules`: 도래 작업 차지, 실행, 완료/실패(재시도) 원자적 처리.
4. **세대 수명주기 및 기동 연동**:
   - `generation_service.py`: `maintenance_jobs`에 `schedules` 주기 작업 등록(5초). `coordinate` 활성 세대 진입 시 `process_due_schedules`를 즉시 1회 호출해 배포 중 도래한 놓친 예약 즉시 복구.
   - `main.py`: 단일 인스턴스/레거시 모드 lifespan 기동 시 즉시 복구 및 백그라운드 폴링 루프 연동.
5. **오케스트레이터 API 및 MCP 도구 노출**:
   - API: `POST /api/orchestrator/schedules`, `GET /api/orchestrator/schedules`, `DELETE /api/orchestrator/schedules/{id}`.
   - MCP 도구: `schedule_action`, `list_scheduled_actions` (query_tool, readOnlyHint), `cancel_scheduled_action`.
   - `tool_policy.py`: `AUTO_APPROVED_TOOLS` 및 `READ_ONLY_TOOLS` 등록.
6. **검증**:
   - `tests/test_scheduled_actions.py` (13 passed): 상대 시각 파싱, 등록/조회/취소, stop_all/cancel_run/send_message 실행, 4개 폴러 동시 실행 시 중복 0(배타적 리스), 세대 전환(A→B) 후 정확히 1회 실행, 배포 중 지난 시각 예약 복구, 만료 정책, API/MCP 호출, 한도 대기 resume_after DB 보존 및 재기동 후 재무장.
   - 관련 회귀 테스트 (`test_generations.py`, `test_generation_deployment.py`, `test_generation_processes.py`, `test_orchestrator.py`, `test_api_runs.py`, `test_tasks.py`, `test_planner.py`, `test_checklists.py`): 171 passed.
   - Ruff 린트 전체 통과.
