"""GitHub 저장소 리버스 엔지니어링 파이프라인 (ADR 0038).

요청 → 수집(shallow clone) → 목록화 → 구조 파악 → 핵심 흐름 → 정리·요약의
전 과정을 단계별 근거(evidence)와 함께 DB에 저장한다. rea에서 차용한 점은
증거 우선(evidence-first)과 한계 명시(모르는 것은 '확인 필요'로 남긴다)다.

불변 조건: 대상 코드를 절대 실행하지 않는다(빌드·스크립트·테스트 일체 금지).
비밀 파일(.env 등)은 읽지 않고 이름도 결과에 싣지 않는다. 네트워크는
git clone에만 쓴다.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import shutil
import uuid
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
    ("summarize", "정리·요약"),
)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

CLONE_TIMEOUT = 180
MAX_FILES = 20000
MAX_CONTENT_BYTES = 256 * 1024
MAX_PARSE_FILES = 4000
MAX_EVIDENCE = 30
STALE_MINUTES = 30
MAX_CONCURRENT = 2

# 비밀 파일: 읽지 않고 개수만 센다. 이름도 결과에 싣지 않는다.
SECRET_PATTERNS = (".env*", "*.pem", "*.key", "*credentials*", "*secret*",
                   "*token*", "id_rsa*", ".npmrc", ".pypirc", "*.p12", "*.pfx")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".turbo", ".tox", ".mypy_cache", ".pytest_cache"}
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


def normalize_target(raw: str) -> dict[str, str]:
    """대상을 {kind, url|path, label}로 정규화한다. URL 자격·비https는 거부한다."""
    text = (raw or "").strip()
    if not text:
        raise ReverseError(422, "empty_target", "분석할 대상을 입력하세요.")
    if len(text) > 500:
        raise ReverseError(422, "target_too_long", "대상 입력이 너무 깁니다.")
    short = re.fullmatch(r"[\w.\-]+/[\w.\-]+", text)
    if short is not None:
        return {"kind": "git_url", "url": f"https://github.com/{text}.git", "label": text}
    if "://" in text or text.startswith("git@"):
        if text.startswith(("git@", "http://", "ftp://", "file://")):
            raise ReverseError(422, "unsafe_scheme", "https로 시작하는 Git URL만 수집합니다.")
        parts = urlsplit(text)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ReverseError(422, "bad_url", "https Git URL 형식이 올바르지 않습니다.")
        match = GITHUB_REPO.match(text)
        if match:
            url = f"https://github.com/{match.group(1)}.git"
            return {"kind": "git_url", "url": url, "label": match.group(1)}
        if not parts.path:
            raise ReverseError(422, "bad_url", "https Git URL 형식이 올바르지 않습니다.")
        return {"kind": "git_url", "url": text, "label": parts.hostname + parts.path}
    path = Path(text).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ReverseError(422, "bad_local_path", "로컬 경로가 존재하는 디렉터리여야 합니다.")
    return {"kind": "local_path", "path": str(path.resolve()), "label": path.resolve().name}


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
    """파일 걷기: 비밀·제외 디렉터리는 건너뛰고 언어 통계와 크기 한도를 기록한다."""
    files: list[dict[str, Any]] = []
    lang_counts: dict[str, int] = {}
    secrets = 0
    skipped_dirs = sorted(d for d in SKIP_DIRS if (root / d).is_dir())
    too_big = 0
    total_bytes = 0
    truncated = False
    walked = 0
    for path in sorted(root.rglob("*")):
        walked += 1
        if walked > WALK_CAP:
            truncated = True
            break
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if _is_secret(path.name):
            secrets += 1
            continue
        size = path.stat().st_size
        total_bytes += size
        lang = LANGS.get(path.suffix.lower(), "other")
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
        if len(files) >= MAX_FILES:
            truncated = True
            continue
        entry: dict[str, Any] = {"path": rel, "size": size, "lang": lang}
        if size > MAX_CONTENT_BYTES:
            too_big += 1
            entry["content"] = "too_big"
        files.append(entry)
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
        if not child.is_dir() or child.name in SKIP_DIRS:
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
    support = {"tests", "test", "scripts", "docs", "website", "examples", "third_party", "experiments"}

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


def _ideas(inv: dict[str, Any], struct: dict[str, Any]) -> list[dict[str, Any]]:
    """관찰된 특성에서만 차용 아이디어를 만든다. 근거 없는 제안은 넣지 않는다."""
    present = [f["path"] for f in inv["files"]]
    lowered = " ".join(present).lower()
    ideas: list[dict[str, Any]] = []

    def has(*needles: str) -> str | None:
        return next((p for p in present if all(n in p.lower() for n in needles)), None)

    mcp_hit = has("mcp")
    if mcp_hit is not None:
        ideas.append({"title": "MCP 도구 카탈로그 방식",
                      "body": "분석 대상이 에이전트 연동(MCP) 규약을 갖고 있다. "
                              "도구마다 읽기 전용 표시와 사용 순서를 문서화하는 점이 따까리 MCP 설계에 참고된다.",
                      "evidence": [mcp_hit]})
    contract = has("contract") or has("spec", ".md") or has("protocol", ".md")
    if contract is not None:
        ideas.append({"title": "계약 문서 우선",
                      "body": "동작 계약을 별도 문서로 고정하고 구현이 따르게 한다. "
                              "따까리 ADR·마이그레이션 계약과 같은 결이다.",
                      "evidence": [contract]})
    skill = has("skill") or has("agents.md") or has("workflow")
    if skill is not None:
        ideas.append({"title": "에이전트용 절차 문서",
                      "body": "사람용 설명과 별도로 에이전트가 순서대로 따를 절차 문서를 둔다. "
                              "따까리 기획 세션 지시문에 같은 구조를 쓸 수 있다.",
                      "evidence": [skill]})
    showcase = next((d["dir"] for d in struct["topdirs"]
                     if d["dir"].lower() in ("showcase", "showcases", "examples", "guides")), None)
    if showcase is not None:
        ideas.append({"title": "사례 기반 문서",
                      "body": "실제 목표물로 끝까지 해본 사례를 문서 앞단에 둔다. "
                              "모바일에서 원문 나열보다 이해가 빠르다는 디자인시스템 기록과 일치한다.",
                      "evidence": [showcase + "/"]})
    staged = [h for h in struct["headings"] if any(k in h.lower() for k in
              ("quick start", "how ", "tutorial", "guide", "단계", "시작", "workflow"))]
    if staged:
        ideas.append({"title": "단계별 안내 구조",
                      "body": "빠른 시작 → 동작 원리 → 대상별 안내의 순서가 고정돼 있다. "
                              "리버스 결과 페이지의 타임라인 구성에 그대로 쓸 수 있다.",
                      "evidence": [f"README 소제목: {staged[0]}"]})
    if "evidence" in lowered or "limitation" in lowered or "한계" in lowered:
        hit = next((p for p in present if "evidence" in p.lower() or "limitation" in p.lower()), "README")
        ideas.append({"title": "근거·한계 명시 관행",
                      "body": "결론마다 근거와 모르는 범위를 함께 적는다. "
                              "이 기능의 단계별 evidence·확인 필요 표시에 같은 규칙을 적용했다.",
                      "evidence": [hit]})
    return ideas[:6]


def build_result(target: dict[str, str], revision: str | None, inv: dict[str, Any],
                 struct: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    """관찰된 것만 결과에 담는다. 실행하지 않은 런타임 동작은 한계로 남긴다."""
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
        modules.append({"path": hub["path"], "role": f"참조 {hub['referenced']}건 — {top} 영역의 중심 모듈",
                        "evidence": [hub["path"]]})
    for d in struct["topdirs"][:6]:
        if not any(m["path"].startswith(d["dir"] + "/") for m in modules):
            modules.append({"path": d["dir"] + "/", "role": (d["role"] or "영역") + f" — 파일 {d['files']}개",
                            "evidence": [d["dir"] + "/"]})
    flows = [{"title": f"{c['entry']} 흐름", "steps": c["reaches"], "evidence": c["evidence"]}
             for c in flow["chains"][:6]]
    limitations = ["대상 코드를 실행하지 않고 읽기만 했으므로 런타임 동작·외부 연동은 확인 필요",
                   "정적 import 추적이라 동적 로딩·플러그인·리플렉션 경로는 놓칠 수 있어 확인 필요"]
    if inv["secrets"]:
        limitations.append(f"비밀 의심 파일 {inv['secrets']}개는 읽지 않음")
    if inv["too_big"]:
        limitations.append(f"큰 파일 {inv['too_big']}개는 이름·크기만 기록하고 내용은 읽지 않음")
    if inv["truncated"]:
        limitations.append("확인 필요: 파일 목록이 한도로 잘려 전체 구조가 아닐 수 있음")
    return {"overview": overview, "architecture": architecture, "modules": modules[:10],
            "flows": flows, "ideas": _ideas(inv, struct),
            "limitations": limitations,
            "stats": {"files": len(inv["files"]), "bytes": inv["total_bytes"],
                      "langs": inv["top_langs"][:6], "revision": revision,
                      "files_parsed": flow["files_parsed"]}}


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


async def _git(*args: str, timeout: int = 30) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise ReverseError(503, "git_timeout", "Git 명령이 제한 시간을 넘겼습니다.") from None
    if proc.returncode != 0:
        raise ReverseError(503, "git_failed", "Git 명령이 실패했습니다: " + err.decode()[:200])
    return out.decode()


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


async def _load(job_id: uuid.UUID):
    from ttakkari import db as dbmod
    from ttakkari.models import ReverseJob
    async with dbmod.sessionmaker()() as session:
        job = await session.get(ReverseJob, job_id)
        if job is None:
            raise ReverseError(404, "unknown_job", "리버스 엔지니어링 작업을 찾을 수 없습니다.")
        if _stale(job):
            job.status, job.error = FAILED, "서버 재시작 등으로 중단되었습니다. 다시 시작하세요."
            stages = [dict(s) for s in (job.stages or [])]
            for stage in stages:
                if stage["status"] == RUNNING:
                    stage.update(status=FAILED, finished_at=utcnow().isoformat())
            job.stages = stages
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
                if dest.exists():
                    shutil.rmtree(dest)
                await _git("clone", "--depth", "1", target["url"], str(dest), timeout=CLONE_TIMEOUT)
                revision = (await _git("-C", str(dest), "rev-parse", "HEAD")).strip()
                root = dest
                collect_ev = [f"shallow clone 완료, revision {revision[:12]}"]
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
            await _step("summarize", RUNNING)
            result = await asyncio.to_thread(build_result, target, revision or None, inv, struct, flow)
            await _step("summarize", DONE, result=result, job_status=DONE)
    except ReverseError as exc:
        await _mark(job_id, current["key"], FAILED, error=exc.message, job_status=FAILED)
    except asyncio.CancelledError:
        await _mark(job_id, current["key"], FAILED,
                    error="서버가 종료되어 중단되었습니다.", job_status=FAILED)
        raise
    except Exception:  # noqa: BLE001 - 작업 하나 실패가 서버를 흔들지 않게
        await _mark(job_id, current["key"], FAILED,
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
