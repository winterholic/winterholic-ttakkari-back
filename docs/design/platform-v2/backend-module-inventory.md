# 따까리 플랫폼 V2 백엔드 모듈 감사 인벤토리 (P1 Corrected Candidate)
> 스캔 커밋: `f304d1e60a746df1c17f7ad7cb2532baf3ad51ca` | 소스 트리: `a32e4c95795a790216efe042796237fe0b948fa4` | 판정: `coverage_partially_unverified`  
> 공식 최신 기준: `c8f10156845d007ec6496a65d6d8417be19260bd` (`origin/main` 클린 스냅샷 전이 원장 포함)

> [!IMPORTANT] **수집 범위 한정 및 대조 커버리지(Coverage) 분리 안내**
> 본 감사는 독립 discovery 후보 집합 및 소스 바인딩과의 대조를 거친 **수집 범위 한정** 결과입니다.
> 라우트 등 독립 정적 발견 집합이 없는 범주는 coverage=unknown으로 명시되며, 100% 완전성을 주장하지 않습니다.
> 스캐너 판정: `coverage_partially_unverified` (reconciled: ['migrations', 'source_files'], unknown: ['routes', 'tools', 'tables', 'configs'])

## 1. 수집 범위 및 대조 커버리지 결과 (Collection Scope & Coverage)

| 구분 (Category) | 수집 항목 수 (Collected) | 후보 항목 수 (Candidates) | 대조 상태 (Coverage) | 미매핑 (Unmatched) |
|---|---|---|---|---|
| **Routes (API/WS)** | 579 | - | `unknown` | 0 |
| **MCP Tools** | 133 | 133 | `unknown` | 0 |
| **DB Tables (ORM)** | 106 | 106 | `unknown` | 0 |
| **Alembic Migrations** | 67 | 67 | `reconciled` | 0 |
| **Settings / Configs** | 231 | 231 | `unknown` | 0 |
| **Source Files (.py)** | 385 | 385 | `reconciled` | 0 |
| **총 코드 라인 수 (LoC)** | 94926 | - | - | - |

### 범위 밖 원장 (Out-of-Scope Limits Ledger)
- 1. ORM Base.metadata(88/106 tables)를 검사하였으며, 실제 운영 PostgreSQL 물리 DB에 직접 연결하여 쿼리하지 않음.
- 2. 파이썬 소스 분모는 src/**/*.py에 한정되며, frontend, scripts, tests, 배포 쉘 스크립트 등은 이 분모에 포함되지 않음.
- 3. op.execute 66건은 AST call-site 수이며, SQL 런타임 의미·트리거·인덱스·downgrade 영향이 해석된 것이 아님 (정적 문자열/파라미터화/보간 분류만 제공).
- 4. 파일 SCC 8개는 정적 AST 파일 상호 참조 컴포넌트이며, 런타임 import cycle 또는 순환 실행 증거가 아님 (TYPE_CHECKING/lazy import 미구분).
- 5. 분모 내 unmatched=0은 수집된 항목의 분류 완료를 의미할 뿐 제품 전체의 기능 전수성이나 런타임 무결성을 입증하지 않음 (100% 전수 커버리지 주장 철회 및 범위 한정, routes 등 독립 대조 불가 범주는 unknown 처리).

## 2. 25개 도메인 모듈 인벤토리 명세 (4 Logical Planes)

