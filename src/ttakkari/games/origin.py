"""별도 게임 origin 정적 서빙 (TASK-G07, docs/design/game-studio.md §6.2, ADR 0049).

불변 빌드 스냅샷을 따까리 API 와 다른 origin 에서 자격증명 없이 내려준다. 이 모듈은 DB·세션·쿠키·bearer 를 읽지 않는다.
접근 권한은 URL 경로의 서명된 단기 토큰(`/p/<token>/...`) 하나뿐이고, 토큰은 빌드의 번들 위치를 직접 담아 두므로
게임 origin 프로세스는 파일 시스템의 읽기 전용 번들만 건드린다. 토큰이 경로에 있어서 게임의 상대 경로 자산 요청이
같은 토큰을 그대로 가져간다(vite base './').

두 방식으로 쓴다. 단일 터널을 쓰는 운영에서는 `GameOriginMiddleware` 가 Host 가 game_origin_host 인 요청만 이 앱으로 보내고
(API 라우트는 그 host 로는 도달할 수 없다), 로컬 검증이나 별도 포트 운영에서는 `ttakkari serve-games` 가 이 앱만 띄운다.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import jwt
from starlette.responses import FileResponse, PlainTextResponse, Response

from ttakkari.config import get_settings
from ttakkari.security.auth import ISSUER

AUDIENCE = "game-play"
PREFIX = "/p/"
ENTRYPOINT = "index.html"
SANDBOX_TOKENS = "allow-scripts allow-pointer-lock allow-orientation-lock"
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=()"

for _ext, _type in {".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm", ".json": "application/json",
                    ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
                    ".wav": "audio/wav", ".webp": "image/webp", ".glb": "model/gltf-binary"}.items():
    mimetypes.add_type(_type, _ext)


def frame_ancestors() -> list[str]:
    s = get_settings()
    return [o.rstrip("/") for o in (s.game_frame_ancestors or s.cors_origins) if o.startswith(("http://", "https://"))]


def content_security_policy() -> str:
    """게임 문서가 할 수 있는 일을 자기 origin 의 정적 자원으로 좁힌다.

    connect-src 'self' 는 Phaser 로더의 XHR 때문이고, 외부 네트워크·폼·base·플러그인·하위 frame 은 모두 막는다.
    sandbox 지시어는 frame 속성이 빠진 채 직접 열려도 opaque origin 으로 실행되게 하는 두 번째 방어선이다.
    """
    ancestors = " ".join(frame_ancestors()) or "'none'"
    return "; ".join([
        "default-src 'none'", "script-src 'self' 'wasm-unsafe-eval'", "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:", "media-src 'self' data: blob:", "font-src 'self' data:",
        "connect-src 'self' data: blob:", "worker-src 'self' blob:", "frame-src 'none'", "object-src 'none'",
        "base-uri 'none'", "form-action 'none'", f"frame-ancestors {ancestors}", f"sandbox {SANDBOX_TOKENS}",
    ])


def security_headers() -> dict[str, str]:
    # CORS 는 자격증명 없는 공개 정적 응답이라는 뜻의 `*` 만 쓴다(opaque origin 의 module script 가 받으려면 필요).
    # Allow-Credentials 는 절대 내지 않고, Set-Cookie 도 이 앱은 만들지 않는다.
    return {
        "Content-Security-Policy": content_security_policy(), "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex, nofollow, noarchive", "Permissions-Policy": PERMISSIONS_POLICY,
        "Access-Control-Allow-Origin": "*", "Cross-Origin-Resource-Policy": "cross-origin",
    }


def issue_play_token(game_id: uuid.UUID, build_id: uuid.UUID, bundle_rel_path: str) -> tuple[str, datetime]:
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(seconds=s.game_play_token_ttl_seconds)
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": str(build_id), "gid": str(game_id), "bp": bundle_rel_path, "exp": exp, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256"), exp


def decode_play_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)


def origin_problem() -> str | None:
    """게임 origin 설정이 안전하게 쓸 수 있는 상태인지. 문제가 있으면 사유를 돌려준다."""
    s = get_settings()
    host, url = s.game_origin_host.strip().lower(), s.game_origin_url.strip()
    if not host or not url:
        return "game_origin_host 와 game_origin_url 이 설정되지 않았습니다."
    parts = urlsplit(url)
    if parts.netloc.lower() != host:
        return "game_origin_url 의 host 가 game_origin_host 와 다릅니다."
    if parts.scheme not in {"http", "https"} or parts.path not in {"", "/"}:
        return "game_origin_url 은 경로 없는 http(s) origin 이어야 합니다."
    if s.is_prod and parts.scheme != "https":
        return "운영에서는 game_origin_url 이 https 여야 합니다."
    protected = {(urlsplit(o).netloc or "").lower() for o in [*s.cors_origins, *frame_ancestors(), s.public_share_origin] if o}
    if parts.netloc.lower() in protected or host in protected:
        return "게임 origin 이 따까리 API/PWA origin 과 같습니다. 서로 다른 hostname 이어야 합니다."
    return None


def play_url(token: str, parent_origin: str | None = None) -> str:
    base = get_settings().game_origin_url.rstrip("/")
    frag = f"#ttk-parent={quote(parent_origin, safe='')}" if parent_origin else ""
    return f"{base}{PREFIX}{token}/{ENTRYPOINT}{frag}"


def resolve_file(bundle: Path, rel: str) -> Path | None:
    """번들 안의 일반 파일만 돌려준다. 이탈(.., 절대·역슬래시·NUL), 숨김 파일, 심볼릭 링크, 번들 밖 실경로는 모두 거부한다."""
    if rel == "" or rel.endswith("/"):
        rel += ENTRYPOINT
    if "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in {"", ".", ".."} or p.startswith(".") for p in parts):
        return None
    root = bundle.resolve()
    cur = bundle
    for p in parts:
        cur = cur / p
        if cur.is_symlink():
            return None
    try:
        real = cur.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if root not in real.parents or not real.is_file():
        return None
    return real


def _bundle_from_claims(claims: dict) -> Path | None:
    rel = claims.get("bp")
    if not isinstance(rel, str) or not rel:
        return None
    root = get_settings().game_builds_dir.resolve()
    bundle = (root / rel).resolve()
    return bundle if root in bundle.parents and bundle.is_dir() else None


class GameOriginApp:
    """토큰으로만 접근하는 읽기 전용 정적 서버. 요청의 쿠키·인증 헤더는 읽지 않고 어떤 쿠키도 설정하지 않는다."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            return
        await self._response(scope).__call__(scope, receive, send)

    def _response(self, scope) -> Response:
        headers = security_headers()
        method, path = scope["method"], scope["path"]
        if method == "OPTIONS":
            return Response(status_code=204, headers={**headers, "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS", "Access-Control-Max-Age": "600"})
        if method not in {"GET", "HEAD"}:
            return PlainTextResponse("method not allowed", status_code=405, headers={**headers, "Allow": "GET, HEAD, OPTIONS"})
        if not path.startswith(PREFIX):
            return PlainTextResponse("not found", status_code=404, headers=headers)
        token, _, rel = path[len(PREFIX):].partition("/")
        try:
            claims = decode_play_token(token)
        except jwt.ExpiredSignatureError:
            return PlainTextResponse("expired", status_code=403, headers=headers)
        except jwt.PyJWTError:
            return PlainTextResponse("forbidden", status_code=403, headers=headers)
        bundle = _bundle_from_claims(claims)
        file = resolve_file(bundle, rel) if bundle else None
        if file is None:
            return PlainTextResponse("not found", status_code=404, headers=headers)
        return FileResponse(file, headers=headers, media_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream")


def _host_of(scope) -> str:
    for k, v in scope.get("headers", []):
        if k == b"host":
            return v.decode("latin-1").lower()
    return ""


class GameOriginMiddleware:
    """Host 가 게임 origin 인 요청은 게임 앱으로만 보낸다. 다른 Host 로는 게임 경로가 존재하지 않는다(API 앱에 /p/ 라우트 없음)."""

    def __init__(self, app, host: str):
        self.app = app
        self.host = host.strip().lower()
        self.game = GameOriginApp()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.host:
            h = _host_of(scope)
            if h == self.host or (":" not in self.host and h.split(":")[0] == self.host):
                return await self.game(scope, receive, send)
        return await self.app(scope, receive, send)


class PlayTokenAccessFilter(logging.Filter):
    """uvicorn access log 는 URL 전체를 찍는다. 경로의 재생 토큰을 지운다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            path = str(record.args[2])
            if path.startswith(PREFIX):
                rest = path[len(PREFIX):].partition("/")[2]
                args = list(record.args)
                args[2] = f"{PREFIX}-/{rest}"
                record.args = tuple(args)
        return True


def install_access_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, PlayTokenAccessFilter) for f in access.filters):
        access.addFilter(PlayTokenAccessFilter())


app = GameOriginApp()
