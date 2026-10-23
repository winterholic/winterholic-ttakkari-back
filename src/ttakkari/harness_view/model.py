"""하네스 조회 항목 모델. 본문은 마스킹된 상태로만 들고 있고 목록 응답에는 싣지 않는다."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

CATEGORIES: dict[str, str] = {
    "instruction": "지침 파일",
    "setting": "설정",
    "hook": "훅",
    "skill": "스킬",
    "agent": "에이전트",
    "rule": "규칙",
    "memory": "메모리",
    "plugin": "플러그인",
    "mcp": "MCP",
    "prompt": "따까리 프롬프트",
    "guard": "안전 가드",
    "policy": "라우팅·모델·Qwen",
    "capability": "실행 환경",
}
# 엔진 값: 지침이 읽히는 도구. shared 는 여러 도구가 함께 읽는 ~/.agents 쪽 파일이다.
ENGINES = ("claude", "codex", "antigravity", "muse", "qwen-local", "shared", "ttakkari")
SCOPES = ("global", "project", "account", "ttakkari")
SESSIONS = ("desk", "lead", "worker", "orchestrator")
LONG_LINES = 400
LONG_CHARS = 20_000
PREVIEW = 160


@dataclass
class Item:
    category: str
    name: str
    key: str  # id 를 만드는 출처 키(경로 또는 코드 위치). 같은 출처는 항상 같은 id 가 된다.
    scope: str
    engine: str
    description: str = ""
    group: str = ""
    sessions: tuple[str, ...] = ()
    source: str = "file"  # file | code | db | config | generated
    path: str | None = None
    mtime: float | None = None
    present: bool = True
    active: bool | None = None
    active_note: str = ""
    kind: str = "markdown"  # markdown | json | text
    body: str = ""
    truncated: bool = False
    masked: int = 0
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    # 깨진 참조 검사에 쓰는 후보 목록은 수집 단계에서만 쓰고 응답에는 넣지 않는다.
    refs: list[str] = field(default_factory=list)
    alias_of: str | None = None
    # 계층 분류(taxonomy.assign 이 수집 뒤에 채운다). hgroup 은 최상위 7개 그룹, concern 은 관심사, layer 는 적용 레이어.
    hgroup: str = ""
    concern: str = ""
    layer: str = ""
    project: str = ""  # 프로젝트별 그룹의 프로젝트 키(예: projects/aip). 그 밖에는 빈 값.
    pool: str = ""  # 프로젝트별 그룹의 범위: personal | company
    hint_group: str = ""  # 수집기가 파생 규칙 대신 직접 지정하는 그룹(Muse·Qwen 처럼 category 만으로 못 가르는 항목)
    hint_layer: str = ""
    hint_concern: str = ""
    real_path: str | None = None
    abs_path: str | None = None  # 깨진 참조 검사용 실제 경로. 응답에는 넣지 않는다.
    _search: str = ""

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.category}|{self.key}".encode()).hexdigest()[:14]

    @property
    def chars(self) -> int:
        return len(self.body)

    @property
    def lines(self) -> int:
        return self.body.count("\n") + 1 if self.body else 0

    def finalize(self) -> None:
        self._search = "\n".join((self.name, self.description, self.group, self.path or "", self.body)).lower()

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "category": self.category, "name": self.name, "group": self.group,
            "description": self.description[:PREVIEW * 2], "scope": self.scope, "engine": self.engine,
            "sessions": list(self.sessions), "source": self.source, "path": self.path, "mtime": self.mtime,
            "present": self.present, "active": self.active, "active_note": self.active_note, "kind": self.kind,
            "chars": self.chars, "lines": self.lines, "masked": self.masked, "warnings": list(self.warnings),
            "alias_of": self.alias_of,
            "hgroup": self.hgroup, "concern": self.concern, "layer": self.layer, "project": self.project,
            "pool": self.pool,
        }


FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_KEY_LINE = re.compile(r"^([A-Za-z_][\w\-]*):[ \t]*(.*)$")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """YAML 부분집합(키: 값, 따옴표, > | 블록, 들여쓴 이어쓰기)만 읽는다. 값에 콜론이 있어도 깨지지 않게 직접 읽는다."""
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    key: str | None = None
    block: str | None = None
    parts: list[str] = []

    def flush() -> None:
        if key is not None:
            joiner = "\n" if block == "|" else " "
            meta[key] = joiner.join(p for p in parts if p).strip()

    for line in m.group(1).splitlines():
        km = _KEY_LINE.match(line) if not line.startswith((" ", "\t")) else None
        if km:
            flush()
            key, value = km.group(1), km.group(2).strip()
            block = value[:1] if value[:1] in (">", "|") else None
            parts = [] if block else [value.strip("\"'")]
        elif key is not None:
            parts.append(line.strip())
    flush()
    return meta, text[m.end():]
