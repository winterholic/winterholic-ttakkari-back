# 따까리 플랫폼 V2 데이터 소유권 및 분리 후보 분석 (Data Ownership - Corrected)
> 기준 소스 SHA: `bbf62d5c1bd923dfc0c692692703b7cf5b7182c7` | 감사 시각: 2026-10-24 | 교정 검증: P1 B Backend 감사 교정

## 1. 개요 및 데이터 소유권 원칙 (Data Ownership Principles)
플랫폼 V2는 모듈러 모놀리스 및 점진적 서비스 분리를 지향하며, 다음 불변식을 준수한다:
1. **단일 소유권 원칙**: 각 DB 테이블은 오직 하나의 도메인 모듈에만 소유된다 (소유 모듈만 직접 DDL/DML 수행).
2. **명시적 계약 소비 (Contract-based Access)**: 타 모듈 데이터 접근 시 직접 SQL JOIN을 지양하고, API/서비스 인터페이스 또는 뷰/이벤트를 통해 소비한다.
3. **공유 신원(Identity) 격리**: User, Workspace, Session, Run 등 플랫폼 공통 Identity는 Control/Execution Plane이 소유하며, 도메인 모듈은 외래키 제약 대신 단순 ID(UUID/String) 참조로 결합도를 낮춘다.

## 2. 모듈별 DB 테이블 소유권 및 외래키 분석 (Table Ownership Ledger)

