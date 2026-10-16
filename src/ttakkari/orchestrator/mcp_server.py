"""모든 Run 에 붙는 stdio MCP 서버 `ttakkari`(ADR 0006·0007). 엔진이 띄우고 백엔드 API 를 부른다.

env: TTK_ORCH_TOKEN(세션 한정 토큰), TTK_API_URL(백엔드 주소), TTK_ROLE(agent | orchestrator). 직접 실행하지 않는다.
작업 세션 도구는 orchestrator 일 때만 등록한다(목록에 안 보이게).
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ttakkari.orchestrator.tool_policy import READ_ONLY_TOOLS
from ttakkari.pms import PmsOperation

ROLE = os.environ.get("TTK_ROLE", "orchestrator")
server = MCPServer(
    name="ttakkari",
    instructions="The user's own Ttakkari app: long-term memory about the user (recall/remember/link), files "
                 "(list_artifacts/share_file), notifications, other conversations, and the user's memos and calendar "
                 "(Asia/Seoul)."
                 + (" As orchestrator, delegate work with spawn_worker; results come back as '[작업 보고]' messages, "
                    "so do not poll." if ROLE == "orchestrator" else ""),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def query_tool(fn):
    return server.tool(annotations=READ_ONLY)(fn)


async def _call(method: str, path: str, body: dict[str, Any] | None = None, *, prefix: str = "/api/orchestrator",
                params: dict[str, Any] | None = None) -> str:
    api = os.environ.get("TTK_API_URL", "http://127.0.0.1:8787").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('TTK_ORCH_TOKEN', '')}"}
    async with httpx.AsyncClient(base_url=api, headers=headers, timeout=30) as c:
        r = await c.request(method, f"{prefix}{path}", json=body,
                            params={k: v for k, v in (params or {}).items() if v is not None} or None)
    try:
        data = r.json()
    except ValueError:
        data = {"message": r.text[:500]}
    if r.is_error:
        # 오류도 문자열로 돌려줘 모델이 읽고 대응하게 한다.
        return json.dumps({"error": r.status_code, **(data if isinstance(data, dict) else {"detail": data})},
                          ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


# ---- 공통(모든 Run)

async def _agent(method: str, path: str, body: dict[str, Any] | None = None, **params: Any) -> str:
    return await _call(method, path, body, prefix="/api/agent", params=params)


@query_tool
async def library_catalog() -> str:
    """Browse context library categories, knowledge/work-history collections and project landmarks before choosing
    what to recall. Returns names, aliases, summaries and verified source paths; no document bodies."""
    return await _agent("GET", "/library")


@query_tool
async def recall(query: str, k: int = 8) -> str:
    """Search saved facts, entities/relations, indexed documents and meeting minutes (type "meeting": summary, decisions,
    action items and transcript passages; these also match by meaning via local vectors). Use project names or aliases
    and a few concrete keywords. Hits include source paths/meeting ids and timestamps; read the original for detail
    (get_meeting for meetings), and try different keywords when terminology differs."""
    return await _agent("POST", "/recall", {"query": query, "k": k})


@server.tool()
async def remember(content: str, kind: Literal["fact", "preference", "decision", "event", "instruction", "note"] = "fact",
                   entities: list[str] | None = None) -> str:
    """Save one durable fact about the user or their work, written to stand alone. `entities` are names it is
    about (people, projects, tools); they are created if missing. Never store secrets."""
    return await _agent("POST", "/remember", {"content": content, "kind": kind, "entities": entities or []})


@query_tool
async def get_entity(name: str) -> str:
    """Look up a person/project/tool/topic by name or alias, with its relations and linked memories."""
    return await _agent("GET", f"/entities/{name}")


@server.tool()
async def describe_entity(name: str, type: Literal["person", "project", "org", "place", "tool", "topic", "preference", "other"] = "other",
                          summary: str | None = None, aliases: list[str] | None = None) -> str:
    """Create or update an entity (name, type, one-paragraph summary, aliases)."""
    return await _agent("POST", "/entities", {"name": name, "type": type, "summary": summary, "aliases": aliases or []})


@server.tool()
async def link(subject: str, predicate: str, object: str, note: str | None = None) -> str:
    """Record a relation between two entities, e.g. link('winterholic', 'works_on', 'Ttakkari')."""
    return await _agent("POST", "/link", {"subject": subject, "predicate": predicate, "object": object, "note": note})


@server.tool()
async def forget(memory_id: str) -> str:
    """Archive a memory you saved that turned out wrong or outdated (user-entered memories cannot be removed)."""
    return await _agent("POST", "/forget", {"id": memory_id})


@query_tool
async def list_artifacts(query: str | None = None, kind: str | None = None, limit: int = 20,
                         area: Literal["files", "agent"] | None = None) -> str:
    """List files in the user's library (results and uploads), newest first, with their paths on disk.
    `area` keeps one tab: "files" (uploads and files handed to the user) or "agent" (files made while working)."""
    return await _agent("GET", "/artifacts", q=query, kind=kind, limit=limit, area=area)


@server.tool()
async def set_artifact_area(artifact_id: str, area: Literal["files", "agent"]) -> str:
    """Move a library file between the "files" and "agent" tabs (reversible). Files the user uploaded or registered
    cannot be moved by you."""
    return await _agent("PATCH", f"/artifacts/{artifact_id}", {"area": area})


@server.tool()
async def share_file(path: str) -> str:
    """Put a file (inside the workspace, permission folders or inbox) into the user's library so they can open
    and download it on their phone."""
    return await _agent("POST", "/share", {"path": path})


@server.tool()
async def notify_user(title: str, body: str = "") -> str:
    """Send a push notification to the user's phone. Only for things they must see now."""
    return await _agent("POST", "/notify", {"title": title, "body": body})


