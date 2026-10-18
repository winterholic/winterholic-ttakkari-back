"""사용자 입력 검증과 GitHub 주소 해석(ADR 0042 §7).

입력은 절대 fetch 하지 않는다. 검증을 통과한 조각(owner·repo·번호·ref·path)만 client 의 고정 경로 템플릿에 끼운다.
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

from ttakkari.github.errors import GithubError
from ttakkari.github.schemas import ResolveOut

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
POSITIVE_RE = re.compile(r"^[1-9][0-9]{0,18}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MAX_NUMBER = 2**31 - 1
MAX_BIG_ID = 2**53
MAX_PATH = 1024
MAX_REF = 255
MAX_QUERY = 2048
HOSTS = ("github.com", "www.github.com")
# 첫 경로 마디가 이 이름이면 owner 가 아니라 GitHub 자체 페이지다.
RESERVED_FIRST = frozenset({
    "orgs", "settings", "marketplace", "features", "topics", "sponsors", "explore", "notifications", "pulls", "issues",
    "search", "login", "logout", "join", "new", "about", "pricing", "enterprise", "collections", "trending", "apps",
    "codespaces", "security", "sessions", "site", "customer-stories", "readme", "support",
})
_REF_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def _bad(message: str) -> GithubError:
    return GithubError("invalid_target", message)


def _has_control(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def owner(value: str) -> str:
    if not isinstance(value, str) or not OWNER_RE.fullmatch(value):
        raise _bad("owner(계정·조직) 이름 형식이 올바르지 않습니다.")
    return value


def repo(value: str) -> str:
    if (not isinstance(value, str) or not REPO_RE.fullmatch(value) or value in (".", "..")
            or value.lower().endswith(".git")):
        raise _bad("저장소 이름 형식이 올바르지 않습니다.")
    return value


def number(value: str | int) -> int:
    text = str(value)
    if not POSITIVE_RE.fullmatch(text) or int(text) > MAX_NUMBER:
        raise _bad("번호는 양의 정수여야 합니다.")
    return int(text)


def big_id(value: str | int) -> int:
    text = str(value)
    if not POSITIVE_RE.fullmatch(text) or int(text) > MAX_BIG_ID:
        raise _bad("식별자는 양의 정수여야 합니다.")
    return int(text)


def path(value: str | None) -> str:
    """저장소 안 경로. 빈 문자열은 루트. 한 번 디코딩한 모양까지 `..`·NUL 을 거른다(이중 해석 방어)."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_PATH or _has_control(value) or "\\" in value:
        raise _bad("경로 형식이 올바르지 않습니다.")
    if value.startswith("/"):
        raise _bad("경로는 저장소 루트 기준 상대 경로여야 합니다.")
    value = value.removesuffix("/")
    if value == "":
        return ""
    decoded = unquote(value)
    if _has_control(decoded) or "\\" in decoded:
        raise _bad("경로 형식이 올바르지 않습니다.")
    for text in (value, decoded):
        for seg in text.split("/"):
            if seg in ("", ".", ".."):
                raise _bad("경로에 빈 마디·`.`·`..`를 쓸 수 없습니다.")
    return value


def quote_path(value: str) -> str:
    """검증된 경로를 마디별로 퍼센트 인코딩한다(`/`만 구분자로 남긴다)."""
    return "/".join(quote(seg, safe="") for seg in value.split("/")) if value else ""


def ref(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > MAX_REF or value.startswith(("-", "/")) or ".." in value
            or "@{" in value or _REF_BAD.search(value) or value.endswith("/")):
        raise _bad("브랜치·태그·커밋 이름 형식이 올바르지 않습니다.")
    return value