| 테이블명 | 소유 모듈 ID | 컬럼 수 | 외래키(FK) 참조 대상 | 데이터 결합도 (Coupling) |
|---|---|---|---|---|
| `credentials` | `core.auth_session` | 3 | `None` | Low (독립 테이블) |
| `refresh_tokens` | `core.auth_session` | 9 | `None` | Low (독립 테이블) |
| `login_throttle` | `core.auth_session` | 11 | `None` | Low (독립 테이블) |
| `login_bucket` | `core.auth_session` | 4 | `None` | Low (독립 테이블) |
| `login_attempts` | `core.auth_session` | 8 | `None` | Low (독립 테이블) |
| `sessions` | `core.auth_session` | 16 | `workspace_id->workspaces.id, parent_id->sessions.id` | High (외래키 결합) |
| `audit_logs` | `core.auth_session` | 10 | `None` | Low (독립 테이블) |
| `workspaces` | `core.workspaces_transfers` | 10 | `None` | Low (독립 테이블) |
| `transfer_tokens` | `core.workspaces_transfers` | 6 | `None` | Low (독립 테이블) |
| `engine_accounts` | `core.accounts_quota` | 12 | `None` | Low (독립 테이블) |
| `account_usage_samples` | `core.accounts_quota` | 8 | `account_id->engine_accounts.id` | High (외래키 결합) |
| `qwen_token_usage` | `core.accounts_quota` | 10 | `run_id->runs.id` | High (외래키 결합) |
| `qwen_flash_usage` | `core.accounts_quota` | 9 | `None` | Low (독립 테이블) |
| `runtime_generations` | `core.generation_deploy` | 11 | `None` | Low (독립 테이블) |
| `runtime_control` | `core.generation_deploy` | 4 | `active_generation->runtime_generations.id, executor_generation->runtime_generations.id` | High (외래키 결합) |
| `runtime_commands` | `core.generation_deploy` | 8 | `generation_id->runtime_generations.id, run_id->runs.id, session_id->sessions.id` | High (외래키 결합) |
| `app_settings` | `core.system_routing` | 3 | `None` | Low (독립 테이블) |
| `desk_feedback` | `core.system_routing` | 9 | `run_id->runs.id, session_id->sessions.id` | High (외래키 결합) |
| `notifications` | `core.system_routing` | 9 | `None` | Low (독립 테이블) |
| `push_subscriptions` | `core.system_routing` | 8 | `None` | Low (독립 테이블) |
| `ops_issues` | `ops.monitoring` | 19 | `None` | Low (독립 테이블) |
| `ops_events` | `ops.monitoring` | 13 | `issue_id->ops_issues.id` | High (외래키 결합) |
| `ops_event_receipts` | `ops.monitoring` | 2 | `None` | Low (독립 테이블) |
| `ops_issue_stats` | `ops.monitoring` | 3 | `issue_id->ops_issues.id` | High (외래키 결합) |
| `ops_ingest_cursors` | `ops.monitoring` | 4 | `None` | Low (독립 테이블) |
| `runs` | `exec.run_engine` | 28 | `workspace_id->workspaces.id, session_id->sessions.id` | High (외래키 결합) |
| `run_events` | `exec.run_engine` | 6 | `run_id->runs.id` | High (외래키 결합) |
| `runtime_leases` | `exec.run_engine` | 7 | `session_id->sessions.id, generation_id->runtime_generations.id, run_id->runs.id, account_id->engine_accounts.id` | High (외래키 결합) |
| `runtime_process_groups` | `exec.run_engine` | 9 | `generation_id->runtime_generations.id, run_id->runs.id` | High (외래키 결합) |
| `retired_integration_records` | `exec.run_engine` | 4 | `None` | Low (독립 테이블) |
| `scheduled_actions` | `exec.durable_scheduler` | 15 | `None` | Low (독립 테이블) |
| `system_resource_samples` | `exec.host_gpu` | 8 | `None` | Low (독립 테이블) |
| `change_sets` | `delivery.git_cas` | 27 | `None` | Low (독립 테이블) |
| `ctx_entities` | `domain.memory_ontology` | 11 | `None` | Low (독립 테이블) |
| `ctx_relations` | `domain.memory_ontology` | 7 | `object_id->ctx_entities.id, subject_id->ctx_entities.id` | High (외래키 결합) |
| `ctx_memories` | `domain.memory_ontology` | 12 | `None` | Low (독립 테이블) |
| `ctx_sources` | `domain.memory_ontology` | 10 | `entity_id->ctx_entities.id` | High (외래키 결합) |
| `ctx_documents` | `domain.memory_ontology` | 7 | `source_id->ctx_sources.id` | High (외래키 결합) |
| `ctx_chunks` | `domain.memory_ontology` | 6 | `document_id->ctx_documents.id` | High (외래키 결합) |
| `kb_sources` | `domain.knowledge_rag` | 11 | `None` | Low (독립 테이블) |
| `kb_documents` | `domain.knowledge_rag` | 6 | `None` | Low (독립 테이블) |
| `kb_revisions` | `domain.knowledge_rag` | 12 | `document_id->kb_documents.id, source_id->kb_sources.id, supersedes_id->kb_revisions.id` | High (외래키 결합) |
| `kb_passages` | `domain.knowledge_rag` | 9 | `revision_id->kb_revisions.id` | High (외래키 결합) |
| `kb_evidence` | `domain.knowledge_rag` | 7 | `passage_id->kb_passages.id` | High (외래키 결합) |
| `knowledge_revisions` | `domain.knowledge_rag` | 11 | `source_id->ctx_sources.id` | High (외래키 결합) |
| `knowledge_units` | `domain.knowledge_rag` | 15 | `revision_id->knowledge_revisions.id` | High (외래키 결합) |
| `knowledge_evidence` | `domain.knowledge_rag` | 6 | `unit_id->knowledge_units.id` | High (외래키 결합) |
| `knowledge_assertions` | `domain.knowledge_rag` | 18 | `supersedes_id->knowledge_assertions.id, subject_entity_id->ctx_entities.id, object_entity_id->ctx_entities.id` | High (외래키 결합) |
| `knowledge_assertion_evidence` | `domain.knowledge_rag` | 3 | `assertion_id->knowledge_assertions.id, evidence_id->knowledge_evidence.id` | High (외래키 결합) |
| `knowledge_review_events` | `domain.knowledge_rag` | 8 | `assertion_id->knowledge_assertions.id` | High (외래키 결합) |
| `knowledge_extraction_jobs` | `domain.knowledge_rag` | 11 | `revision_id->knowledge_revisions.id, run_id->runs.id` | High (외래키 결합) |
| `kb_document_access` | `domain.knowledge_rag` | 4 | `document_id->kb_documents.id` | High (외래키 결합) |
| `kb_observations` | `domain.knowledge_rag` | 7 | `revision_id->kb_revisions.id, document_id->kb_documents.id` | High (외래키 결합) |
| `kb_passage_vectors` | `domain.knowledge_rag` | 5 | `passage_id->kb_passages.id` | High (외래키 결합) |
| `collector_jobs` | `domain.collector` | 21 | `None` | Low (독립 테이블) |
| `collector_outbox` | `domain.collector` | 13 | `job_id->collector_jobs.id` | High (외래키 결합) |
| `collector_source_policies` | `domain.collector` | 8 | `None` | Low (독립 테이블) |
| `collector_host_state` | `domain.collector` | 9 | `None` | Low (독립 테이블) |
| `collector_fetch_cache` | `domain.collector` | 11 | `None` | Low (독립 테이블) |
| `collector_items` | `domain.collector` | 37 | `job_id->collector_jobs.id` | High (외래키 결합) |
| `collector_dedupe_bands` | `domain.collector` | 5 | `item_id->collector_items.id` | High (외래키 결합) |
| `collector_budget_buckets` | `domain.collector` | 9 | `None` | Low (독립 테이블) |
| `collector_budget_reservations` | `domain.collector` | 14 | `None` | Low (독립 테이블) |
| `collector_cache` | `domain.collector` | 9 | `None` | Low (독립 테이블) |
| `collector_runtime_flags` | `domain.collector` | 3 | `None` | Low (독립 테이블) |
| `collector_sources` | `domain.collector` | 15 | `last_job_id->collector_jobs.id` | High (외래키 결합) |
| `collector_deliveries` | `domain.collector` | 6 | `outbox_id->collector_outbox.id, revision_id->kb_revisions.id, item_id->collector_items.id` | High (외래키 결합) |
| `collector_daily_budgets` | `domain.collector` | 2 | `None` | Low (독립 테이블) |
| `collector_reviews` | `domain.collector` | 7 | `item_id->collector_items.id` | High (외래키 결합) |
| `collector_quality_models` | `domain.collector` | 6 | `None` | Low (독립 테이블) |
| `artcraft_tasks` | `domain.creative_studio` | 10 | `session_id->sessions.id` | High (외래키 결합) |
| `notebooklm_accounts` | `domain.notebooklm` | 10 | `None` | Low (독립 테이블) |
| `notebooklm_sources` | `domain.notebooklm` | 11 | `None` | Low (독립 테이블) |
| `notebooklm_jobs` | `domain.notebooklm` | 15 | `None` | Low (독립 테이블) |
| `research_studies` | `domain.research_lab` | 11 | `None` | Low (독립 테이블) |
| `research_nodes` | `domain.research_lab` | 11 | `supersedes_id->research_nodes.id, evidence_id->kb_evidence.id, study_id->research_studies.id` | High (외래키 결합) |
| `research_relations` | `domain.research_lab` | 8 | `from_id->research_nodes.id, to_id->research_nodes.id, study_id->research_studies.id` | High (외래키 결합) |
| `research_experiments` | `domain.research_lab` | 14 | `study_id->research_studies.id` | High (외래키 결합) |
| `research_events` | `domain.research_lab` | 6 | `study_id->research_studies.id` | High (외래키 결합) |
| `reverse_jobs` | `domain.reverse_eng` | 14 | `verifier_session_id->sessions.id, session_id->sessions.id, workspace_id->workspaces.id` | High (외래키 결합) |
| `meetings` | `domain.meetings_stt` | 28 | `pms_project_id->pms_projects.id, analysis_run_id->runs.id, entity_id->ctx_entities.id, analysis_session_id->sessions.id, source_meeting_id->meetings.id, audio_artifact_id->artifacts.id` | High (외래키 결합) |
| `local_stt_jobs` | `domain.meetings_stt` | 12 | `meeting_id->meetings.id, parent_job_id->local_stt_jobs.id, audio_artifact_id->artifacts.id` | High (외래키 결합) |
| `local_stt_segments` | `domain.meetings_stt` | 17 | `job_id->local_stt_jobs.id` | High (외래키 결합) |
| `meeting_items` | `domain.meetings_stt` | 13 | `meeting_id->meetings.id` | High (외래키 결합) |
| `meeting_proposals` | `domain.meetings_stt` | 10 | `item_id->meeting_items.id, meeting_id->meetings.id` | High (외래키 결합) |
| `meeting_chunks` | `domain.meetings_stt` | 11 | `entity_id->ctx_entities.id, meeting_id->meetings.id` | High (외래키 결합) |
| `pms_projects` | `domain.pms_tasks` | 10 | `None` | Low (독립 테이블) |
| `pms_tasks` | `domain.pms_tasks` | 16 | `parent_id->pms_tasks.id, project_id->pms_projects.id` | High (외래키 결합) |
| `pms_milestones` | `domain.pms_tasks` | 6 | `project_id->pms_projects.id` | High (외래키 결합) |
| `pms_notes` | `domain.pms_tasks` | 12 | `project_id->pms_projects.id, task_id->pms_tasks.id` | High (외래키 결합) |
| `checklist_templates` | `domain.pms_tasks` | 4 | `None` | Low (독립 테이블) |
| `checklist_items` | `domain.pms_tasks` | 7 | `template_id->checklist_templates.id` | High (외래키 결합) |
| `checklist_reminders` | `domain.pms_tasks` | 3 | `None` | Low (독립 테이블) |
| `tts_jobs` | `domain.media_tts` | 9 | `None` | Low (독립 테이블) |
| `tts_cache` | `domain.media_tts` | 13 | `None` | Low (독립 테이블) |
| `game_projects` | `domain.game_studio` | 11 | `pms_project_id->pms_projects.id, workspace_id->workspaces.id` | High (외래키 결합) |
| `game_builds` | `domain.game_studio` | 19 | `run_id->runs.id, restored_from_id->game_builds.id, artifact_id->artifacts.id, game_id->game_projects.id` | High (외래키 결합) |
| `game_feedbacks` | `domain.game_studio` | 9 | `screenshot_artifact_id->artifacts.id, game_id->game_projects.id, run_id->runs.id, build_id->game_builds.id` | High (외래키 결합) |
| `weather_briefings` | `domain.personal_tools` | 22 | `session_id->sessions.id, run_id->runs.id` | High (외래키 결합) |
| `memo_categories` | `domain.personal_tools` | 5 | `None` | Low (독립 테이블) |
| `memos` | `domain.personal_tools` | 12 | `category_id->memo_categories.id` | High (외래키 결합) |
| `calendar_events` | `domain.personal_tools` | 16 | `None` | Low (독립 테이블) |
| `tossinvest_sync_runs` | `domain.personal_tools` | 11 | `None` | Low (독립 테이블) |
| `google_accounts` | `domain.external_connectors` | 11 | `None` | Low (독립 테이블) |
| `artifacts` | `domain.shares_artifacts` | 23 | `session_id->sessions.id, run_id->runs.id, workspace_id->workspaces.id` | High (외래키 결합) |
| `public_shares` | `domain.shares_artifacts` | 19 | `None` | Low (독립 테이블) |

