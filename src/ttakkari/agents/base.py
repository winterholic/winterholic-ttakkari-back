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
    # 붙일 stdio MCP 서버 {name: {command, args, env}}. 오케스트레이터 Run 에만 쓴다(ADR 0006).
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_id: str | None = None
    # 이 Run 만 claude 설정 출처를 좁힌다(예: "project,local"). 비우면 전역 설정 claude_setting_sources 를 따른다.
    setting_sources: str | None = None


@dataclass
class Normalized:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseState:
    engine_session_id: str | None = None
    resume_mismatch: bool = False
    result_text: str | None = None
    is_error: bool = False
    error: str | None = None
    cost_usd: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    touched_files: set[str] = field(default_factory=set)
    last_message: str | None = None
    # claude rate_limit_event 의 unifiedWindows(계정 사용량 화면의 대체 출처)
    rate_limits: dict[str, Any] | None = None
    # Provider-observed account quota, separate from this Run's token totals.
    account_usage: dict[str, Any] | None = None
    # 엔진이 구조화된 이벤트로 알려 준 한도 도달(Claude rate_limit_event status=rejected, assistant error=rate_limit).
    # 문구 휴리스틱이 놓쳐도 한도로 본다.
    limit_hit: bool = False
    limit_text: str | None = None
    limit_resets_at: float | None = None
    # 마지막 모델 요청이 차지한 컨텍스트(토큰)와 모델 한도. 메인이 세션 교체를 판단하는 근거다.
    context_tokens: int | None = None
    context_window: int | None = None
    context_model: str | None = None
    pending_tool_calls: set[str] = field(default_factory=set)
    adapter_data: dict[str, Any] = field(default_factory=dict)
    protocol_final_seen: bool = False


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