| ID | Plane | 모듈명 | 진입점 (API/MCP) | 규모 (Files/Lines) | DB 테이블 | 실행 모드 | 제안 역할 | 난이도 | 상태 |
|---|---|---|---|---|---|---|---|---|---|
| `core.auth_session` | Control | **인증·세션·보안 제어** | API 13개, MCP 2개 | 19개 / 5,202줄 | 7개 | `request` | `Core` | `medium` | `implemented` |
| `core.workspaces_transfers` | Control | **워크스페이스·파일 전송** | API 22개, MCP 0개 | 6개 / 1,006줄 | 2개 | `request` | `Core` | `low` | `implemented` |
| `core.accounts_quota` | Control | **계정 풀·쿼터·토큰 계측** | API 17개, MCP 1개 | 2개 / 1,076줄 | 4개 | `request` | `Core` | `medium` | `implemented` |
| `core.generation_deploy` | Control | **세대 배포·수명주기 감독** | API 6개, MCP 2개 | 9개 / 4,005줄 | 3개 | `resident` | `Core` | `high` | `implemented` |
| `core.system_routing` | Control | **시스템 상태·설정·알림** | API 26개, MCP 1개 | 26개 / 5,164줄 | 4개 | `request` | `Core` | `low` | `implemented` |
| `ops.monitoring` | Control | **운영 관측성·오류 수집·배포 로그** | API 16개, MCP 6개 | 9개 / 1,485줄 | 5개 | `request` | `service` | `low` | `implemented` |
| `exec.run_engine` | Execution | **에이전트 실행 엔진·런타임** | API 31개, MCP 12개 | 56개 / 15,762줄 | 5개 | `longjob` | `worker` | `high` | `implemented` |
| `exec.durable_scheduler` | Execution | **내구성 스케줄러·Lease 실행기** | API 0개, MCP 3개 | 2개 / 380줄 | 1개 | `resident` | `worker` | `medium` | `implemented` |
| `exec.host_gpu` | Execution | **호스트 GPU 가속기·LLM 서빙** | API 4개, MCP 5개 | 5개 / 1,641줄 | 1개 | `resident` | `service` | `medium` | `partial` |
| `delivery.git_cas` | Delivery | **코드 배포 파이프라인·Git CAS** | API 0개, MCP 0개 | 8개 / 1,852줄 | 1개 | `longjob` | `service` | `high` | `partial` |
| `domain.memory_ontology` | Domain | **기억·엔티티 온톨로지 RAG** | API 38개, MCP 9개 | 10개 / 1,675줄 | 6개 | `request` | `library` | `medium` | `implemented` |
| `domain.knowledge_rag` | Domain | **검증 지식 베이스(KB)·증거 추출** | API 35개, MCP 13개 | 33개 / 6,417줄 | 15개 | `longjob` | `service` | `high` | `implemented` |
| `domain.collector` | Domain | **웹 데이터 수집·정제 플랫폼** | API 30개, MCP 0개 | 29개 / 6,865줄 | 16개 | `longjob` | `worker` | `medium` | `partial` |
| `domain.creative_studio` | Domain | **크리에이티브 스튜디오·디렉터·ComfyUI** | API 81개, MCP 27개 | 34개 / 16,742줄 | 1개 | `longjob` | `보류/단계적 격리 (FK cascade, file/process coupling 선행 해소 필요)` | `high` | `partial` |
| `domain.notebooklm` | Domain | **NotebookLM 브라우저 연동** | API 38개, MCP 11개 | 13개 / 1,913줄 | 3개 | `longjob` | `worker` | `medium` | `implemented` |
| `domain.research_lab` | Domain | **리서치 랩·실험 벤치마크** | API 15개, MCP 1개 | 7개 / 1,225줄 | 5개 | `request` | `library` | `low` | `prototype` |
| `domain.reverse_eng` | Domain | **리버스 엔지니어링·재구성** | API 11개, MCP 5개 | 3개 / 2,352줄 | 1개 | `longjob` | `job` | `medium` | `implemented` |
| `domain.meetings_stt` | Domain | **회의록·로컬 Whisper STT** | API 25개, MCP 4개 | 24개 / 4,142줄 | 6개 | `longjob` | `worker` | `high` | `implemented` |
| `domain.pms_tasks` | Domain | **PMS·마일스톤·체크리스트** | API 40개, MCP 11개 | 13개 / 2,374줄 | 7개 | `request` | `library` | `low` | `implemented` |
| `domain.design_system` | Domain | **디자인 시스템 카탈로그** | API 7개, MCP 3개 | 3개 / 514줄 | 0개 | `request` | `library` | `low` | `implemented` |
| `domain.media_tts` | Domain | **로컬·클라우드 음성 합성(TTS)** | API 9개, MCP 0개 | 12개 / 2,302줄 | 2개 | `request` | `service` | `medium` | `partial` |
| `domain.game_studio` | Domain | **게임 스튜디오 프로토타입** | API 14개, MCP 0개 | 12개 / 2,955줄 | 3개 | `request` | `library` | `low` | `prototype` |
| `domain.personal_tools` | Domain | **개인 일상 도구(메모·일정·날씨·주식)** | API 42개, MCP 13개 | 7개 / 1,724줄 | 5개 | `request` | `library` | `low` | `implemented` |
| `domain.external_connectors` | Domain | **외부 서비스 커넥터(Google·GitHub·RDP)** | API 36개, MCP 1개 | 29개 / 4,328줄 | 1개 | `request` | `shared adapter` | `medium` | `implemented` |
| `domain.shares_artifacts` | Domain | **결과물 공유·공개 배포** | API 23개, MCP 3개 | 14개 / 1,825줄 | 2개 | `request` | `Core` | `low` | `implemented` |

## 3. 모듈별 상세 명세