## 3. 크로스 모듈 외래키 결합 현황 (Cross-Module Foreign Keys)

| 소스 테이블 | 소스 모듈 | 참조 테이블 | 참조 대상 모듈 | 외래키 컬럼 | 결합 영향 및 분리 장애 |
|---|---|---|---|---|---|
| `sessions` | `core.auth_session` | `workspaces` | `core.workspaces_transfers` | `workspace_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `qwen_token_usage` | `core.accounts_quota` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runtime_commands` | `core.generation_deploy` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runtime_commands` | `core.generation_deploy` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `desk_feedback` | `core.system_routing` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `desk_feedback` | `core.system_routing` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runs` | `exec.run_engine` | `workspaces` | `core.workspaces_transfers` | `workspace_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runs` | `exec.run_engine` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runtime_leases` | `exec.run_engine` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runtime_leases` | `exec.run_engine` | `runtime_generations` | `core.generation_deploy` | `generation_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runtime_leases` | `exec.run_engine` | `engine_accounts` | `core.accounts_quota` | `account_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `runtime_process_groups` | `exec.run_engine` | `runtime_generations` | `core.generation_deploy` | `generation_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `knowledge_revisions` | `domain.knowledge_rag` | `ctx_sources` | `domain.memory_ontology` | `source_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `knowledge_assertions` | `domain.knowledge_rag` | `ctx_entities` | `domain.memory_ontology` | `subject_entity_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `knowledge_assertions` | `domain.knowledge_rag` | `ctx_entities` | `domain.memory_ontology` | `object_entity_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `knowledge_extraction_jobs` | `domain.knowledge_rag` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `collector_deliveries` | `domain.collector` | `kb_revisions` | `domain.knowledge_rag` | `revision_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `artcraft_tasks` | `domain.creative_studio` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `research_nodes` | `domain.research_lab` | `kb_evidence` | `domain.knowledge_rag` | `evidence_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `reverse_jobs` | `domain.reverse_eng` | `sessions` | `core.auth_session` | `verifier_session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `reverse_jobs` | `domain.reverse_eng` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `reverse_jobs` | `domain.reverse_eng` | `workspaces` | `core.workspaces_transfers` | `workspace_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `meetings` | `domain.meetings_stt` | `pms_projects` | `domain.pms_tasks` | `pms_project_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `meetings` | `domain.meetings_stt` | `runs` | `exec.run_engine` | `analysis_run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `meetings` | `domain.meetings_stt` | `ctx_entities` | `domain.memory_ontology` | `entity_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `meetings` | `domain.meetings_stt` | `sessions` | `core.auth_session` | `analysis_session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `meetings` | `domain.meetings_stt` | `artifacts` | `domain.shares_artifacts` | `audio_artifact_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `local_stt_jobs` | `domain.meetings_stt` | `artifacts` | `domain.shares_artifacts` | `audio_artifact_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `meeting_chunks` | `domain.meetings_stt` | `ctx_entities` | `domain.memory_ontology` | `entity_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `game_projects` | `domain.game_studio` | `pms_projects` | `domain.pms_tasks` | `pms_project_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `game_projects` | `domain.game_studio` | `workspaces` | `core.workspaces_transfers` | `workspace_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `game_builds` | `domain.game_studio` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `game_builds` | `domain.game_studio` | `artifacts` | `domain.shares_artifacts` | `artifact_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `game_feedbacks` | `domain.game_studio` | `artifacts` | `domain.shares_artifacts` | `screenshot_artifact_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `game_feedbacks` | `domain.game_studio` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `weather_briefings` | `domain.personal_tools` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `weather_briefings` | `domain.personal_tools` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `artifacts` | `domain.shares_artifacts` | `sessions` | `core.auth_session` | `session_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `artifacts` | `domain.shares_artifacts` | `runs` | `exec.run_engine` | `run_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |
| `artifacts` | `domain.shares_artifacts` | `workspaces` | `core.workspaces_transfers` | `workspace_id` | DDL 순서 종속, 삭제 cascade, 트랜잭션 경합 |