@query_tool
async def list_sessions(limit: int = 20) -> str:
    """List the user's recent conversations in Ttakkari."""
    return await _agent("GET", "/sessions", limit=limit)


@query_tool
async def read_session(session_id: str, last: int = 10) -> str:
    """Read the last turns (instruction and result) of another conversation."""
    return await _agent("GET", f"/sessions/{session_id}", last=last)


@query_tool
async def server_resources() -> str:
    """Current load of the Mac running Ttakkari: CPU, memory, disk, network and top processes (names only).
    Check it before starting heavy work."""
    return await _agent("GET", "/system/resources")


# ---- 메모·캘린더(ADR 0008). 시각은 모두 Asia/Seoul 이다.

@query_tool
async def list_memo_categories() -> str:
    """List the user's memo categories in their order. A memo belongs to at most one category (or none)."""
    return await _agent("GET", "/memo-categories")


@query_tool
async def list_memo_tags(category: str | None = None) -> str:
    """List memo tags with how many memos use each (most used first), to reuse existing tags. `category` is a
    category name, or "none" for uncategorized memos."""
    return await _agent("GET", "/memos/tags", category=category)


@query_tool
async def list_memos(tags: list[str] | None = None, category: str | None = None, limit: int = 30) -> str:
    """List the user's memos (pinned first, then newest). Returns title, category, tags and a short excerpt; use
    get_memo for the full Markdown body. `tags` keeps memos that have all of them; `category` is a category name,
    or "none" for uncategorized memos."""
    return await _agent("GET", "/memos", tag=tags or None, category=category, limit=limit)


@query_tool
async def search_memos(query: str, tags: list[str] | None = None, category: str | None = None, limit: int = 20) -> str:
    """Search memos by words in the title, body or tags, optionally narrowed by `tags` (all must match) and
    `category` (a name, or "none" for uncategorized)."""
    return await _agent("GET", "/memos", q=query, tag=tags or None, category=category, limit=limit)


@query_tool
async def get_memo(memo_id: str) -> str:
    """Read one memo with its full Markdown body and current `rev`."""
    return await _agent("GET", f"/memos/{memo_id}")


@server.tool()
async def create_memo(body: str, title: str = "", tags: list[str] | None = None, category: str | None = None,
                      pinned: bool = False) -> str:
    """Save a new memo (Markdown). Use for 'jot this down' requests. Give a short title and 1-3 tags when obvious.
    `category` must be an existing category name (see list_memo_categories); leave it out when none fits."""
    return await _agent("POST", "/memos", {"title": title, "body": body, "tags": tags or [], "category": category,
                                           "pinned": pinned})