### `core.auth_session`: 인증·세션·보안 제어 (Control Plane)
- **사용자 및 비즈니스 역할**: 사용자 및 에이전트 인증, JWT/세션 발급/만료, 브루트포스 차단(로그인 스로틀링), 감사 로그
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 세션 캐시 미소비, JWT 연산, DB 요청당 1-2건)
- **현재 배포 결합도**: Core 세대 슬롯 밀결합(FastAPI auth middleware 및 dependency에 직결)
- **V2 제안 역할**: `Core` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 플랫폼 전반의 단일 신원 인증 및 세션 검증 토큰 분리로 다른 서비스들의 인증 위임 가능
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 19개 파일, 5,202 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.generation_deploy`, `core.system_routing`, `domain.collector`, `domain.knowledge_rag`, `domain.memory_ontology`, `domain.pms_tasks`, `domain.research_lab`, `domain.shares_artifacts`, `exec.run_engine`
- **소유 DB 테이블 (7개)**: `credentials`, `refresh_tokens`, `login_throttle`, `login_bucket`, `login_attempts`, `sessions`, `audit_logs`
- **대표 API 경로 (13개)**: `POST /api/auth/login`, `POST /api/auth/refresh`, `POST /api/auth/logout`, `POST /api/auth/logout-all`, `GET /api/auth/me` 외 8개
- **소유 MCP 도구 (2개)**: `list_sessions`, `read_session`

### `core.workspaces_transfers`: 워크스페이스·파일 전송 (Control Plane)
- **사용자 및 비즈니스 역할**: 작업 디렉토리 격리, 파일 다운로드/업로드 토큰 발급, 샌드박스 경로 제어
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 파일 I/O 스트리밍, 버퍼 스트리밍, DB 토큰 검증 1건)
- **현재 배포 결합도**: Core 세대 슬롯 밀결합(FastAPI static/streaming endpoints)
- **V2 제안 역할**: `Core` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 정적 파일 서빙 및 업로드/다운로드 경로 분리 시 대용량 파일 전송 중 Core 이벤트 루프 차단 해소
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 6개 파일, 1,006 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `domain.shares_artifacts`, `exec.run_engine`
- **소유 DB 테이블 (2개)**: `workspaces`, `transfer_tokens`
- **대표 API 경로 (22개)**: `GET /api/workspaces`, `POST /api/workspaces`, `GET /api/workspaces/default`, `GET /api/workspaces/{ws_id}`, `PATCH /api/workspaces/{ws_id}` 외 17개
- **소유 MCP 도구 (0개)**: 없음

### `core.accounts_quota`: 계정 풀·쿼터·토큰 계측 (Control Plane)
- **사용자 및 비즈니스 역할**: AI 공급자(Antigravity, Codex, Muse, Qwen) 계정 자격증명 관리, 토큰 사용량 계측 및 쿼터 제한
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 비동기 사용량 기록 또는 요청당 1건)
- **현재 배포 결합도**: Core 세대 슬롯 결합(Run 실행 전 계정 배정)
- **V2 제안 역할**: `Core` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 계정 회전 및 쿼터 차단 로직을 중앙 집중화하여 다중 런타임 간 계정 충돌 방지
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 2개 파일, 1,076 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `exec.run_engine`
- **소유 DB 테이블 (4개)**: `engine_accounts`, `account_usage_samples`, `qwen_token_usage`, `qwen_flash_usage`
- **대표 API 경로 (17개)**: `GET /api/accounts`, `GET /api/accounts/qwen-flash-usage`, `GET /api/accounts/qwen-usage`, `POST /api/accounts`, `GET /api/accounts/override` 외 12개
- **소유 MCP 도구 (1개)**: `account_usage`

### `core.generation_deploy`: 세대 배포·수명주기 감독 (Control Plane)
- **사용자 및 비즈니스 역할**: 3 슬롯(8791/8792/8793) 롤링 무중단 배포 관리, 헬스체크, 커트오버, 이전 세대 drain/종료
- **실행 형태 및 자원 특성**: `resident` | 미측정 (코드추론: 주기적 폴링, DB 1-2건 리스 갱신)
- **현재 배포 결합도**: Core 세대 슬롯 자체 제어자(메타 수명주기)
- **V2 제안 역할**: `Core` (이전 난이도: `high`)
- **측정 가능한 기대 이점**: 세대 전환 시 기존 활성 Run의 안전한 드레인 및 zero-downtime 배포 보장
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 9개 파일, 4,005 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `core.workspaces_transfers`, `domain.collector`, `domain.creative_studio`, `domain.knowledge_rag`, `domain.media_tts`, `domain.meetings_stt`, `domain.memory_ontology`, `domain.notebooklm`, `domain.personal_tools`, `domain.pms_tasks`, `domain.shares_artifacts`, `exec.durable_scheduler`, `exec.host_gpu`, `exec.run_engine`, `ops.monitoring`
- **소유 DB 테이블 (3개)**: `runtime_generations`, `runtime_control`, `runtime_commands`
- **대표 API 경로 (6개)**: `GET /api/bootstrap-ready`, `GET /api/ready`, `GET /api/health`, `GET /api/version`, `POST /api/agent/deploy` 외 1개
- **소유 MCP 도구 (2개)**: `backend_deploy_status`, `request_backend_deploy`

### `core.system_routing`: 시스템 상태·설정·알림 (Control Plane)
- **사용자 및 비즈니스 역할**: 시스템 전역 설정(Settings), 푸시 알림(WebPush), 데스크 피드백, 모델 및 도구 라우팅 정책
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 요청당 1건)
- **현재 배포 결합도**: Core 세대 슬롯 밀결합(전역 app state)
- **V2 제안 역할**: `Core` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 전역 설정 및 푸시 디스패처 모듈화로 도메인 모듈들이 비동기 이벤트로 알림 요청 가능
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 26개 파일, 5,164 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.generation_deploy`, `core.workspaces_transfers`, `domain.creative_studio`, `domain.game_studio`, `domain.media_tts`, `domain.meetings_stt`, `domain.memory_ontology`, `domain.notebooklm`, `domain.personal_tools`, `domain.pms_tasks`, `domain.reverse_eng`, `domain.shares_artifacts`, `exec.durable_scheduler`, `exec.run_engine`, `ops.monitoring`
- **소유 DB 테이블 (4개)**: `app_settings`, `desk_feedback`, `notifications`, `push_subscriptions`
- **대표 API 경로 (26개)**: `GET /api/system/qwen-worker`, `PUT /api/system/qwen-worker`, `GET /api/system/info`, `GET /api/system/resources`, `GET /api/system/backup-status` 외 21개
- **소유 MCP 도구 (1개)**: `notify_user`