## 4. 스키마 진화 및 마이그레이션 DDL 영향 분석

### 4.1 Alembic 62개 마이그레이션 체인 및 DDL 분석
- **체인 구조**: Alembic `ScriptDirectory` 기준 62 revisions, 단일 base `0001`, 단일 head `0062_delivery_change_sets`로 선형 구성됨.
- **복원된 down_revision**: 타입 주석형 대입(`down_revision: str | None = '...'`) 24건을 AST로 복원하여 revision chain 무결성을 확보함.
- **DDL 효과**: 총 78개 테이블 생성, 2개 테이블 삭제(`clovanote_jobs`, `clovanote_notes`), 인덱스 및 FK 생성 포함.
- **DDL AST Call-site Inventory (op.execute)**: 총 66건의 call-site가 감지되었으며, 이는 AST 호출 위치의 집계이며 런타임 SQL 실행 의미·파티셔닝·트리거 동작·downgrade 영향이 해석된 것이 아님 (정적 문자열/파라미터화/보간 분류 제공).

### 4.2 주요 도메인별 결합 분석 교정

1. **Creative Studio (`artcraft_tasks`) 데이터 결합 확인**:
   - **정정 사항**: 이전 감사의 '외래키 없음 / DB 결합 제로' 주장은 허위임.
   - **실제 외래키**: `artcraft_tasks.session_id -> sessions.id (ON DELETE CASCADE, unique=True)`가 명시적으로 선언되어 있음.
   - **수명주기 결합**: `ChatSession` 삭제 시 `artcraft_tasks`가 cascade 삭제되며, 세션 조회/정리 로직이 `ArtcraftTask`에 직접 결합되어 있음.