@server.tool()
async def update_memo(memo_id: str, append: str | None = None, body: str | None = None, title: str | None = None,
                      tags: list[str] | None = None, category: str | None = None, pinned: bool | None = None,
                      rev: int | None = None) -> str:
    """Edit a memo. On memos the user wrote you may only `append` text and change tags/category/pinned; title and body
    can be replaced only on memos you created. `tags` replaces the whole list. `category` is an existing category
    name, or "" to make it uncategorized. Pass `rev` from get_memo to avoid overwriting a newer edit."""
    patch = {"append": append, "body": body, "title": title, "tags": tags, "category": category, "pinned": pinned,
             "rev": rev}
    return await _agent("PATCH", f"/memos/{memo_id}", {k: v for k, v in patch.items() if v is not None})


@server.tool()
async def delete_memo(memo_id: str) -> str:
    """Archive (soft-delete) a memo you created. Memos the user wrote cannot be deleted by you."""
    return await _agent("DELETE", f"/memos/{memo_id}")


ReminderOpt = Literal["at_start", "10m", "30m", "1h", "1d", "day_9am", "prev_day_9am"]


@query_tool
async def list_events(start_date: str, end_date: str, query: str | None = None) -> str:
    """List calendar events from start_date to end_date (YYYY-MM-DD, inclusive, at most 400 days), plus Korean public
    holidays in that range. Times are Asia/Seoul wall time: all-day events use dates, timed events use
    YYYY-MM-DDTHH:MM (end of an all-day event is inclusive). `reminder` is the push reminder option or null."""
    return await _agent("GET", "/events", **{"from": start_date, "to": end_date, "q": query})


@server.tool()
async def create_event(title: str, start: str, end: str | None = None, all_day: bool = False,
                       description: str = "", location: str = "", reminder: ReminderOpt | None = None) -> str:
    """Create an event in Asia/Seoul time. All-day: start/end are YYYY-MM-DD (end inclusive, defaults to start).
    Timed: YYYY-MM-DDTHH:MM (end defaults to one hour later). Recurring events are not supported (on hold).
    `reminder` (off by default; set it only when the user asks): timed events take at_start/10m/30m/1h/1d
    (before start); all-day events take day_9am or prev_day_9am (KST). The user gets a phone push at that time."""
    return await _agent("POST", "/events", {"title": title, "start": start, "end": end, "all_day": all_day,
                                            "description": description, "location": location, "reminder": reminder})


@server.tool()
async def update_event(event_id: str, title: str | None = None, start: str | None = None, end: str | None = None,
                       all_day: bool | None = None, description: str | None = None, location: str | None = None,
                       reminder: ReminderOpt | Literal["off"] | None = None) -> str:
    """Change fields of an event. Send only what changes; when changing all_day, send start and end too (and a
    reminder that fits the new kind if one is set). `reminder="off"` turns the reminder off."""
    patch: dict[str, Any] = {k: v for k, v in {"title": title, "start": start, "end": end, "all_day": all_day,
                                                "description": description, "location": location}.items() if v is not None}
    if reminder is not None:
        patch["reminder"] = None if reminder == "off" else reminder
    return await _agent("PATCH", f"/events/{event_id}", patch)


@server.tool()
async def delete_event(event_id: str) -> str:
    """Archive (soft-delete) an event you created. Events the user made cannot be deleted by you; tell them to remove it in the app."""
    return await _agent("DELETE", f"/events/{event_id}")


# ---- 회의록(ADR 0010). 읽기 전용이다. 제안 승인 같은 반영은 사용자가 앱에서만 한다.

@query_tool
async def list_meetings(query: str | None = None, project: str | None = None, status: str | None = None, limit: int = 20) -> str:
    """List the user's meeting minutes (newest first) with title, project, status, summary and item counts. `project` is a
    project name or alias; `status` e.g. analyzed or review. Read-only; use get_meeting for the details."""
    return await _agent("GET", "/meetings", q=query, project=project, status=status, limit=limit)


