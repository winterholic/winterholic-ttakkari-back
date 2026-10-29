"""SEC2-GATE-00: 라우트·도구 인벤토리 스냅샷 테스트.

app 라우트(HTTP + WebSocket)와 MCP 도구 목록을 코드에서 자동 수집해 고정
분류표(ROUTE_SCOPE / MCP_SCOPE)와 비교한다. 분류표에 없는 라우트·도구가
추가되면 실패한다. 새 라우트 추가 절차: ROUTE_SCOPE에 (method, path)와 범위
분류를 한 줄 추가한다. 런타임 동작 변경 없음.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRoute, APIWebSocketRoute

from ttakkari.api.transfers import _simple_auth
from ttakkari.main import app
from ttakkari.orchestrator.runtime import require_orchestrator, require_orchestrator_run
from ttakkari.security.auth import require_user

pytestmark = [pytest.mark.security, pytest.mark.contract]


_AUTH_CALLS = {
    "require_user": require_user,
    "require_orchestrator": require_orchestrator,
    "require_orchestrator_run": require_orchestrator_run,
    "_simple_auth": _simple_auth,
}
_SCOPE_MARKERS = ("agent_access", "user_access")

ROUTE_LABELS = frozenset(
    {
        "public",
        "owner-plain",
        "owner-scoped",
        "agent-plain",
        "agent-scoped",
        "transfer-plain",
        "ws-ticket",
    }
)

MCP_SCOPES = frozenset(
    {
        "memory",
        "files",
        "sessions",
        "planner",
        "meetings",
        "knowledge",
        "pms",
        "notebooklm",
        "google_account",
        "reverse",
        "stocks",
        "design",
        "ops",
        "orchestration",
        "host",
        "context",
        "comfy",
        "creative",
        "notify",
    }
)


def _markers(route) -> set[str]:
    found: set[str] = set()
    seen: set[int] = set()

    def walk(dep) -> None:
        for sub in getattr(dep, "dependencies", []) or []:
            name = getattr(getattr(sub, "call", None), "__name__", None)
            if (name in _AUTH_CALLS and sub.call is _AUTH_CALLS[name]) or name in _SCOPE_MARKERS:
                found.add(name)
            if id(sub) not in seen:
                seen.add(id(sub))
                walk(sub)

    walk(getattr(route, "dependant", route))
    return found


def _label(markers: set[str]) -> str:
    if markers & {"require_orchestrator", "require_orchestrator_run"}:
        auth = "agent"
    elif "require_user" in markers:
        auth = "owner"
    elif "_simple_auth" in markers:
        return "transfer-plain"
    else:
        return "public"
    if markers & set(_SCOPE_MARKERS):
        return f"{auth}-scoped"
    return f"{auth}-plain"


def _unfolded():
    for r in app.routes:
        # FastAPI's lazy _IncludedRouter resolves nested includes with the
        # effective path and merged dependencies. A one-level original_router
        # walk drops routes from routers included inside another router.
        effective = getattr(r, "effective_route_contexts", None)
        if callable(effective):
            yield from effective()
            continue
        inner = getattr(getattr(r, "original_router", None), "routes", None)
        if inner is not None:
            yield from inner
        else:
            yield r


def collect_routes() -> dict[tuple[str, str], str]:
    """(method, path) -> 범위 분류. WS는 method 자리에 "WS"."""
    out: dict[tuple[str, str], str] = {}
    dupes: list[str] = []
    for r in _unfolded():
        original = getattr(r, "original_route", r)
        if isinstance(original, APIWebSocketRoute):
            path = getattr(getattr(r, "starlette_route", None), "path", None) or original.path
            key = ("WS", path)
            if key in out:
                dupes.append(f"WS {path}")
            out[key] = "ws-ticket"
        elif isinstance(original, APIRoute):
            path = getattr(r, "path_format", None) or original.path
            methods = getattr(r, "methods", None) or original.methods
            for m in sorted(methods):
                k = (m, path)
                if k in out:
                    dupes.append(f"{m} {path}")
                out[k] = _label(_markers(r if hasattr(r, "dependant") else original))
    assert not dupes, f"중복 라우트(먼저 선언된 쪽만 동작): {dupes}"
    return out


async def collect_tool_names() -> list[str]:
    from ttakkari.orchestrator import mcp_server as ms

    tools = await ms.server.list_tools()
    if isinstance(tools, tuple):
        tools = tools[0]
    return sorted(t.name for t in tools)


def orchestrator_only_tools() -> frozenset[str]:
    """@orchestrator_tool로 등록돼 agent 역할에 안 보이는 도구."""
    from ttakkari.orchestrator import mcp_server as ms

    tree = ast.parse(Path(ms.__file__).read_text())
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for d in node.decorator_list:
                if isinstance(d, ast.Name) and d.id == "orchestrator_tool":
                    out.add(node.name)
    return frozenset(out)


def test_route_inventory_matches_manifest():
    live = collect_routes()
    missing = sorted(k for k in live if k not in ROUTE_SCOPE)
    assert not missing, (
        f"분류 없는 라우트 {len(missing)}개. ROUTE_SCOPE에 추가할 것: {missing}"
    )
    stale = sorted(k for k in ROUTE_SCOPE if k not in live)
    assert not stale, f"사라진 라우트 {len(stale)}개. ROUTE_SCOPE에서 지울 것: {stale}"
    changed = {k: (ROUTE_SCOPE[k], live[k]) for k in live if ROUTE_SCOPE[k] != live[k]}
    assert not changed, f"인증·범위 선언이 바뀐 라우트: {changed}"


def test_nested_included_router_routes_are_collected():
    live = collect_routes()
    github_routes = {key for key in live if key[1].startswith("/api/github/")}
    assert len(github_routes) == 21
    assert ("GET", "/api/github/accounts") in github_routes


def test_route_scope_labels_valid():
    bad = {k: v for k, v in ROUTE_SCOPE.items() if v not in ROUTE_LABELS}
    assert not bad, f"허용되지 않은 범위 분류: {bad}"


def test_auth_classification_rejects_same_named_noop_dependency():
    async def require_user():
        return None

    route = APIRoute("/fake", lambda: None, methods=["GET"], dependencies=[Depends(require_user)])
    assert _label(_markers(route)) == "public"


async def test_authentication_markers_reject_missing_credentials():
    with pytest.raises(HTTPException) as owner_error:
        await require_user(None)
    assert owner_error.value.status_code == 401

    with pytest.raises(HTTPException) as agent_error:
        await require_orchestrator(None, None)
    assert agent_error.value.status_code == 401

    request = Request({"type": "http", "method": "POST", "path": "/api/transfers/simple", "headers": []})
    with pytest.raises(HTTPException) as transfer_error:
        await _simple_auth(None, request, None)
    assert transfer_error.value.status_code == 401


async def test_mcp_inventory_matches_manifest():
    names = await collect_tool_names()
    missing = sorted(n for n in names if n not in MCP_SCOPE)
    assert not missing, (
        f"분류 없는 MCP 도구 {len(missing)}개. MCP_SCOPE에 추가할 것: {missing}"
    )
    stale = sorted(n for n in MCP_SCOPE if n not in names)
    assert not stale, f"사라진 MCP 도구 {len(stale)}개. MCP_SCOPE에서 지울 것: {stale}"
    orch = orchestrator_only_tools()
    assert orch == MCP_ORCHESTRATOR_ONLY, (
        f"orchestrator 전용 도구 집합 변경. 코드={sorted(orch)} 목록={sorted(MCP_ORCHESTRATOR_ONLY)}"
    )
    assert MCP_ORCHESTRATOR_ONLY < set(names), "전용 도구 중 등록되지 않은 이름이 있다"
    agent = set(names) - orch
    assert agent, "agent 역할 도구가 비어 있다"


def test_mcp_scope_values_valid():
    bad = {k: v for k, v in MCP_SCOPE.items() if v not in MCP_SCOPES}
    assert not bad, f"허용되지 않은 도구 범위 분류: {bad}"


ROUTE_SCOPE: dict[tuple[str, str], str] = {
    ("POST", "/api/orchestrator/copilot/activate"): "agent-plain",
    ("GET", "/api/agent/collection/missions/{mission_id}"): "agent-plain",
    ("POST", "/api/agent/collection/missions/{mission_id}/retry"): "agent-plain",
    ("GET", "/api/agent/github/accounts"): "agent-plain",
    ("POST", "/api/agent/github/switch-account"): "agent-plain",
    ("GET", "/api/assistant/collect/missions"): "owner-plain",
    ("POST", "/api/assistant/collect/missions"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/plan"): "owner-plain",
    ("GET", "/api/assistant/collect/missions/{mission_id}"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/{mission_id}/cancel"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/{mission_id}/extend"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/{mission_id}/pause"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/{mission_id}/rerun"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/{mission_id}/resume"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/{mission_id}/schedule"): "owner-plain",
    ("POST", "/api/assistant/collect/missions/{mission_id}/items/{item_id}/unhold"): "owner-plain",
    ("POST", "/api/assistant/collect/items/{item_id}/import"): "owner-scoped",
    ("POST", "/api/assistant/collect/items/{item_id}/reprocess"): "owner-scoped",
    ("GET", "/api/assistant/collect/summary"): "owner-scoped",
    ("GET", "/api/assistant/collect/budgets"): "owner-scoped",
    ("GET", "/api/assistant/collect/review"): "owner-scoped",
    ("GET", "/api/assistant/collect/review/audit"): "owner-scoped",
    ("GET", "/api/assistant/collect/items/{item_id}"): "owner-scoped",
    ("GET", "/api/assistant/collect/deliveries"): "owner-scoped",
    ("POST", "/api/assistant/collect/deliveries/{outbox_id}/reconcile"): "owner-scoped",
    ("GET", "/api/assistant/collect/runtime"): "owner-scoped",
    ("PUT", "/api/assistant/collect/runtime"): "owner-scoped",
    ("POST", "/api/assistant/collect/pilot"): "owner-scoped",
    ("GET", "/api/assistant/collect/quality"): "owner-scoped",
    ("POST", "/api/assistant/collect/quality/train"): "owner-scoped",
    ("POST", "/api/assistant/collect/items/{item_id}/reviews"): "owner-scoped",
    ("GET", "/api/agent/kb/summary"): "agent-scoped",
    ("GET", "/api/agent/kb/observations"): "agent-scoped",
    ("GET", "/api/agent/kb/search"): "agent-scoped",
    ("GET", "/api/agent/kb/documents"): "agent-scoped",
    ("GET", "/api/agent/kb/documents/{document_id}"): "agent-scoped",
    ("GET", "/api/agent/kb/passages/{passage_id}/evidence"): "agent-scoped",
    ("GET", "/api/assistant/collect/sources"): "owner-scoped",
    ("POST", "/api/assistant/collect/sources"): "owner-scoped",
    ("PATCH", "/api/assistant/collect/sources/{source_id}"): "owner-scoped",
    ("POST", "/api/assistant/collect/sources/{source_id}/run"): "owner-scoped",
    ("POST", "/api/assistant/collect/jobs/{job_id}/retry"): "owner-scoped",
    ("GET", "/api/assistant/collect/jobs/{job_id}/items"): "owner-scoped",
    ("GET", "/api/assistant/collect/items/{item_id}/export"): "owner-scoped",
    ("GET", "/api/agent/comfy/profiles"): "agent-scoped",
    ("GET", "/api/agent/comfy/readiness"): "agent-scoped",
    ("GET", "/api/agent/comfy/jobs"): "agent-scoped",
    ("GET", "/api/agent/comfy/jobs/{task_id}"): "agent-scoped",
    ("POST", "/api/agent/comfy/submit"): "agent-scoped",
    ("POST", "/api/agent/comfy/jobs/{task_id}/cancel"): "agent-scoped",
    ("POST", "/api/agent/comfy/jobs/{task_id}/reconcile"): "agent-scoped",
    ("POST", "/api/agent/creative-v2/{operation}"): "agent-scoped",
    ("DELETE", "/api/accounts/override"): "owner-plain",
    ("DELETE", "/api/accounts/{account_id}"): "owner-plain",
    ("DELETE", "/api/agent/checklists/items/{id}"): "agent-scoped",
    ("DELETE", "/api/agent/checklists/templates/{id}"): "agent-scoped",
    ("DELETE", "/api/agent/events/{event_id}"): "agent-plain",
    ("DELETE", "/api/agent/memos/{memo_id}"): "agent-plain",
    ("DELETE", "/api/agent/notebooklm/notebooks/{notebook_id}"): "agent-scoped",
    (
        "DELETE",
        "/api/agent/notebooklm/notebooks/{notebook_id}/sources/{source_id}",
    ): "agent-scoped",
    ("DELETE", "/api/artifacts/{art_id}"): "owner-plain",
    ("DELETE", "/api/assistant/entities/{eid}"): "owner-plain",
    ("DELETE", "/api/assistant/instructions"): "owner-plain",
    ("DELETE", "/api/assistant/memories/{mid}"): "owner-plain",
    ("DELETE", "/api/assistant/relations/{rid}"): "owner-plain",
    ("DELETE", "/api/assistant/sources/{sid}"): "owner-plain",
    ("DELETE", "/api/calendar/events/{event_id}"): "owner-plain",
    ("DELETE", "/api/checklists/items/{id}"): "owner-scoped",
    ("DELETE", "/api/checklists/templates/{id}"): "owner-scoped",
    ("DELETE", "/api/meetings/{mid}"): "owner-plain",
    ("DELETE", "/api/memo-categories/{category_id}"): "owner-plain",
    ("DELETE", "/api/memos/{memo_id}"): "owner-plain",
    ("DELETE", "/api/google-accounts/{account_id}/session"): "owner-scoped",
    ("DELETE", "/api/notebooklm/auth/session"): "owner-scoped",
    ("DELETE", "/api/notebooklm/notebooks/{notebook_id}"): "owner-scoped",
    (
        "DELETE",
        "/api/notebooklm/notebooks/{notebook_id}/sources/{source_id}",
    ): "owner-scoped",
    ("DELETE", "/api/orchestrator/luna-common30-v2/{job_id}"): "agent-plain",
    ("DELETE", "/api/orchestrator/schedules/{schedule_id}"): "agent-plain",
    ("DELETE", "/api/pms/milestones/{mid}"): "owner-plain",
    ("DELETE", "/api/pms/notes/{nid}"): "owner-plain",
    ("DELETE", "/api/pms/projects/{pid}"): "owner-plain",
    ("DELETE", "/api/pms/tasks/{tid}"): "owner-plain",
    ("DELETE", "/api/push/subscriptions"): "owner-plain",
    ("DELETE", "/api/reverse/{job_id}"): "owner-scoped",
    ("DELETE", "/api/shares/{sid}"): "owner-plain",
    ("DELETE", "/api/transfer-tokens/{token_id}"): "owner-plain",
    ("DELETE", "/api/transfers/{tid}"): "owner-plain",
    ("DELETE", "/api/tts/jobs/{job_id}"): "owner-plain",
    ("GET", "/api/accounts"): "owner-plain",
    ("GET", "/api/accounts/override"): "owner-plain",
    ("GET", "/api/accounts/qwen-flash-usage"): "owner-plain",
    ("GET", "/api/accounts/qwen-usage"): "owner-plain",
    ("GET", "/api/accounts/usage"): "owner-plain",
    ("GET", "/api/accounts/usage/cached"): "owner-plain",
    ("GET", "/api/accounts/{account_id}/usage"): "owner-plain",
    ("GET", "/api/agent/artifacts"): "agent-plain",
    ("GET", "/api/agent/checklists"): "agent-scoped",
    ("GET", "/api/agent/checklists/templates"): "agent-scoped",
    ("GET", "/api/agent/collect/jobs"): "agent-scoped",
    ("GET", "/api/agent/collect/jobs/{job_id}"): "agent-scoped",
    ("GET", "/api/agent/deploy/diagnostics"): "agent-plain",
    ("GET", "/api/agent/deploy/{job_id}"): "agent-plain",
    ("GET", "/api/agent/design-systems"): "agent-scoped",
    ("GET", "/api/agent/design-systems/{system}/item"): "agent-scoped",
    ("GET", "/api/agent/design-systems/{system}/items"): "agent-scoped",
    ("GET", "/api/agent/entities/{name}"): "agent-plain",
    ("GET", "/api/agent/events"): "agent-plain",
    ("GET", "/api/agent/execution-capabilities"): "agent-plain",
    ("GET", "/api/agent/host-gpu/status"): "agent-plain",
    ("GET", "/api/agent/operations"): "agent-plain",
    ("GET", "/api/agent/operations/{job_id}"): "agent-plain",
    ("GET", "/api/agent/knowledge/assertions/{aid}"): "agent-plain",
    ("GET", "/api/agent/knowledge/extractions/{jid}"): "agent-plain",
    ("GET", "/api/agent/knowledge/extractions/{jid}/batch"): "agent-plain",
    ("GET", "/api/agent/knowledge/revisions"): "agent-plain",
    ("GET", "/api/agent/library"): "agent-plain",
    ("GET", "/api/agent/meetings"): "agent-plain",
    ("GET", "/api/agent/meetings/search"): "agent-plain",
    ("GET", "/api/agent/meetings/semantic"): "agent-plain",
    ("GET", "/api/agent/meetings/{mid}"): "agent-plain",
    ("GET", "/api/agent/memo-categories"): "agent-plain",
    ("GET", "/api/agent/memos"): "agent-plain",
    ("GET", "/api/agent/memos/tags"): "agent-plain",
    ("GET", "/api/agent/memos/{memo_id}"): "agent-plain",
    ("GET", "/api/agent/google-accounts"): "agent-scoped",
    ("GET", "/api/agent/google-accounts/{account_id}"): "agent-scoped",
    ("GET", "/api/agent/notebooklm/auth/status"): "agent-scoped",
    ("GET", "/api/agent/notebooklm/jobs"): "agent-scoped",
    ("GET", "/api/agent/notebooklm/jobs/{job_id}"): "agent-scoped",
    ("GET", "/api/agent/notebooklm/notebooks"): "agent-scoped",
    ("GET", "/api/agent/notebooklm/notebooks/{notebook_id}"): "agent-scoped",
    ("GET", "/api/agent/notebooklm/notebooks/{notebook_id}/sources"): "agent-scoped",
    ("GET", "/api/agent/ops-logs/deploys"): "agent-scoped",
    ("GET", "/api/agent/ops-logs/deploys/{ref}/log"): "agent-scoped",
    ("GET", "/api/agent/ops-logs/events"): "agent-scoped",
    ("GET", "/api/agent/ops-logs/issues"): "agent-scoped",
    ("GET", "/api/agent/ops-logs/issues/{issue}"): "agent-scoped",
    ("GET", "/api/agent/ops-logs/trend"): "agent-scoped",
    ("GET", "/api/agent/reverse"): "agent-scoped",
    ("GET", "/api/agent/reverse/{job_id}"): "agent-scoped",
    ("GET", "/api/agent/sessions"): "agent-plain",
    ("GET", "/api/agent/sessions/{session_id}"): "agent-plain",
    ("GET", "/api/agent/stocks/tossinvest/login-help"): "agent-scoped",
    ("GET", "/api/agent/stocks/tossinvest/runs"): "agent-scoped",
    ("GET", "/api/agent/stocks/tossinvest/status"): "agent-scoped",
    ("GET", "/api/agent/system/resources"): "agent-plain",
    ("GET", "/api/artcraft/agent-config"): "owner-plain",
    ("GET", "/api/artcraft/apps"): "owner-plain",
    ("GET", "/api/artcraft/bugs"): "owner-plain",
    ("GET", "/api/artcraft/catalog"): "owner-plain",
    ("GET", "/api/artcraft/jobs"): "owner-plain",
    ("GET", "/api/artcraft/jobs/{job_id}"): "owner-plain",
    ("GET", "/api/artcraft/outputs"): "owner-plain",
    ("GET", "/api/artcraft/tasks"): "owner-plain",
    ("GET", "/api/artcraft/tasks/{task_id}"): "owner-plain",
    ("GET", "/api/artifacts"): "owner-plain",
    ("GET", "/api/artifacts/{art_id}"): "owner-plain",
    ("GET", "/api/artifacts/{art_id}/content"): "owner-plain",
    ("GET", "/api/assistant/collect/jobs"): "owner-scoped",
    ("GET", "/api/assistant/collect/jobs/{job_id}"): "owner-scoped",
    ("GET", "/api/assistant/entities"): "owner-plain",
    ("GET", "/api/assistant/entities/{eid}"): "owner-plain",
    ("GET", "/api/assistant/instructions"): "owner-plain",
    ("GET", "/api/assistant/knowledge/assertions"): "owner-plain",
    ("GET", "/api/assistant/knowledge/assertions/{aid}"): "owner-plain",
    ("GET", "/api/assistant/knowledge/assertions/{aid}/impact"): "owner-plain",
    ("GET", "/api/assistant/knowledge/extractions/{jid}"): "owner-plain",
    ("GET", "/api/assistant/knowledge/extractions/{jid}/batch"): "owner-plain",
    ("GET", "/api/assistant/knowledge/revisions"): "owner-plain",
    ("GET", "/api/assistant/knowledge/status"): "owner-plain",
    ("GET", "/api/assistant/knowledge/units"): "owner-plain",
    ("GET", "/api/assistant/kb/summary"): "owner-plain",
    ("GET", "/api/assistant/kb/observations"): "owner-plain",
    ("GET", "/api/assistant/kb/documents"): "owner-plain",
    ("GET", "/api/assistant/kb/documents/{document_id}"): "owner-plain",
    ("GET", "/api/assistant/kb/search"): "owner-plain",
    ("GET", "/api/assistant/kb/passages/{passage_id}/evidence"): "owner-plain",
    ("GET", "/api/assistant/library"): "owner-plain",
    ("GET", "/api/assistant/library/documents/{did}/chunks"): "owner-plain",
    ("GET", "/api/assistant/library/entities/{eid}"): "owner-plain",
    ("GET", "/api/assistant/library/graph"): "owner-plain",
    ("GET", "/api/assistant/library/items/{kind}/{iid}"): "owner-plain",
    ("GET", "/api/assistant/library/overview"): "owner-plain",
    ("GET", "/api/assistant/library/quality"): "owner-plain",
    ("GET", "/api/assistant/library/sources/{sid}/documents"): "owner-plain",
    ("GET", "/api/assistant/library/tree"): "owner-plain",
    ("GET", "/api/assistant/memories"): "owner-plain",
    ("GET", "/api/assistant/sources"): "owner-plain",
    ("GET", "/api/assistant/status"): "owner-plain",
    ("GET", "/api/assistant/tools"): "owner-plain",
    ("GET", "/api/auth/me"): "owner-plain",
    ("GET", "/api/bootstrap-ready"): "public",
    ("GET", "/api/calendar/events"): "owner-plain",
    ("GET", "/api/calendar/events/{event_id}"): "owner-plain",
    ("GET", "/api/calendar/holidays"): "owner-plain",
    ("GET", "/api/checklists"): "owner-scoped",
    ("GET", "/api/checklists/templates"): "owner-scoped",
    ("GET", "/api/comfy/jobs/{task_id}"): "owner-plain",
    ("GET", "/api/comfy/profiles"): "owner-plain",
    ("GET", "/api/creative/catalog"): "owner-plain",
    ("GET", "/api/creative/projects"): "owner-plain",
    ("GET", "/api/creative/projects/{pid}"): "owner-plain",
    ("GET", "/api/creative/projects/{pid}/media/{kind}/{ident}"): "public",
    ("GET", "/api/creative/productions/{pid}"): "owner-plain",
    ("GET", "/api/creative/records"): "owner-plain",
    ("GET", "/api/creative/records/{rid}"): "owner-plain",
    ("GET", "/api/creative/records/{rid}/media/{media_id}"): "public",
    ("GET", "/api/creative/v2/catalog"): "owner-plain",
    ("GET", "/api/creative/v2/capabilities"): "owner-plain",
    ("GET", "/api/creative/v2/registries"): "owner-plain",
    ("GET", "/api/creative/v2/matrix"): "owner-plain",
    ("GET", "/api/creative/v2/duration/policy"): "owner-plain",
    ("GET", "/api/creative/v2/intent/contract"): "owner-plain",
    ("GET", "/api/creative/v2/style-dna"): "owner-plain",
    ("GET", "/api/creative/v2/style-dna/{style_id}/{version}"): "owner-plain",
    ("GET", "/api/creative/v2/style-dna/works/{work_id}"): "owner-plain",
    ("GET", "/api/creative/v2/plans"): "owner-plain",
    ("GET", "/api/creative/v2/plans/{pid}"): "owner-plain",
    ("GET", "/api/creative/v2/plans/{pid}/production"): "owner-plain",
    ("GET", "/api/creative/v2/plans/{pid}/production/media/{ident}"): "public",
    ("GET", "/api/design-systems"): "owner-scoped",
    ("GET", "/api/design-systems/{system}/item"): "owner-scoped",
    ("GET", "/api/design-systems/{system}/items"): "owner-scoped",
    ("GET", "/api/design-systems/{system}/preview"): "owner-scoped",
    ("GET", "/api/harness"): "owner-scoped",
    ("GET", "/api/harness/items"): "owner-scoped",
    ("GET", "/api/harness/items/{item_id}"): "owner-scoped",
    ("GET", "/api/harness/layers"): "owner-scoped",
    ("GET", "/api/harness/groups/{group}"): "owner-scoped",
    ("GET", "/api/harness/projects"): "owner-scoped",
    ("GET", "/api/harness/projects/detail"): "owner-scoped",
    ("GET", "/api/desk/eval-set"): "owner-plain",
    ("GET", "/api/desk/feedback"): "owner-plain",
    ("GET", "/api/dl/{token}"): "public",
    ("GET", "/api/games"): "owner-plain",
    ("GET", "/api/games/{game_id}"): "owner-plain",
    ("GET", "/api/games/{game_id}/builds"): "owner-plain",
    ("GET", "/api/games/{game_id}/builds/{build_id}"): "owner-plain",
    ("GET", "/api/games/{game_id}/compare"): "owner-plain",
    ("GET", "/api/games/{game_id}/requests"): "owner-plain",
    ("GET", "/api/games/{game_id}/versions"): "owner-plain",
    ("GET", "/api/github/accounts"): "owner-plain",
    ("GET", "/api/github/me"): "owner-plain",
    ("GET", "/api/github/me/orgs"): "owner-plain",
    ("GET", "/api/github/me/repos"): "owner-plain",
    ("GET", "/api/github/owners/{owner}"): "owner-plain",
    ("GET", "/api/github/owners/{owner}/repos"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/actions/runs"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/actions/runs/{run_id}"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/branches"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/contents"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/issues"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/issues/{number}"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/issues/{number}/comments"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/pulls"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/pulls/{number}"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/pulls/{number}/files"): "owner-plain",
    ("GET", "/api/github/repos/{owner}/{repo}/readme"): "owner-plain",
    ("GET", "/api/github/resolve"): "owner-plain",
    ("GET", "/api/health"): "public",
    ("GET", "/api/meetings"): "owner-plain",
    ("GET", "/api/meetings/providers"): "owner-plain",
    ("GET", "/api/meetings/{mid}"): "owner-plain",
    ("GET", "/api/meetings/{mid}/stt"): "owner-plain",
    ("GET", "/api/meetings/{mid}/stt/{jid}/segments"): "owner-plain",
    ("GET", "/api/meetings/{mid}/stt/{jid}/utterances"): "owner-plain",
    ("GET", "/api/memo-categories"): "owner-plain",
    ("GET", "/api/memos"): "owner-plain",
    ("GET", "/api/memos/tags"): "owner-plain",
    ("GET", "/api/memos/{memo_id}"): "owner-plain",
    ("GET", "/api/google-accounts"): "owner-scoped",
    ("GET", "/api/google-accounts/{account_id}"): "owner-scoped",
    ("GET", "/api/notebooklm/auth/status"): "owner-scoped",
    ("GET", "/api/notebooklm/jobs"): "owner-scoped",
    ("GET", "/api/notebooklm/jobs/{job_id}"): "owner-scoped",
    ("GET", "/api/notebooklm/notebooks"): "owner-scoped",
    ("GET", "/api/notebooklm/notebooks/{notebook_id}"): "owner-scoped",
    ("GET", "/api/notebooklm/notebooks/{notebook_id}/sources"): "owner-scoped",
    ("GET", "/api/notifications"): "owner-plain",
    ("GET", "/api/ops-logs/deploys"): "owner-scoped",
    ("GET", "/api/ops-logs/deploys/{ref}/log"): "owner-scoped",
    ("GET", "/api/ops-logs/events"): "owner-scoped",
    ("GET", "/api/ops-logs/issues"): "owner-scoped",
    ("GET", "/api/ops-logs/issues/{issue}"): "owner-scoped",
    ("GET", "/api/ops-logs/trend"): "owner-scoped",
    ("GET", "/api/orchestrator/accounts/usage"): "agent-plain",
    ("GET", "/api/orchestrator/context"): "agent-plain",
    ("GET", "/api/orchestrator/luna-common30-v2/{job_id}"): "agent-plain",
    ("GET", "/api/orchestrator/pms/projects"): "agent-plain",
    ("GET", "/api/orchestrator/schedules"): "agent-plain",
    ("POST", "/api/orchestrator/work"): "agent-plain",
    ("GET", "/api/orchestrator/workers"): "agent-plain",
    ("GET", "/api/orchestrator/workers/metrics"): "agent-plain",
    ("GET", "/api/orchestrator/workers/{worker_id}"): "agent-plain",
    ("GET", "/api/pms/projects"): "owner-plain",
    ("GET", "/api/pms/projects/{pid}"): "owner-plain",
    ("GET", "/api/pms/projects/{pid}/dashboard"): "owner-plain",
    ("GET", "/api/pms/projects/{pid}/milestones"): "owner-plain",
    ("GET", "/api/pms/projects/{pid}/notes"): "owner-plain",
    ("GET", "/api/pms/projects/{pid}/tasks"): "owner-plain",
    ("GET", "/api/public/shares/{sid}"): "public",
    ("GET", "/api/public/shares/{sid}/download"): "public",
    ("GET", "/api/push/vapid-public-key"): "owner-plain",
    ("GET", "/api/qwen-status"): "public",
    ("GET", "/api/ready"): "public",
    ("GET", "/api/remote-desktop/devices"): "owner-plain",
    ("GET", "/api/research/discover"): "owner-plain",
    ("GET", "/api/research/studies"): "owner-plain",
    ("GET", "/api/research/studies/{study_id}/compare"): "owner-plain",
    ("GET", "/api/research/studies/{study_id}/resume"): "owner-plain",
    ("GET", "/api/research/dashboard"): "owner-plain",
    ("GET", "/api/research/experiments/{experiment_id}"): "owner-plain",
    ("GET", "/api/research/experiments/{experiment_id}/compare-series"): "owner-plain",
    ("GET", "/api/research/methods"): "owner-plain",
    ("GET", "/api/research/methods/{id_or_slug}"): "owner-plain",
    ("GET", "/api/research/runs"): "owner-plain",
    ("GET", "/api/research/runs/{run_id}"): "owner-plain",
    ("POST", "/api/research/runs/{run_id}/retry"): "owner-plain",
    ("GET", "/api/research/taxonomy"): "owner-plain",
    ("GET", "/api/reverse"): "owner-scoped",
    ("GET", "/api/reverse/{job_id}"): "owner-scoped",
    ("GET", "/api/runs/failed"): "owner-plain",
    ("GET", "/api/runs/{run_id}"): "owner-plain",
    ("GET", "/api/runs/{run_id}/diff"): "owner-plain",
    ("GET", "/api/runs/{run_id}/events"): "owner-plain",
    ("GET", "/api/runs/{run_id}/stream"): "owner-plain",
    ("GET", "/api/sessions"): "owner-plain",
    ("GET", "/api/sessions/{session_id}"): "owner-plain",
    ("GET", "/api/sessions/{session_id}/runs"): "owner-plain",
    ("GET", "/api/shares"): "owner-plain",
    ("GET", "/api/shares/{sid}"): "owner-plain",
    ("GET", "/api/stocks/overview"): "owner-scoped",
    ("GET", "/api/stocks/tossinvest/login-help"): "owner-scoped",
    ("GET", "/api/stocks/tossinvest/runs"): "owner-scoped",
    ("GET", "/api/stocks/tossinvest/status"): "owner-scoped",
    ("GET", "/api/system/backup-status"): "owner-plain",
    ("GET", "/api/system/info"): "owner-plain",
    ("GET", "/api/system/qwen-worker"): "owner-plain",
    ("GET", "/api/system/resources"): "owner-plain",
    ("GET", "/api/system/resources/history"): "owner-plain",
    ("GET", "/api/tasks/active"): "owner-plain",
    ("GET", "/api/tasks/library"): "owner-plain",
    ("GET", "/api/tasks/search"): "owner-plain",
    ("GET", "/api/tasks/{session_id}/tree"): "owner-plain",
    ("GET", "/api/transfer-tokens"): "owner-plain",
    ("GET", "/api/transfers/folders"): "owner-plain",
    ("GET", "/api/transfers/{tid}"): "owner-plain",
    ("GET", "/api/tts/audio/{token}"): "public",
    ("GET", "/api/tts/cache/stats"): "owner-plain",
    ("GET", "/api/tts/jobs/{job_id}"): "owner-plain",
    ("GET", "/api/tts/status"): "owner-plain",
    ("GET", "/api/tts/voices"): "owner-plain",
    ("GET", "/api/version"): "public",
    ("GET", "/api/view/{token}"): "public",
    ("GET", "/api/weather/briefings"): "owner-plain",
    ("GET", "/api/weather/briefings/{day}"): "owner-plain",
    ("GET", "/api/weather/latest"): "owner-plain",
    ("GET", "/api/workspaces"): "owner-plain",
    ("GET", "/api/workspaces/default"): "owner-plain",
    ("GET", "/api/workspaces/{ws_id}"): "owner-plain",
    ("GET", "/api/workspaces/{ws_id}/files"): "owner-plain",
    ("GET", "/api/workspaces/{ws_id}/search"): "owner-plain",
    ("HEAD", "/api/public/shares/{sid}"): "public",
    ("HEAD", "/api/public/shares/{sid}/download"): "public",
    ("PATCH", "/api/accounts/{account_id}"): "owner-plain",
    ("PATCH", "/api/agent/artifacts/{art_id}"): "agent-plain",
    ("PATCH", "/api/agent/checklists/items/{id}"): "agent-scoped",
    ("PATCH", "/api/agent/checklists/templates/{id}"): "agent-scoped",
    ("PATCH", "/api/agent/events/{event_id}"): "agent-plain",
    ("PATCH", "/api/agent/memos/{memo_id}"): "agent-plain",
    ("PATCH", "/api/agent/notebooklm/notebooks/{notebook_id}"): "agent-scoped",
    ("PATCH", "/api/agent/ops-logs/issues/{issue}"): "agent-scoped",
    ("PATCH", "/api/artcraft/tasks/{task_id}"): "owner-plain",
    ("PATCH", "/api/artifacts/{art_id}"): "owner-plain",
    ("PATCH", "/api/assistant/entities/{eid}"): "owner-plain",
    ("PATCH", "/api/assistant/memories/{mid}"): "owner-plain",
    ("PATCH", "/api/calendar/events/{event_id}"): "owner-plain",
    ("PATCH", "/api/checklists/items/{id}"): "owner-scoped",
    ("PATCH", "/api/checklists/templates/{id}"): "owner-scoped",
    ("PATCH", "/api/creative/projects/{pid}"): "owner-plain",
    ("PATCH", "/api/creative/v2/plans/{pid}"): "owner-plain",
    ("PATCH", "/api/meetings/{mid}"): "owner-plain",
    ("PATCH", "/api/meetings/{mid}/items/{item_id}"): "owner-plain",
    ("PATCH", "/api/memo-categories/{category_id}"): "owner-plain",
    ("PATCH", "/api/memos/{memo_id}"): "owner-plain",
    ("PATCH", "/api/notebooklm/notebooks/{notebook_id}"): "owner-scoped",
    ("PATCH", "/api/notifications/{notification_id}/read"): "owner-plain",
    ("PATCH", "/api/ops-logs/issues/{issue}"): "owner-scoped",
    ("PATCH", "/api/pms/milestones/{mid}"): "owner-plain",
    ("PATCH", "/api/pms/notes/{nid}"): "owner-plain",
    ("PATCH", "/api/pms/projects/{pid}"): "owner-plain",
    ("PATCH", "/api/pms/tasks/{tid}"): "owner-plain",
    ("PATCH", "/api/research/studies/{study_id}"): "owner-plain",
    ("PATCH", "/api/research/methods/{method_id}"): "owner-plain",
    ("PATCH", "/api/sessions/{session_id}"): "owner-plain",
    ("PATCH", "/api/workspaces/{ws_id}"): "owner-plain",
    ("POST", "/api/accounts"): "owner-plain",
    ("POST", "/api/accounts/reorder"): "owner-plain",
    ("POST", "/api/accounts/{account_id}/antigravity/check"): "owner-plain",
    ("POST", "/api/accounts/{account_id}/opencode/check"): "owner-plain",
    ("POST", "/api/accounts/{account_id}/copilot/check"): "owner-plain",
    ("POST", "/api/accounts/{account_id}/codex-reset"): "owner-plain",
    ("POST", "/api/accounts/{account_id}/cooldown/clear"): "owner-plain",
    ("POST", "/api/accounts/{account_id}/usage/refresh"): "owner-plain",
    ("POST", "/api/agent/checklists/items"): "agent-scoped",
    ("POST", "/api/agent/checklists/templates"): "agent-scoped",
    ("POST", "/api/agent/collect/jobs"): "agent-scoped",
    ("POST", "/api/agent/collect/jobs/{job_id}/cancel"): "agent-scoped",
    ("POST", "/api/agent/deploy"): "agent-plain",
    ("POST", "/api/agent/deploy/{job_id}/recover-drain"): "agent-plain",
    ("POST", "/api/agent/entities"): "agent-plain",
    ("POST", "/api/agent/events"): "agent-plain",
    ("POST", "/api/agent/execution-capabilities/challenge"): "agent-plain",
    ("POST", "/api/agent/execution-capabilities/report"): "agent-plain",
    ("POST", "/api/agent/forget"): "agent-plain",
    ("POST", "/api/agent/host-gpu/bench"): "agent-plain",
    ("POST", "/api/agent/host-gpu/start"): "agent-plain",
    ("POST", "/api/agent/host-gpu/stop"): "agent-plain",
    ("POST", "/api/agent/operations"): "agent-plain",
    ("POST", "/api/agent/operations/{job_id}/cancel"): "agent-plain",
    ("POST", "/api/agent/knowledge/assertions/{aid}/reviews"): "agent-plain",
    ("POST", "/api/agent/knowledge/extractions"): "agent-plain",
    ("POST", "/api/agent/knowledge/extractions/{jid}/batches"): "agent-plain",
    ("POST", "/api/agent/knowledge/extractions/{jid}/retry"): "agent-plain",
    ("POST", "/api/agent/knowledge/recall"): "agent-plain",
    ("POST", "/api/agent/link"): "agent-plain",
    ("POST", "/api/agent/memos"): "agent-plain",
    ("POST", "/api/agent/notebooklm/auth/verify"): "agent-scoped",
    ("POST", "/api/agent/notebooklm/jobs/{job_id}/cancel"): "agent-scoped",
    ("POST", "/api/agent/notebooklm/notebooks"): "agent-scoped",
    ("POST", "/api/agent/notebooklm/notebooks/{notebook_id}/queries"): "agent-scoped",
    ("POST", "/api/agent/notebooklm/notebooks/{notebook_id}/query"): "agent-scoped",
    (
        "POST",
        "/api/agent/notebooklm/notebooks/{notebook_id}/sources/text",
    ): "agent-scoped",
    (
        "POST",
        "/api/agent/notebooklm/notebooks/{notebook_id}/sources/url",
    ): "agent-scoped",
    ("POST", "/api/agent/notify"): "agent-plain",
    ("POST", "/api/agent/ops-logs/sync"): "agent-scoped",
    ("POST", "/api/agent/pms"): "agent-plain",
    ("POST", "/api/agent/recall"): "agent-plain",
    ("POST", "/api/agent/remember"): "agent-plain",
    ("POST", "/api/agent/research"): "agent-plain",
    ("POST", "/api/agent/reverse/start"): "agent-scoped",
    ("POST", "/api/agent/reverse/{job_id}/messages"): "agent-scoped",
    ("POST", "/api/agent/reverse/{job_id}/retry"): "agent-scoped",
    ("POST", "/api/agent/keep"): "agent-plain",
    ("POST", "/api/agent/share"): "agent-plain",
    ("POST", "/api/artcraft/apps/{app}/gui"): "owner-plain",
    ("POST", "/api/artcraft/remote-check"): "owner-plain",
    ("POST", "/api/artcraft/smoke"): "owner-plain",
    ("POST", "/api/artcraft/tasks"): "owner-plain",
    ("POST", "/api/artcraft/tasks/{task_id}/runs"): "owner-plain",
    ("POST", "/api/artifacts"): "owner-plain",
    ("POST", "/api/artifacts/resolve"): "owner-plain",
    ("POST", "/api/artifacts/{art_id}/download-link"): "owner-plain",
    ("POST", "/api/artifacts/{art_id}/preview/retry"): "owner-plain",
    ("POST", "/api/assistant/collect/jobs"): "owner-scoped",
    ("POST", "/api/assistant/collect/jobs/{job_id}/cancel"): "owner-scoped",
    ("POST", "/api/assistant/entities"): "owner-plain",
    ("POST", "/api/assistant/knowledge/assertions"): "owner-plain",
    ("POST", "/api/assistant/kb/documents"): "owner-plain",
    ("POST", "/api/assistant/knowledge/assertions/{aid}/corrections"): "owner-plain",
    ("POST", "/api/assistant/knowledge/assertions/{aid}/reviews"): "owner-plain",
    ("POST", "/api/assistant/knowledge/extractions"): "owner-plain",
    ("POST", "/api/assistant/knowledge/extractions/{jid}/batches"): "owner-plain",
    ("POST", "/api/assistant/knowledge/extractions/{jid}/retry"): "owner-plain",
    ("POST", "/api/assistant/knowledge/imports"): "owner-plain",
    ("POST", "/api/assistant/knowledge/recall"): "owner-plain",
    ("POST", "/api/assistant/library/search-lab"): "owner-plain",
    ("POST", "/api/assistant/memories"): "owner-plain",
    ("POST", "/api/assistant/recall"): "owner-plain",
    ("POST", "/api/assistant/relations"): "owner-plain",
    ("POST", "/api/assistant/sources"): "owner-plain",
    ("POST", "/api/assistant/sources/{sid}/reindex"): "owner-plain",
    ("POST", "/api/auth/login"): "public",
    ("POST", "/api/auth/logout"): "public",
    ("POST", "/api/auth/logout-all"): "owner-plain",
    ("POST", "/api/auth/refresh"): "public",
    ("POST", "/api/calendar/events"): "owner-plain",
    ("POST", "/api/checklists/items"): "owner-scoped",
    ("POST", "/api/checklists/templates"): "owner-scoped",
    ("POST", "/api/comfy/jobs"): "owner-plain",
    ("POST", "/api/comfy/jobs/{task_id}/cancel"): "owner-plain",
    ("POST", "/api/comfy/jobs/{task_id}/reconcile"): "owner-plain",
    ("POST", "/api/creative/projects"): "owner-plain",
    ("POST", "/api/creative/productions"): "owner-plain",
    ("POST", "/api/creative/projects/{pid}/assets"): "owner-plain",
    ("POST", "/api/creative/projects/{pid}/demo"): "owner-plain",
    ("POST", "/api/creative/projects/{pid}/media-link"): "owner-plain",
    ("POST", "/api/creative/projects/{pid}/renders"): "owner-plain",
    ("POST", "/api/creative/projects/{pid}/scenes/{scene_id}/illustration"): "owner-plain",
    ("POST", "/api/creative/productions/{pid}/artifacts"): "owner-plain",
    ("POST", "/api/creative/productions/{pid}/attempts"): "owner-plain",
    ("POST", "/api/creative/productions/{pid}/attempts/{attempt_id}/cancel"): "owner-plain",
    ("POST", "/api/creative/validate"): "owner-plain",
    ("POST", "/api/creative/v2/duration/preview"): "owner-plain",
    ("POST", "/api/creative/v2/duration/recheck"): "owner-plain",
    ("POST", "/api/creative/v2/intent/runs"): "owner-plain",
    ("GET", "/api/creative/v2/intent/runs/{run_id}"): "owner-plain",
    ("POST", "/api/creative/v2/intent/runs/{run_id}/resume"): "owner-plain",
    ("POST", "/api/creative/v2/intent/validate"): "owner-plain",
    ("POST", "/api/creative/v2/intent/hint"): "owner-plain",
    ("POST", "/api/creative/v2/match"): "owner-plain",
    ("POST", "/api/creative/v2/style-dna"): "owner-plain",
    ("POST", "/api/creative/v2/style-dna/compose"): "owner-plain",
    ("POST", "/api/creative/v2/style-dna/works/{work_id}/pin"): "owner-plain",
    ("POST", "/api/creative/v2/plans"): "owner-plain",
    ("POST", "/api/creative/v2/plans/{pid}/approve"): "owner-plain",
    ("POST", "/api/creative/v2/plans/{pid}/production"): "owner-plain",
    ("POST", "/api/creative/v2/plans/{pid}/production/render"): "owner-plain",
    ("POST", "/api/creative/v2/plans/{pid}/production/generate-assets"): "owner-plain",
    ("POST", "/api/creative/v2/plans/{pid}/production/assets"): "owner-plain",
    ("POST", "/api/creative/v2/plans/{pid}/production/media-link"): "owner-plain",
    ("POST", "/api/desk"): "owner-plain",
    ("POST", "/api/exports"): "owner-plain",
    ("POST", "/api/games"): "owner-plain",
    ("POST", "/api/games/create"): "owner-plain",
    ("POST", "/api/games/{game_id}/builds"): "owner-plain",
    ("POST", "/api/games/{game_id}/lock/approve"): "owner-plain",
    ("POST", "/api/games/{game_id}/play"): "owner-plain",
    ("POST", "/api/games/{game_id}/rollback"): "owner-plain",
    ("POST", "/api/games/{game_id}/requests"): "owner-plain",
    ("POST", "/api/meetings"): "owner-plain",
    ("POST", "/api/meetings/{mid}/analyze"): "owner-plain",
    ("POST", "/api/meetings/{mid}/analyze/cancel"): "owner-plain",
    ("POST", "/api/meetings/{mid}/proposals/{pid}/approve"): "owner-plain",
    ("POST", "/api/meetings/{mid}/proposals/{pid}/reject"): "owner-plain",
    ("POST", "/api/meetings/{mid}/retranscribe"): "owner-plain",
    ("POST", "/api/meetings/{mid}/stt/{jid}/corrections"): "owner-plain",
    (
        "POST",
        "/api/meetings/{mid}/stt/{jid}/overlap/{candidate_index}/compare",
    ): "owner-plain",
    ("POST", "/api/meetings/{mid}/stt/{jid}/reprocess"): "owner-plain",
    ("POST", "/api/meetings/{mid}/transcribe"): "owner-plain",
    ("POST", "/api/meetings/{mid}/transcript"): "owner-plain",
    ("POST", "/api/memo-categories"): "owner-plain",
    ("POST", "/api/memos"): "owner-plain",
    ("POST", "/api/google-accounts/{account_id}/features/{feature}/resume"): "owner-scoped",
    ("POST", "/api/google-accounts/{account_id}/features/{feature}/verify"): "owner-scoped",
    ("POST", "/api/google-accounts/{account_id}/login"): "owner-scoped",
    ("POST", "/api/google-accounts/{account_id}/login/cancel"): "owner-scoped",
    ("POST", "/api/google-accounts/{account_id}/login/complete"): "owner-scoped",
    ("POST", "/api/notebooklm/auth/login-window"): "owner-scoped",
    ("POST", "/api/notebooklm/auth/login-window/cancel"): "owner-scoped",
    ("POST", "/api/notebooklm/auth/login-window/complete"): "owner-scoped",
    ("POST", "/api/notebooklm/auth/resume"): "owner-scoped",
    ("POST", "/api/notebooklm/auth/verify"): "owner-scoped",
    ("POST", "/api/notebooklm/jobs/{job_id}/cancel"): "owner-scoped",
    ("POST", "/api/notebooklm/notebooks"): "owner-scoped",
    ("POST", "/api/notebooklm/notebooks/{notebook_id}/queries"): "owner-scoped",
    ("POST", "/api/notebooklm/notebooks/{notebook_id}/query"): "owner-scoped",
    ("POST", "/api/notebooklm/notebooks/{notebook_id}/sources/text"): "owner-scoped",
    ("POST", "/api/notebooklm/notebooks/{notebook_id}/sources/upload"): "owner-scoped",
    ("POST", "/api/notebooklm/notebooks/{notebook_id}/sources/url"): "owner-scoped",
    ("POST", "/api/notifications/read-all"): "owner-plain",
    ("POST", "/api/ops-logs/sync"): "owner-scoped",
    ("POST", "/api/orchestrator/agents/{session_id}/stop"): "agent-plain",
    ("POST", "/api/orchestrator/context/reset"): "agent-plain",
    ("POST", "/api/orchestrator/luna-common30-v2"): "agent-plain",
    ("POST", "/api/orchestrator/pms/projects/{pid}/tasks"): "agent-plain",
    ("POST", "/api/orchestrator/schedules"): "agent-plain",
    ("POST", "/api/orchestrator/stop-all"): "agent-plain",
    ("POST", "/api/orchestrator/workers"): "agent-plain",
    ("POST", "/api/orchestrator/workers/{worker_id}/messages"): "agent-plain",
    ("POST", "/api/orchestrator/workers/{worker_id}/dispatch"): "agent-plain",
    ("POST", "/api/orchestrator/workers/{worker_id}/reconcile"): "agent-plain",
    ("POST", "/api/orchestrator/workers/{worker_id}/stop"): "agent-plain",
    ("POST", "/api/orchestrator/workers/{worker_id}/verification"): "agent-plain",
    ("POST", "/api/pms/projects"): "owner-plain",
    ("POST", "/api/pms/projects/{pid}/milestones"): "owner-plain",
    ("POST", "/api/pms/projects/{pid}/notes"): "owner-plain",
    ("POST", "/api/pms/projects/{pid}/tasks"): "owner-plain",
    ("POST", "/api/pms/tasks/{tid}/move"): "owner-plain",
    ("POST", "/api/push/subscriptions"): "owner-plain",
    ("POST", "/api/push/test"): "owner-plain",
    ("POST", "/api/remote-desktop/devices/{device_id}/connect"): "owner-plain",
    ("POST", "/api/remote-desktop/disconnect"): "owner-plain",
    ("POST", "/api/research/experiments/{experiment_id}/control"): "owner-plain",
    ("POST", "/api/research/experiments/{experiment_id}/execute"): "owner-plain",
    ("POST", "/api/research/experiments/{experiment_id}/report"): "owner-plain",
    ("POST", "/api/research/studies"): "owner-plain",
    ("POST", "/api/research/studies/{study_id}/experiments"): "owner-plain",
    ("POST", "/api/research/studies/{study_id}/nodes"): "owner-plain",
    ("POST", "/api/research/studies/{study_id}/relations"): "owner-plain",
    ("POST", "/api/research/studies/{study_id}/select"): "owner-plain",
    ("POST", "/api/research/studies/{study_id}/sources"): "owner-plain",
    ("POST", "/api/research/catalog/sync"): "owner-plain",
    ("POST", "/api/research/experiments/validate"): "owner-plain",
    ("POST", "/api/research/experiments/{experiment_id}/analyze"): "owner-plain",
    ("POST", "/api/research/experiments/{experiment_id}/external-report"): "owner-plain",
    ("POST", "/api/research/experiments/{experiment_id}/run"): "owner-plain",
    ("POST", "/api/research/methods"): "owner-plain",
    ("POST", "/api/research/methods/{method_id}/promote"): "owner-plain",
    ("POST", "/api/research/methods/{method_id}/relations"): "owner-plain",
    ("POST", "/api/research/runs/{run_id}/cancel"): "owner-plain",
    ("POST", "/api/research/studies/{study_id}/best-results"): "owner-plain",
    ("POST", "/api/research/studies/{study_id}/export-report"): "owner-plain",
    ("POST", "/api/research/taxonomy"): "owner-plain",
    ("POST", "/api/reverse"): "owner-scoped",
    ("POST", "/api/reverse/{job_id}/messages"): "owner-scoped",
    ("POST", "/api/reverse/{job_id}/retry"): "owner-scoped",
    ("POST", "/api/runs/{run_id}/cancel"): "owner-plain",
    ("POST", "/api/runs/{run_id}/desk-feedback"): "owner-plain",
    ("POST", "/api/sessions"): "owner-plain",
    ("POST", "/api/sessions/{session_id}/runs"): "owner-plain",
    ("POST", "/api/shares"): "owner-plain",
    ("POST", "/api/shares/preview"): "owner-plain",
    ("POST", "/api/stocks/tossinvest/check"): "owner-scoped",
    ("POST", "/api/stocks/tossinvest/login-window"): "owner-scoped",
    ("POST", "/api/stocks/tossinvest/sync"): "owner-scoped",
    ("POST", "/api/system/stop-all"): "owner-plain",
    ("POST", "/api/transfer-tokens"): "owner-plain",
    ("POST", "/api/transfers"): "owner-plain",
    ("POST", "/api/transfers/folders"): "owner-plain",
    ("POST", "/api/transfers/raw"): "transfer-plain",
    ("POST", "/api/transfers/simple"): "transfer-plain",
    ("POST", "/api/transfers/{tid}/complete"): "owner-plain",
    ("POST", "/api/tts/cache/prune"): "owner-plain",
    ("POST", "/api/tts/jobs"): "owner-plain",
    ("POST", "/api/tts/speak"): "owner-plain",
    ("POST", "/api/uploads"): "owner-plain",
    ("POST", "/api/weather/generate"): "owner-plain",
    ("POST", "/api/workspaces"): "owner-plain",
    ("PUT", "/api/accounts/override"): "owner-plain",
    ("PUT", "/api/artcraft/bugs/{bug_id}"): "owner-plain",
    ("PUT", "/api/assistant/instructions"): "owner-plain",
    ("PUT", "/api/assistant/kb/documents/{document_id}/engine-access"): "owner-plain",
    ("PUT", "/api/meetings/{mid}/stt/{jid}/speakers"): "owner-plain",
    ("PUT", "/api/memo-categories/order"): "owner-plain",
    ("PUT", "/api/remote-desktop/devices/{device_id}"): "owner-plain",
    ("PUT", "/api/shares/{sid}/publication"): "owner-plain",
    ("PUT", "/api/system/qwen-worker"): "owner-plain",
    ("PUT", "/api/transfers/{tid}"): "owner-plain",
    ("WS", "/api/remote-desktop/socket"): "ws-ticket",
}


MCP_ORCHESTRATOR_ONLY = frozenset(
    {
        "account_usage",
        "luna_common30_v2_cancel",
        "luna_common30_v2_status",
        "my_context",
        "reset_my_context",
        "manage_work",
        "dispatch_queued_worker_run",
        "reconcile_worker_lease",
        "start_luna_common30_v2",
        "stop_agents",
    }
)
# Worker management (spawn/message/list/get/stop_worker) registers for both MCP roles;
# the API enforces one-level delegation and subtree ownership (2026-10-28).

MCP_SCOPE: dict[str, str] = {
    "activate_copilot_account": "planner",
    "collection_mission_status": "knowledge",
    "collection_mission_retry": "knowledge",
    "github_accounts": "host",
    "github_switch_account": "host",
    "comfy_cancel": "comfy",
    "comfy_job": "comfy",
    "comfy_jobs": "comfy",
    "comfy_profiles": "comfy",
    "comfy_readiness": "comfy",
    "comfy_reconcile": "comfy",
    "comfy_submit": "comfy",
    "creative_v2_auto_assets_status": "creative",
    "creative_v2_auto_assets_start": "creative",
    "creative_v2_auto_assets_retry": "creative",
    "creative_v2_capabilities": "creative",
    "creative_v2_catalog": "creative",
    "creative_v2_plan": "creative",
    "creative_v2_plan_approve": "creative",
    "creative_v2_plan_create": "creative",
    "creative_v2_plan_edit": "creative",
    "creative_v2_plans": "creative",
    "creative_v2_production_assets": "creative",
    "creative_v2_production_dispatch": "creative",
    "creative_v2_production_media_link": "creative",
    "creative_v2_production_render": "creative",
    "creative_v2_production_status": "creative",
    "creative_v2_recipe_matrix": "creative",
    "creative_v2_registries": "creative",
    "creative_v2_styles": "creative",
    "creative_v2_style": "creative",
    "creative_v2_style_put": "creative",
    "account_usage": "orchestration",
    "backend_deploy_status": "ops",
    "begin_environment_probe": "host",
    "cancel_scheduled_action": "planner",
    "create_checklist_item": "planner",
    "create_checklist_template": "planner",
    "create_event": "planner",
    "create_memo": "planner",
    "create_pms_task": "pms",
    "delete_checklist_item": "planner",
    "delete_checklist_template": "planner",
    "delete_event": "planner",
    "delete_memo": "planner",
    "describe_entity": "memory",
    "design_system_catalog": "design",
    "design_system_item": "design",
    "design_system_search": "design",
    "execution_capabilities": "host",
    "forget": "memory",
    "get_entity": "memory",
    "get_meeting": "meetings",
    "get_memo": "planner",
    "get_worker": "orchestration",
    "dispatch_queued_worker_run": "orchestration",
    "reconcile_worker_lease": "orchestration",
    "host_gpu_bench": "host",
    "host_gpu_start": "host",
    "host_gpu_status": "host",
    "host_gpu_stop": "host",
    "host_operation_cancel": "host",
    "host_operation_start": "host",
    "host_operation_status": "host",
    "knowledge_assertion": "knowledge",
    "knowledge_extraction_status": "knowledge",
    "knowledge_next_batch": "knowledge",
    "kb_search": "knowledge",
    "kb_summary": "knowledge",
    "kb_observations": "knowledge",
    "kb_documents": "knowledge",
    "kb_document": "knowledge",
    "kb_evidence": "knowledge",
    "knowledge_recall": "knowledge",
    "knowledge_retry_extraction": "knowledge",
    "knowledge_review_assertion": "knowledge",
    "knowledge_sources": "knowledge",
    "knowledge_start_extraction": "knowledge",
    "knowledge_submit_batch": "knowledge",
    "library_catalog": "knowledge",
    "link": "memory",
    "keep_artifact": "files",
    "list_artifacts": "files",
    "list_checklist": "planner",
    "list_checklist_templates": "planner",
    "list_events": "planner",
    "list_meetings": "meetings",
    "list_memo_categories": "planner",
    "list_memo_tags": "planner",
    "list_memos": "planner",
    "list_pms_projects": "pms",
    "list_scheduled_actions": "planner",
    "list_sessions": "sessions",
    "list_workers": "orchestration",
    "luna_common30_v2_cancel": "orchestration",
    "luna_common30_v2_status": "orchestration",
    "manage_pms": "pms",
    "manage_research": "knowledge",
    "message_worker": "orchestration",
    "my_context": "context",
    "google_account_status": "google_account",
    "notebooklm_add_source": "notebooklm",
    "notebooklm_create": "notebooklm",
    "notebooklm_delete": "notebooklm",
    "notebooklm_delete_source": "notebooklm",
    "notebooklm_get": "notebooklm",
    "notebooklm_job": "notebooklm",
    "notebooklm_job_cancel": "notebooklm",
    "notebooklm_list": "notebooklm",
    "notebooklm_query": "notebooklm",
    "notebooklm_rename": "notebooklm",
    "notebooklm_status": "notebooklm",
    "notify_user": "notify",
    "ops_deploy_log": "ops",
    "ops_get_issue": "ops",
    "ops_list_deploys": "ops",
    "ops_list_issues": "ops",
    "ops_search_logs": "ops",
    "ops_update_issue": "ops",
    "read_session": "sessions",
    "recall": "memory",
    "remember": "memory",
    "report_execution_environment": "host",
    "request_backend_deploy": "ops",
    "reset_my_context": "context",
    "manage_work": "context",
    "reverse_get": "reverse",
    "reverse_list": "reverse",
    "reverse_message": "reverse",
    "reverse_retry": "reverse",
    "reverse_start": "reverse",
    "schedule_action": "planner",
    "search_meeting_items": "meetings",
    "search_meetings": "meetings",
    "search_memos": "planner",
    "server_resources": "host",
    "set_artifact_area": "files",
    "share_file": "files",
    "spawn_worker": "orchestration",
    "start_luna_common30_v2": "orchestration",
    "stop_agents": "orchestration",
    "stop_worker": "orchestration",
    "tossinvest_status": "stocks",
    "update_checklist_item": "planner",
    "update_checklist_template": "planner",
    "update_event": "planner",
    "update_memo": "planner",
}