2. **회의록(Meetings) 다중 외래키 결합 (6개 FK 필드)**:
   - `Meeting` 테이블은 자기 참조 1개(`source_meeting_id -> meetings.id`)와 5개 외부 모듈 참조를 가짐:
     - `entity_id -> ctx_entities.id` (`domain.memory_ontology`)
     - `pms_project_id -> pms_projects.id` (`domain.pms_tasks`)
     - `audio_artifact_id -> artifacts.id` (`domain.shares_artifacts`)
     - `analysis_run_id -> runs.id` (`exec.run_engine`)
     - `analysis_session_id -> sessions.id` (`core.auth_session`)
   - 회의록은 5개 상이한 모듈과 DB 수준에서 직결되어 있어 독립 분리 시 정규화 및 느슨한 결합 전환이 선행되어야 함.

3. **수집기(Collector Platform) 결합 분석**:
   - `collector_jobs`, `collector_outbox` 테이블 자체는 `job_id -> collector_jobs.id`로 내부 결합되어 있음.
   - 그러나 에이전트 intake 경로는 `ChatSession`을 읽어 scope를 판정하고, HTTP 요청이 job 등록 및 commit을 동기 수행하므로 완전한 독립 런타임이 아님.

## 5. 우선 분리 후보 비교 분석 (Creative Studio 즉시 분리 권고 철회 및 재평가)