@query_tool
async def get_meeting(meeting_id: str) -> str:
    """Read one meeting: summary, participants and its discussions, decisions, action items (with owner, due, status) and
    open issues. Decisions the user approved are also in `recall` as memories. Read-only; the raw transcript is only in the app."""
    return await _agent("GET", f"/meetings/{meeting_id}")


@query_tool
async def search_meeting_items(query: str | None = None, kind: Literal["discussion", "decision", "action", "open_issue"] | None = None,
                               status: Literal["open", "done", "cancelled"] | None = None, project: str | None = None,
                               limit: int = 20) -> str:
    """Search decisions / action items / open issues across meetings, e.g. kind="action", status="open" for pending work
    on a project. Each hit names its meeting. Without `query` it lists the newest. Read-only."""
    return await _agent("GET", "/meetings/search", q=query, kind=kind, status=status, project=project, limit=limit)


@query_tool
async def search_meetings(query: str, project: str | None = None,
                          kind: Literal["summary", "decision", "action", "open_issue", "discussion", "transcript"] | None = None,
                          limit: int = 8) -> str:
    """Meaning-based search over meeting minutes and transcript passages (local vector embeddings + keywords), e.g.
    "배포 일정 언제로 정했지" finds the passage even when worded differently. Each hit has meeting_id, title, date, kind and the
    passage text. Use get_meeting for the structured result. Read-only."""
    return await _agent("GET", "/meetings/semantic", q=query, project=project, kind=kind, limit=limit)




@server.tool()
async def request_backend_deploy(revision: str) -> str:
    """Reserve deployment of a committed, clean Ttakkari backend SHA. An independent controller tests it,
    then applies the configured strategy. With generations enabled, reserve verified personal Ttakkari changes
    proactively under the user's standing authorization unless they defer deployment. Active Runs, including
    the requester, do not block cutover: a ready candidate takes new requests while old engines drain. A full
    pair of slots queues later deployments automatically. Legacy idle and initial bootstrap wait for all Runs.
    Do not keep a Run alive just to poll. Acceptance is not deployment success; draining means new requests
    already switched. No shell restart, lock-file writes or service permissions needed."""
    return await _agent("POST", "/deploy", {"revision": revision})


@query_tool
async def backend_deploy_status(job_id: str) -> str:
    """Read a persisted deployment reservation. Generation draining means the new version is active,
    succeeded also confirms retirement of the previous generation. superseded_by points to a newer
    cumulative reservation; recovery_pending requires inspection. Do not wait in an active Run."""
    return await _agent("GET", f"/deploy/{job_id}")


# ---- 오케스트레이터 전용

def orchestrator_tool(fn):
    if ROLE != "orchestrator":
        return fn
    return query_tool(fn) if fn.__name__ in READ_ONLY_TOOLS else server.tool()(fn)


@orchestrator_tool
async def spawn_worker(title: str, prompt: str, engine: Literal["auto", "claude", "codex", "antigravity"] = "auto",
                       model: str | None = None,
                       effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
                       escalation_reason: str | None = None) -> str:
    """Start a new worker session that does a task in parallel. `title` is a short tab label (Korean is fine).
    `prompt` must be self-contained: the worker does not see this conversation.
    Use engine="auto" for general tasks: fresh usage chooses the engine on each turn; the model is a preference.
    Explicit engine="claude"/"codex"/"antigravity" locks that engine, including on a usage limit.
    Use antigravity only with an explicitly selected Gemini 3.8 Flash model; it is excluded from auto.
    Without `model`: claude → sonnet/high, codex or auto → gpt-6-luna/high, antigravity → gemini-3.8-flash-high.
    opus / gpt-6.1-sol are orchestrator-grade and need `escalation_reason` (why this part needs deep reasoning,
    a design decision or new ideas). Auto workers can move to another account with the same-grade
    model of the other engine. Returns the worker_id."""
    body: dict[str, Any] = {"title": title, "prompt": prompt, "engine": engine}
    if model:
        body["model"] = model
    if effort:
        body["effort"] = effort
    if escalation_reason:
        body["escalation_reason"] = escalation_reason
    return await _call("POST", "/workers", body)


