"""조회 도구의 의미와 사용자가 위임한 PMS 관리의 실행 권한을 구분한다."""

READ_ONLY_TOOLS = frozenset({
    "library_catalog", "recall", "get_entity", "list_artifacts", "list_sessions", "read_session",
    "server_resources", "list_memo_categories", "list_memo_tags", "list_memos", "search_memos", "get_memo",
    "list_events", "list_meetings", "get_meeting", "search_meeting_items", "search_meetings",
    "list_clovanote_notes", "get_clovanote_note",
    "account_usage", "list_pms_projects", "my_context", "list_workers", "get_worker",
})

# 2026-10-14 사용자 결정: PMS/WBS는 에이전트가 전체 관리하며 변경 시 승인을 다시 요구하지 않는다.
# 쓰기를 READ_ONLY_TOOLS에 넣어 readOnlyHint로 위장하지 않는다.
PMS_TOOLS = frozenset({"manage_pms", "create_pms_task", "list_pms_projects"})
AUTO_APPROVED_TOOLS = READ_ONLY_TOOLS | PMS_TOOLS
