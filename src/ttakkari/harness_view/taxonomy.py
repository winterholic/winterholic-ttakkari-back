"""하네스 항목을 7개 최상위 그룹 › 관심사 › 레이어로 분류하고, 그룹·프로젝트 요약을 만든다(ADR 0070).

모든 항목은 정확히 한 그룹에 속한다(`validate` 가 시험에서 확인한다). 분류는 수집기가 붙인 hint_* 값을 먼저 쓰고, 없으면
category·scope·engine·source 로 파생한다. 레이어 정의에는 순서의 근거(code: 이 저장소 코드로 확인, external: 엔진 CLI 동작이라
확인 필요)를 함께 담아 화면에 그대로 보여 준다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from ttakkari.harness_view.model import Item

GROUPS: dict[str, dict[str, str]] = {
    "claude": {"label": "Claude 하네스", "short": "Claude", "kind": "main", "engine": "claude",
               "blurb": "Claude Code CLI 의 사용자 전역 설정(~/.claude)과 따까리 계정 폴더"},
    "codex": {"label": "Codex 하네스", "short": "Codex", "kind": "main", "engine": "codex",
              "blurb": "Codex CLI 의 전역 설정(~/.codex)"},
    "muse": {"label": "Muse 하네스", "short": "Muse", "kind": "main", "engine": "muse",
             "blurb": "Muse Code 의 설정(~/.config/muse)과 번들 플러그인(~/.local/share/muse)"},
    "qwen": {"label": "Qwen Code 하네스", "short": "Qwen Code", "kind": "main", "engine": "qwen-local",
             "blurb": "로컬 Qwen 모델로 Claude Code 를 구동하는 qwen-worker 하네스와 따까리 qwen-local 어댑터"},
    "antigravity": {"label": "Antigravity 하네스", "short": "Antigravity", "kind": "main", "engine": "antigravity",
                    "blurb": "Antigravity CLI 의 전역 설정(~/.gemini)과 공용 ~/.agents"},
    "ttakkari": {"label": "따까리 하네스", "short": "따까리", "kind": "ttakkari", "engine": "ttakkari",
                 "blurb": "접수·기획·작업자 세션에 따까리가 주입하는 지침, 라우팅, 가드, MCP 도구, 실행 환경"},
    "projects": {"label": "프로젝트별 하네스", "short": "프로젝트", "kind": "projects", "engine": "",
                 "blurb": "프로젝트 폴더의 CLAUDE.md·AGENTS.md·GEMINI.md·.claude·.codex·.agents"},
}
GROUP_ORDER = tuple(GROUPS)
MAIN_GROUPS = tuple(g for g, d in GROUPS.items() if d["kind"] == "main")

CONCERNS: dict[str, str] = {
    "instruction": "지침", "skill": "스킬", "hook": "훅", "setting": "권한·설정", "mcp": "MCP", "memory": "메모리",
    "agent": "에이전트·커맨드", "plugin": "플러그인",
    "prompt": "주입 지침", "policy": "라우팅·모델·계정", "guard": "가드", "capability": "실행 환경",
}
CONCERN_ORDER = tuple(CONCERNS)

ENGINE_LABELS = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity", "shared": "공용(.agents)",
                 "muse": "Muse", "qwen-local": "Qwen", "ttakkari": "따까리"}
PROJECT_ENGINES = ("claude", "codex", "antigravity", "shared")

CATEGORY_CONCERN = {
    "instruction": "instruction", "rule": "instruction", "setting": "setting", "hook": "hook", "skill": "skill",
    "agent": "agent", "memory": "memory", "plugin": "plugin", "mcp": "mcp", "prompt": "prompt", "policy": "policy",
    "guard": "guard", "capability": "capability",
}
ENGINE_GROUP = {"claude": "claude", "codex": "codex", "antigravity": "antigravity", "muse": "muse", "qwen-local": "qwen",
                # ~/.agents 는 npx skills 등이 여러 도구용으로 설치하는 공용 폴더다. 규칙 폴더가 Antigravity 규칙이라 그쪽에 센다.
                "shared": "antigravity"}
SOURCE_LAYER = {"code": "code", "config": "config", "file": "file", "db": "db", "generated": "measured"}


@dataclass(frozen=True)
class LayerDef:
    id: str
    label: str
    note: str
    evidence: str = "external"  # code | external
    applies: bool = True


_USER_NOTE = "엔진 CLI 가 사용자 단위로 읽는 설정. 로드 순서는 코드가 아니라 CLI 동작이라 확인 필요"
_PROJECT_NOTE = "작업 디렉터리의 프로젝트 파일. 프로젝트별 하네스 그룹에서 본다. 사용자 레이어 위에 얹힌다(확인 필요)"
_SESSION_NOTE = "따까리가 Run 마다 얹는 지침·가드·MCP. 전달 방식은 어댑터 코드로 확인"

GROUP_LAYERS: dict[str, tuple[LayerDef, ...]] = {
    "claude": (
        LayerDef("user", "사용자 전역", _USER_NOTE + "(~/.claude)"),
        LayerDef("account", "따까리 계정 폴더", "계정마다 CLAUDE_CONFIG_DIR 를 바꿔 실행한다. 계정 폴더의 자동 메모리", "code"),
        LayerDef("project", "프로젝트", _PROJECT_NOTE),
        LayerDef("session", "세션 주입(따까리)", _SESSION_NOTE, "code"),
    ),
    "codex": (
        LayerDef("user", "사용자 전역", _USER_NOTE + "(~/.codex)"),
        LayerDef("project", "프로젝트", _PROJECT_NOTE),
        LayerDef("session", "세션 주입(따까리)", _SESSION_NOTE, "code"),
    ),
    "muse": (
        LayerDef("builtin", "번들(플러그인)", "Muse 와 함께 설치된 플러그인과 스킬. 수정하지 않는다"),
        LayerDef("user", "사용자 설정", _USER_NOTE + "(~/.config/muse)"),
        LayerDef("project", "프로젝트", _PROJECT_NOTE),
        LayerDef("session", "세션 주입(따까리)", _SESSION_NOTE, "code"),
    ),
    "qwen": (
        LayerDef("harness", "하네스 저장소", "qwen-local-harness 의 지침·프롬프트·스킬·기본 설정. 실행 때 그대로 쓰이는 파일", "code"),
        LayerDef("runtime", "실행 시 생성", "요청마다 하네스가 만드는 --settings 와 Claude Code 실행 옵션(runtime.py)", "code"),
        LayerDef("project", "프로젝트", "프로젝트 지침·설정은 로드하지 않는다(--bare, --setting-sources user)", "code", False),
        LayerDef("session", "세션 주입(따까리)", "qwen-local 어댑터가 넘기는 짧은 계약과 활성화·모델 설정", "code"),
    ),
    "antigravity": (
        LayerDef("builtin", "내장", "Antigravity CLI 에 내장된 스킬. 수정하지 않는다"),
        LayerDef("user", "사용자 전역", _USER_NOTE + "(~/.gemini, ~/.agents)"),
        LayerDef("project", "프로젝트", _PROJECT_NOTE),
        LayerDef("session", "세션 주입(따까리)", _SESSION_NOTE, "code"),
    ),
    "ttakkari": (
        LayerDef("code", "코드 내장", "따까리 코드의 상수·함수가 만드는 지침·가드·도구(배포 때 바뀐다)", "code"),
        LayerDef("config", "설정(config)", "~/.ttakkari/config.env 의 TTAKKARI_* 값(허용 목록 필드만)", "code"),
        LayerDef("file", "비서 지침 파일", "~/.ttakkari/harness/*.md. 비서 관리 > 지침에서 저장한 파일", "code"),
        LayerDef("db", "DB(설정·기억·계정)", "고정 기억·엔진 계정·앱 설정. 실행 시점의 값이 합쳐진다", "code"),
        LayerDef("measured", "측정·생성", "capability 측정처럼 실행 환경에서 만들어진 값", "code"),
    ),
    "projects": (
        LayerDef("root", "프로젝트 루트", "프로젝트 최상위 폴더의 지침·설정"),
        LayerDef("nested", "하위 폴더", "모노레포 하위 폴더의 지침·설정. 루트 위에 얹힌다(확인 필요)"),
    ),
}
LAYER_LABEL = {ld.id: ld.label for defs in GROUP_LAYERS.values() for ld in defs}


def layer_rank(group: str, layer: str) -> int:
    for n, ld in enumerate(GROUP_LAYERS.get(group, ())):
        if ld.id == layer:
            return n
    return 99


def classify(item: Item) -> tuple[str, str, str]:
    """(그룹, 관심사, 레이어). 수집기의 hint 가 우선이다."""
    if item.hint_group:
        group = item.hint_group
    elif item.category in ("guard", "capability", "policy", "prompt") or item.scope == "ttakkari":
        group = "ttakkari"
    elif item.scope == "project":
        group = "projects"
    else:
        group = ENGINE_GROUP.get(item.engine, "ttakkari")
    if item.hint_concern:
        concern = item.hint_concern
    elif group == "ttakkari" and item.key == "db|pinned_brief":
        concern = "memory"
    else:
        concern = CATEGORY_CONCERN.get(item.category, "setting")
        if item.category == "rule" and item.engine == "codex" and item.group == "Codex 실행 규칙":
            concern = "setting"  # prefix_rule 은 명령 허용·금지 정책이라 지침이 아니라 권한이다
    if item.hint_layer:
        layer = item.hint_layer
    elif group == "ttakkari":
        layer = SOURCE_LAYER.get(item.source, "code")
    elif group == "projects":
        layer = "root"
    elif item.scope == "account":
        layer = "account"
    else:
        layer = "user"
    return group, concern, layer


def assign(items: list[Item]) -> None:
    for item in items:
        item.hgroup, item.concern, item.layer = classify(item)
        if item.hgroup != "projects":
            item.project = item.pool = ""


def validate(items: list[Item]) -> list[str]:
    """분류 불변 조건 위반 목록. 비어 있어야 한다: 모든 항목이 정확히 한 그룹·한 관심사·그 그룹의 레이어에 속하고, id 가 겹치지 않는다."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for item in items:
        if item.hgroup not in GROUPS:
            problems.append(f"{item.name}: 그룹 없음({item.hgroup!r})")
            continue
        if item.concern not in CONCERNS:
            problems.append(f"{item.name}: 관심사 없음({item.concern!r})")
        if item.layer not in {ld.id for ld in GROUP_LAYERS[item.hgroup]}:
            problems.append(f"{item.name}: 레이어 {item.layer!r} 가 {item.hgroup} 그룹에 없다")
        if item.hgroup == "projects" and not item.project:
            problems.append(f"{item.name}: 프로젝트 키 없음")
        if item.id in seen:
            problems.append(f"{item.name}: id 중복({seen[item.id]})")
        seen[item.id] = item.name
    return problems