### `ops.monitoring`: 운영 관측성·오류 수집·배포 로그 (Control Plane)
- **사용자 및 비즈니스 역할**: Sentry 호환 에러 인제스트, 운영 로그 검색, 인시던트 이슈 트래킹, 세대 배포 감사
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: DB conn 1-2건)
- **현재 배포 결합도**: Core API 라우터 (관측/제어 성격, Delivery 파이프라인과 분리)
- **V2 제안 역할**: `service` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 모니터링 시스템을 Core와 독립시켜 Core 장애 상황에서도 관측성 유지
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 9개 파일, 1,485 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.generation_deploy`, `core.system_routing`, `exec.run_engine`
- **소유 DB 테이블 (5개)**: `ops_issues`, `ops_events`, `ops_event_receipts`, `ops_issue_stats`, `ops_ingest_cursors`
- **대표 API 경로 (16개)**: `GET /api/ops-logs/issues`, `GET /api/ops-logs/issues/{issue}`, `PATCH /api/ops-logs/issues/{issue}`, `GET /api/ops-logs/events`, `GET /api/ops-logs/trend` 외 11개
- **소유 MCP 도구 (6개)**: `ops_deploy_log`, `ops_get_issue`, `ops_list_deploys`, `ops_list_issues`, `ops_search_logs`, `ops_update_issue`

### `exec.run_engine`: 에이전트 실행 엔진·런타임 (Execution Plane)
- **사용자 및 비즈니스 역할**: AI CLI(Antigravity, Codex, Muse, Qwen) 하위 프로세스 기동, 스트림 SSE 이벤트 전달, 중단/취소
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (코드추론: 하위 프로세스 I/O 및 파싱, 로그 버퍼링, 이벤트 영속화 1-2건)
- **현재 배포 결합도**: Core와 분리 가능하나 현재는 ASGI 프로세스 내부에서 서브프로세스 관리 중
- **V2 제안 역할**: `worker` (이전 난이도: `high`)
- **측정 가능한 기대 이점**: Core 웹 서버와 긴 에이전트 CLI 실행의 수명주기 분리로 Core 배포 시 에이전트 중단 방지
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 56개 파일, 15,762 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.generation_deploy`, `core.system_routing`, `delivery.git_cas`, `domain.creative_studio`, `domain.game_studio`, `domain.knowledge_rag`, `domain.meetings_stt`, `domain.memory_ontology`, `domain.pms_tasks`, `domain.shares_artifacts`, `exec.durable_scheduler`, `ops.monitoring`
- **소유 DB 테이블 (5개)**: `runs`, `run_events`, `runtime_leases`, `runtime_process_groups`, `retired_integration_records`
- **대표 API 경로 (31개)**: `GET /api/runs/failed`, `GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/cancel`, `GET /api/runs/{run_id}/events`, `GET /api/runs/{run_id}/stream` 외 26개
- **소유 MCP 도구 (12개)**: `begin_environment_probe`, `execution_capabilities`, `get_worker`, `list_workers`, `luna_common30_v2_cancel`, `luna_common30_v2_status`, `message_worker`, `report_execution_environment`, `spawn_worker`, `start_luna_common30_v2`, `stop_agents`, `stop_worker`

### `exec.durable_scheduler`: 내구성 스케줄러·Lease 실행기 (Execution Plane)
- **사용자 및 비즈니스 역할**: cron 기반 주기적 작업, 예약 작업(scheduled actions), PostgreSQL 행 잠금/Lease 기반 중복 방지 실행
- **실행 형태 및 자원 특성**: `resident` | 미측정 (코드추론: 주기 루프, lease 갱신 1건)
- **현재 배포 결합도**: Core 수명주기와 결합(lifespan startup/shutdown)
- **V2 제안 역할**: `worker` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 독립 프로세스 워커로 전환 시 스케줄러 재시작 없이 웹 서버만 단독 롤링 배포 가능
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 2개 파일, 380 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `exec.run_engine`
- **소유 DB 테이블 (1개)**: `scheduled_actions`
- **대표 API 경로 (0개)**: 없음
- **소유 MCP 도구 (3개)**: `cancel_scheduled_action`, `list_scheduled_actions`, `schedule_action`

### `exec.host_gpu`: 호스트 GPU 가속기·LLM 서빙 (Execution Plane)
- **사용자 및 비즈니스 역할**: Mac Studio Apple Silicon Metal 기반 llama-server 관리, GPU 벤치마크, 로컬 LLM 추론 자원 배정
- **실행 형태 및 자원 특성**: `resident` | 미측정 (실측 실행 증거 없음. 런타임 프로브 필요; 과거 추정 수치 전면 정정)
- **현재 배포 결합도**: 현재 Core API에서 서브프로세스로 기동/관리
- **V2 제안 역할**: `service` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: GPU llama-server 수명을 백엔드 재배포와 완전히 격리하여 가속기 로딩 오버헤드 0화
- **현재 구현 상태**: `partial`
- **소유 코드 규모**: 5개 파일, 1,641 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `exec.run_engine`
- **소유 DB 테이블 (1개)**: `system_resource_samples`
- **대표 API 경로 (4개)**: `POST /api/agent/host-gpu/start`, `GET /api/agent/host-gpu/status`, `POST /api/agent/host-gpu/bench`, `POST /api/agent/host-gpu/stop`
- **소유 MCP 도구 (5개)**: `host_gpu_bench`, `host_gpu_start`, `host_gpu_status`, `host_gpu_stop`, `server_resources`