> [!WARNING] **Creative Studio 즉시 독립 배포 권고 철회**
> 이전 보고의 'Creative Studio DB 결합 제로 / 즉시 독립 배포 최우선 후보' 결론을 전면 철회합니다.
> 실제 코드 조사 결과, Creative Studio는 데이터베이스, 파일시스템, 외부 라이브러리 동적 로딩, GPU Lease, 렌더링 람다에서 복합 결합을 가지고 있어 P1 단계에서 즉시 분리할 수 없습니다.

| 평가 항목 | **후보 1: Creative Studio (`domain.creative_studio`)** (보류/재평가) | **후보 2: Collector Platform (`domain.collector`)** | **후보 3: Meeting STT (`domain.meetings_stt`)** |
|---|---|---|---|
| **비즈니스 특성** | 멀티미디어(이미지/영상/음악) 디렉터 계획 및 ComfyUI/Artcraft 자산 생성 | 대규모 웹 문서 크롤링 및 Stage 정제 파이프라인 | Whisper 기반 음성 전사, 화자 분리, 회의 요약 |
| **Data Coupling** | **높음 (교정됨)**: `artcraft_tasks.session_id -> sessions.id` ON DELETE CASCADE 결합. 세션 삭제 시 트랜잭션 직결 | **중간**: `collector_jobs`, `collector_outbox` 2개 소유. ChatSession 읽기 결합 | **매우 높음**: `meetings` 테이블에 6개 FK (1 self, 5 external) 결합 |
| **Resource 점유** | 미측정 (실측 실행 증거 없음; execute_cpu_attempt는 원본 반환 더미 lambda임) | 미측정 (네트워크 I/O 및 비동기 백그라운드 태스크 부하 실측 증거 없음) | 미측정 (Whisper 모델 로딩 및 연산 자원 실측 증거 없음) |
| **수명주기 결합** | ArtCraft의 파일시스템 고정 경로, state.json, managed Comfy의 GPU lease 및 동적 import 결합 | 장기 배치 워커, Core와 HTTP 등록 결합 | 회의 분석 시 Run 및 Session 수명주기와 결합 |
| **Core 무배포 분리 적합도** | **보류 (즉시 분리 불가, 사전 디커플링 필수)** | **양호 (워커 프로세스 분리 적합)** | **보류 (데이터 정규화 및 FK 해체 선행 필요)** |

### 5.1 Creative Studio 결합 요인 상세 분석
1. **DB FK 결합**: `artcraft_tasks`가 `sessions.id`를 ON DELETE CASCADE로 직접 참조하여 세션 라이프사이클에 종속됨.
2. **호스트 파일시스템 결합**: ArtCraft 작업 디렉토리 고정 경로(`/Users/winterholic/.ttakkari/...`), subprocess/server 프로세스 상태 관리, `data_dir/artcraft/state.json` 영속화 결합.
3. **Managed Comfy의 런타임 결합**: 외부 `ttk_comfy.*` 모듈 동적 임포트(5건), `ResourceLeaseManager`를 통한 Core 내부 GPU Lease 점유.
4. **더미 렌더러 분리 한계**: `execute_cpu_attempt`는 현재 원본을 돌려주는 더미 lambda 경로로 동작하며, 실제 외부 엔진 렌더 파이프라인과 엄격히 구별되어야 함.
5. **자산 전달 계약**: `/api/creative`의 production/assets 파일은 Core의 세션/아티팩트 및 다운로드 토큰(`transfers.tokens`)과 밀결합됨.

### 5.2 교정 권고안
- Creative Studio의 즉시 독립 분리 추천을 철회하고, **보류(Hold)** 판정으로 변경함.
- 향후 분리를 위해서는: 1) `session_id` FK를 논리 ID로 전환, 2) ArtCraft 고정 경로를 워크스페이스 스토리지 인터페이스로 추상화, 3) GPU Lease를 gRPC/HTTP 리스 에이전트로 분리하는 작업이 선행되어야 함.
- 분리 후보로서 가장 위험도가 낮고 독립적인 대상은 백그라운드 워커 형태의 **Collector Platform (`domain.collector`)**임.