# ---------------------------------------------------------------- 요약

def _counts(items: list[Item]) -> dict[str, int]:
    return {"total": len(items), "present": sum(1 for i in items if i.present),
            "missing": sum(1 for i in items if not i.present),
            "inactive": sum(1 for i in items if i.present and i.active is False),
            "warnings": sum(1 for i in items if i.warnings)}


def _concern_rows(items: list[Item], order: tuple[str, ...] = CONCERN_ORDER) -> list[dict[str, Any]]:
    rows = []
    for cid in order:
        subset = [i for i in items if i.concern == cid]
        if subset:
            rows.append({"id": cid, "label": CONCERNS[cid], **_counts(subset), "exists": any(i.present for i in subset)})
    return rows


def group_cards(items: list[Item]) -> list[dict[str, Any]]:
    cards = []
    for gid in GROUP_ORDER:
        meta = GROUPS[gid]
        subset = [i for i in items if i.hgroup == gid]
        card = {"id": gid, "label": meta["label"], "short": meta["short"], "kind": meta["kind"], "blurb": meta["blurb"],
                **_counts(subset), "exists": any(i.present for i in subset),
                "concerns": [{"id": c["id"], "label": c["label"], "total": c["total"], "present": c["present"]}
                             for c in _concern_rows(subset)]}
        if gid == "projects":
            keys = {i.project for i in subset}
            card["projects"] = len(keys)
            card["company_projects"] = len({i.project for i in subset if i.pool == "company"})
        cards.append(card)
    return cards