### `delivery.git_cas`: 코드 배포 파이프라인·Git CAS (Delivery Plane)
- **사용자 및 비즈니스 역할**: Git 워크트리 격리 검증, 변경셋(ChangeSet) 증적 관리, merge queue, 자동 수복(bounded repair)
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (코드추론: Git I/O, 테스트 서브프로세스, DB 1건)
- **현재 배포 결합도**: 별도 CLI 및 orchestrator 연동 (ChangeSet 증적 관리)
- **V2 제안 역할**: `service` (이전 난이도: `high`)
- **측정 가능한 기대 이점**: 무중단 배포 및 자동 검증 엔진의 독립 실행으로 배포 파이프라인 장애가 운영 서빙에 전파되지 않음
- **현재 구현 상태**: `partial`
- **소유 코드 규모**: 8개 파일, 1,852 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`
- **소유 DB 테이블 (1개)**: `change_sets`
- **대표 API 경로 (0개)**: 없음
- **소유 MCP 도구 (0개)**: 없음

### `domain.memory_ontology`: 기억·엔티티 온톨로지 RAG (Domain Plane)
- **사용자 및 비즈니스 역할**: 에이전트 장기 기억(Recall/Remember), 엔티티 및 관계 온톨로지 그래프 탐색, 맥락 라이브러리
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 어휘 검색 + 벡터 임베딩 코사인 유사도, DB 1-2건)
- **현재 배포 결합도**: Core 통합 라우터 서빙
- **V2 제안 역할**: `library` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: RAG 엔진과 저장소 계층을 라이브러리화하여 다양한 에이전트에서 인프로세스 또는 경량 API로 공유
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 10개 파일, 1,675 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `domain.knowledge_rag`, `domain.meetings_stt`, `exec.run_engine`
- **소유 DB 테이블 (6개)**: `ctx_entities`, `ctx_relations`, `ctx_memories`, `ctx_sources`, `ctx_documents`, `ctx_chunks`
- **대표 API 경로 (38개)**: `GET /api/agent/library`, `POST /api/agent/recall`, `POST /api/agent/remember`, `POST /api/agent/forget`, `POST /api/agent/entities` 외 33개
- **소유 MCP 도구 (9개)**: `describe_entity`, `forget`, `get_entity`, `library_catalog`, `link`, `my_context`, `recall`, `remember`, `reset_my_context`

### `domain.knowledge_rag`: 검증 지식 베이스(KB)·증거 추출 (Domain Plane)
- **사용자 및 비즈니스 역할**: 불변 지식 베이스(KB), 소스 텍스트 증거(Evidence), 온톨로지 단정(Assertion) 추출 및 심사
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (코드추론: 추출 배치 파이프라인, DB 트랜잭션 1-3건)
- **현재 배포 결합도**: Core 통합 라우터 및 백그라운드 태스크
- **V2 제안 역할**: `service` (이전 난이도: `high`)
- **측정 가능한 기대 이점**: 지식 추출 배치의 장시간 연산 분리로 Core API 지연 방지 및 지식 저장소 독립 마이그레이션
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 33개 파일, 6,417 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `domain.collector`, `domain.memory_ontology`, `exec.run_engine`
- **소유 DB 테이블 (15개)**: `kb_sources`, `kb_documents`, `kb_revisions`, `kb_passages`, `kb_evidence`, `knowledge_revisions`, `knowledge_units`, `knowledge_evidence`, `knowledge_assertions`, `knowledge_assertion_evidence`, `knowledge_review_events`, `knowledge_extraction_jobs`, `kb_document_access`, `kb_observations`, `kb_passage_vectors`
- **대표 API 경로 (35개)**: `POST /api/assistant/knowledge/imports`, `GET /api/assistant/knowledge/revisions`, `GET /api/assistant/knowledge/units`, `POST /api/assistant/knowledge/assertions`, `GET /api/assistant/knowledge/assertions` 외 30개
- **소유 MCP 도구 (13개)**: `kb_document`, `kb_documents`, `kb_evidence`, `kb_search`, `knowledge_assertion`, `knowledge_extraction_status`, `knowledge_next_batch`, `knowledge_recall`, `knowledge_retry_extraction`, `knowledge_review_assertion`, `knowledge_sources`, `knowledge_start_extraction`, `knowledge_submit_batch`

### `domain.collector`: 웹 데이터 수집·정제 플랫폼 (Domain Plane)
- **사용자 및 비즈니스 역할**: 웹 문서 수집(Intake), Robots/Polite 속도 조절, Stage 2 파이프라인(텍스트 정제/블롭 저장/품질 검사)
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (코드추론: HTML 파싱, 텍스트 추출, 네트워크 I/O, DB 스테이지 트랜잭션)
- **현재 배포 결합도**: 독립 패키지 구조이나 Core 프로세스 내부 runner 구동 및 ChatSession 읽기 결합
- **V2 제안 역할**: `worker` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 수집 파이프라인 크롤링 부하를 Core 웹 프로세스에서 완전히 격리하고 독자 재시작/스케일링
- **현재 구현 상태**: `partial`
- **소유 코드 규모**: 29개 파일, 6,865 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `domain.knowledge_rag`, `exec.run_engine`
- **소유 DB 테이블 (16개)**: `collector_jobs`, `collector_outbox`, `collector_source_policies`, `collector_host_state`, `collector_fetch_cache`, `collector_items`, `collector_dedupe_bands`, `collector_budget_buckets`, `collector_budget_reservations`, `collector_cache`, `collector_runtime_flags`, `collector_sources`, `collector_deliveries`, `collector_daily_budgets`, `collector_reviews`, `collector_quality_models`
- **대표 API 경로 (30개)**: `POST /api/assistant/collect/items/{item_id}/reviews`, `GET /api/assistant/collect/quality`, `POST /api/assistant/collect/quality/train`, `GET /api/assistant/collect/sources`, `POST /api/assistant/collect/sources` 외 25개
- **소유 MCP 도구 (0개)**: 없음

### `domain.creative_studio`: 크리에이티브 스튜디오·디렉터·ComfyUI (Domain Plane)
- **사용자 및 비즈니스 역할**: 멀티미디어 제작(이미지/영상/음악) 디렉터 계획 수립, ComfyUI 워크플로우 렌더링, Artcraft 자산 생성
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (실측 실행 증거 없음. execute_cpu_attempt는 원본 반환 더미 lambda임)
- **현재 배포 결합도**: Core API 라우터, artcraft_tasks.session_id FK cascade, managed Comfy GPU lease 및 dynamic import 복합 결합
- **V2 제안 역할**: `보류/단계적 격리 (FK cascade, file/process coupling 선행 해소 필요)` (이전 난이도: `high`)
- **측정 가능한 기대 이점**: 세션 ID 분리 및 managed Comfy/ArtCraft 프로세스 decoupling 후 점진적 서비스화 가능 (현재 즉시 분리 불가)
- **현재 구현 상태**: `partial`
- **소유 코드 규모**: 34개 파일, 16,742 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.generation_deploy`, `core.system_routing`, `core.workspaces_transfers`, `domain.shares_artifacts`, `exec.host_gpu`, `exec.run_engine`
- **소유 DB 테이블 (1개)**: `artcraft_tasks`
- **대표 API 경로 (81개)**: `GET /api/artcraft/apps`, `POST /api/artcraft/remote-check`, `POST /api/artcraft/smoke`, `POST /api/artcraft/apps/{app}/gui`, `GET /api/artcraft/jobs` 외 76개
- **소유 MCP 도구 (27개)**: `comfy_cancel`, `comfy_job`, `comfy_jobs`, `comfy_profiles`, `comfy_readiness`, `comfy_reconcile`, `comfy_submit`, `creative_v2_auto_assets_retry`, `creative_v2_auto_assets_start`, `creative_v2_auto_assets_status`, `creative_v2_capabilities`, `creative_v2_catalog`, `creative_v2_plan`, `creative_v2_plan_approve`, `creative_v2_plan_create`, `creative_v2_plan_edit`, `creative_v2_plans`, `creative_v2_production_assets`, `creative_v2_production_dispatch`, `creative_v2_production_media_link`, `creative_v2_production_render`, `creative_v2_production_status`, `creative_v2_recipe_matrix`, `creative_v2_registries`, `creative_v2_style`, `creative_v2_style_put`, `creative_v2_styles`

