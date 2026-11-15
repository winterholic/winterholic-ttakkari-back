"""저장소 가치 평가 루브릭: 축·항목 정의, 유형 분류, 결정론 점수, 가중·등급·약점 Top3(ADR 0073).

전부 순수 함수다. LLM 은 점수를 매기는 데만 쓰고 산수는 여기서 한다.
항목 추가·제거는 ITEMS·WEIGHTS·ADVICE 상수에서 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ttakkari.harness_view import sources as harness_sources
from ttakkari.harness_view.model import LONG_CHARS, LONG_LINES
from ttakkari.harness_view.sources import CODEX_DOC_LIMIT

RUBRIC_VERSION = "1"

AXES = ("code_quality", "architecture", "maintainability", "ai_harness", "originality")
REPO_TYPES = ("library", "app", "docs", "experiment", "unknown")

ITEMS: dict[str, tuple[str, ...]] = {
    "code_quality": ("tests", "lint_config", "file_size", "todo_density", "build_lock", "readability"),
    "architecture": ("structure", "docs_adr", "entrypoints", "cycles", "layering"),
    "maintainability": ("recent_commits", "last_commit", "active_months", "contributors_bus",
                        "ci", "releases", "issue_hygiene", "license"),
    "ai_harness": ("presence", "length", "refs", "dup_names", "coherence"),
    "originality": ("verdict",),
}
LLM_ITEMS = frozenset({"code_quality.readability", "architecture.layering",
                       "ai_harness.coherence", "originality.verdict"})

# 유형별 축 가중치. 행 합은 1. docs 는 코드·구조 비중을 낮추고 테스트 감점을 빼고 본다.
WEIGHTS: dict[str, dict[str, float]] = {
    "library": {"code_quality": 0.30, "architecture": 0.20, "maintainability": 0.25,
                "ai_harness": 0.15, "originality": 0.10},
    "app": {"code_quality": 0.30, "architecture": 0.25, "maintainability": 0.25,
            "ai_harness": 0.10, "originality": 0.10},
    "docs": {"code_quality": 0.10, "architecture": 0.10, "maintainability": 0.35,
             "ai_harness": 0.15, "originality": 0.30},
    "experiment": {"code_quality": 0.20, "architecture": 0.15, "maintainability": 0.20,
                   "ai_harness": 0.15, "originality": 0.30},
    "unknown": {"code_quality": 0.25, "architecture": 0.20, "maintainability": 0.30,
                "ai_harness": 0.15, "originality": 0.10},
}

ADVICE: dict[str, str] = {
    "code_quality.tests": "핵심 모듈부터 테스트 파일을 추가하고 테스트 대 소스 비율을 0.3 이상으로 올리세요.",
    "code_quality.lint_config": "린터·포매터·타입 검사 설정을 모두 두고 CI 에서 검사하세요.",
    "code_quality.file_size": "100KB 넘는 소스 파일을 기능 단위로 나누세요.",
    "code_quality.todo_density": "표본에서 본 TODO/FIXME 를 이슈로 옮기거나 처리하세요.",
    "code_quality.build_lock": "빌드 선언 파일과 잠금 파일을 함께 두어 재현 가능하게 하세요.",
    "code_quality.readability": "표본 파일의 이름·함수 길이·주석을 다듬어 가독성을 올리세요.",
    "architecture.structure": "관련 파일을 디렉터리로 묶고 루트 직속 소스를 10개 이하로 줄이세요.",
    "architecture.docs_adr": "README 를 보강하고 docs/ 와 결정 기록(ADR)을 두세요.",
    "architecture.entrypoints": "진입점을 명확히 하고 패키지 선언 파일에 실행 방법을 적으세요.",
    "architecture.cycles": "상호 참조하는 모듈 쌍을 한 방향 의존으로 풀어내세요.",
    "architecture.layering": "계층·책임 경계를 정리해 진입점에서 핵심 흐름이 보이게 하세요.",
    "maintainability.recent_commits": "최근 90일 변경이 적습니다. 유지보수 상태를 README 에 명시하세요.",
    "maintainability.last_commit": "마지막 변경이 오래됐습니다. 보관(archive) 여부를 정하세요.",
    "maintainability.active_months": "활동한 달이 적습니다. 당분간 손볼 계획이 있는지 밝히세요.",
    "maintainability.contributors_bus": "기여가 한 사람에 몰렸습니다. 문서화로 버스 팩터를 올리세요.",
    "maintainability.ci": ".github/workflows 에 검사·빌드 워크플로를 추가하세요.",
    "maintainability.releases": "릴리스를 주기적으로 내고 변경점을 적으세요.",
    "maintainability.issue_hygiene": "밀린 이슈·PR 을 닫거나 정리하고 대응 주기를 만드세요.",
    "maintainability.license": "라이선스 파일을 추가해 재사용 조건을 밝히세요.",
    "ai_harness.presence": "에이전트 지침 파일(AGENTS.md 등)을 두면 협업 효율이 오릅니다.",
    "ai_harness.length": "긴 지침 파일을 400줄·20,000자 안으로 나누세요.",
    "ai_harness.refs": "지침 속 깨진 참조를 고치거나 지우세요.",
    "ai_harness.dup_names": "같은 이름의 스킬·에이전트 정의를 하나로 합치세요.",
    "ai_harness.coherence": "지침 간 모순을 없애고 역할 분담을 명시하세요.",
    "originality.verdict": "README 에 기존 도구와의 차이를 구체적으로 적으세요.",
}

SOURCE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt",
    ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".swift", ".scala", ".m",
    ".mm", ".r", ".jl", ".lua", ".pl", ".sh", ".vue", ".svelte",
})
_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)")
_TEST_FILE_RE = re.compile(r"(^|/)(test_.*|.*[._-](test|spec))\.[^.]+$")
MANIFESTS = frozenset({"pyproject.toml", "setup.py", "setup.cfg", "package.json", "go.mod",
                       "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
                       "Gemfile", "composer.json"})
BUILD_FILES = MANIFESTS | {"Makefile", "CMakeLists.txt", "requirements.txt"}
LOCK_FILES = frozenset({"uv.lock", "poetry.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                        "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", "Pipfile.lock"})
LINT_FILES = frozenset({"ruff.toml", ".ruff.toml", "eslint.config.js", "eslint.config.mjs",
                        "biome.json", "biome.jsonc", ".golangci.yml", ".golangci.yaml",
                        ".flake8", ".pylintrc", "pylintrc", ".rubocop.yml"})
LINT_PREFIXES = (".eslintrc",)
FORMAT_FILES = frozenset({".editorconfig", ".prettierrc", ".prettierrc.json", ".prettierrc.yml",
                          "prettier.config.js", ".clang-format", "rustfmt.toml", ".style.yapf"})
TYPES_FILES = frozenset({"mypy.ini", ".mypy.ini", "pyrightconfig.json", "tsconfig.json"})
ENTRY_NAMES = frozenset({"__main__.py", "main.py", "main.ts", "main.js", "main.go", "main.rs",
                         "main.java", "app.py", "app.ts", "app.js", "server.py", "server.ts",
                         "server.js", "index.ts", "index.js", "cli.py", "manage.py", "run.py"})
DEPLOY_HINTS = frozenset({"Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Procfile",
                          "vercel.json", "fly.toml", "railway.json", "app.yaml"})
OSI_LICENSES = frozenset({"mit", "apache-2.0", "gpl-2.0", "gpl-3.0", "lgpl-2.1", "lgpl-3.0",
                           "bsd-2-clause", "bsd-3-clause", "isc", "mpl-2.0", "unlicense",
                           "cc0-1.0", "agpl-3.0", "artistic-2.0", "epl-2.0"})
_OVERSIZE_BYTES = 100 * 1024
_TODO_RE = re.compile(r"\b(TODO|FIXME)\b")
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"""(?:from\s+|require\()\s*['"]([^'"]+)['"]""")
_EVIDENCE_PREFIX = "evidence:"


@dataclass
class ItemScore:
    axis: str
    item_id: str
    score: float | None  # 0~5. None 은 미채점(unavailable·unverified·skipped·na·pending)
    status: str  # scored|unavailable|unverified|skipped|na|pending
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.axis}.{self.item_id}"

    def to_dict(self) -> dict[str, Any]:
        return {"item": self.key, "score": self.score, "status": self.status, "evidence": self.evidence}


def _band(value: float, bands: list[tuple[float, int]]) -> int:
    """내림차순 경계표. 5점은 기준을 넘어선 값에만 준다."""
    for edge, score in bands:
        if value >= edge:
            return score
    return 0


def _blobs(facts: dict) -> list[dict]:
    return [t for t in facts.get("tree", []) if t.get("type") == "blob"]


def _is_source(path: str) -> bool:
    low = path.lower()
    return any(low.endswith(ext) for ext in SOURCE_EXTS)


def _is_test(path: str) -> bool:
    low = path.lower()
    return bool(_TEST_DIR_RE.search(low) or _TEST_FILE_RE.search(low))


def _scored(axis: str, item_id: str, score: float, evidence: dict) -> ItemScore:
    return ItemScore(axis, item_id, float(score), "scored", evidence)


def _miss(axis: str, item_id: str, status: str, evidence: dict | None = None) -> ItemScore:
    return ItemScore(axis, item_id, None, status, evidence or {})


# ---- 유형 분류 ----

def classify_repo_type(facts: dict) -> tuple[str, list[str]]:
    """규칙 기반 저장소 유형. 애매하면 unknown 이고 기본 가중치를 쓴다."""
    blobs = _blobs(facts)
    if not blobs:
        return "unknown", ["빈 트리"]
    names = {p.rsplit("/", 1)[-1] for p in (b["path"] for b in blobs)}
    has_manifest = bool(names & MANIFESTS)
    readme_len = len(facts.get("readme_text", ""))
    md_ratio = sum(1 for b in blobs if b["path"].lower().endswith(".md")) / len(blobs)
    reasons: list[str] = []
    if len(blobs) < 8 and not has_manifest and readme_len < 300:
        return "experiment", [f"파일 {len(blobs)}개·선언 파일 없음·짧은 README"]
    if md_ratio >= 0.6 and not has_manifest:
        return "docs", [f"마크다운 비율 {md_ratio:.0%}"]
    entries = [b["path"] for b in blobs if b["path"].rsplit("/", 1)[-1] in ENTRY_NAMES]
    deploy = sorted(names & DEPLOY_HINTS)
    if entries and (deploy or has_manifest):
        reasons.append(f"진입점 {len(entries)}개")
        if deploy:
            reasons.append(f"배포 설정 {','.join(deploy)}")
        return "app", reasons
    if has_manifest:
        return "library", [f"선언 파일 {(names & MANIFESTS)}"]
    return "unknown", ["해당 규칙 없음"]


# ---- code_quality ----

def score_code_quality(facts: dict, repo_type: str = "unknown") -> list[ItemScore]:
    axis, out = "code_quality", []
    sources = [b for b in _blobs(facts) if _is_source(b["path"]) and not _is_test(b["path"])]
    tests = [b for b in _blobs(facts) if _is_source(b["path"]) and _is_test(b["path"])]
    if repo_type == "docs":
        out.append(_miss(axis, "tests", "skipped", {"reason": "문서 저장소는 테스트 감점 제외"}))
    elif not sources:
        out.append(_miss(axis, "tests", "unavailable", {"reason": "소스 파일 없음"}))
    else:
        ratio = len(tests) / len(sources)
        out.append(_scored(axis, "tests", _band(ratio, [(0.8, 5), (0.5, 4), (0.3, 3), (0.15, 2), (0.01, 1)]),
                           {"test_files": len(tests), "source_files": len(sources), "ratio": round(ratio, 3)}))
    names = {b["path"].rsplit("/", 1)[-1] for b in _blobs(facts)}
    cats = set()
    if names & LINT_FILES or any(n.startswith(LINT_PREFIXES) for n in names):
        cats.add("lint")
    if names & FORMAT_FILES:
        cats.add("format")
    if names & TYPES_FILES:
        cats.add("types")
    out.append(_scored(axis, "lint_config", {3: 5, 2: 3, 1: 1}.get(len(cats), 0),
                       {"categories": sorted(cats)}))
    if not sources:
        out.append(_miss(axis, "file_size", "unavailable", {"reason": "소스 파일 없음"}))
    else:
        big = [b["path"] for b in sources if (b.get("size") or 0) > _OVERSIZE_BYTES]
        ratio = len(big) / len(sources)
        inv = _band(1 - ratio, [(1.0, 5), (0.98, 4), (0.95, 3), (0.90, 2), (0.80, 1)])
        out.append(_scored(axis, "file_size", inv,
                           {"oversize": len(big), "source_files": len(sources),
                            "paths": big[:10], "threshold_bytes": _OVERSIZE_BYTES}))
    samples = facts.get("samples", {})
    if not samples:
        out.append(_miss(axis, "todo_density", "unavailable", {"reason": "표본 없음"}))
    else:
        todos = sum(len(_TODO_RE.findall(text)) for text in samples.values())
        lines = sum(max(1, len(text.splitlines())) for text in samples.values())
        per_kloc = todos / lines * 1000
        out.append(_scored(axis, "todo_density",
                           5 if todos == 0 else _band(10 - per_kloc, [(9, 4), (7, 3), (4, 2), (0.01, 1)]),
                           {"todos": todos, "lines": lines, "per_kloc": round(per_kloc, 2),
                            "files": len(samples)}))
    build = sorted(names & BUILD_FILES)
    lock = sorted(names & LOCK_FILES)
    out.append(_scored(axis, "build_lock", 5 if build and lock else 3 if build else 2 if lock else 0,
                       {"build": build, "lock": lock}))
    out.append(_miss(axis, "readability", "pending", {"reason": "LLM 판정 대기"}))
    return out


# ---- architecture ----

def score_architecture(facts: dict) -> list[ItemScore]:
    axis, out = "architecture", []
    blobs = _blobs(facts)
    depths = [p.count("/") for p in (b["path"] for b in blobs)]
    top_dirs = {p.split("/", 1)[0] for p in (b["path"] for b in blobs) if "/" in p}
    root_files = sum(1 for b in blobs if "/" not in b["path"])
    max_depth = max(depths) if depths else 0
    points = (2 if 2 <= max_depth <= 4 else 1 if max_depth == 5 else 0)
    points += 2 if len(top_dirs) >= 3 else 1 if len(top_dirs) == 2 else 0
    points += 1 if root_files <= 10 else 0
    out.append(_scored(axis, "structure", points,
                       {"max_depth": max_depth, "top_dirs": len(top_dirs), "root_files": root_files}))
    readme_len = len(facts.get("readme_text", ""))
    has_docs = any(b["path"].startswith("docs/") for b in blobs)
    adr = [b["path"] for b in blobs
           if "/adr/" in b["path"].lower() or "adr" in b["path"].rsplit("/", 1)[-1].lower()]
    points = (2 if readme_len >= 2000 else 1 if readme_len >= 300 else 0)
    points += 1 if has_docs else 0
    points += 2 if len(adr) >= 2 else 1 if adr else 0
    out.append(_scored(axis, "docs_adr", min(5, points),
                       {"readme_chars": readme_len, "docs_dir": has_docs, "adr_files": adr[:10]}))
    names = {b["path"].rsplit("/", 1)[-1] for b in blobs}
    entries = sorted(b["path"] for b in blobs if b["path"].rsplit("/", 1)[-1] in ENTRY_NAMES)
    manifests = sorted(names & MANIFESTS)
    out.append(_scored(axis, "entrypoints", 5 if entries and manifests else 3 if entries else 2 if manifests else 0,
                       {"entries": entries[:10], "manifests": manifests}))
    samples = facts.get("samples", {})
    if len(samples) < 2:
        out.append(_miss(axis, "cycles", "unavailable", {"reason": "표본 2개 미만"}))
    else:
        stems = {path.rsplit("/", 1)[-1].rsplit(".", 1)[0]: path for path in samples}
        edges: dict[str, set[str]] = {path: set() for path in samples}
        for path, text in samples.items():
            imported = set()
            for mod, mods in _PY_IMPORT_RE.findall(text):
                imported.update((mod or "").split("."))
                imported.update(m.strip().split(".")[0] for m in (mods or "").split(","))
            imported.update(m.rsplit("/", 1)[-1].split(".")[0] for m in _JS_IMPORT_RE.findall(text))
            for stem, target in stems.items():
                if stem and stem in imported and target != path:
                    edges[path].add(target)
        pairs = sorted({tuple(sorted((a, b))) for a, targets in edges.items()
                        for b in targets if a in edges.get(b, set())})
        out.append(_scored(axis, "cycles", 4 if not pairs else 2 if len(pairs) == 1 else 0,
                           {"checked": len(samples), "pairs": [list(p) for p in pairs],
                            "note": "표본 한정이라 5점은 주지 않음"}))
    out.append(_miss(axis, "layering", "pending", {"reason": "LLM 판정 대기"}))
    return out


# ---- maintainability ----

def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def score_maintainability(facts: dict, *, now: datetime | None = None) -> list[ItemScore]:
    axis, out = "maintainability", []
    now = now or datetime.now(UTC)
    dates = sorted((d for c in facts.get("commits", []) if (d := _parse_date(c.get("date")))),
                   reverse=True)
    recent = sum(1 for d in dates if (now - d).days <= 90)
    out.append(_scored(axis, "recent_commits", _band(recent, [(30, 5), (12, 4), (5, 3), (2, 2), (1, 1)]),
                       {"count": recent, "window_days": 90}))
    if not dates:
        out.append(_scored(axis, "last_commit", 0, {"days_ago": None, "reason": "커밋 없음"}))
    else:
        days = max(0, (now - dates[0]).days)
        out.append(_scored(axis, "last_commit",
                           5 if days <= 7 else 4 if days <= 30 else 3 if days <= 90 else 2 if days <= 180 else 1 if days <= 365 else 0,
                           {"days_ago": days}))
    months = {d.strftime("%Y-%m") for d in dates if d <= now and (now - d).days <= 365}
    for m in facts.get("stats_months") or []:  # 커밋 목록 상한(100개)을 넘긴 저장소의 통계 보충
        if isinstance(m, str) and len(m) == 7:
            months.add(m)
    out.append(_scored(axis, "active_months", _band(len(months), [(10, 5), (7, 4), (5, 3), (3, 2), (1, 1)]),
                       {"months": len(months), "window_months": 12}))
    contributors = facts.get("contributors")
    if contributors is None:
        out.append(_miss(axis, "contributors_bus", "unavailable", {"reason": "기여자 통계 없음"}))
    elif not contributors:
        out.append(_scored(axis, "contributors_bus", 0, {"n": 0, "reason": "기여자 없음"}))
    else:
        ordered = sorted(contributors, key=lambda c: c.get("contributions", 0), reverse=True)
        total = sum(c.get("contributions", 0) for c in ordered) or 1
        acc, bus = 0, len(ordered)
        for i, c in enumerate(ordered):
            acc += c.get("contributions", 0)
            if acc / total >= 0.8:
                bus = i + 1
                break
        score = 5 if bus >= 4 else 4 if bus == 3 else 3 if bus == 2 else 2 if len(ordered) >= 2 else 1
        out.append(_scored(axis, "contributors_bus", score,
                           {"n": len(ordered), "bus": bus,
                            "top_share": round(ordered[0].get("contributions", 0) / total, 3)}))
    workflows = facts.get("workflows")
    if workflows is None:
        out.append(_miss(axis, "ci", "unavailable", {"reason": "워크플로 조회 실패"}))
    else:
        out.append(_scored(axis, "ci", 5 if len(workflows) >= 3 else 4 if len(workflows) == 2 else 3 if workflows else 0,
                           {"workflows": workflows[:10]}))
    releases = facts.get("releases")
    if releases is None:
        out.append(_miss(axis, "releases", "unavailable", {"reason": "릴리스 조회 실패"}))
        pubs: list = []
    else:
        pubs = sorted((d for r in releases
                       if not r.get("draft") and not r.get("prerelease") and (d := _parse_date(r.get("published_at")))
                       and (now - d).days <= 365), reverse=True)
    if releases is not None:
        gaps = [(pubs[i] - pubs[i + 1]).days for i in range(len(pubs) - 1)]
        out.append(_scored(axis, "releases",
                           _band(len(pubs), [(6, 5), (3, 4), (2, 3), (1, 2)]),
                           {"count_12mo": len(pubs),
                            "median_gap_days": sorted(gaps)[len(gaps) // 2] if gaps else None}))
    issues, pulls = facts.get("issues_sample"), facts.get("pulls_sample")
    sample = (issues or []) + (pulls or [])
    if issues is None and pulls is None:
        out.append(_miss(axis, "issue_hygiene", "unavailable", {"reason": "이슈·PR 조회 실패"}))
    elif len(sample) < 5:
        out.append(_miss(axis, "issue_hygiene", "unavailable",
                         {"reason": "표본 5개 미만", "sample": len(sample)}))
    else:
        closed = sum(1 for i in sample if i.get("state") in ("closed", "merged"))
        ratio = closed / len(sample)
        out.append(_scored(axis, "issue_hygiene",
                           _band(ratio, [(0.9, 5), (0.75, 4), (0.6, 3), (0.4, 2), (0.01, 1)]),
                           {"open": facts.get("repo_meta", {}).get("open_issues_count"),
                            "sample_closed": closed, "sample_total": len(sample),
                            "ratio": round(ratio, 3)}))
    lic = (facts.get("repo_meta", {}).get("license") or "").lower()
    out.append(_scored(axis, "license", 5 if lic in OSI_LICENSES else 3 if lic else 0, {"license": lic or None}))
    return out


# ---- ai_harness ----

def score_ai_harness(facts: dict) -> list[ItemScore]:
    axis = "ai_harness"
    found = facts.get("harness_found", [])
    if not found:
        return [_miss(axis, item_id, "na", {"reason": "하네스 파일 없음"})
                for item_id in ("presence", "length", "refs", "dup_names")]
    out = [_scored(axis, "presence", 5 if len(found) >= 6 else 4 if len(found) >= 4 else 3 if len(found) >= 2 else 2,
                   {"found": sorted(found)[:20], "count": len(found)})]
    texts = facts.get("harness_texts", {})
    md_texts = {p: t for p, t in texts.items() if p.lower().endswith(".md")}
    if not md_texts:
        out.append(_miss(axis, "length", "unavailable", {"reason": "지침 본문 없음"}))
    else:
        cut = [p for p in facts.get("harness_truncated", []) if p.lower().endswith(".md")]
        over = [p for p, t in sorted(md_texts.items())
                if (p.rsplit("/", 1)[-1] == "AGENTS.md" and len(t.encode()) > CODEX_DOC_LIMIT)
                or t.count("\n") + 1 > LONG_LINES or len(t) > LONG_CHARS]
        over = sorted(set(over) | set(cut))  # 수집 상한에 잘린 본문은 한도를 넘긴 것으로 본다
        out.append(_scored(axis, "length", 5 if not over else 3 if len(over) == 1 else 1,
                           {"checked": len(md_texts), "over": over,
                            "limits": {"lines": LONG_LINES, "chars": LONG_CHARS,
                                       "agents_bytes": CODEX_DOC_LIMIT}}))
    if not texts:
        out.append(_miss(axis, "refs", "unavailable", {"reason": "지침 본문 없음"}))
    else:
        tree_paths = {b["path"] for b in _blobs(facts)}
        broken: list[str] = []
        for path, text in sorted(texts.items()):
            base = path.rsplit("/", 1)[0]
            for ref in harness_sources.extract_refs(text):
                if ref.startswith(("~/", "/")) or ref in broken:
                    continue
                target = f"{base}/{ref}" if base else ref
                if target not in tree_paths:
                    broken.append(ref if len(broken) < 10 else "")
        broken = [b for b in broken if b]
        out.append(_scored(axis, "refs", 5 if not broken else 3 if len(broken) == 1 else 1,
                           {"checked": len(texts), "broken": broken}))
    groups: dict[str, list[str]] = {}
    for path in found:
        groups.setdefault(path.rsplit("/", 1)[-1].lower(), []).append(path)
    dup = {k: sorted(v) for k, v in groups.items() if len({p.rsplit("/", 1)[0] for p in v}) > 1}
    out.append(_scored(axis, "dup_names", 5 if not dup else 3 if len(dup) == 1 else 1,
                       {"groups": dup}))
    out.append(_miss(axis, "coherence", "pending", {"reason": "LLM 판정 대기"}))
    return out


# ---- LLM 점수 적용 ----

def apply_llm_scores(items: list[ItemScore], llm_out: dict, *, valid_paths: set[str]) -> list[ItemScore]:
    """LLM 판정을 항목에 얹는다. 인용 없거나 없는 경로 인용은 폐기하고 0~5 로 clamp 한다."""
    by_key = {i.key: i for i in items}
    for row in llm_out.get("scores", []) if isinstance(llm_out, dict) else []:
        if not isinstance(row, dict) or row.get("item") not in LLM_ITEMS:
            continue
        target = by_key.get(row["item"])
        if target is None:
            continue
        citations = row.get("citations") or []
        score = row.get("score")
        ok_cites = [c for c in citations
                    if isinstance(c, str) and (c in valid_paths or c.startswith(_EVIDENCE_PREFIX))]
        if not citations or len(ok_cites) != len(citations) or not isinstance(score, (int, float)):
            target.status, target.score = "unverified", None
            target.evidence = {"reason": "인용 근거 없음", "citations": citations}
            continue
        target.score = float(max(0, min(5, score)))
        target.status = "scored"
        target.evidence = {"citations": ok_cites, "confidence": row.get("confidence")}
        if target.key == "originality.verdict":
            target.evidence["low_reliability"] = True
    return items


# ---- 집계 ----

def axis_scores(items_by_axis: dict[str, list[ItemScore]]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for axis in AXES:
        got = [i.score for i in items_by_axis.get(axis, []) if i.status == "scored" and i.score is not None]
        out[axis] = sum(got) / len(got) if got else None
    return out


def weighted_score(axes: dict[str, float | None], repo_type: str) -> tuple[float, dict[str, float]]:
    """축 점수(0~5)의 가중합을 0~100 으로. 미채점 축은 제외하고 가중치를 재정규화한다."""
    weights = WEIGHTS.get(repo_type, WEIGHTS["unknown"])
    total = sum(w for a, w in weights.items() if axes.get(a) is not None)
    contrib = {a: (axes[a] / 5 * 100 * weights[a] / total if axes.get(a) is not None and total else 0.0)
               for a in AXES}
    return round(sum(contrib.values()), 1), {a: round(v, 2) for a, v in contrib.items()}


def grade_for(score: float) -> str:
    return ("A+" if score >= 90 else "A" if score >= 85 else "B+" if score >= 80 else "B" if score >= 70
            else "C" if score >= 60 else "D")


def top_weaknesses(items: list[ItemScore], axes: dict[str, float | None], repo_type: str) -> list[dict]:
    """가중 손실이 큰 항목 Top3. 보강안은 템플릿 문장이라 LLM 이 필요 없다."""
    weights = WEIGHTS.get(repo_type, WEIGHTS["unknown"])
    total = sum(w for a, w in weights.items() if axes.get(a) is not None) or 1.0
    counts: dict[str, int] = {}
    for i in items:
        if i.status == "scored" and i.score is not None:
            counts[i.axis] = counts.get(i.axis, 0) + 1
    ranked = []
    for i in items:
        if i.status != "scored" or i.score is None or i.score >= 5:
            continue
        loss = (5 - i.score) / 5 * 100 * weights[i.axis] / total / max(1, counts.get(i.axis, 1))
        ranked.append({"item": i.key, "score": i.score, "loss": round(loss, 2),
                       "advice": ADVICE.get(i.key, "")})
    ranked.sort(key=lambda r: -r["loss"])
    return ranked[:3]
