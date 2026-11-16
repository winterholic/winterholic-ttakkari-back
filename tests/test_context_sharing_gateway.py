"""Gateway control 클라이언트: control 토큰과 public 읽기 키를 섞지 않고, 오류를 성공으로 만들지 않는다."""
from __future__ import annotations

import httpx
import pytest

from ttakkari.config import Settings
from ttakkari.context_sharing.gateway import (
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


async def test_requests_carry_only_the_control_token_to_the_control_origin():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    reply = await transport(handler).request("PUT", "/control/v1/projects/a/snapshot", {"x": 1})
    assert reply.status == 200 and reply.body == {"ok": True}
    req, = seen
    assert str(req.url) == "https://control.example.test/control/v1/projects/a/snapshot"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


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