def labels(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    parts = value.split(",")
    if len(parts) > 10 or any(not p.strip() or len(p) > 50 or _has_control(p) for p in parts):
        raise _bad("labels 형식이 올바르지 않습니다(최대 10개, 각 50자).")
    return ",".join(p.strip() for p in parts)


# ---- 입력 해석 ----

def _seg(raw: str) -> str:
    seg = unquote(raw)
    if _has_control(seg) or "/" in seg or "\\" in seg:
        raise _bad("주소 형식이 올바르지 않습니다.")
    return seg


def _out(kind: str, **fields: object) -> ResolveOut:
    return ResolveOut(kind=kind, needs_ref_resolution=False, **fields)  # type: ignore[arg-type]


def parse(q: str) -> ResolveOut:
    """`owner/repo`·`owner`·github.com 주소를 구조화한다. 쿼리·프래그먼트는 버린다."""
    if not isinstance(q, str):
        raise _bad("입력이 비어 있습니다.")
    text = q.strip()
    if not text or len(text) > MAX_QUERY:
        raise _bad("입력이 비어 있거나 너무 깁니다.")
    if _has_control(text) or "\\" in text or any(c.isspace() for c in text):
        raise _bad("공백·제어 문자·역슬래시는 쓸 수 없습니다.")
    if text.startswith("//"):
        raise _bad("주소 형식이 올바르지 않습니다.")
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError as e:
            raise _bad("주소 형식이 올바르지 않습니다.") from e
        if parts.scheme.lower() != "https":
            raise _bad("https 주소만 지원합니다.")
        if "@" in parts.netloc:
            raise _bad("주소에 계정 정보를 넣을 수 없습니다.")
        try:
            port = parts.port
        except ValueError as e:
            raise _bad("주소 형식이 올바르지 않습니다.") from e
        if port is not None:
            raise _bad("포트를 지정할 수 없습니다.")
        host = (parts.hostname or "").lower()
        if host not in HOSTS:
            raise GithubError("unsupported_host")
        raw_path = parts.path
    else:
        head = text.split("?", 1)[0].split("#", 1)[0]
        first = head.split("/", 1)[0].lower()
        if first in HOSTS:
            raw_path = "/" + head.split("/", 1)[1] if "/" in head else ""
        elif "." in first or "@" in first or ":" in first:
            raise GithubError("unsupported_host")
        else:
            raw_path = "/" + head
    raw_segments = raw_path.strip("/").split("/") if raw_path.strip("/") else []
    if raw_path.endswith("//") or "//" in raw_path or not raw_segments:
        raise _bad("주소에 저장소 경로가 없습니다.")
    segs = [_seg(s) for s in raw_segments]
    first = segs[0]
    if first.lower() == "orgs":
        if len(segs) < 2:
            raise _bad("조직 이름이 없습니다.")
        return _out("owner", owner=owner(segs[1]))
    if first.lower() in RESERVED_FIRST:
        raise _bad("GitHub 사이트 자체 페이지는 지원하지 않습니다.")
    own = owner(first)
    if len(segs) == 1:
        return _out("owner", owner=own)
    name = segs[1]
    if name.lower().endswith(".git"):
        name = name[:-4]
    rep = repo(name)
    rest = segs[2:]
    if not rest:
        return _out("repo", owner=own, repo=rep)
    section, tail = rest[0], rest[1:]
    if section in ("tree", "blob"):
        if not tail:
            return _out("repo", owner=own, repo=rep)
        ref_path = path("/".join(tail))
        return ResolveOut(kind="code", owner=own, repo=rep, is_file=section == "blob", needs_ref_resolution=True,
                          ref_path=ref_path)
    if section == "pulls":
        return _out("pulls", owner=own, repo=rep)
    if section == "pull" and tail:
        extra = {"tab": "files"} if len(tail) > 1 and tail[1] == "files" else {}
        return _out("pull", owner=own, repo=rep, number=number(tail[0]), **extra)
    if section == "issues":
        return _out("issue", owner=own, repo=rep, number=number(tail[0])) if tail else _out("issues", owner=own, repo=rep)
    if section == "actions":
        if len(tail) >= 2 and tail[0] == "runs":
            run_id = big_id(tail[1])
            if len(tail) >= 4 and tail[2] == "job":
                return _out("job", owner=own, repo=rep, run_id=run_id, job_id=big_id(tail[3]))
            return _out("run", owner=own, repo=rep, run_id=run_id)
        return _out("actions", owner=own, repo=rep)
    return _out("repo", owner=own, repo=rep)


def split_ref_path(ref_path: str, refs: list[str]) -> tuple[str, str] | None:
    """`feature/login-ui/src/a.py` 를 실제 브랜치·태그 이름으로 가른다. 가장 긴 일치가 이긴다(세그먼트 경계만)."""
    best = ""
    for name in refs:
        if (ref_path == name or ref_path.startswith(name + "/")) and len(name) > len(best):
            best = name
    if best:
        return best, ref_path[len(best) + 1:]
    first, _, rest = ref_path.partition("/")
    if SHA_RE.fullmatch(first):
        return first, rest
    return None


def with_ref(out: ResolveOut, refs: list[str]) -> ResolveOut:
    found = split_ref_path(out.ref_path or "", refs)
    if found is None:
        raise GithubError("ref_not_found")
    ref_name, sub = found
    return out.model_copy(update={"ref": ref_name, "path": sub, "needs_ref_resolution": False, "ref_path": None})
