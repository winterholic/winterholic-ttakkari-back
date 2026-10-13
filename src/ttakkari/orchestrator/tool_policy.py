"""따까리 자체 MCP 도구 중 상태를 바꾸지 않는 조회만 자동 승인한다."""

READ_ONLY_TOOLS = frozenset({
    "library_catalog", "recall", "get_entity", "list_artifacts", "list_sessions", "read_session",
    "server_resources", "list_memo_categories", "list_memo_tags", "list_memos", "search_memos", "get_memo",
    "list_events", "list_meetings", "get_meeting", "search_meeting_items",
    "account_usage", "my_context", "list_workers", "get_worker",
})
