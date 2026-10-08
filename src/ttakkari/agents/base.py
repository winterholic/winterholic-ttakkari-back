"""에이전트 엔진 어댑터 공통 계약.

엔진마다 출력 형식이 다르므로 여기서 정규화 이벤트로 바꾼다. 정규화 이벤트 type 은
프론트와의 계약이다(docs/api-contract.md 의 "Run 이벤트" 절).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

TOOL_OUTPUT_LIMIT = 8000
GUARD_MARKER = "[ttakkari-guard]"


@dataclass
class LaunchSpec:
    cwd: Path
    prompt: str
    engine_session_id: str | None
    model: str | None
    effort: str | None
    outbox: Path
    write_roots: list[Path]
    system_note: str
    codex_sandbox: str = "workspace-write"


@dataclass
class Normalized:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseState:
    engine_session_id: str | None = None
    result_text: str | None = None
    is_error: bool = False
    error: str | None = None
    cost_usd: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    touched_files: set[str] = field(default_factory=set)
    last_message: str | None = None


class AgentAdapter(Protocol):
    engine: str

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]: ...

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]: ...


def truncate(text: Any, limit: int = TOOL_OUTPUT_LIMIT) -> str:
    if not isinstance(text, str):
        if isinstance(text, list):
            text = "\n".join(
                (x.get("text", "") if isinstance(x, dict) else str(x)) for x in text
            )
        else:
            text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… ({len(text) - limit}자 생략)"


def abs_path(raw: str, cwd: Path) -> str:
    p = Path(raw)
    return str(p if p.is_absolute() else cwd / p)
