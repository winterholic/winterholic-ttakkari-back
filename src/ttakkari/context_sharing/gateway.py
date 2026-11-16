"""Gateway control 앱 전용 클라이언트. public 읽기 키와 control 토큰은 설정·경로·헤더 모두 분리한다.

HTTP transport(HttpGatewayTransport)와 계약 어댑터(GatewayControl)를 나눠, 코어 계약이 바뀌면 GatewayControl 의
경로·본문 매핑만 고친다. 계약은 docs/context-gateway-contract.md 에 적혀 있다.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from ttakkari.config import Settings, get_settings

CONTROL_PREFIX = "/control/v1"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value.lower()


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


def _connection_settings(s: Settings) -> tuple[Settings, list[str]]:
    """Production-only, owner-readable Gateway credentials; never an app policy source.

    Explicit settings always win, including incomplete settings (fail closed).
    This file can supply only the three Gateway connection values, not arbitrary
    environment variables, commands, paths or operational/security settings.
    """
    if s.env != "prod" or any((s.context_gateway_control_url, s.context_gateway_control_token,
                                s.context_gateway_public_url)):
        return s, []
    path = s.home.parent / "private-resources/service-data/context-gateway/secrets/publisher.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return s, []
    except OSError:
        return s, ["gateway_credentials_file_invalid"]
    try:
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
                raise ValueError("invalid credential file permissions or size")
            raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError("credential file grew beyond the size limit")
            data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"control_url", "control_token", "public_url"}:
            raise ValueError("invalid fields")
        if any(not isinstance(value, str) or not value for value in data.values()):
            raise ValueError("invalid values")
        for name in ("control_url", "public_url"):
            parts = urlsplit(data[name])
            if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                    or parts.query or parts.fragment or parts.path not in {"", "/"}):
                raise ValueError("invalid origin")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", data["control_token"]):
            raise ValueError("invalid control token")
        return s.model_copy(update={"context_gateway_control_url": data["control_url"],
                                    "context_gateway_control_token": data["control_token"],
                                    "context_gateway_public_url": data["public_url"]}), []
    except (OSError, ValueError, UnicodeError, RecursionError):
        return s, ["gateway_credentials_file_invalid"]


def _status(s: Settings, problems: list[str]) -> dict[str, Any]:
    missing = [name for name, v in (("context_gateway_control_url", s.context_gateway_control_url),
                                    ("context_gateway_control_token", s.context_gateway_control_token)) if not v]
    if s.context_gateway_control_url and (p := _control_url_problem(s.context_gateway_control_url)):
        problems.append(p)
    host = urlsplit(s.context_gateway_control_url).hostname if s.context_gateway_control_url and not problems else None
    return {"configured": not missing and not problems, "missing": missing, "problems": problems,
            "control_host": host, "public_url": s.context_gateway_public_url or None}


def gateway_status(s: Settings | None = None) -> dict[str, Any]:
    """설정 상태. 토큰·전체 control URL 은 싣지 않고 호스트만 보인다."""
    effective, problems = _connection_settings(s or get_settings())
    return _status(effective, problems)


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

    async def site_status(self) -> bool:
        body = await self._send("GET", "/site", None, {200})
        if type(body.get("enabled")) is not bool:
            raise GatewayBadResponse("site status response is missing enabled")
        return body["enabled"]

    async def set_site_enabled(self, enabled: bool) -> bool:
        body = await self._send("PATCH", "/site", {"enabled": enabled}, {200})
        if type(body.get("enabled")) is not bool or body["enabled"] is not enabled:
            raise GatewayBadResponse("site status response does not match requested state")
        return body["enabled"]

    async def _send(self, method: str, path: str, body: dict[str, Any] | None, ok: set[int]) -> dict[str, Any]:
        reply = await self.require().request(method, f"{CONTROL_PREFIX}{path}", body)
        if reply.status in ok:
            return reply.body
        if reply.status >= 500:
            raise GatewayUnavailable(f"http_{reply.status}")
        if 400 <= reply.status < 500:
            raise GatewayRejected(reply.status, _code(reply))
        raise GatewayBadResponse(f"unexpected_http_{reply.status}")

    async def publish_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """코어 계약에서 요구하는 전체 영수증을 확인해야 성공으로 돌려준다."""
        documents = payload.get("documents")
        if not isinstance(documents, list) or not documents:
            raise GatewayBadResponse("snapshot request has no documents")
        sent: dict[str, str] = {}
        for doc in documents:
            if (not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not doc["id"]
                    or not isinstance(doc.get("sha256"), str) or not _SHA256.fullmatch(doc["sha256"])
                    or doc["id"] in sent):
                raise GatewayBadResponse("snapshot request contains invalid or duplicate document metadata")
            sent[doc["id"]] = doc["sha256"]
        body = await self._send("POST", "/snapshots", payload, {201})
        docs = body.get("documents")
        if (not _is_uuid(body.get("id")) or not _is_uuid(body.get("project_id"))
                or not isinstance(body.get("manifest_sha256"), str)
                or not _SHA256.fullmatch(body["manifest_sha256"]) or not isinstance(docs, list)
                or len(docs) != len(sent)):
            raise GatewayBadResponse("snapshot receipt is missing required fields")
        received: dict[str, str] = {}
        for doc in docs:
            if (not isinstance(doc, dict) or not isinstance(doc.get("id"), str)
                    or not isinstance(doc.get("sha256"), str) or not _SHA256.fullmatch(doc["sha256"])
                    or doc["id"] in received):
                raise GatewayBadResponse("snapshot receipt contains invalid document metadata")
            received[doc["id"]] = doc["sha256"]
        if received != sent:
            raise GatewayBadResponse("snapshot receipt does not match what was published")
        return body

    async def revoke_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        if not _is_uuid(snapshot_id):
            raise GatewayBadResponse("snapshot id is not a UUID")
        body = await self._send("POST", f"/snapshots/{snapshot_id}/revoke", None, {200})
        if body.get("revoked") is not True or str(body.get("snapshot_id", "")).lower() != snapshot_id.lower():
            raise GatewayBadResponse("snapshot revoke receipt is missing")
        return body

    async def revoke_project_sharing(self, project_id: str) -> dict[str, Any]:
        if not _is_uuid(project_id):
            raise GatewayBadResponse("project id is not a UUID")
        body = await self._send("POST", f"/projects/{project_id}/revoke", None, {200})
        revoked_at = body.get("revoked_at")
        count = body.get("snapshots_revoked")
        if (str(body.get("project_id", "")).lower() != project_id.lower()
                or body.get("revoked") is not True or not isinstance(revoked_at, str) or not revoked_at
                or isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise GatewayBadResponse("project revoke receipt is missing or invalid")
        return body

    async def issue_key(self, project_id: str, *, label: str, scopes: list[str]) -> dict[str, Any]:
        """키는 단회 교환용이며 Gateway 계약의 고정 3,600초 수명을 따른다."""
        body = await self._send("POST", "/keys", {"label": label,
                                                  "projects": {project_id: {"scopes": scopes}}}, {201})
        if (not _is_uuid(body.get("key_id"))
                or not all(isinstance(body.get(k), str) and body[k] for k in ("key", "expires_at"))):
            raise GatewayBadResponse("key response is missing key_id/key/expires_at")
        return body

    async def revoke_key(self, key_id: str) -> dict[str, Any]:
        if not _is_uuid(key_id):
            raise GatewayBadResponse("key id is not a UUID")
        body = await self._send("POST", f"/keys/{key_id}/revoke", None, {200})
        if body.get("revoked") is not True or str(body.get("key_id", "")).lower() != key_id.lower():
            raise GatewayBadResponse("key revoke receipt is missing")
        return body


def build_control(s: Settings | None = None) -> GatewayControl:
    s, problems = _connection_settings(s or get_settings())
    status = _status(s, problems)
    transport = None
    if status["configured"]:
        transport = HttpGatewayTransport(s.context_gateway_control_url, s.context_gateway_control_token,
                                         timeout=s.context_gateway_timeout_seconds)
    return GatewayControl(transport, public_url=s.context_gateway_public_url or None, status=status)