### `domain.notebooklm`: NotebookLM 브라우저 연동 (Domain Plane)
- **사용자 및 비즈니스 역할**: Google NotebookLM 브라우저 세션 제어, 노트북 소스 추가/삭제, 자동 질문 질의 및 응답 수집
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (코드추론: Chromium 프로세스 메모리, DB 1건)
- **현재 배포 결합도**: Core API 및 백그라운드 브라우저 태스크
- **V2 제안 역할**: `worker` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 헤드리스 브라우저의 메모리 누수 및 좀비 프로세스 문제를 Core로부터 완전 격리
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 13개 파일, 1,913 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `domain.external_connectors`, `exec.run_engine`
- **소유 DB 테이블 (3개)**: `notebooklm_accounts`, `notebooklm_sources`, `notebooklm_jobs`
- **대표 API 경로 (38개)**: `GET /api/notebooklm/auth/status`, `POST /api/notebooklm/auth/login-window`, `POST /api/notebooklm/auth/login-window/cancel`, `POST /api/notebooklm/auth/login-window/complete`, `POST /api/notebooklm/auth/resume` 외 33개
- **소유 MCP 도구 (11개)**: `notebooklm_add_source`, `notebooklm_create`, `notebooklm_delete`, `notebooklm_delete_source`, `notebooklm_get`, `notebooklm_job`, `notebooklm_job_cancel`, `notebooklm_list`, `notebooklm_query`, `notebooklm_rename`, `notebooklm_status`

### `domain.research_lab`: 리서치 랩·실험 벤치마크 (Domain Plane)
- **사용자 및 비즈니스 역할**: 모델 벤치마크 연구 실험, 연구 노드/릴레이션 관리, 가설 검증 결과 수집
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: DB 1건)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `library` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 연구 데이터 스키마와 실험 메트릭 독립 관리
- **현재 구현 상태**: `prototype`
- **소유 코드 규모**: 7개 파일, 1,225 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `domain.knowledge_rag`, `exec.run_engine`
- **소유 DB 테이블 (5개)**: `research_studies`, `research_nodes`, `research_relations`, `research_experiments`, `research_events`
- **대표 API 경로 (15개)**: `GET /api/research/studies`, `POST /api/research/studies`, `PATCH /api/research/studies/{study_id}`, `GET /api/research/studies/{study_id}/resume`, `POST /api/research/studies/{study_id}/sources` 외 10개
- **소유 MCP 도구 (1개)**: `manage_research`

