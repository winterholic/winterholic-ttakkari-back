"""Unit tests for Platform V2 Frontend API Audit Scanner (Worker C)
TDD: Verify accurate HTTP method inference (with AST function boundary parsing),
coverage of stream.ts / useConnection.ts / refreshAccessToken / publicShareRequest,
frontend module/plane fields, binding to 25 backend module registry,
and negative controls (NC-API-MULTI-CALL, NC-API-COMMENT-STRING, NC-REGISTRY-MISSING, NC-REGISTRY-MALFORMED).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.platform_v2_test_audit import audit_frontend_api


def test_accurate_http_method_inference_against_misclassifications():
    """Verify that getSession, getArtifact, getOpsIssue are accurately parsed as GET,

    and not corrupted by neighboring function declarations (POST or PATCH).
    """
    client_ts_path = Path(
        "/Users/winterholic/development/projects/winterholic-ttakkari/front/src/api/client.ts"
    )
    assert client_ts_path.exists()

    result = audit_frontend_api(client_ts_path=client_ts_path)
    endpoints = {e["function"]: e for e in result["endpoints"]}

    # Negative counter-examples from verification report
    assert "getSession" in endpoints
    assert endpoints["getSession"]["method"] == "GET", (
        f"getSession must be GET, got {endpoints['getSession']['method']}"
    )

    assert "getArtifact" in endpoints
    assert endpoints["getArtifact"]["method"] == "GET", (
        f"getArtifact must be GET, got {endpoints['getArtifact']['method']}"
    )

    assert "getOpsIssue" in endpoints
    assert endpoints["getOpsIssue"]["method"] == "GET", (
        f"getOpsIssue must be GET, got {endpoints['getOpsIssue']['method']}"
    )

    # Positive methods
    assert endpoints["createSession"]["method"] == "POST"
    assert endpoints["patchSession"]["method"] == "PATCH"
    assert endpoints["patchOpsIssue"]["method"] == "PATCH"


def test_stream_and_fetch_sources_included():
    """Verify stream.ts SSE (line 45), useConnection.ts fetchHealth (line 25),

    refreshAccessToken direct fetch, and publicShareRequest wrappers.
    """
    front_root = Path(
        "/Users/winterholic/development/projects/winterholic-ttakkari/front"
    )
    result = audit_frontend_api(front_root=front_root)
    endpoints = result["endpoints"]

    # 1. SSE stream endpoint in stream.ts
    stream_matches = [e for e in endpoints if "stream.ts" in e["source_file"]]
    assert len(stream_matches) >= 1, "stream.ts SSE endpoint must be included"
    stream_ep = stream_matches[0]
    assert stream_ep["function"] == "subscribeRun"
    assert stream_ep["method"] == "GET"
    assert stream_ep["line_number"] == 45, (
        f"Expected line 45 for fetchEventSource, got {stream_ep['line_number']}"
    )
    assert stream_ep.get("protocol") == "Server-Sent Events (SSE)"

    # 2. useConnection.ts /api/health
    health_matches = [e for e in endpoints if e["endpoint"] == "/api/health"]
    assert len(health_matches) >= 1, "useConnection.ts /api/health must be included"
    health_ep = health_matches[0]
    assert health_ep["function"] == "fetchHealth", (
        f"Expected function fetchHealth, got {health_ep['function']}"
    )
    assert health_ep["line_number"] == 25, (
        f"Expected line 25 for fetch in useConnection.ts, got {health_ep['line_number']}"
    )
    assert health_ep["method"] == "GET"

    # 3. refreshAccessToken direct fetch
    refresh_matches = [e for e in endpoints if e["function"] == "refreshAccessToken"]
    assert len(refresh_matches) >= 1, "refreshAccessToken direct fetch must be included"
    assert refresh_matches[0]["method"] == "POST"
    assert refresh_matches[0]["endpoint"] == "/api/auth/refresh"

    # 4. publicShareRequest wrappers
    public_matches = [e for e in endpoints if "public/shares" in e["endpoint"]]
    assert len(public_matches) >= 2, "Public meeting share wrappers must be included"
    pub_fns = {e["function"] for e in public_matches}
    assert "getPublicMeetingShare" in pub_fns
    assert "downloadPublicMeetingShare" in pub_fns


def test_nc_api_multi_call_negative_control():
    """NC-API-MULTI-CALL: Function with multiple API calls must represent all calls,

    not just the first one.
    """
    client_code = """
    export async function raw(path: string, options?: any) { return fetch(path, options); }
    export async function json(path: string, options?: any) { return raw(path, options); }
    export const twoCalls = () => { json("/api/one"); return json("/api/two", {method:"POST"}); };
    """
    result = audit_frontend_api(client_source_text=client_code)
    calls = [e for e in result["endpoints"] if e["function"] == "twoCalls"]

    assert len(calls) == 2, f"Expected 2 calls for twoCalls, got {len(calls)}"
    assert calls[0]["endpoint"] == "/api/one"
    assert calls[0]["method"] == "GET"
    assert calls[1]["endpoint"] == "/api/two"
    assert calls[1]["method"] == "POST"


def test_nc_api_comment_string_negative_control():
    """NC-API-COMMENT-STRING: Commented json/raw call must not generate an endpoint."""
    client_code = """
    export async function raw(path: string, options?: any) { return fetch(path, options); }
    export async function json(path: string, options?: any) { return raw(path, options); }
    // export const fake = () => json("/api/comment");
    /* export const fakeBlock = () => json("/api/block-comment"); */
    export const real = () => json("/api/real");
    """
    result = audit_frontend_api(client_source_text=client_code)
    endpoints = [e["endpoint"] for e in result["endpoints"]]

    assert "/api/comment" not in endpoints, "Commented endpoint must not be extracted"
    assert "/api/block-comment" not in endpoints, (
        "Block-commented endpoint must not be extracted"
    )
    assert "/api/real" in endpoints


def test_nc_registry_missing_negative_control():
    """NC-REGISTRY-MISSING: Nonexistent registry path must make registry binding incomplete/unknown

    and must NOT emit provisional IDs as if checked.
    """
    nonexistent_path = REPO_ROOT / "docs/design/platform-v2/nonexistent-registry.json"
    result = audit_frontend_api(backend_inventory_path=nonexistent_path)

    assert result["registry_status"] == "missing"
    assert result["backend_registry_version"] == "missing"
    assert result["coverage_status"] == "incomplete"

    # All endpoints must have unknown status and None backend ID
    for ep in result["endpoints"]:
        assert ep["mapping_status"] == "unknown", (
            f"Expected unknown mapping_status when registry is missing, got {ep['mapping_status']} for {ep['function']}"
        )
        assert ep["backend_module_id"] is None, (
            f"Expected None backend_module_id when registry is missing, got {ep['backend_module_id']} for {ep['function']}"
        )

    assert len(result["unmatched_ledger"]) == len(result["endpoints"])


def test_nc_registry_malformed_negative_control(tmp_path: Path):
    """NC-REGISTRY-MALFORMED: Malformed JSON registry must mark registry binding incomplete/unknown

    and must NOT emit provisional IDs.
    """
    malformed_file = tmp_path / "malformed_registry.json"
    malformed_file.write_text("{ this is not valid json", encoding="utf-8")

    result = audit_frontend_api(backend_inventory_path=malformed_file)

    assert result["registry_status"] == "malformed"
    assert result["backend_registry_version"] == "malformed"
    assert result["coverage_status"] == "incomplete"

    for ep in result["endpoints"]:
        assert ep["mapping_status"] == "unknown"
        assert ep["backend_module_id"] is None

    assert len(result["unmatched_ledger"]) == len(result["endpoints"])


def test_fields_and_backend_module_binding_and_unmatched_ledger():
    """Verify that every endpoint has:

    frontend_module, plane, version, source_file, line_number.
    When registry is valid, mapped backend module IDs are strictly subset of the 25 live backend module inventory.
    """
    front_root = Path(
        "/Users/winterholic/development/projects/winterholic-ttakkari/front"
    )
    backend_inv_path = (
        REPO_ROOT / "docs/design/platform-v2/backend-module-inventory.json"
    )
    result = audit_frontend_api(
        front_root=front_root, backend_inventory_path=backend_inv_path
    )

    endpoints = result["endpoints"]
    assert len(endpoints) > 0
    assert result["registry_status"] == "valid"
    # The live client contains dynamic callsites. Registry completeness and
    # extraction completeness are separate: retain every unresolved callsite.
    assert result["coverage_status"] == "incomplete"
    assert result["unresolved_ledger"]
    assert all(item["reason"] for item in result["unresolved_ledger"])

    valid_backend_ids = {
        "core.auth_session",
        "core.workspaces_transfers",
        "core.accounts_quota",
        "core.generation_deploy",
        "core.system_routing",
        "exec.run_engine",
        "exec.durable_scheduler",
        "exec.host_gpu",
        "exec.delivery",
        "domain.memory_ontology",
        "domain.knowledge_rag",
        "domain.collector",
        "domain.creative_studio",
        "domain.notebooklm",
        "domain.research_lab",
        "domain.reverse_eng",
        "domain.meetings_stt",
        "domain.pms_tasks",
        "domain.design_system",
        "domain.media_tts",
        "domain.game_studio",
        "domain.personal_tools",
        "domain.external_connectors",
        "domain.shares_artifacts",
        "ops.monitoring",
    }

    invented_forbidden_ids = {
        "domain.tasks",
        "domain.files.workspace",
        "domain.opslogs",
    }

    for ep in endpoints:
        assert "frontend_module" in ep, f"Missing frontend_module in {ep}"
        assert "plane" in ep, f"Missing plane in {ep}"
        assert "version" in ep, f"Missing version in {ep}"
        assert "source_file" in ep, f"Missing source_file in {ep}"
        assert "line_number" in ep, f"Missing line_number in {ep}"
        assert "mapping_status" in ep, f"Missing mapping_status in {ep}"
        assert ep["mapping_status"] in {"provisional", "unknown", "verified"}

        mapped_b_id = ep.get("backend_module_id")
        if mapped_b_id:
            assert mapped_b_id not in invented_forbidden_ids, (
                f"Must not invent nonexistent id {mapped_b_id}"
            )
            assert mapped_b_id in valid_backend_ids, (
                f"Invalid backend module ID {mapped_b_id}"
            )

    assert "unmatched_ledger" in result
