"""GitHub 저장소 리버스 엔지니어링 파이프라인 (ADR 0038, 0040).

요청 → 수집 → 목록화 → 구조 파악 → 핵심 흐름 → 모델 조사 → 모델 합성 → 정리·요약의
전 과정을 단계별 근거(evidence)와 함께 DB에 저장한다. 정적 단계(목록화·구조·흐름)는
모델이 읽기 전 사전 조사로 남고, 모델 조사·합성 단계에서 모델이 실제 코드를 읽고 합성한다.
rea에서 차용한 점은 증거 우선(evidence-first)과 한계 명시(모르는 것은 '확인 필요'로 남긴다)다.
모델은 저장소 파일을 직접 만지지 않고, 백엔드가 읽기 전용으로 읽어 인라인 발췌로
전달한 것만 본다(상대경로만 공개, 절대경로 미공개).

불변 조건: 대상 코드를 절대 실행하지 않는다(빌드·스크립트·테스트 일체 금지).
비밀 파일(.env 등)은 읽지 않고 이름도 결과에 싣지 않는다. 네트워크는
git clone에만 쓴다.
"""
from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import json
import os
import re
import shutil
import signal
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

STAGES: tuple[tuple[str, str], ...] = (
    ("request", "요청 접수"),
    ("collect", "수집"),
    ("inventory", "목록화"),
    ("structure", "구조 파악"),
    ("flows", "핵심 흐름"),
    ("survey", "모델 조사"),
    ("synthesize", "모델 합성"),
    ("summarize", "정리·요약"),
)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

CLONE_TIMEOUT = 180
# clone 결과물 상한(초과 시 실패 + 정리). rea 실측 75M 기준 여유분.
CLONE_BYTES_CAP = 512 * 1024 * 1024
# reverse 작업 디렉터리 전체 상한(초과 시 끝난 작업의 clone부터 오래된 순 정리).
REVERSE_DISK_CAP = 2 * 1024 * 1024 * 1024
# git 명령 공통 제한. clone에는 CLONE_TIMEOUT을 쓴다.
GIT_TIMEOUT = 60
MAX_FILES = 20000
MAX_CONTENT_BYTES = 256 * 1024
MAX_PARSE_FILES = 4000
MAX_EVIDENCE = 30
STALE_MINUTES = 30
MAX_CONCURRENT = 2
# 모델에 전달하는 발췌 상한.
MAX_MODEL_FILES = 24
MAX_MODEL_EXCERPT_CHARS = 12_000
MAX_MODEL_PROMPT_CHARS = 200_000
MODEL_TIMEOUT_SECONDS = 300.0

# 비밀 파일: 읽지 않고 개수만 센다. 이름도 결과에 싣지 않는다.
SECRET_PATTERNS = (".env*", "*.pem", "*.key", "*credentials*", "*secret*",
                   "*token*", "id_rsa*", ".npmrc", ".pypirc", "*.p12", "*.pfx")
# 걷기에서 가지치기하는 서드파티·캐시 디렉터리. 핵심 모듈이 서드파티로 채워지지 않게 한다.
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             ".turbo", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
             "site-packages", ".uv-cache", "uv-cache", ".stt-runtime", ".gradle",
             ".idea", ".vscode"}
# 진입점 후보에서 내리는 지원 디렉터리(테스트·예제·웹사이트).
ENTRY_EXCLUDE_TOPS = {"tests", "test", "examples", "example", "website", "docs", "demo"}
# 원격 수집 허용 호스트(https 전용). 그 외 공개 호스트는 설정으로 확장한다.
ALLOWED_GIT_HOSTS = {"github.com"}
# dist/build/target/.next는 1차 빌드 산출물이라 역분석 대상에 둔다. 걷기 상한으로 폭주를 막는다.
WALK_CAP = 50000

LANGS = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
         ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
         ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby", ".php": "php",
         ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
         ".cs": "csharp", ".swift": "swift", ".kt": "kotlin", ".sh": "shell",
         ".sql": "sql", ".md": "markdown", ".mdx": "markdown", ".json": "json",
         ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".css": "css",
         ".html": "html", ".vue": "vue", ".svelte": "svelte"}

MANIFESTS = ("package.json", "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml",
             "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json",
             "Package.swift", "pubspec.yaml")
CONVENTIONAL_ENTRIES = ("main.py", "app.py", "cli.py", "manage.py", "index.js",
                        "index.ts", "main.js", "main.ts")

PY_IMPORT = re.compile(r"^\s*(?:import\s+([\w\.]+)|from\s+(\.*)([\w\.]*)\s+import\s+)", re.MULTILINE)
JS_IMPORT = re.compile(r"(?:import\s[^'\"]*?from\s*|require\s*\(|export\s[^'\"]*?from\s*)['\"]([^'\"]+)['\"]")
MD_HEADING = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)
GITHUB_REPO = re.compile(r"^https://github\.com/([\w.\-]+/[\w.\-]+?)(?:\.git|/.*)?$")


class ReverseError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(UTC)


def _fresh_stages() -> list[dict[str, Any]]:
    return [{"key": k, "label": label, "status": "pending", "started_at": None,
             "finished_at": None, "evidence": [], "notes": []} for k, label in STAGES]


def _allowed_git_hosts() -> set[str]:
    try:
        from ttakkari.config import get_settings
        extra = getattr(get_settings(), "reverse_allowed_hosts", None) or []
        return set(ALLOWED_GIT_HOSTS) | {h.lower() for h in extra}
    except Exception:  # noqa: BLE001 - 설정 없이도 기본 허용 목록으로 동작
        return set(ALLOWED_GIT_HOSTS)


def _reject_nonpublic_host(host: str) -> None:
    """IP 리터럴·loopback·사설망·링크 로컬은 거부한다. DNS 역참조는 범위 밖으로 둔다."""
    lowered = host.lower().rstrip(".")
    if lowered in ("localhost", "localhost.localdomain"):
        raise ReverseError(422, "private_host", "내부 주소의 저장소는 수집하지 않습니다.")
    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        return
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        raise ReverseError(422, "private_host", "내부 주소의 저장소는 수집하지 않습니다.")