### `domain.reverse_eng`: 리버스 엔지니어링·재구성 (Domain Plane)
- **사용자 및 비즈니스 역할**: 바이너리/스크립트 디컴파일, 리버스 작업 추적, 코드 재구성 프롬프트 파이프라인
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (코드추론: 분석 연산, DB 1건)
- **현재 배포 결합도**: Core API 라우터 및 작업 큐
- **V2 제안 역할**: `job` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 헤비한 분석 작업 단독 큐 분리
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 3개 파일, 2,352 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `core.workspaces_transfers`, `exec.run_engine`
- **소유 DB 테이블 (1개)**: `reverse_jobs`
- **대표 API 경로 (11개)**: `POST /api/reverse`, `GET /api/reverse`, `GET /api/reverse/{job_id}`, `POST /api/reverse/{job_id}/retry`, `POST /api/reverse/{job_id}/messages` 외 6개
- **소유 MCP 도구 (5개)**: `reverse_get`, `reverse_list`, `reverse_message`, `reverse_retry`, `reverse_start`

### `domain.meetings_stt`: 회의록·로컬 Whisper STT (Domain Plane)
- **사용자 및 비즈니스 역할**: 음성 회의 녹음 전사(로컬 Whisper), 회의 요약, 안건/결정사항/액션아이템 추출
- **실행 형태 및 자원 특성**: `longjob` | 미측정 (실측 실행 증거 없음. 런타임 프로브 필요; 과거 추정 수치 전면 정정)
- **현재 배포 결합도**: Core API 라우터 및 6개 외래키(1 self, 5 external) 결합
- **V2 제안 역할**: `worker` (이전 난이도: `high`)
- **측정 가능한 기대 이점**: Whisper 모델 로딩 및 무거운 음성 전사 연산이 웹 요청 처리 지연을 유발하지 않도록 격리
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 24개 파일, 4,142 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.generation_deploy`, `core.system_routing`, `core.workspaces_transfers`, `domain.memory_ontology`, `domain.shares_artifacts`, `exec.run_engine`
- **소유 DB 테이블 (6개)**: `meetings`, `local_stt_jobs`, `local_stt_segments`, `meeting_items`, `meeting_proposals`, `meeting_chunks`
- **대표 API 경로 (25개)**: `GET /api/meetings/providers`, `GET /api/meetings`, `POST /api/meetings`, `GET /api/meetings/{mid}`, `GET /api/meetings/{mid}/stt` 외 20개
- **소유 MCP 도구 (4개)**: `get_meeting`, `list_meetings`, `search_meeting_items`, `search_meetings`

### `domain.pms_tasks`: PMS·마일스톤·체크리스트 (Domain Plane)
- **사용자 및 비즈니스 역할**: 프로젝트/마일스톤/태스크 관리, 반복 체크리스트 및 템플릿 관리, 진행 상태 추적
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: DB 요청당 1건)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `library` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 도메인 데이터가 명확하고 부작용이 적어 서비스화 또는 코어 라이브러리로 유지 용이
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 13개 파일, 2,374 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `core.workspaces_transfers`, `exec.run_engine`, `ops.monitoring`
- **소유 DB 테이블 (7개)**: `pms_projects`, `pms_tasks`, `pms_milestones`, `pms_notes`, `checklist_templates`, `checklist_items`, `checklist_reminders`
- **대표 API 경로 (40개)**: `GET /api/pms/projects`, `POST /api/pms/projects`, `GET /api/pms/projects/{pid}`, `PATCH /api/pms/projects/{pid}`, `DELETE /api/pms/projects/{pid}` 외 35개
- **소유 MCP 도구 (11개)**: `create_checklist_item`, `create_checklist_template`, `create_pms_task`, `delete_checklist_item`, `delete_checklist_template`, `list_checklist`, `list_checklist_templates`, `list_pms_projects`, `manage_pms`, `update_checklist_item`, `update_checklist_template`

### `domain.design_system`: 디자인 시스템 카탈로그 (Domain Plane)
- **사용자 및 비즈니스 역할**: UI 디자인 토큰, 컴포넌트 명세, 디자인 가이드 검색 및 에이전트 제공
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 인메모리 캐시, 파일시스템 기반)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `library` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 프론트엔드 빌드 산출물과 정적 파일 동기화만으로 독립 배포 가능
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 3개 파일, 514 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `exec.run_engine`
- **소유 DB 테이블 (0개)**: 없음
- **대표 API 경로 (7개)**: `GET /api/design-systems`, `GET /api/design-systems/{system}/items`, `GET /api/design-systems/{system}/item`, `GET /api/design-systems/{system}/preview`, `GET /api/agent/design-systems` 외 2개
- **소유 MCP 도구 (3개)**: `design_system_catalog`, `design_system_item`, `design_system_search`

### `domain.media_tts`: 로컬·클라우드 음성 합성(TTS) (Domain Plane)
- **사용자 및 비즈니스 역할**: 텍스트 음성 변환(TTS), 음성 파일 캐싱, 스트리밍 서빙
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 디스크 캐시 I/O, 외부 API 또는 로컬 엔진)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `service` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 음성 합성 외부 API 또는 로컬 엔진 지연이 Core 요청 쓰레드를 잠식하지 않음
- **현재 구현 상태**: `partial`
- **소유 코드 규모**: 12개 파일, 2,302 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `exec.run_engine`
- **소유 DB 테이블 (2개)**: `tts_jobs`, `tts_cache`
- **대표 API 경로 (9개)**: `GET /api/tts/status`, `GET /api/tts/voices`, `POST /api/tts/speak`, `POST /api/tts/jobs`, `GET /api/tts/jobs/{job_id}` 외 4개
- **소유 MCP 도구 (0개)**: 없음

### `domain.game_studio`: 게임 스튜디오 프로토타입 (Domain Plane)
- **사용자 및 비즈니스 역할**: HTML5/Canvas 게임 자동 생성, 샌드박스 빌드, 사용자 플레이 피드백 수집
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: DB 1건)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `library` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 게임 프로젝트 데이터 격리
- **현재 구현 상태**: `prototype`
- **소유 코드 규모**: 12개 파일, 2,955 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `domain.shares_artifacts`, `exec.run_engine`
- **소유 DB 테이블 (3개)**: `game_projects`, `game_builds`, `game_feedbacks`
- **대표 API 경로 (14개)**: `GET /api/games`, `POST /api/games`, `POST /api/games/create`, `GET /api/games/{game_id}`, `POST /api/games/{game_id}/lock/approve` 외 9개
- **소유 MCP 도구 (0개)**: 없음

### `domain.personal_tools`: 개인 일상 도구(메모·일정·날씨·주식) (Domain Plane)
- **사용자 및 비즈니스 역할**: 개인 메모, 캘린더 일정, 실시간 날씨 브리핑, 토스증권 포트폴리오 동기화
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 외부 날씨/주식 API 네트워크 I/O, DB 1건)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `library` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 개인 편의 기능들을 독립 도메인으로 묶어 비즈니스 로직과 분리
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 7개 파일, 1,724 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `core.workspaces_transfers`, `exec.run_engine`
- **소유 DB 테이블 (5개)**: `weather_briefings`, `memo_categories`, `memos`, `calendar_events`, `tossinvest_sync_runs`
- **대표 API 경로 (42개)**: `GET /api/agent/memo-categories`, `GET /api/agent/memos/tags`, `GET /api/agent/memos`, `GET /api/agent/memos/{memo_id}`, `POST /api/agent/memos` 외 37개
- **소유 MCP 도구 (13개)**: `create_event`, `create_memo`, `delete_event`, `delete_memo`, `get_memo`, `list_events`, `list_memo_categories`, `list_memo_tags`, `list_memos`, `search_memos`, `tossinvest_status`, `update_event`, `update_memo`

### `domain.external_connectors`: 외부 서비스 커넥터(Google·GitHub·RDP) (Domain Plane)
- **사용자 및 비즈니스 역할**: Google 계정 OAuth/토큰 리프레시, GitHub 저장소 탐색, 원격 데스크톱 세션 연결
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: 외부 서비스 네트워크 I/O)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `shared adapter` (이전 난이도: `medium`)
- **측정 가능한 기대 이점**: 외부 서비스 인증 프로토콜 변경 시 어댑터만 단독 업데이트
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 29개 파일, 4,328 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.system_routing`, `exec.run_engine`, `ops.monitoring`
- **소유 DB 테이블 (1개)**: `google_accounts`
- **대표 API 경로 (36개)**: `GET /api/google-accounts`, `GET /api/google-accounts/{account_id}`, `POST /api/google-accounts/{account_id}/login`, `POST /api/google-accounts/{account_id}/login/complete`, `POST /api/google-accounts/{account_id}/login/cancel` 외 31개
- **소유 MCP 도구 (1개)**: `google_account_status`

