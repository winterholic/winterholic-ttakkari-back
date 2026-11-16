"""Gateway control 앱 전용 클라이언트. public 읽기 키와 control 토큰은 설정·경로·헤더 모두 분리한다.

HTTP transport(HttpGatewayTransport)와 계약 어댑터(GatewayControl)를 나눠, 코어 계약이 바뀌면 GatewayControl 의
경로·본문 매핑만 고친다. docs/context-gateway-contract.md 가 없던 시점에는 코어 작업 트리의
control 앱(POST /control/v1/snapshots, /snapshots/{id}/revoke, /keys, /keys/{id}/revoke)을 기준으로 맞췄다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from ttakkari.config import Settings, get_settings

CONTROL_PREFIX = "/control/v1"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class GatewayError(Exception):
    """Gateway 호출 실패의 공통 뿌리. 메시지에 토큰·본문을 싣지 않는다."""


class GatewayNotConfigured(GatewayError):
    def __init__(self, status: dict[str, Any]) -> None:
        super().__init__("gateway_not_configured")
        self.status = status


class GatewayUnavailable(GatewayError):
    """연결 실패·시간 초과·5xx."""


class GatewayRejected(GatewayError):
    """Gateway 가 요청을 거절했거나(4xx) 인증에 실패했다."""

    def __init__(self, http_status: int, gateway_code: str | None = None) -> None:
        super().__init__(f"gateway_rejected:{http_status}")
        self.http_status = http_status
        self.gateway_code = gateway_code


class GatewayBadResponse(GatewayError):
    """성공 응답이지만 계약과 다르다(필수 필드 누락·digest 불일치)."""


@dataclass(frozen=True)
class GatewayReply:
    status: int
    body: dict[str, Any]


class GatewayTransport(Protocol):
    async def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> GatewayReply: ...


def _control_url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "control URL 은 http(s)://호스트 형식이어야 합니다."
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        return "원격 control URL 은 https 여야 합니다."
    if parts.username or parts.password:
        return "control URL 에 인증정보를 넣을 수 없습니다."
    return None


def gateway_status(s: Settings | None = None) -> dict[str, Any]:
    """설정 상태. 토큰·전체 URL 은 싣지 않고 호스트만 보인다."""
    s = s or get_settings()
    missing = [name for name, v in (("context_gateway_control_url", s.context_gateway_control_url),
                                    ("context_gateway_control_token", s.context_gateway_control_token)) if not v]
    problems: list[str] = []
    if s.context_gateway_control_url and (p := _control_url_problem(s.context_gateway_control_url)):
        problems.append(p)
    host = urlsplit(s.context_gateway_control_url).hostname if s.context_gateway_control_url and not problems else None
    return {"configured": not missing and not problems, "missing": missing, "problems": problems,
            "control_host": host, "public_url": s.context_gateway_public_url or None}


class HttpGatewayTransport:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0,
                 http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._http_transport = http_transport  # 시험에서만 바꾼다. 클라이언트 설정(리다이렉트 금지)은 항상 이 클래스가 정한다.

    def __repr__(self) -> str:
        return f"HttpGatewayTransport(base_url={self._base!r})"

    async def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> GatewayReply:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            # 리다이렉트를 따르지 않는다. 토큰이 다른 호스트로 넘어가지 않게 한다.
            async with httpx.AsyncClient(follow_redirects=False, timeout=self._timeout,
                                         transport=self._http_transport) as c:
                r = await c.request(method, self._base + path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            # 원 예외 문구에 헤더·본문이 섞일 수 있어 종류만 남긴다.
            raise GatewayUnavailable(type(exc).__name__) from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        return GatewayReply(r.status_code, data if isinstance(data, dict) else {})


def _code(reply: GatewayReply) -> str | None:
    code = reply.body.get("code")
    return code if isinstance(code, str) and len(code) <= 64 and code.replace("_", "").isalnum() else None


class GatewayControl:
    """Private Publisher 가 쓰는 control 계약 어댑터. 모든 메서드는 확인된 성공만 값으로 돌려준다."""

    def __init__(self, transport: GatewayTransport | None, *, public_url: str | None = None,
                 status: dict[str, Any] | None = None) -> None:
        self._transport = transport
        self.public_url = public_url
        self.status = status or gateway_status()

    @property
    def configured(self) -> bool:
        return self._transport is not None

    def require(self) -> GatewayTransport:
        if self._transport is None:
            raise GatewayNotConfigured(self.status)
        return self._transport

    async def _send(self, method: str, path: str, body: dict[str, Any] | None, ok: set[int]) -> dict[str, Any]:
        reply = await self.require().request(method, f"{CONTROL_PREFIX}{path}", body)
        if reply.status in ok:
            return reply.body
        if reply.status >= 500:
            raise GatewayUnavailable(f"http_{reply.status}")
        raise GatewayRejected(reply.status, _code(reply))

    async def publish_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """payload 는 코어 SnapshotPublishRequest 와 같다. 영수증에 문서 해시가 있으면 보낸 것과 같아야 성공이다.

        계약은 영수증에 프로젝트 UUID 를 약속하지 않는다. 있으면 body["project_id"] 로 받아 키 발급에 쓴다."""
        body = await self._send("POST", "/snapshots", payload, {200, 201})
        sent = {d["id"]: d["sha256"] for d in payload["documents"]}
        docs = body.get("documents")
        if not isinstance(body.get("id"), str) or not body.get("manifest_sha256"):
            raise GatewayBadResponse("snapshot receipt is missing id/manifest_sha256")
        if isinstance(docs, list) and {d.get("id"): d.get("sha256") for d in docs if isinstance(d, dict)} != sent:
            raise GatewayBadResponse("snapshot receipt does not match what was published")
        return body

    async def revoke_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        body = await self._send("POST", f"/snapshots/{snapshot_id}/revoke", None, {200})
        if body.get("revoked") is not True or str(body.get("snapshot_id", "")).lower() != snapshot_id.lower():
            raise GatewayBadResponse("snapshot revoke receipt is missing")
        return body

    async def issue_key(self, project_id: str, *, ttl_seconds: int, label: str, scopes: list[str]) -> dict[str, Any]:
        """키는 단회 교환용이다. 코어가 수명을 정하므로 요청보다 오래 사는 키는 호출한 쪽이 거절한다."""
        body = await self._send("POST", "/keys", {"label": label, "ttl_seconds": ttl_seconds,
                                                  "projects": {project_id: {"scopes": scopes}}}, {200, 201})
        if not all(isinstance(body.get(k), str) and body[k] for k in ("key_id", "key", "expires_at")):
            raise GatewayBadResponse("key response is missing key_id/key/expires_at")
        return body

    async def revoke_key(self, key_id: str) -> dict[str, Any]:
        body = await self._send("POST", f"/keys/{key_id}/revoke", None, {200})
        if body.get("revoked") is not True or str(body.get("key_id", "")).lower() != key_id.lower():
            raise GatewayBadResponse("key revoke receipt is missing")
        return body


def build_control(s: Settings | None = None) -> GatewayControl:
    s = s or get_settings()
    status = gateway_status(s)
    transport = None
    if status["configured"]:
        transport = HttpGatewayTransport(s.context_gateway_control_url, s.context_gateway_control_token,
                                         timeout=s.context_gateway_timeout_seconds)
    return GatewayControl(transport, public_url=s.context_gateway_public_url or None, status=status)
