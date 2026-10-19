"""소스 중복 판별 키(ADR 0045). (계정, 노트북, dedupe_key) 가 같으면 같은 소스로 본다.

URL 은 대소문자·기본 포트·fragment·추적 파라미터·쿼리 순서·YouTube 주소 변형을 정규화한다.
텍스트·파일은 내용 해시다. 키는 내용을 되돌릴 수 없는 해시와 접두사만 담는다.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ttakkari.notebooklm.errors import Invalid

_TRACKING = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|igshid|si|feature|ref|ref_src)$", re.IGNORECASE)
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
MAX_URL_CHARS = 2000


def _youtube_id(host: str, path: str, query: list[tuple[str, str]]) -> str | None:
    if host not in _YT_HOSTS:
        return None
    if host.endswith("youtu.be"):
        vid = path.strip("/").split("/")[0]
    elif path.rstrip("/") == "/watch":
        vid = dict(query).get("v", "")
    else:
        m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", path)
        vid = m.group(1) if m else ""
    return vid if _YT_ID.match(vid or "") else None


def normalize_url(raw: str) -> tuple[str, str]:
    """(정규화 URL, 종류 url|youtube). http(s) 가 아니거나 사설/루프백 주소면 Invalid."""
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_CHARS or any(c in url for c in "\r\n\t "):
        raise Invalid("올바른 URL이 아닙니다.")
    parts = urlparse(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise Invalid("http(s) 주소만 등록할 수 있습니다.")
    host = parts.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise Invalid("내부 주소는 등록할 수 없습니다.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
        raise Invalid("내부 주소는 등록할 수 없습니다.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    vid = _youtube_id(host, parts.path, query)
    if vid:
        return f"https://www.youtube.com/watch?v={vid}", "youtube"
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    norm = urlunparse((parts.scheme.lower(), netloc, path, "", urlencode(sorted(query)), ""))
    return norm, "url"


def _sha(prefix: str, data: bytes) -> str:
    return f"{prefix}:{hashlib.sha256(data).hexdigest()}"


def url_key(normalized: str) -> str:
    return _sha("url", normalized.encode())


def text_key(text: str) -> str:
    return _sha("text", text.replace("\r\n", "\n").strip().encode())


def file_key(data_sha256: str) -> str:
    return f"file:{data_sha256}"
