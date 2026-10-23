"""자체 MCP 실행 권한과 도구의 읽기·쓰기 의미를 구분한다(ADR 0003)."""

READ_ONLY_TOOLS = frozenset({
    "kb_search", "kb_documents", "kb_document", "kb_evidence",
    "list_checklist", "list_checklist_templates", "library_catalog", "recall", "get_entity", "list_artifacts", "list_sessions", "read_session",
    "server_resources", "list_memo_categories", "list_memo_tags", "list_memos", "search_memos", "get_memo",
    "list_events", "list_meetings", "get_meeting", "search_meeting_items", "search_meetings",
    "backend_deploy_status", "account_usage", "list_pms_projects", "my_context", "list_workers", "get_worker",
    "ops_list_issues", "ops_get_issue", "ops_search_logs", "ops_list_deploys", "ops_deploy_log",
    "design_system_catalog", "design_system_search", "design_system_item", "execution_capabilities",
    "luna_common30_v2_status", "knowledge_extraction_status", "knowledge_sources", "knowledge_recall", "knowledge_assertion", "knowledge_next_batch",
    "reverse_list", "reverse_get", "tossinvest_status",
    "notebooklm_status", "notebooklm_list", "notebooklm_get", "notebooklm_job", "google_account_status",
    "list_scheduled_actions",
    "comfy_profiles", "comfy_jobs", "comfy_job", "creative_v2_catalog", "creative_v2_capabilities",
    "creative_v2_recipe_matrix", "creative_v2_registries", "creative_v2_plans", "creative_v2_plan",
    "creative_v2_auto_assets_status", "creative_v2_styles", "creative_v2_style",
})
# 리버스 엔지니어링 시작은 상태를 바꾸므로 읽기 전용으로 표시하지 않는다.
# 사용자 지시로 요청하는 작업이라 무인 실행 승인 목록에는 둔다.
REVERSE_TOOLS = frozenset({"reverse_start", "reverse_retry", "reverse_message"})

# 2026-10-14 사용자 결정: PMS/WBS는 에이전트가 전체 관리하며 변경 시 승인을 다시 요구하지 않는다.
# 쓰기를 READ_ONLY_TOOLS에 넣어 readOnlyHint로 위장하지 않는다.
PMS_TOOLS = frozenset({"manage_pms", "create_pms_task", "list_pms_projects"})
# 사용자 지시에 따른 자기 배포는 예약 API에 위임하고 작업 내 직접 재시작은 하지 않는다.
DEPLOY_TOOLS = frozenset({"request_backend_deploy"})
# 사용자가 일일·고정 체크리스트 전체 관리를 MCP에 명시적으로 위임했다.
CHECKLIST_TOOLS = frozenset({"create_checklist_item", "update_checklist_item", "delete_checklist_item",
                             "create_checklist_template", "update_checklist_template", "delete_checklist_template"})
# 사용자 지시에 따른 자체 앱 관리는 무인 실행에서 승인 프롬프트를 만들지 않는다.
# 실제 권한은 API의 세션 토큰·역할·범위 검사로 제한한다. 쓰기를 readOnlyHint로 위장하지 않는다.
ASSISTANT_WRITE_TOOLS = frozenset({
    "knowledge_retry_extraction", "knowledge_start_extraction", "knowledge_submit_batch", "knowledge_review_assertion",
    "remember", "describe_entity", "link", "forget", "share_file", "set_artifact_area", "notify_user",
    "create_memo", "update_memo", "delete_memo", "create_event", "update_event", "delete_event",
})
# 운영 로그 이슈의 해결·메모. 조회는 READ_ONLY_TOOLS, 개인 세션 한정은 API가 검사한다(ADR 0033).
OPS_LOG_TOOLS = frozenset({"ops_update_issue"})
# NotebookLM 쓰기·질의(ADR 0045). 사용자 지시로 쓰는 도구라 승인 프롬프트를 만들지 않고, 삭제는 confirm=true 를 요구한다.
# 개인 세션 한정과 계정 상태·쿨다운 검사는 API 가 한다. 쓰기를 readOnlyHint 로 위장하지 않는다.
NOTEBOOKLM_TOOLS = frozenset({"notebooklm_create", "notebooklm_rename", "notebooklm_delete", "notebooklm_add_source",
                              "notebooklm_delete_source", "notebooklm_query", "notebooklm_job_cancel"})
# 엔진 무관 실행 환경 보고와 호스트 GPU 시험 경로(ADR 0037). 상태 조회도 고아 정리를 하므로 읽기 전용으로 표시하지 않는다.
HOST_ENV_TOOLS = frozenset({"begin_environment_probe", "report_execution_environment", "host_gpu_start",
                            "host_gpu_status", "host_gpu_bench", "host_gpu_stop"})
WORKER_MANAGEMENT_TOOLS = frozenset({"spawn_worker", "message_worker", "stop_worker", "stop_agents", "reset_my_context"})
BENCHMARK_TOOLS = frozenset({"start_luna_common30_v2", "luna_common30_v2_cancel"})
# Research Lab: 연구 지식·실험·재개 관리(ADR 0056)
RESEARCH_TOOLS = frozenset({"manage_research"})
SCHEDULE_TOOLS = frozenset({"schedule_action", "cancel_scheduled_action"})
# Creative Studio and managed Comfy are explicit personal-session services. Plan approval,
# fixed profiles, host leases and runtime ownership remain enforced by their service layer.
CREATIVE_TOOLS = frozenset({
    "comfy_cancel", "comfy_readiness", "comfy_reconcile", "comfy_submit",
    "creative_v2_plan_create", "creative_v2_plan_edit", "creative_v2_plan_approve", "creative_v2_style_put",
    "creative_v2_auto_assets_start", "creative_v2_auto_assets_retry",
    "creative_v2_production_assets", "creative_v2_production_dispatch", "creative_v2_production_render",
    "creative_v2_production_media_link", "creative_v2_production_status",
})
AUTO_APPROVED_TOOLS = (READ_ONLY_TOOLS | PMS_TOOLS | DEPLOY_TOOLS | CHECKLIST_TOOLS | OPS_LOG_TOOLS
                       | ASSISTANT_WRITE_TOOLS | NOTEBOOKLM_TOOLS | HOST_ENV_TOOLS | WORKER_MANAGEMENT_TOOLS
                       | BENCHMARK_TOOLS | REVERSE_TOOLS | SCHEDULE_TOOLS | RESEARCH_TOOLS | CREATIVE_TOOLS)
