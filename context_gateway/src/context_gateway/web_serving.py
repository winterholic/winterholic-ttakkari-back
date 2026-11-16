from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles

_ASSET_SUFFIXES = {".css", ".ico", ".js", ".png", ".svg", ".ttf", ".webp", ".woff", ".woff2"}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "object-src 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "cache-control": "no-store",
    "content-security-policy": _CSP,
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}


class WebServingApp:
    """Serve only built UI files while forwarding the public API boundary."""

    def __init__(self, public_app: Any, web_dir: Path | str):
        self.public_app = public_app
        self.web_dir = Path(web_dir).resolve(strict=True)
        if not self.web_dir.is_dir() or not (self.web_dir / "index.html").is_file():
            raise ValueError("web directory must contain a built index.html")
        self.static_app = StaticFiles(directory=self.web_dir, check_dir=True, html=False)

    async def __call__(self, scope, receive, send):
        secure_send = self._secure_send(send)
        if scope["type"] != "http":
            await self.public_app(scope, receive, secure_send)
            return

        path = scope.get("path", "")
        if self._is_public_route(path):
            await self.public_app(scope, receive, secure_send)
            return

        if not self._is_built_file(path, scope.get("method", "GET")):
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, secure_send)
            return

        static_scope = {**scope, "path": "/index.html" if path == "/" else path}
        try:
            await self.static_app(static_scope, receive, secure_send)
        except HTTPException as exc:
            await PlainTextResponse("Not Found", status_code=exc.status_code)(
                scope, receive, secure_send
            )

    @staticmethod
    def _is_public_route(path: str) -> bool:
        return (
            path == "/healthz"
            or path == "/api/v1"
            or path == "/mcp"
            or path.startswith(("/api/v1/", "/mcp/"))
        )

    @staticmethod
    def _is_built_file(path: str, method: str) -> bool:
        if method not in {"GET", "HEAD"} or "\\" in path:
            return False
        parts = PurePosixPath(path).parts
        if any(part in {".", ".."} or part.startswith(".") for part in parts):
            return False
        if path in {"/", "/index.html"}:
            return True
        if not path.startswith("/assets/"):
            return False
        relative = PurePosixPath(path).relative_to("/assets/")
        return (
            len(relative.parts) == 1
            and relative.name not in {"", ".", ".."}
            and Path(relative.name).suffix.lower() in _ASSET_SUFFIXES
        )

    @staticmethod
    def _secure_send(send):
        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in _SECURITY_HEADERS
                ]
                headers.extend((key.encode(), value.encode()) for key, value in _SECURITY_HEADERS.items())
                message = {**message, "headers": headers}
            await send(message)

        return send_with_security_headers