def group_detail(items: list[Item], gid: str, injection: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    meta = GROUPS[gid]
    subset = [i for i in items if i.hgroup == gid]
    layers = []
    for n, ld in enumerate(GROUP_LAYERS[gid], 1):
        members = [i for i in subset if i.layer == ld.id]
        row: dict[str, Any] = {
            "id": ld.id, "label": ld.label, "note": ld.note, "order": n, "evidence": ld.evidence,
            "applies": ld.applies, **_counts(members),
            "concerns": dict(Counter(i.concern for i in members)),
        }
        if ld.id == "project" and gid in MAIN_GROUPS:
            engine_items = [i for i in items if i.hgroup == "projects" and i.engine in _project_engines(gid)]
            row["link"] = {"group": "projects", "label": "프로젝트별 하네스에서 보기",
                           "total": len(engine_items), "projects": len({i.project for i in engine_items})}
        if ld.id == "session" and injection is not None:
            row["injection"] = injection
        layers.append(row)
    notes = []
    if gid == "claude":
        notes.append("메모리는 따까리 계정 폴더의 자동 메모리와 ~/.claude/memory 를 함께 센다.")
    if gid == "antigravity":
        notes.append("~/.agents(공용 스킬·규칙)는 여러 도구가 읽는 폴더라 이 그룹에 센다. Codex 가 읽는지는 확인 필요.")
    if gid == "codex":
        notes.append("실행 규칙(.rules)은 명령 허용·금지 정책이라 '권한·설정'에 둔다.")
    if gid == "projects":
        notes.append("Qwen 하네스 저장소(qwen-local-harness)는 Qwen Code 하네스 그룹에서 본다.")
    return {"id": gid, "label": meta["label"], "kind": meta["kind"], "blurb": meta["blurb"], **_counts(subset),
            "exists": any(i.present for i in subset), "concerns": _concern_rows(subset), "layers": layers, "notes": notes}


def _project_engines(gid: str) -> tuple[str, ...]:
    return {"claude": ("claude",), "codex": ("codex",), "antigravity": ("antigravity", "shared"),
            "muse": ("shared",), "qwen": ()}.get(gid, ())


def projects_list(items: list[Item], seen: dict[str, str] | None = None) -> dict[str, Any]:
    by_key: dict[str, list[Item]] = {}
    for item in items:
        if item.hgroup == "projects":
            by_key.setdefault(item.project, []).append(item)
    rows = []
    for key, members in by_key.items():
        pool = members[0].pool or "personal"
        rows.append({
            "key": key, "name": key.rsplit("/", 1)[-1], "container": key.split("/")[0] if "/" in key else "",
            "pool": pool, **_counts(members),
            "engines": dict(Counter(i.engine for i in members)),
            "concerns": {c: n for c, n in Counter(i.concern for i in members).items()},
        })
    rows.sort(key=lambda r: (r["pool"] != "company", r["key"].lower()))
    without = sorted(k for k in (seen or {}) if k not in by_key)
    return {"projects": rows, "total": len(rows), "scanned": len(rows) + len(without),
            "without_harness": without[:80],
            "company": sum(1 for r in rows if r["pool"] == "company")}


def project_detail(items: list[Item], key: str) -> dict[str, Any] | None:
    members = [i for i in items if i.hgroup == "projects" and i.project == key]
    if not members:
        return None
    engines = []
    for engine in PROJECT_ENGINES:
        mine = [i for i in members if i.engine == engine]
        if not mine:
            continue
        concerns = []
        for cid in CONCERN_ORDER:
            rows = sorted((i for i in mine if i.concern == cid), key=lambda i: (layer_rank("projects", i.layer), i.group, i.name))
            if rows:
                concerns.append({"id": cid, "label": CONCERNS[cid], "total": len(rows),
                                 "items": [r.summary() for r in rows[:120]], "truncated": len(rows) > 120})
        engines.append({"id": engine, "label": ENGINE_LABELS[engine], **_counts(mine), "concerns": concerns})
    return {"key": key, "name": key.rsplit("/", 1)[-1], "pool": members[0].pool, **_counts(members), "engines": engines}


# ---------------------------------------------------------------- 상세 보조: 경로·대응 항목

def crumb(item: Item) -> list[dict[str, str]]:
    meta = GROUPS.get(item.hgroup)
    if meta is None:
        return []
    out = [{"kind": "group", "id": item.hgroup, "label": meta["label"]}]
    if item.hgroup == "projects":
        out.append({"kind": "project", "id": item.project, "label": item.project.rsplit("/", 1)[-1]})
        out.append({"kind": "engine", "id": item.engine, "label": ENGINE_LABELS.get(item.engine, item.engine)})
    out.append({"kind": "concern", "id": item.concern, "label": CONCERNS.get(item.concern, item.concern)})
    return out


# 대응 항목은 엔진마다 한두 개뿐인 "파일형" 관심사에서만 찾는다. 스킬·MCP 처럼 수십 개인 목록은 관심사 링크로 대신한다.
COUNTERPART_CONCERNS = ("instruction", "setting")
COUNTERPART_MAX_PER_ENGINE = 3


def counterparts(items: list[Item], item: Item) -> dict[str, Any]:
    if item.hgroup == "projects":
        peers = [i for i in items if i.hgroup == "projects" and i.project == item.project and i.engine != item.engine
                 and i.concern == item.concern and i.layer == item.layer]
        label = lambda i: ENGINE_LABELS.get(i.engine, i.engine)
    elif item.hgroup in MAIN_GROUPS:
        peers = [i for i in items if i.hgroup in MAIN_GROUPS and i.hgroup != item.hgroup and i.concern == item.concern
                 and i.layer == item.layer]
        label = lambda i: GROUPS[i.hgroup]["short"]
        if item.concern == "instruction":
            # 참조 문서·규칙 폴더가 아니라 엔진의 대표 지침 파일끼리만 짝지운다
            peers = [i for i in peers if i.category == "instruction"]
    else:
        return {"items": [], "concerns": []}
    out_items: list[dict[str, Any]] = []
    concerns: list[dict[str, Any]] = []
    groups = Counter((i.hgroup, i.engine) for i in peers)
    for (hg, eng), n in groups.items():
        sample = [i for i in peers if i.hgroup == hg and i.engine == eng]
        if item.concern in COUNTERPART_CONCERNS and n <= COUNTERPART_MAX_PER_ENGINE:
            out_items += [{"id": i.id, "name": i.name, "engine": i.engine, "hgroup": hg, "label": label(i),
                           "present": i.present, "path": i.path} for i in sample]
        else:
            concerns.append({"hgroup": hg, "engine": eng, "label": label(sample[0]), "concern": item.concern,
                             "project": item.project if item.hgroup == "projects" else "", "total": n})
    return {"items": out_items, "concerns": concerns}
