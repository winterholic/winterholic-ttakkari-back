"""Gateway control 클라이언트: control 토큰과 public 읽기 키를 섞지 않고, 오류를 성공으로 만들지 않는다."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ttakkari.config import Settings
from ttakkari.context_sharing.gateway import (
    GatewayBadResponse,
    GatewayControl,
    GatewayNotConfigured,
    GatewayRejected,
    GatewayUnavailable,
    HttpGatewayTransport,
    build_control,
    gateway_status,
)

pytestmark = pytest.mark.security
TOKEN = "synthetic-control-token-0123456789"


def transport(handler, **kw) -> HttpGatewayTransport:
    return HttpGatewayTransport("https://control.example.test/", TOKEN, http_transport=httpx.MockTransport(handler), **kw)


async def test_snapshot_adapter_uses_flat_post_contract_and_only_the_control_token():
    seen = []
    snapshot_id = "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67"
    project_id = "7bf808c4-b492-442e-9e7e-c753b42b420d"
    payload = {"project_slug": "synthetic-alpha", "project_name": "Synthetic Alpha", "project_description": "",
               "source_refs": ["library-document:5b9d7d8f-7e96-49db-b8eb-9577e35aa982"],
               "source_revision": "library-index:revision", "generated_at": "2026-11-16T00:00:00+00:00",
               "verified_at": None,
               "documents": [{"id": "overview", "title": "Overview", "content": "hello",
                              "content_type": "text/markdown", "source_refs": ["opaque-doc-ref"],
                              "source_revision": "document-sha", "generated_at": "2026-11-16T00:00:00+00:00",
                              "verified_at": None, "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": snapshot_id, "project_id": project_id,
                                         "manifest_sha256": "a" * 64,
                                         "documents": [{"id": "overview", "sha256": payload["documents"][0]["sha256"]}]})

    await GatewayControl(transport(handler)).publish_snapshot(payload)
    req, = seen
    assert req.method == "POST"
    assert str(req.url) == "https://control.example.test/control/v1/snapshots"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content) == payload


async def test_redirects_are_not_followed_so_the_token_cannot_leak():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example.test/steal"})

    reply = await transport(handler).request("POST", "/control/v1/x")
    assert reply.status == 302 and calls == ["https://control.example.test/control/v1/x"]


@pytest.mark.parametrize("exc", [httpx.ConnectError("boom " + TOKEN), httpx.ReadTimeout("slow " + TOKEN)])
async def test_network_errors_become_unavailable_without_leaking_the_token(exc):
    def handler(request: httpx.Request):
        raise exc

    with pytest.raises(GatewayUnavailable) as e:
        await transport(handler).request("GET", "/control/v1/x")
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value)


async def test_non_json_reply_is_still_a_reply_with_empty_body():
    reply = await transport(lambda r: httpx.Response(502, text="<html>bad gateway</html>")).request("GET", "/x")
    assert reply.status == 502 and reply.body == {}


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_status_reports_missing_settings_without_values():
    s = settings(context_gateway_control_url="", context_gateway_control_token="")
    st = gateway_status(s)
    assert st["configured"] is False
    assert st["missing"] == ["context_gateway_control_url", "context_gateway_control_token"]
    with pytest.raises(GatewayNotConfigured):
        build_control(s).require()


def test_status_hides_the_token_and_exposes_only_the_host():
    s = settings(context_gateway_control_url="https://control.example.test/base", context_gateway_control_token=TOKEN)
    st = gateway_status(s)
    assert st == {"configured": True, "missing": [], "control_host": "control.example.test", "public_url": None,
                  "problems": []}


@pytest.mark.parametrize("url", ["http://control.example.test", "ftp://x", "control.example.test"])
def test_plain_http_to_a_remote_control_host_is_rejected(url):
    s = settings(context_gateway_control_url=url, context_gateway_control_token=TOKEN)
    st = gateway_status(s)
    assert st["configured"] is False and st["problems"]
    with pytest.raises(GatewayNotConfigured):
        build_control(s).require()


def test_loopback_http_is_allowed_for_local_development():
    s = settings(context_gateway_control_url="http://127.0.0.1:8899", context_gateway_control_token=TOKEN)
    assert gateway_status(s)["configured"] is True


def test_control_token_is_not_in_the_settings_repr():
    s = settings(context_gateway_control_url="https://c.example.test", context_gateway_control_token=TOKEN)
    assert TOKEN not in repr(s)


async def test_http_errors_map_to_rejected_with_gateway_code_only():
    def handler(request):
        return httpx.Response(409, json={"code": "snapshot_conflict", "message": "Authorization: Bearer " + TOKEN})

    control = GatewayControl(transport(handler))
    with pytest.raises(GatewayRejected) as e:
        await control.revoke_key("11111111-1111-1111-1111-111111111111")
    assert e.value.gateway_code == "snapshot_conflict"
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value.__dict__)


async def test_transport_repr_hides_the_token():
    t = transport(lambda r: httpx.Response(200, json={}))
    assert TOKEN not in repr(t) and TOKEN not in str(t.__dict__.get("_base"))


@pytest.mark.parametrize("receipt", [
    {"id": "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67", "manifest_sha256": "a" * 64,
     "documents": [{"id": "overview", "sha256": "b" * 64}]},
    {"id": "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67", "project_id": "7bf808c4-b492-442e-9e7e-c753b42b420d",
     "manifest_sha256": "a" * 64},
    {"id": "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67", "project_id": "7bf808c4-b492-442e-9e7e-c753b42b420d",
     "manifest_sha256": "a" * 64, "documents": [{"id": "overview", "sha256": "c" * 64}]},
    {"id": "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67", "project_id": "7bf808c4-b492-442e-9e7e-c753b42b420d",
     "manifest_sha256": "a" * 64, "documents": [{"id": ["overview"], "sha256": "b" * 64}]},
])
async def test_publish_requires_a_complete_matching_gateway_receipt(receipt):
    control = GatewayControl(transport(lambda _: httpx.Response(201, json=receipt)))
    payload = {"documents": [{"id": "overview", "sha256": "b" * 64, "content": "body"}]}

    with pytest.raises(GatewayBadResponse):
        await control.publish_snapshot(payload)


async def test_publish_accepts_only_the_contract_created_status():
    receipt = {"id": "4f9b23e2-fd9e-4b93-92be-cbb5b88a4f67", "project_id": "7bf808c4-b492-442e-9e7e-c753b42b420d",
               "manifest_sha256": "a" * 64, "documents": [{"id": "overview", "sha256": "b" * 64}]}
    control = GatewayControl(transport(lambda _: httpx.Response(200, json=receipt)))

    with pytest.raises(GatewayBadResponse):
        await control.publish_snapshot({"documents": [{"id": "overview", "sha256": "b" * 64, "content": "body"}]})


async def test_key_issue_omits_ttl_field_not_supported_by_gateway_contract():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        return httpx.Response(201, json={"key_id": "87fae3bb-8b59-4e96-a10b-7152eb18e231", "key": "synthetic-one-time-key",
                                         "expires_at": expires})

    control = GatewayControl(transport(handler))
    await control.issue_key("7bf808c4-b492-442e-9e7e-c753b42b420d", label="synthetic-client", scopes=["read"])

    assert seen == [{"label": "synthetic-client", "projects": {
        "7bf808c4-b492-442e-9e7e-c753b42b420d": {"scopes": ["read"]}}}]


async def test_key_issue_accepts_only_the_contract_created_status():
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    control = GatewayControl(transport(lambda _: httpx.Response(200, json={
        "key_id": "87fae3bb-8b59-4e96-a10b-7152eb18e231", "key": "synthetic-one-time-key",
        "expires_at": expires})))

    with pytest.raises(GatewayBadResponse):
        await control.issue_key("7bf808c4-b492-442e-9e7e-c753b42b420d", label="synthetic-client", scopes=["read"])


async def test_issue_key_rejects_non_uuid_gateway_key_id():
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    control = GatewayControl(transport(lambda _: httpx.Response(201, json={
        "key_id": "../../unexpected-path", "key": "synthetic-one-time-key", "expires_at": expires})))

    with pytest.raises(GatewayBadResponse):
        await control.issue_key("7bf808c4-b492-442e-9e7e-c753b42b420d", label="synthetic-client", scopes=["read"])


@pytest.mark.parametrize("operation", ["revoke_snapshot", "revoke_key"])
async def test_revoke_refuses_non_uuid_gateway_id_before_sending(operation):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"revoked": True, "snapshot_id": "../../bad", "key_id": "../../bad"})

    control = GatewayControl(transport(handler))
    with pytest.raises(GatewayBadResponse):
        await getattr(control, operation)("../../bad")
    assert calls == []


async def test_project_revoke_uses_core_route_and_validates_receipt():
    requests = []
    project_id = "7bf808c4-b492-442e-9e7e-c753b42b420d"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"project_id": project_id, "revoked": True,
                                         "revoked_at": "2026-11-16T00:00:00+00:00", "snapshots_revoked": 2})

    receipt = await GatewayControl(transport(handler)).revoke_project_sharing(project_id)

    assert receipt["snapshots_revoked"] == 2
    request, = requests
    assert request.method == "POST"
    assert str(request.url) == f"https://control.example.test/control/v1/projects/{project_id}/revoke"
    assert request.content == b""


async def test_project_revoke_rejects_a_receipt_for_another_project():
    control = GatewayControl(transport(lambda _: httpx.Response(200, json={
        "project_id": "11111111-1111-1111-1111-111111111111", "revoked": True,
        "revoked_at": "2026-11-16T00:00:00+00:00", "snapshots_revoked": 1})))

    with pytest.raises(GatewayBadResponse):
        await control.revoke_project_sharing("7bf808c4-b492-442e-9e7e-c753b42b420d")