### `domain.shares_artifacts`: 결과물 공유·공개 배포 (Domain Plane)
- **사용자 및 비즈니스 역할**: 산출물(Artifacts) 저장/분류, 퍼블릭 읽기 전용 공유 링크 생성 및 서명 토큰 검증
- **실행 형태 및 자원 특성**: `request` | 미측정 (코드추론: DB 1건)
- **현재 배포 결합도**: Core API 라우터
- **V2 제안 역할**: `Core` (이전 난이도: `low`)
- **측정 가능한 기대 이점**: 사용자에게 결과물을 노출하는 정적 게이트웨이 역할
- **현재 구현 상태**: `implemented`
- **소유 코드 규모**: 14개 파일, 1,825 라인 (src_tree: `a32e4c95795a790216efe042796237fe0b948fa4`)
- **내부 의존 모듈**: `core.auth_session`, `core.generation_deploy`, `core.system_routing`, `domain.meetings_stt`, `exec.run_engine`
- **소유 DB 테이블 (2개)**: `artifacts`, `public_shares`
- **대표 API 경로 (23개)**: `GET /api/artifacts`, `POST /api/artifacts`, `POST /api/artifacts/resolve`, `GET /api/artifacts/{art_id}`, `PATCH /api/artifacts/{art_id}` 외 18개
- **소유 MCP 도구 (3개)**: `list_artifacts`, `set_artifact_area`, `share_file`