def normalize_target(raw: str) -> dict[str, str]:
    """대상을 {kind, url|path, label}로 정규화한다.

    원격은 https + 허용 호스트(기본 github.com)만 받는다. loopback·사설망·링크 로컬,
    사용자 자격 포함 URL, 리다이렉트 우회 schemes(git@/http/file)는 422로 거부한다.
    로컬은 앱 allowed_roots 안의 디렉터리만 받는다(/, ~, ~/.ssh, /etc는 밖이라 거부된다).
    """
    text = (raw or "").strip()
    if not text:
        raise ReverseError(422, "empty_target", "분석할 대상을 입력하세요.")
    if len(text) > 500:
        raise ReverseError(422, "target_too_long", "대상 입력이 너무 깁니다.")
    short = re.fullmatch(r"[\w.\-]+/[\w.\-]+", text)
    if short is not None:
        return {"kind": "git_url", "url": f"https://github.com/{text}.git", "label": text}
    if "://" in text or text.startswith("git@"):
        if text.startswith(("git@", "http://", "ftp://", "file://", "ssh://")):
            raise ReverseError(422, "unsafe_scheme", "https로 시작하는 Git URL만 수집합니다.")
        try:
            parts = urlsplit(text)
        except ValueError:
            raise ReverseError(422, "bad_url", "https Git URL 형식이 올바르지 않습니다.") from None
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ReverseError(422, "bad_url", "https Git URL 형식이 올바르지 않습니다.")
        host = parts.hostname.lower()
        _reject_nonpublic_host(host)
        if host not in _allowed_git_hosts():
            raise ReverseError(422, "host_not_allowed", "허용된 Git 호스트가 아닙니다(기본 github.com).")
        match = GITHUB_REPO.match(text)
        if match:
            url = f"https://github.com/{match.group(1)}.git"
            return {"kind": "git_url", "url": url, "label": match.group(1)}
        if not parts.path:
            raise ReverseError(422, "bad_url", "https Git URL 형식이 올바르지 않습니다.")
        return {"kind": "git_url", "url": text, "label": host + parts.path}
    from ttakkari.broker import policy as broker_policy
    from ttakkari.config import get_settings
    try:
        resolved = broker_policy.validate_workspace_root(text, get_settings().allowed_roots)
    except broker_policy.BrokerError as exc:
        raise ReverseError(422, "bad_local_path", "로컬 경로는 허용 영역 안의 디렉터리여야 합니다.") from exc
    return {"kind": "local_path", "path": str(resolved), "label": resolved.name}


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pat) for pat in SECRET_PATTERNS)


def _read_text(path: Path) -> str | None:
    """UTF-8 텍스트만 읽는다. 바이너리·초과분은 None(실행은 하지 않는다)."""
    try:
        if path.stat().st_size > MAX_CONTENT_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError):
        return None


def inventory(root: Path) -> dict[str, Any]:
    """파일 걷기: 제외 디렉터리는 그 자리에서 가지치기하고 상한에서 멈춘다.

    비밀 파일은 읽지 않고 개수만 센다. 심볼릭 링크(파일·디렉터리)는 따라가지 않고
    결과에도 싣지 않는다. 정렬된 rglob 대신 os.walk로 걸어 SKIP_DIRS를 만나는
    즉시 잘라내므로 서드파티·캐시 폭주에 상한이 실제로 동작한다.
    """
    files: list[dict[str, Any]] = []
    lang_counts: dict[str, int] = {}
    secrets = 0
    skipped_dirs = sorted(d for d in SKIP_DIRS if (root / d).is_dir())
    too_big = 0
    total_bytes = 0
    truncated = False
    walked = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        # 그 자리에서 가지치기: 제외·심링크 디렉터리는 내려가지 않는다.
        pruned = [d for d in list(dirnames)
                  if d in SKIP_DIRS or Path(current, d).is_symlink()]
        for d in pruned:
            dirnames.remove(d)
        dirnames.sort()
        for name in sorted(filenames):
            walked += 1
            if walked > WALK_CAP or len(files) >= MAX_FILES:
                truncated = True
                break
            path = Path(current, name)
            if path.is_symlink():
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            if _is_secret(path.name):
                secrets += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total_bytes += size
            lang = LANGS.get(path.suffix.lower(), "other")
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
            entry: dict[str, Any] = {"path": rel, "size": size, "lang": lang}
            if size > MAX_CONTENT_BYTES:
                too_big += 1
                entry["content"] = "too_big"
            files.append(entry)
        if truncated:
            break
    top_langs = sorted(lang_counts.items(), key=lambda kv: -kv[1])[:8]
    evidence = [f"파일 {len(files)}개 관찰, {total_bytes // 1024} KiB",
                "주요 언어: " + (", ".join(f"{k} {v}개" for k, v in top_langs) if top_langs else "없음")]
    if secrets:
        evidence.append(f"비밀 의심 파일 {secrets}개는 읽지 않음")
    if truncated:
        evidence.append(f"확인 필요: {MAX_FILES}개를 초과해 목록이 잘림")
    return {"files": files, "lang_counts": lang_counts, "top_langs": top_langs,
            "secrets": secrets, "too_big": too_big, "total_bytes": total_bytes,
            "truncated": truncated, "skipped_dirs": skipped_dirs, "evidence": evidence[:MAX_EVIDENCE]}