@orchestrator_tool
async def account_usage() -> str:
    """Per-account status for choosing where to run workers: engine, personal/work scope, usable models, 5-hour and
    weekly used/remaining percent, per-account usage cap and remaining headroom to that cap, reset times, when it
    was measured, limit-wait state and running Run count. Antigravity also includes model-group quota buckets
    (`model_quotas`, with source `remaining_percent`) and a separate AI credit balance (`credits_balance`); these
    are not an account-wide cap. A zero credit balance is distinct from null/unobserved, and `credits_error`
    reports a failed credit lookup.
    Values are the app's last observation (null = never seen, stale = old); this tool does not refresh them."""
    return await _call("GET", "/accounts/usage")


@orchestrator_tool
async def start_luna_common30_v2() -> str:
    """Start the fixed 30-case × 3 repetition common30-v2 benchmark on the active generation's personal
    Codex profile using gpt-6-luna/high. No prompt, model, command, corpus or output path can be supplied.
    Returns a job_id; poll with luna_common30_v2_status. Stop with luna_common30_v2_cancel."""
    return await _call("POST", "/luna-common30-v2")


@orchestrator_tool
async def luna_common30_v2_status(job_id: str, offset: int = 0, limit: int = 10) -> str:
    """Read status, summary, and one checkpoint page of raw rows (at most 20) for the fixed Luna benchmark."""
    return await _call("GET", f"/luna-common30-v2/{job_id}", params={"offset": offset, "limit": limit})


@orchestrator_tool
async def luna_common30_v2_cancel(job_id: str) -> str:
    """Cancel a fixed Luna benchmark and persist its partial checkpoint."""
    return await _call("DELETE", f"/luna-common30-v2/{job_id}")


@query_tool
async def list_pms_projects(query: str | None = None, scope: Literal["all", "personal", "work"] = "all") -> str:
    """List PMS projects and their status/progress. Agents manage PMS/WBS on the user's behalf.
    Work sessions see work projects only. Use manage_pms for project details and the full WBS tree."""
    return await _agent("POST", "/pms", {"operation": "list_projects", "data": {"q": query, "scope": scope}})


@server.tool()
async def create_pms_task(project_id: str, title: str, description: str = "",
                          priority: Literal["low", "normal", "high", "urgent"] = "normal",
                          parent_id: str | None = None, after_id: str | None = None,
                          predecessors: list[str] | None = None, assignee: str | None = None,
                          start_date: str | None = None, end_date: str | None = None,
                          status: Literal["todo", "doing", "review", "done"] = "todo", progress: int = 0) -> str:
    """Create a WBS task, including hierarchy, predecessors, owner, dates and progress. Agents can manage PMS
    without another approval. Dates are YYYY-MM-DD; leave unknown dates empty. Use real task UUIDs for links.
    Returns the saved task and recalculated project tree. Use manage_pms to update/move/delete tasks."""
    return await _agent("POST", "/pms", {"operation": "create_task", "project_id": project_id, "data": {
        "title": title, "description": description, "priority": priority, "parent_id": parent_id,
        "after_id": after_id, "predecessors": predecessors or [], "assignee": assignee,
        "start_date": start_date, "end_date": end_date, "status": status, "progress": progress}})


@server.tool()
async def manage_pms(operation: PmsOperation, project_id: str | None = None, item_id: str | None = None,
                     data: dict[str, Any] | None = None) -> str:
    """Manage the user's PMS/WBS: projects, task tree/dependencies/progress, milestones and issues/risks.
    User delegated full PMS management to agents; no extra approval is needed. This does not authorize
    implementing the planned features. Work sessions remain scoped to work projects.

    Project operations use project_id; update/move/delete task/milestone/note use item_id (real UUIDs).
    list_projects data: scope=all|personal|work, q, status=planning|active|on_hold|done.
    create/update_project data: name, description, scope, status, start_date/end_date, members; create also phases.
    list_tasks returns the full numbered tree. create/update_task data: title, description, priority,
    assignee, dates, status=todo|doing|review|done, progress=0..100, predecessors=[UUIDs]. Create also parent_id/after_id.
    move_task data: parent_id=null to outdent, index to reorder. Parent progress/status roll up from children.
    create/update_milestone data: title, due_date (required on create, YYYY-MM-DD), done.
    list_notes data: status=open|resolved|all, kind=issue|risk|note. create/update_note data: title, body,
    kind, severity=low|medium|high, owner, task_id; update also status=open|resolved.
    get_project/dashboard/list_tasks/list_milestones and delete operations need no data (dashboard accepts today).
    Null in update data clears nullable fields; omitted fields stay unchanged. Delete project/task removes
    descendants, so first read the target and act within the user's task scope. Unknown fields are rejected."""
    return await _agent("POST", "/pms", {"operation": operation, "project_id": project_id,
                                          "item_id": item_id, "data": data or {}})