def _manifest_facts(root: Path, inv: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """매니페스트에서 이름·진입점·스크립트 후보를 읽는다."""
    present = [f["path"] for f in inv["files"]]
    notes: list[str] = []
    entries: list[dict[str, str]] = []
    pkg = next((p for p in present if p == "package.json"), None)
    if pkg is not None:
        try:
            data = json.loads((root / pkg).read_text(encoding="utf-8"))
            notes.append(f"package.json: {data.get('name', '?')} {data.get('version', '')}".strip())
            for field in ("main", "bin"):
                value = data.get(field)
                if isinstance(value, str):
                    entries.append({"path": value.lstrip("./"), "via": f"package.json:{field}"})
                elif isinstance(value, dict):
                    entries += [{"path": v.lstrip("./"), "via": f"package.json:bin:{k}"}
                                for k, v in value.items() if isinstance(v, str)]
            scripts = data.get("scripts")
            if isinstance(scripts, dict) and scripts:
                notes.append("scripts: " + ", ".join(sorted(scripts)[:12]))
        except (OSError, ValueError, UnicodeError) as exc:
            notes.append(f"확인 필요: package.json 해석 실패({exc.__class__.__name__})")
    for name in ("pyproject.toml", "Cargo.toml", "go.mod"):
        if name in present:
            head = _read_text(root / name) or ""
            notes.append(f"{name} 존재(선언부 {len(head)}자 확인)")
            if name == "pyproject.toml":
                for m in re.finditer(r"^(\[project\.scripts\]|\[tool\.poetry\.scripts\])(.*?)(?=^\[|\Z)",
                                     head, re.MULTILINE | re.DOTALL):
                    pairs = re.findall(r"^(\w[\w\-]*)\s*=", m.group(2), re.MULTILINE)[:8]
                    if pairs:
                        notes.append("console scripts: " + ", ".join(pairs))
    for rel in present:
        base = rel.rsplit("/", 1)[-1]
        top = rel.split("/")[0] if "/" in rel else ""
        # 테스트·예제·웹사이트 안의 관례적 파일명은 진입점 후보에서 내린다.
        if top in ENTRY_EXCLUDE_TOPS:
            continue
        if base in CONVENTIONAL_ENTRIES or (rel.startswith("cmd/") and rel.count("/") == 2):
            entries.append({"path": rel, "via": "관례적 진입점"})
    seen: set[str] = set()
    uniq = [e for e in entries if not (e["path"] in seen or seen.add(e["path"]))]
    return notes, uniq[:12]


def structure(root: Path, inv: dict[str, Any]) -> dict[str, Any]:
    """README·매니페스트·최상위 디렉터리에서 구조를 파악한다."""
    evidence: list[str] = []
    notes: list[str] = []
    readme = next((f["path"] for f in inv["files"]
                   if f["path"].lower() in ("readme.md", "readme", "readme.txt")), None)
    overview = ""
    headings: list[str] = []
    if readme is not None:
        text = _read_text(root / readme)
        if text is None:
            notes.append("확인 필요: README가 바이너리이거나 너무 큼")
        else:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            title = next((line.lstrip("# ").strip() for line in lines if line.startswith("#")), "")
            body = next((line for line in lines[1:]
                         if not line.startswith(("#", "<", "[", "!", "|", "*", ">", "-", "·"))), "")
            overview = (title + " — " + body)[:600] if title else body[:600]
            headings = MD_HEADING.findall(text)[:15]
            evidence.append(f"{readme}에서 제목·개요 확인")
    else:
        notes.append("확인 필요: README가 없어 개요는 파일 구조 기반 추정치")
    manifest_notes, entries = _manifest_facts(root, inv)
    evidence += manifest_notes[:8]
    topdirs: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        # 심링크 디렉터리는 따라가지 않아 0개짜리로 남지 않게 건너뛴다.
        if child.is_symlink() or not child.is_dir() or child.name in SKIP_DIRS:
            continue
        count = sum(1 for f in inv["files"] if f["path"].startswith(child.name + "/"))
        if count == 0 and not any(child.iterdir()):
            continue
        role = {"src": "주요 소스", "lib": "주요 소스", "docs": "문서", "test": "테스트",
                "tests": "테스트", "examples": "예제", "scripts": "보조 스크립트",
                "website": "웹사이트", "assets": "정적 자산"}.get(child.name, "")
        topdirs.append({"dir": child.name, "files": count, "role": role})
        if len(topdirs) >= 12:
            break
    evidence.append(f"최상위 디렉터리 {len(topdirs)}개 관찰")
    key_files = [f["path"] for f in inv["files"]
                 if "/" not in f["path"] and f["path"].lower() not in ("readme.md", "license", "license.md")]
    return {"overview": overview, "headings": headings, "entries": entries, "topdirs": topdirs,
            "key_files": key_files[:20], "evidence": evidence[:MAX_EVIDENCE], "notes": notes}


def _resolve_relative(base: Path, raw: str, index: dict[str, str]) -> str | None:
    """상대 import를 저장소 내 파일로 푼다. 못 풀면 None(추측하지 않는다)."""
    if not raw.startswith("."):
        return None
    stem = (base / raw).as_posix()
    cands = [stem, stem + ".py", stem + ".ts", stem + ".tsx", stem + ".js",
             stem + ".jsx", stem + "/__init__.py", stem + "/index.ts", stem + "/index.js"]
    if stem.endswith((".js", ".jsx", ".mjs")):
        base_stem = stem.rsplit(".", 1)[0]
        cands += [base_stem + ".ts", base_stem + ".tsx"]
    for cand in cands:
        if cand in index:
            return index[cand]
    return None


def _edges_for(path: str, text: str, index: dict[str, str]) -> list[str]:
    out: list[str] = []
    if path.endswith(".py"):
        for module, dots, rest in PY_IMPORT.findall(text):
            if dots:
                depth = len(dots)
                parts = path.split("/")[:-depth] if depth <= path.count("/") else []
                target = "/".join(parts + ([rest.replace(".", "/")] if rest else []))
                for cand in (target + ".py", target + "/__init__.py", target):
                    if cand in index:
                        out.append(index[cand])
                        break
            elif module:
                top = module.split(".")[0]
                hit = next((k for k in index if k == top + ".py" or k.startswith(top + "/")), None)
                if hit is not None:
                    out.append(hit)
    elif path.rsplit(".", 1)[-1] in ("js", "mjs", "cjs", "jsx", "ts", "tsx", "mts"):
        for raw in JS_IMPORT.findall(text):
            resolved = _resolve_relative(Path(path).parent, raw.split("!")[-1].split("?")[0], index)
            if resolved is not None:
                out.append(resolved)
    return sorted(set(out))


def analyze_flows(root: Path, inv: dict[str, Any], struct: dict[str, Any]) -> dict[str, Any]:
    """정적 import 그래프에서 허브 모듈과 진입점 도달 경로를 찾는다."""
    candidates = [f["path"] for f in inv["files"]
                  if f.get("content") != "too_big" and f["path"].rsplit(".", 1)[-1]
                  in ("py", "js", "mjs", "cjs", "jsx", "ts", "tsx", "mts")]
    entries = {e["path"] for e in struct["entries"]}
    support = {"tests", "test", "scripts", "docs", "website", "examples", "example",
               "demo", "third_party", "experiments"}

    def _prio(path: str) -> tuple[int, str]:
        if path in entries:
            return (0, path)
        if path.split("/")[0] in support:
            return (2, path)
        return (1, path)

    parseable = sorted(candidates, key=_prio)[:MAX_PARSE_FILES]
    index = {p: p for p in parseable}
    edges: dict[str, list[str]] = {}
    for rel in parseable:
        text = _read_text(root / rel)
        if text is None:
            continue
        edges[rel] = _edges_for(rel, text, index)
    indegree: dict[str, int] = {}
    for src, dsts in edges.items():
        for dst in dsts:
            if dst != src:
                indegree[dst] = indegree.get(dst, 0) + 1
    hubs = sorted(indegree, key=lambda k: -indegree[k])[:12]
    evidence = [f"정적 import {sum(len(v) for v in edges.values())}건({len(edges)}개 파일) 관찰"]
    if hubs:
        evidence.append("참조 허브: " + ", ".join(f"{h}({indegree[h]}건)" for h in hubs[:5]))
    notes = ["동적 import·리플렉션·런타임 주입은 정적 추적에 잡히지 않아 확인 필요"]
    chains: list[dict[str, Any]] = []
    for entry in struct["entries"][:6]:
        start = entry["path"]
        if start not in edges:
            match = next((p for p in edges if p.endswith("/" + start) or p == start), None)
            if match is None:
                continue
            start = match
        seen = {start}
        queue = [(start, [start])]
        found: list[str] | None = None
        hubset = set(hubs[:6])
        while queue and found is None:
            node, trail = queue.pop(0)
            for nxt in edges.get(node, []):
                if nxt in seen or len(trail) > 3:
                    continue
                seen.add(nxt)
                if nxt in hubset:
                    found = trail + [nxt]
                    break
                queue.append((nxt, trail + [nxt]))
        if found is None:
            # 허브에 닿지 않으면 참조가 많은 이웃을 따라 내려간다(관찰된 간선만 사용).
            trail = [start]
            while len(trail) < 5:
                nxts = [n for n in edges.get(trail[-1], []) if n not in trail]
                if not nxts:
                    break
                trail.append(max(nxts, key=lambda n: (indegree.get(n, 0), n)))
            found = trail
        chains.append({"entry": entry["path"], "via": entry["via"],
                       "reaches": found,
                       "evidence": [f"{start}에서 {len(edges.get(start, []))}건 import 확인"]})
    if chains:
        evidence.append(f"진입점 후보 {len(chains)}곳에서 흐름 추적")
    return {"hubs": [{"path": h, "referenced": indegree[h]} for h in hubs],
            "chains": chains, "files_parsed": len(edges),
            "evidence": evidence[:MAX_EVIDENCE], "notes": notes}


# 모델 호출 교체 지점. None이면 앱 엔진 실행 경로(desk + worker_routing)를 쓴다.
# 테스트는 가짜 함수로 대체한다.
ModelReview = Callable[[str, str], Awaitable[str]]
MODEL_REVIEW: ModelReview | None = None

SURVEY_SYSTEM = (
    "당신은 소스 저장소를 분석하는 코드 리뷰어다. 한국어로 답한다. "
    "저장소 파일에 직접 접근할 수 없고, 주어진 목록·발췌만으로 판단한다. "
    "반드시 JSON 한 개만 출력한다. 코드펜스 없이 순수 JSON으로 답한다. "
    "형식: {\"read\": [\"상대경로\", ...], \"notes\": [\"관찰 메모\", ...]}. "
    "read는 실제 동작 파악에 꼭 필요한 파일만 최대 24개 고른다. "
    "없는 경로는 절대 적지 않는다. 확신 없는 내용은 notes에 '확인 필요'로 적는다."
)

SYNTH_SYSTEM = (
    "당신은 소스 저장소를 분석하는 코드 리뷰어다. 한국어로 답한다. "
    "주어진 발췌에 나온 내용으로만 주장하고, 모든 주장에 \"상대경로\" 또는 "
    "\"상대경로:줄\" 형태의 근거를 붙인다. 발췌에 없는 내용은 주장하지 말고 "
    "limitations에 '확인 필요'로 적는다. 반드시 JSON 한 개만 출력한다. "
    "코드펜스 없이 순수 JSON으로 답한다. "
    "형식: {\"overview\": \"무엇이고 어떻게 동작하는지 3~6문장\", "
    "\"architecture\": [{\"title\": ..., \"body\": ..., \"evidence\": [...]}], "
    "\"modules\": [{\"path\": ..., \"role\": \"실제 역할 1~2문장\", \"evidence\": [...]}], "
    "\"flows\": [{\"title\": ..., \"steps\": [...], \"evidence\": [...]}], "
    "\"ideas\": [{\"title\": ..., \"body\": \"따까리가 차용할 점\", \"evidence\": [...]}], "
    "\"limitations\": [\"한계·확인 필요\", ...]}."
)


def _parse_model_json(text: str) -> dict[str, Any]:
    """모델 응답에서 JSON 한 개를 꺼낸다. 펜스·앞뒤 잡음을 걷어낸다."""
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ReverseError(503, "model_bad_output", "모델이 JSON을 내놓지 않았습니다.")
        cleaned = cleaned[start:end + 1]
    try:
        data = json.loads(cleaned)
    except ValueError:
        raise ReverseError(503, "model_bad_output", "모델 응답을 JSON으로 읽지 못했습니다.") from None
    if not isinstance(data, dict):
        raise ReverseError(503, "model_bad_output", "모델 응답이 객체가 아닙니다.")
    return data


def _normalize_evidence(item: Any, known: set[str], line_counts: dict[str, int]) -> str | None:
    """근거 한 건을 '경로' 또는 '경로:줄'로 정규화한다. 목록에 없는 파일은 버린다."""
    if not isinstance(item, str) or not item.strip():
        return None
    text = item.strip()
    path, _, lineno = text.partition(":")
    path = path.strip().lstrip("./")
    if path not in known:
        return None
    if not lineno:
        return path
    first = lineno.split("-")[0].strip()
    if not first.isdigit() or int(first) < 1:
        return path
    if path in line_counts and int(first) > line_counts[path]:
        return path
    return f"{path}:{first}"


def _validate_synthesis(data: dict[str, Any], known: set[str],
                        line_counts: dict[str, int]) -> tuple[dict[str, Any], int]:
    """모델 합성을 검증한다. 근거 없는 항목은 버리고 버린 개수를 돌려준다."""
    dropped = 0

    def _claim_list(items: Any, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        nonlocal dropped
        out: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return out
        for entry in items[:12]:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            ev_norm = []
            for raw in (entry.get("evidence") or []):
                norm = _normalize_evidence(raw, known, line_counts) if isinstance(raw, str) else None
                if norm is not None and norm not in ev_norm:
                    ev_norm.append(norm)
            body = {k: entry.get(k) for k in kinds if isinstance(entry.get(k), str) and entry.get(k).strip()}
            if len(body) < len(kinds) or not ev_norm:
                dropped += 1
                continue
            out.append({**body, "evidence": ev_norm[:8]})
        return out

    def _flow_list(items: Any) -> list[dict[str, Any]]:
        nonlocal dropped
        out: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return out
        for entry in items[:8]:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            title = entry.get("title")
            steps = [s for s in (entry.get("steps") or []) if isinstance(s, str) and s.strip()][:10]
            ev_norm = []
            for raw in (entry.get("evidence") or []):
                norm = _normalize_evidence(raw, known, line_counts) if isinstance(raw, str) else None
                if norm is not None and norm not in ev_norm:
                    ev_norm.append(norm)
            if not isinstance(title, str) or not title.strip() or not steps or not ev_norm:
                dropped += 1
                continue
            out.append({"title": title.strip(), "steps": steps, "evidence": ev_norm[:8]})
        return out

    synth = {
        "overview": data.get("overview") if isinstance(data.get("overview"), str) else "",
        "architecture": _claim_list(data.get("architecture"), ("title", "body")),
        "modules": _claim_list(data.get("modules"), ("path", "role")),
        "flows": _flow_list(data.get("flows")),
        "ideas": _claim_list(data.get("ideas"), ("title", "body")),
        "limitations": [str(x).strip() for x in (data.get("limitations") or [])
                        if isinstance(x, str) and x.strip()][:12],
    }
    return synth, dropped


async def _model_route() -> dict[str, Any]:
    """라우팅 정책(engine auto, task_type=reasoning)으로 모델 경로를 정한다.

    1순위는 worker_routing availability의 실행 가능 계정, 계정이 없으면
    정책표(ROUTES reasoning)의 desk 실행 가능(codex/claude) 첫 후보로 폴백한다.
    """
    from ttakkari import db as dbmod
    from ttakkari.runs import profiles as profiles_mod
    from ttakkari.runs import worker_routing
    async with dbmod.sessionmaker()() as session:
        try:
            free, _blocked = await worker_routing.availability(session, "reasoning", "personal")
        except Exception as exc:  # noqa: BLE001 - 계정 조회 실패는 폴백으로
            free = []
            route_error = str(exc)[:120]
        else:
            route_error = ""
    try:
        staged = worker_routing.first_stage(free, "reasoning")
    except ValueError:
        staged = []
    runnable = [p for p in staged if p.engine in ("codex", "claude")]
    if runnable:
        prof = runnable[0]
        return {"engine": prof.engine, "model": prof.model or "?",
                "via": "worker_routing availability(reasoning/personal)",
                "profile": prof, "cfg": {"model": prof.model} if prof.model else None}
    for stage in worker_routing.ROUTES["reasoning"]:
        for engine, model in stage:
            if engine in ("codex", "claude"):
                prof = profiles_mod.Profile(id=uuid.uuid5(uuid.NAMESPACE_URL, f"ttakkari:reverse:{engine}"),
                                            name=f"reverse-{engine}", engine=engine, model=model)
                via = "routing-policy fallback(ROUTES reasoning)"
                if route_error:
                    via += f" — 계정 조회 실패: {route_error}"
                else:
                    via += " — 실행 가능 계정 없음"
                return {"engine": engine, "model": model, "via": via,
                        "profile": prof, "cfg": {"model": model}}
    raise ReverseError(503, "no_model_route", "실행 가능한 모델 경로가 없습니다.")


async def _default_model_review(system: str, prompt: str, route: dict[str, Any]) -> str:
    """앱의 엔진 실행 경로(desk CLI 실행)로 모델에 묻는다. 대상 코드는 실행하지 않는다."""
    from ttakkari.runs import desk
    attempt = await desk._ask(route["profile"], system, prompt,
                              cfg_override=route.get("cfg"),
                              timeout_seconds=MODEL_TIMEOUT_SECONDS)
    if attempt.text is None:
        raise ReverseError(503, "model_failed", "모델 호출이 실패했습니다: " + (attempt.error or "원인 미상")[:200])
    return attempt.text


async def _review(system: str, prompt: str, route: dict[str, Any]) -> str:
    if MODEL_REVIEW is not None:
        return await MODEL_REVIEW(system, prompt)
    return await _default_model_review(system, prompt, route)


def _repo_map(label: str, revision: str | None, inv: dict[str, Any],
              struct: dict[str, Any], flow: dict[str, Any]) -> str:
    paths = [f["path"] for f in inv["files"]]
    listed = paths[:1500]
    more = f"\n... 외 {len(paths) - len(listed)}개 생략" if len(paths) > len(listed) else ""
    doc = {
        "label": label, "revision": revision,
        "langs": inv["top_langs"][:6], "files": len(paths),
        "topdirs": struct["topdirs"][:12], "entries": struct["entries"][:12],
        "headings": struct["headings"][:12],
        "hubs": flow["hubs"][:12],
        "chains": [{"entry": c["entry"], "reaches": c["reaches"]} for c in flow["chains"][:6]],
        "paths": listed,
    }
    text = json.dumps(doc, ensure_ascii=False)
    if len(text) > MAX_MODEL_PROMPT_CHARS:
        doc["paths"] = listed[:400]
        text = json.dumps(doc, ensure_ascii=False)[:MAX_MODEL_PROMPT_CHARS]
    return text + more


def _excerpt(root: Path, rel: str) -> tuple[str, int] | None:
    """파일 한 개를 읽기 전용으로 읽어 줄번호 발췌로 만든다. 비밀 파일은 절대 읽지 않는다."""
    if _is_secret(Path(rel).name):
        return None
    text = _read_text(root / rel)
    if text is None:
        return None
    lines = text.splitlines()
    numbered = [f"{i + 1}: {line[:300]}" for i, line in enumerate(lines)]
    joined = "\n".join(numbered)
    if len(joined) > MAX_MODEL_EXCERPT_CHARS:
        joined = joined[:MAX_MODEL_EXCERPT_CHARS] + "\n... (발췌 잘림)"
    return joined, len(lines)


def _static_fallback(target: dict[str, str], inv: dict[str, Any],
                     struct: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    """모델 합성이 없을 때 쓰는 정적 요약. 모듈 역할은 참조 수 기반 추정치로만 쓴다."""
    langs = ", ".join(f"{k} {v}개" for k, v in inv["top_langs"][:4]) or "확인 필요"
    synth = f"{target['label']} — 파일 {len(inv['files'])}개({inv['total_bytes'] // 1024} KiB), {langs}."
    overview = (struct["overview"] + "\n" + synth)[:1200] if struct["overview"] else synth + " (확인 필요: README 없음)"
    architecture = [{"title": "최상위 구성",
                     "body": "; ".join(f"{d['dir']}({d['files']}개)"
                                       + (f" — {d['role']}" if d["role"] else "") for d in struct["topdirs"][:10]) or "확인 필요",
                     "evidence": [d["dir"] + "/" for d in struct["topdirs"][:10]]}]
    if struct["headings"]:
        architecture.append({"title": "문서 구조", "body": " / ".join(struct["headings"][:10]),
                             "evidence": ["README.md"]})
    modules = []
    for hub in flow["hubs"][:8]:
        top = hub["path"].split("/")[0]
        modules.append({"path": hub["path"], "role": f"참조 {hub['referenced']}건 — {top} 영역의 중심 모듈(정적 추정치, 확인 필요)",
                        "evidence": [hub["path"]]})
    flows = [{"title": f"{c['entry']} 흐름", "steps": c["reaches"], "evidence": c["evidence"]}
             for c in flow["chains"][:6]]
    return {"overview": overview, "architecture": architecture, "modules": modules[:10], "flows": flows}


def build_result(target: dict[str, str], revision: str | None, inv: dict[str, Any],
                 struct: dict[str, Any], flow: dict[str, Any],
                 synth: dict[str, Any] | None = None,
                 model_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """모델 합성과 정적 사전 조사를 병합한다. 실행하지 않은 런타임 동작은 한계로 남긴다.

    파일명 키워드 기반 템플릿 아이디어는 만들지 않는다. 아이디어는 모델 합성에서만 온다.
    모델 합성이 비었으면 정적 폴백 + 한계 표기로 돌린다.
    """
    static = _static_fallback(target, inv, struct, flow)
    model_meta = model_meta or {}
    if synth:
        overview = synth.get("overview") or static["overview"]
        architecture = synth.get("architecture") or static["architecture"]
        modules = synth.get("modules") or static["modules"]
        flows = synth.get("flows") or static["flows"]
        ideas = synth.get("ideas") or []
        limitations = list(synth.get("limitations") or [])
    else:
        overview, architecture, modules = static["overview"], static["architecture"], static["modules"]
        flows, ideas, limitations = static["flows"], [], []
        limitations.append("확인 필요: 모델 합성 없이 정적 관찰만으로 정리함"
                           + (f"({model_meta.get('model_error', '')})" if model_meta.get("model_error") else ""))
    limitations += ["대상 코드를 실행하지 않고 읽기만 했으므로 런타임 동작·외부 연동은 확인 필요",
                    "정적 import 추적이라 동적 로딩·플러그인·리플렉션 경로는 놓칠 수 있어 확인 필요"]
    if inv["secrets"]:
        limitations.append(f"비밀 의심 파일 {inv['secrets']}개는 읽지 않음")
    if inv["too_big"]:
        limitations.append(f"큰 파일 {inv['too_big']}개는 이름·크기만 기록하고 내용은 읽지 않음")
    if inv["truncated"]:
        limitations.append("확인 필요: 파일 목록이 한도로 잘려 전체 구조가 아닐 수 있음")
    if model_meta.get("dropped"):
        limitations.append(f"확인 필요: 근거 없는 모델 주장 {model_meta['dropped']}건을 버림")
    stats = {"files": len(inv["files"]), "bytes": inv["total_bytes"],
             "langs": inv["top_langs"][:6], "revision": revision,
             "files_parsed": flow["files_parsed"]}
    if model_meta.get("engine"):
        stats["model"] = {"engine": model_meta["engine"], "model": model_meta.get("model"),
                          "via": model_meta.get("via"), "files_read": model_meta.get("files_read", 0)}
    return {"overview": overview, "architecture": architecture, "modules": modules[:12],
            "flows": flows, "ideas": ideas[:8],
            "limitations": limitations,
            "stats": stats}


# ---- 작업 기록과 실행 ----

_sem = asyncio.Semaphore(MAX_CONCURRENT)
_tasks: set[asyncio.Task] = set()


def work_root() -> Path:
    from ttakkari.config import get_settings
    root = get_settings().reverse_root
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _job_dir(job_id: uuid.UUID) -> Path:
    return work_root() / job_id.hex


def _safe_git_env() -> dict[str, str]:
    """git 실행 환경. 시스템·전역 설정을 무시하고 비대화형으로 고정한다.

    로컬 경로의 악성 .git/config 실행 위험(core.fsmonitor 등 외부 명령 지정)을
    줄이기 위해 rev-parse에도 잠금·페이저·단말 프롬프트를 끈다.
    clone은 호출 쪽에서 followRedirects·credential helper를 추가로 잠근다.
    """
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


async def _git(*args: str, timeout: int = GIT_TIMEOUT) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL, env=_safe_git_env(), start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        # 자식까지 포함한 프로세스 그룹 전체를 종료하고 회수한다.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()
        raise ReverseError(503, "git_timeout", "Git 명령이 제한 시간을 넘겼습니다.") from None
    if proc.returncode != 0:
        raise ReverseError(503, "git_failed", "Git 명령이 실패했습니다: " + err.decode()[:200])
    return out.decode()


async def _clone(url: str, dest: Path) -> None:
    """리다이렉트·자격 증명 없이 shallow clone한다. 소유자 권한으로 비공개 저장소를 딸 수 없다."""
    if dest.exists():
        shutil.rmtree(dest)
    await _git("-c", "http.followRedirects=false", "-c", "credential.helper=",
               "-c", "protocol.file.allow=never",
               "clone", "--depth", "1", url, str(dest), timeout=CLONE_TIMEOUT)


def _dir_bytes(path: Path) -> int:
    total = 0
    for current, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not Path(current, d).is_symlink()]
        for name in filenames:
            p = Path(current, name)
            if p.is_symlink():
                continue
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def _enforce_disk_cap(root: Path) -> None:
    """reverse 작업 디렉터리 전체가 상한을 넘으면 끝난 작업의 clone부터 오래된 순으로 정리한다."""
    try:
        total = _dir_bytes(root)
    except OSError:
        return
    if total <= REVERSE_DISK_CAP:
        return
    candidates = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
                        key=lambda p: p.stat().st_mtime if p.exists() else 0)
    for victim in candidates:
        if _dir_bytes(root) <= REVERSE_DISK_CAP:
            break
        shutil.rmtree(victim, ignore_errors=True)


def _public(job: Any, detail: bool = False) -> dict[str, Any]:
    stages = job.stages or []
    current = next((s["key"] for s in reversed(stages) if s["status"] in (RUNNING, FAILED)), None)
    if current is None:
        current = next((s["key"] for s in reversed(stages) if s["status"] == DONE), "request")
    out: dict[str, Any] = {"id": str(job.id), "target": job.target, "kind": job.kind,
                           "status": job.status, "revision": job.revision, "stage": current,
                           "error": job.error, "created_at": job.created_at.isoformat(),
                           "updated_at": job.updated_at.isoformat()}
    if detail:
        out["stages"] = stages
        out["result"] = job.result
    elif job.result:
        out["overview"] = (job.result.get("overview") or "")[:300]
    return out


def _stale(job: Any) -> bool:
    return job.status in (QUEUED, RUNNING) and (utcnow() - job.updated_at).total_seconds() > STALE_MINUTES * 60


TERMINAL = (DONE, FAILED)


def _fail_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(s) for s in stages]
    for stage in out:
        if stage["status"] == RUNNING:
            stage.update(status=FAILED, finished_at=utcnow().isoformat())
    return out


async def recover_reverse_jobs() -> int:
    """서버 시작 시 남아 있는 진행 중 작업을 실패로 정리한다. 재시작 복구용."""
    from sqlalchemy import select

    from ttakkari import db as dbmod
    from ttakkari.models import ReverseJob
    async with dbmod.sessionmaker()() as session:
        rows = (await session.execute(
            select(ReverseJob).where(ReverseJob.status.in_((QUEUED, RUNNING)))
        )).scalars().all()
        for job in rows:
            job.status, job.error = FAILED, "서버 재시작 등으로 중단되었습니다. 다시 시작하세요."
            job.stages = _fail_stages(job.stages or _fresh_stages())
            job.updated_at = utcnow()
        await session.commit()
        return len(rows)


async def _load(job_id: uuid.UUID):
    from ttakkari import db as dbmod
    from ttakkari.models import ReverseJob
    async with dbmod.sessionmaker()() as session:
        job = await session.get(ReverseJob, job_id)
        if job is None:
            raise ReverseError(404, "unknown_job", "리버스 엔지니어링 작업을 찾을 수 없습니다.")
        if _stale(job):
            job.status, job.error = FAILED, "서버 재시작 등으로 중단되었습니다. 다시 시작하세요."
            job.stages = _fail_stages(job.stages or _fresh_stages())
            job.updated_at = utcnow()
            await session.commit()
        return job


async def _mark(job_id: uuid.UUID, stage: str, status: str, evidence: list[str] | None = None,
                notes: list[str] | None = None, error: str | None = None,
                revision: str | None = None, result: dict | None = None,
                job_status: str | None = None) -> None:
    from ttakkari import db as dbmod
    from ttakkari.models import ReverseJob
    async with dbmod.sessionmaker()() as session:
        job = await session.get(ReverseJob, job_id)
        if job is None:
            return
        stages = [dict(s) for s in (job.stages or _fresh_stages())]
        for entry in stages:
            if entry["key"] == stage:
                if status == RUNNING and entry["status"] == "pending":
                    entry["started_at"] = utcnow().isoformat()
                if status in (DONE, FAILED):
                    entry["finished_at"] = utcnow().isoformat()
                entry["status"] = status
                if evidence:
                    entry["evidence"] = (entry["evidence"] + evidence)[:MAX_EVIDENCE]
                if notes:
                    entry["notes"] = (entry["notes"] + notes)[:MAX_EVIDENCE]
                break
        job.stages = stages
        if revision is not None:
            job.revision = revision
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        if job_status is not None:
            job.status = job_status
        job.updated_at = utcnow()
        await session.commit()


async def _locked_finish(job_id: uuid.UUID, stage: str, *, result: dict | None,
                         error: str | None, job_status: str,
                         evidence: list[str] | None = None,
                         notes: list[str] | None = None) -> bool:
    """행 잠금 + 조건부 종료 전이. 이미 끝난 작업이면 덮지 않고 False를 돌려준다."""
    from sqlalchemy import select

    from ttakkari import db as dbmod
    from ttakkari.models import ReverseJob
    async with dbmod.sessionmaker()() as session:
        job = (await session.execute(
            select(ReverseJob).where(ReverseJob.id == job_id).with_for_update()
        )).scalar_one_or_none()
        if job is None or job.status in TERMINAL:
            return False
        stages = [dict(s) for s in (job.stages or _fresh_stages())]
        for entry in stages:
            if entry["key"] == stage:
                entry.update(status=DONE if job_status == DONE else FAILED,
                             finished_at=utcnow().isoformat())
                if evidence:
                    entry["evidence"] = (entry["evidence"] + evidence)[:MAX_EVIDENCE]
                if notes:
                    entry["notes"] = (entry["notes"] + notes)[:MAX_EVIDENCE]
                break
        job.stages = stages
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        job.status = job_status
        job.updated_at = utcnow()
        await session.commit()
        return True


async def _survey_stage(root: Path, target: dict[str, str], revision: str | None,
                        inv: dict[str, Any], struct: dict[str, Any],
                        flow: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
    """모델 조사: 사전 조사 지도를 보고 읽을 파일을 지정받는다. 파일은 읽기 전용으로 읽는다."""
    route = await _model_route()
    prompt = ("다음은 저장소 사전 조사 결과다. 실제 동작 파악에 꼭 필요한 파일을 골라라.\n"
              + _repo_map(target["label"], revision, inv, struct, flow))
    raw = await _review(SURVEY_SYSTEM, prompt, route)
    data = _parse_model_json(raw)
    known = {f["path"] for f in inv["files"]}
    reads: dict[str, Any] = {}
    line_counts: dict[str, int] = {}
    wanted = [p for p in (data.get("read") or []) if isinstance(p, str)][:MAX_MODEL_FILES]
    for rel in wanted:
        rel = rel.strip().lstrip("./")
        if rel not in known or rel in reads:
            continue
        excerpt = await asyncio.to_thread(_excerpt, root, rel)
        if excerpt is None:
            continue
        reads[rel], line_counts[rel] = excerpt
    notes = [str(n).strip() for n in (data.get("notes") or []) if isinstance(n, str) and str(n).strip()]
    meta = {"engine": route["engine"], "model": route["model"], "via": route["via"],
            "files_read": len(reads)}
    return {"reads": reads, "notes": notes[:MAX_EVIDENCE], "meta": meta}, line_counts, route


async def _synthesize_stage(root: Path, survey: dict[str, Any], line_counts: dict[str, int],
                            known: set[str], route: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """모델 합성: 발췌만으로 구조화 JSON을 만든다. 근거 없는 주장은 버린다."""
    parts = []
    for rel, text in survey["reads"].items():
        parts.append(f"--- {rel} ---\n{text}")
    prompt = ("다음 발췌만으로 저장소를 설명하는 구조화 JSON을 만들어라. "
              "발췌 밖의 내용은 주장하지 마라.\n\n" + "\n\n".join(parts))
    if len(prompt) > MAX_MODEL_PROMPT_CHARS:
        prompt = prompt[:MAX_MODEL_PROMPT_CHARS]
    raw = await _review(SYNTH_SYSTEM, prompt, route)
    data = _parse_model_json(raw)
    return _validate_synthesis(data, known, line_counts)


async def _pipeline(job_id: uuid.UUID, target: dict[str, str]) -> None:
    """단계별 파이프라인. 대상 코드는 읽거나 clone만 하고 절대 실행하지 않는다."""
    current = {"key": "request"}

    async def _step(key: str, status: str, **kwargs: Any) -> None:
        current["key"] = key
        await _mark(job_id, key, status, **kwargs)

    try:
        async with _sem:
            await _step("request", DONE, evidence=[f"대상: {target['label']}({target['kind']})"],
                        job_status=RUNNING)
            await _step("collect", RUNNING)
            if target["kind"] == "git_url":
                dest = _job_dir(job_id)
                await _clone(target["url"], dest)
                revision = (await _git("-C", str(dest), "rev-parse", "HEAD")).strip()
                size = await asyncio.to_thread(_dir_bytes, dest)
                if size > CLONE_BYTES_CAP:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise ReverseError(503, "clone_too_big",
                                       f"수집 결과가 상한({CLONE_BYTES_CAP // 1024 // 1024} MiB)을 넘겼습니다.")
                await asyncio.to_thread(_enforce_disk_cap, work_root())
                root = dest
                collect_ev = [f"shallow clone 완료, revision {revision[:12]}, {size // 1024} KiB"]
            else:
                root = Path(target["path"])
                try:
                    revision = (await _git("-C", str(root), "rev-parse", "HEAD")).strip()
                except ReverseError:
                    revision = ""
                collect_ev = [f"로컬 디렉터리 사용: {root}"]
                if not revision:
                    collect_ev.append("확인 필요: Git 저장소가 아니라 revision 없음")
            await _step("collect", DONE, evidence=collect_ev, revision=revision or None)
            await _step("inventory", RUNNING)
            inv = await asyncio.to_thread(inventory, root)
            await _step("inventory", DONE, evidence=inv["evidence"])
            await _step("structure", RUNNING)
            struct = await asyncio.to_thread(structure, root, inv)
            await _step("structure", DONE, evidence=struct["evidence"], notes=struct["notes"])
            await _step("flows", RUNNING)
            flow = await asyncio.to_thread(analyze_flows, root, inv, struct)
            await _step("flows", DONE, evidence=flow["evidence"], notes=flow["notes"])
            synth: dict[str, Any] | None = None
            model_meta: dict[str, Any] = {}
            await _step("survey", RUNNING)
            try:
                survey, line_counts, route = await _survey_stage(
                    root, target, revision or None, inv, struct, flow)
                model_meta.update(survey["meta"])
                survey_ev = [f"모델 경로: {route['engine']}/{route['model']}({route['via']})",
                             f"모델이 읽을 파일 {len(survey['reads'])}개 지정"]
                survey_ev += [f"읽음: {rel}" for rel in list(survey["reads"])[:10]]
                await _step("survey", DONE, evidence=survey_ev, notes=survey["notes"])
                await _step("synthesize", RUNNING)
                known = {f["path"] for f in inv["files"]}
                synth, dropped = await _synthesize_stage(
                    root, survey, line_counts, known, route)
                model_meta.update({"dropped": dropped, "files_read": len(survey["reads"])})
                synth_ev = [(f"모델 합성: 개요·아키텍처 {len(synth['architecture'])}건·"
                             f"모듈 {len(synth['modules'])}건·흐름 {len(synth['flows'])}건·"
                             f"아이디어 {len(synth['ideas'])}건")]
                if dropped:
                    synth_ev.append(f"근거 없는 주장 {dropped}건 제외")
                await _step("synthesize", DONE, evidence=synth_ev)
            except ReverseError as exc:
                model_meta["model_error"] = exc.message
                if not model_meta.get("engine"):
                    await _step("survey", FAILED, notes=[exc.message])
                    await _step("synthesize", FAILED, notes=["조사 실패로 건너뜀"])
                else:
                    await _step("synthesize", FAILED, notes=[exc.message])
            await _step("summarize", RUNNING)
            result = await asyncio.to_thread(build_result, target, revision or None,
                                             inv, struct, flow, synth, model_meta)
            summarize_ev = [(f"최종 정리: 모듈 {len(result['modules'])}건·"
                             f"흐름 {len(result['flows'])}건·아이디어 {len(result['ideas'])}건")]
            summarize_ev.append("모델 합성 병합" if synth else "정적 폴백으로 정리(모델 실패)")
            await _locked_finish(job_id, "summarize", result=result, error=None,
                                 job_status=DONE, evidence=summarize_ev)
    except ReverseError as exc:
        await _locked_finish(job_id, current["key"], result=None,
                             error=exc.message, job_status=FAILED)
    except asyncio.CancelledError:
        await _locked_finish(job_id, current["key"], result=None,
                             error="서버가 종료되어 중단되었습니다.", job_status=FAILED)
        raise
    except Exception:  # noqa: BLE001 - 작업 하나 실패가 서버를 흔들지 않게
        await _locked_finish(job_id, current["key"], result=None,
                             error="예기치 않은 오류로 실패했습니다.", job_status=FAILED)


async def start_analysis(raw_target: str, db: Any) -> dict[str, Any]:
    from ttakkari.models import ReverseJob
    target = normalize_target(raw_target)
    job = ReverseJob(target=target.get("url", target.get("path", raw_target)), kind=target["kind"],
                     status=QUEUED, stages=_fresh_stages())
    db.add(job)
    await db.flush()
    job_id = job.id
    label = target["label"]
    await db.commit()
    await _mark(job_id, "request", RUNNING, evidence=[f"대상 접수: {label}"],
                job_status=QUEUED)
    task = asyncio.get_running_loop().create_task(_pipeline(job_id, target))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"id": str(job_id), "target": job.target, "kind": job.kind,
            "status": QUEUED, "stage": "request"}


async def list_jobs(db: Any) -> dict[str, Any]:
    from sqlalchemy import select

    from ttakkari.models import ReverseJob
    rows = (await db.execute(select(ReverseJob).order_by(ReverseJob.created_at.desc()).limit(50))).scalars().all()
    touched = False
    for job in rows:
        if _stale(job):
            job.status, job.error = FAILED, "서버 재시작 등으로 중단되었습니다. 다시 시작하세요."
            job.stages = _fail_stages(job.stages or _fresh_stages())
            job.updated_at = utcnow()
            touched = True
    if touched:
        await db.commit()
    return {"items": [_public(job) for job in rows]}


async def job_detail(job_id: str, db: Any) -> dict[str, Any]:
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise ReverseError(404, "unknown_job", "리버스 엔지니어링 작업을 찾을 수 없습니다.") from None
    job = await _load(uid)
    return _public(job, detail=True)


async def remove_job(job_id: str, db: Any) -> dict[str, Any]:
    from ttakkari.models import ReverseJob
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise ReverseError(404, "unknown_job", "리버스 엔지니어링 작업을 찾을 수 없습니다.") from None
    job = await db.get(ReverseJob, uid)
    if job is None:
        raise ReverseError(404, "unknown_job", "리버스 엔지니어링 작업을 찾을 수 없습니다.")
    if job.status in (QUEUED, RUNNING):
        raise ReverseError(409, "job_running", "진행 중인 작업은 끝난 뒤 지울 수 있습니다.")
    await db.delete(job)
    await db.commit()
    shutil.rmtree(_job_dir(uid), ignore_errors=True)
    return {"id": job_id, "deleted": True}