@orchestrator_tool
async def message_worker(worker_id: str, prompt: str, fresh_context: bool = False) -> str:
    """Send a follow-up instruction to an existing worker. It continues the same conversation and is queued if
    the worker is busy. fresh_context=True starts the worker's engine conversation over (use when its context is
    high); then the prompt must be self-contained and say where the progress is recorded."""
    return await _call("POST", f"/workers/{worker_id}/messages", {"prompt": prompt, "fresh_context": fresh_context})


@orchestrator_tool
async def my_context() -> str:
    """Your own conversation's context usage (percent of the model's context window) from your last finished turn."""
    return await _call("GET", "/context")


@orchestrator_tool
async def reset_my_context() -> str:
    """Start your own conversation over from your next turn. This turn finishes normally. Before calling, make sure
    unfinished work, decisions and next steps are recorded (remember / work-history / worker sessions)."""
    return await _call("POST", "/context/reset")


@orchestrator_tool
async def list_workers() -> str:
    """List this orchestrator's worker sessions with status and a short last result."""
    return await _call("GET", "/workers")


@orchestrator_tool
async def get_worker(worker_id: str) -> str:
    """Get one worker's status, latest result (up to 4000 chars), error and produced file names."""
    return await _call("GET", f"/workers/{worker_id}")


@orchestrator_tool
async def stop_worker(worker_id: str) -> str:
    """Cancel the worker's running and queued instructions."""
    return await _call("POST", f"/workers/{worker_id}/stop")


@query_tool
async def list_checklist(day: str | None = None) -> str:
    """List a day's checklist and completion counts. day is YYYY-MM-DD, defaults to today (Asia/Seoul)."""
    return await _agent("GET", "/checklists", day=day)


@query_tool
async def list_checklist_templates() -> str:
    """List fixed checklist templates repeated daily. Editing these preserves existing daily records."""
    return await _agent("GET", "/checklists/templates")


@server.tool()
async def create_checklist_item(title: str, day: str | None = None) -> str:
    """Add an item to a date's checklist (YYYY-MM-DD, default today)."""
    return await _agent("POST", "/checklists/items", {"title": title, "day": day})


@server.tool()
async def update_checklist_item(item_id: str, title: str | None = None, checked: bool | None = None) -> str:
    """Rename or check/uncheck one daily item. Use list_checklist for its ID."""
    return await _agent("PATCH", f"/checklists/items/{item_id}",
                        {k: v for k, v in {"title": title, "checked": checked}.items() if v is not None})


@server.tool()
async def delete_checklist_item(item_id: str) -> str:
    """Remove an item from just that day's checklist. Fixed template remains for future days."""
    return await _agent("DELETE", f"/checklists/items/{item_id}")


@server.tool()
async def create_checklist_template(title: str) -> str:
    """Add a fixed item repeated from today, unchecked each day."""
    return await _agent("POST", "/checklists/templates", {"title": title})


@server.tool()
async def update_checklist_template(template_id: str, title: str) -> str:
    """Rename a fixed template for future daily copies. Existing daily records keep their text and checks."""
    return await _agent("PATCH", f"/checklists/templates/{template_id}", {"title": title})


@server.tool()
async def delete_checklist_template(template_id: str) -> str:
    """Stop a fixed item's future repetitions. Existing daily records are preserved."""
    return await _agent("DELETE", f"/checklists/templates/{template_id}")


if __name__ == "__main__":
    server.run("stdio")
