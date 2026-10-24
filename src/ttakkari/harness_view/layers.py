"""세션 종류·엔진을 고르면 실제로 합쳐지는 지침 레이어를 순서대로 돌려준다.

시스템 프롬프트 레이어는 runs/manager.py 의 _system_note_parts 가 만든 목록을 그대로 쓴다(실제 주입과 같은 코드).
엔진 CLI 가 스스로 읽는 파일(CLAUDE.md 등)은 따까리 코드 밖의 동작이라 evidence="external"로 구분한다.
"""
from __future__ import annotations

import hashlib
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.harness_view.model import Item
from ttakkari.harness_view.safe import mask_text
from ttakkari.models import ChatSession, SessionKind

SESSION_KINDS = ("desk", "lead", "worker", "orchestrator")
LAYER_ENGINES = ("claude", "codex", "antigravity", "muse", "qwen-local")
SESSION_LABEL = {"desk": "접수", "lead": "기획", "worker": "작업자", "orchestrator": "메인 오케스트레이터(옛)"}
PART_ITEMS = {
    "system_note": ("prompt", "code|SYSTEM_NOTE", "기본 안내", "runs/manager.py SYSTEM_NOTE"),
    "agent_note": ("prompt", "code|AGENT_NOTE", "ttakkari MCP 도구 안내", "orchestrator/runtime.py AGENT_NOTE"),
    "capabilities": ("prompt", "code|capabilities.prompt_note()", "실행 환경 capability 요약",
                     "runs/capabilities.py prompt_note()"),
    "orchestrator_note": ("prompt", "code|ORCHESTRATOR_NOTE", "메인 오케스트레이터 역할", "orchestrator/runtime.py"),
    "lead_note": ("prompt", "code|LEAD_NOTE", "기획 세션 역할", "orchestrator/runtime.py LEAD_NOTE"),
    "common_instructions": ("prompt", "harness|common", "사용자 공통 지침", "~/.ttakkari/harness/instructions.md"),
    "planning_contract": ("prompt", "code|PLANNING_CONTRACT", "기획 실행 계약", "runs/instructions.py PLANNING_CONTRACT"),
    "role_instructions": ("prompt", None, "역할 지침", "~/.ttakkari/harness/<role>.md"),
    "pinned_brief": ("prompt", "db|pinned_brief", "고정 기억(핀)", "DB 맥락 도서관 pinned_brief"),
    "routing_note": ("prompt", "code|MODEL_ROUTING_NOTE", "모델 라우팅 정책", "orchestrator/runtime.py model_routing_note()"),
}


def item_id(category: str, key: str) -> str:
    return hashlib.sha1(f"{category}|{key}".encode()).hexdigest()[:14]


@dataclass
class Layer:
    id: str
    title: str
    origin: str  # code | file | db | generated | engine | cli
    source: str
    applies: bool = True
    condition: str = ""
    evidence: str = "code"  # code: 이 저장소 코드로 확인 / external: 엔진 CLI 동작(코드 밖, 확인 필요)
    phase: str = "prompt"  # prompt(시스템 프롬프트 본문) | delivery(전달 방식) | load(엔진이 읽는 파일) | tool | guard
    text: str = ""
    chars: int = 0
    item_id: str | None = None
    order: int = 0
    refs: list[str] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        preview = self.text[:240] + ("…" if len(self.text) > 240 else "")
        return {"order": self.order, "id": self.id, "title": self.title, "origin": self.origin, "source": self.source,
                "applies": self.applies, "condition": self.condition, "evidence": self.evidence, "phase": self.phase,
                "chars": self.chars, "preview": preview, "item_id": self.item_id, "refs": self.refs}


def _layer(lid: str, title: str, origin: str, source: str, *, text: str = "", **kw: Any) -> Layer:
    masked, _ = mask_text(text)
    return Layer(lid, title, origin, source, text=masked, chars=len(text), **kw)


def _skill_count(items: list[Item], engine: str) -> int:
    return sum(1 for i in items if i.category == "skill" and i.engine == engine and i.present and i.active)


def _transient_session(kind: str) -> ChatSession:
    sess = ChatSession()
    sess.kind = SessionKind.orchestrator if kind in ("lead", "orchestrator") else SessionKind.worker
    sess.parent_id = uuid.uuid4() if kind == "lead" else None
    return sess


async def build(db: AsyncSession, items: list[Item], *, session: str, engine: str, pool: str, settings: Any,
                platform: str | None = None) -> dict[str, Any]:
    if session not in SESSION_KINDS:
        raise ValueError("알 수 없는 세션 종류")
    if engine not in LAYER_ENGINES:
        raise ValueError("알 수 없는 엔진")
    platform = platform or sys.platform
    by_key = {(i.category, i.name): i for i in items}
    layers: list[Layer] = []
    notes: list[str] = []
    if session == "desk":
        layers, notes = _desk(items, engine, settings)
    else:
        layers, notes = await _work(db, items, session=session, engine=engine, pool=pool, settings=settings,
                                    platform=platform, by_key=by_key)
    for n, layer in enumerate(layers, 1):
        layer.order = n
    prompt_chars = sum(layer.chars for layer in layers if layer.phase == "prompt" and layer.applies)
    return {
        "session": session, "session_label": SESSION_LABEL[session], "engine": engine, "pool": pool,
        "layers": [layer.view() for layer in layers], "notes": notes,
        "prompt_chars": prompt_chars,
        "applied": sum(1 for layer in layers if layer.applies), "total": len(layers),
        "external": sum(1 for layer in layers if layer.applies and layer.evidence == "external"),
    }


def _find(items: list[Item], category: str, key: str) -> Item | None:
    wanted = item_id(category, key)
    return next((i for i in items if i.id == wanted), None)


def _link(items: list[Item], category: str, name: str, engine: str | None = None) -> str | None:
    for i in items:
        if i.category == category and i.name == name and (engine is None or i.engine == engine) and i.present:
            return i.id
    return None


async def _work(db: AsyncSession, items: list[Item], *, session: str, engine: str, pool: str, settings: Any,
                platform: str, by_key: dict[tuple[str, str], Item]) -> tuple[list[Layer], list[str]]:
    from ttakkari.runs import manager

    layers: list[Layer] = []
    notes: list[str] = []
    sess = _transient_session(session)
    parts = await manager._system_note_parts(db, Path("<outbox>"), sess, scope="work" if pool == "work" else "all")
    cloud = engine != "qwen-local"
    role_key = {"lead": "harness|lead", "orchestrator": "harness|lead", "worker": "harness|worker"}[session]
    present = dict(parts)
    role_note = {"lead": "lead_note", "orchestrator": "orchestrator_note"}.get(session)
    canonical = ["system_note", "agent_note", "capabilities", *([role_note] if role_note else []),
                 "common_instructions", "role_instructions", "pinned_brief",
                 *(["planning_contract", "routing_note"] if session in ("lead", "orchestrator") else [])]
    for pid in canonical:
        category, key, title, source = PART_ITEMS[pid]
        key = key or role_key
        origin = "db" if pid == "pinned_brief" else "file" if pid.endswith("instructions") else (
            "generated" if pid in ("capabilities", "routing_note") else "code")
        found = _find(items, category, key)
        if pid not in present:
            # 파일·기억이 비어 있어 실제로는 합쳐지지 않는 레이어. 없다는 사실을 그대로 보여 준다.
            layers.append(Layer(pid, title, origin, source, applies=False, phase="prompt", evidence="code",
                                condition="비어 있거나 파일 없음: 합쳐지지 않는다", item_id=found.id if found else None))
            continue
        text = present[pid]
        layers.append(_layer(pid, title, origin, source, text=text, applies=cloud, phase="prompt",
                             condition="" if cloud else "qwen-local 은 이 레이어를 받지 않는다(짧은 계약으로 대체)",
                             item_id=found.id if found else None))
    if not cloud:
        short = _find(items, "prompt", "code|qwen_short_note")
        layers.append(_layer("qwen_short_note", "Qwen 짧은 계약", "code", "agents/qwen_local.py build_short_system_note",
                             text=short.body if short else "", phase="prompt", item_id=short.id if short else None))
        notes.append("qwen-local 은 MCP 도구도 없고, 위 클라우드용 레이어를 모두 뺀 한 줄 계약만 받는다.")
    notes.append("레이어 본문은 실제 주입 함수(_system_note_parts)가 만든 것이다. outbox 경로는 Run 마다 채워진다.")

    if engine == "claude":
        sources = settings.claude_setting_sources
        loads_user = not sources
        layers += [
            _layer("claude_flag", "--append-system-prompt 로 합본 전달", "engine", "agents/claude.py build_command",
                   phase="delivery", condition="Claude Code 기본 시스템 프롬프트 뒤에 붙는다"),
            _layer("claude_guard", "PreToolUse guard 훅(--settings)", "engine", "security/claude_hook.py",
                   phase="guard", item_id=_link(items, "guard", "차단 명령 이름"),
                   condition="도구 호출마다 guard.evaluate, 오류 시 차단"),
            _layer("claude_mcp", f"ttakkari MCP ({'orchestrator' if session != 'worker' else 'agent'} 역할)", "engine",
                   "orchestrator/runtime.py mcp_server_spec", phase="tool", item_id=_link(items, "mcp", "ttakkari (MCP 서버)")),
            _layer("claude_sources", f"--setting-sources = {sources or '(비어 있음)'}", "engine", "config claude_setting_sources",
                   phase="delivery",
                   condition="비어 있으면 사용자 전역 설정이 로드된다" if loads_user else f"{sources} 만 로드, 전역 설정 제외"),
            _layer("claude_user_md", "~/.claude/CLAUDE.md", "cli", "Claude Code 가 로드", applies=loads_user,
                   evidence="external", phase="load", item_id=_link(items, "instruction", "CLAUDE.md", "claude"),
                   condition="계정 config_dir 가 지정된 계정은 그 폴더의 CLAUDE.md 를 본다(CLAUDE_CONFIG_DIR)"),
            _layer("claude_skills", f"스킬 목록(활성 {_skill_count(items, 'claude')}개)", "cli", "Claude Code 가 로드",
                   applies=loads_user, evidence="external", phase="load", condition="description 만 프롬프트에 노출"),
            _layer("claude_hooks", "~/.claude/settings.json 훅", "cli", "Claude Code 가 로드", applies=loads_user,
                   evidence="external", phase="load", item_id=_link(items, "setting", "settings.json 전체", "claude"),
                   condition="따까리 guard 훅과 함께 실행된다"),
            _layer("claude_memory", "자동 메모리(MEMORY.md + 계정별 memory/)", "cli", "Claude Code 가 로드",
                   evidence="external", phase="load", condition="계정 config_dir 의 projects/<경로>/memory"),
            _layer("claude_project", "작업 디렉터리의 CLAUDE.md", "cli", "Claude Code 가 로드", evidence="external",
                   phase="load", condition="워크스페이스 루트 기준(project 범위 항목 참고)"),
        ]
    elif engine == "codex":
        mac = platform == "darwin"
        layers += [
            _layer("codex_flag", "-c developer_instructions 로 합본 전달", "engine", "agents/codex.py build_command",
                   phase="delivery", condition="합본 뒤에 안내 문구가 더 붙을 수 있다"),
            _layer("codex_browser", "macOS 브라우저 검증·preflight 안내", "code", "agents/codex.py build_command",
                   applies=mac, phase="prompt", condition="darwin 이고 sandbox 가 danger-full-access 가 아닐 때(고정 문구, 본문은 코드 참조)"),
            _layer("codex_sandbox", "sandbox_mode·approval_policy 덮어쓰기", "engine", "agents/codex.py",
                   phase="guard", item_id=_link(items, "guard", "엔진별 강제 수단"),
                   condition="전역 config.toml 값이 danger-full-access 여도 워크스페이스 설정으로 덮는다"),
            _layer("codex_mcp", "ttakkari MCP + 승인 설정", "engine", "agents/codex.py", phase="tool",
                   item_id=_link(items, "mcp", "ttakkari (MCP 서버)")),
            _layer("codex_agents_md", "~/.codex/AGENTS.md", "cli", "Codex 가 로드", evidence="external", phase="load",
                   item_id=_link(items, "instruction", "AGENTS.md", "codex"), condition="CODEX_HOME 기준"),
            _layer("codex_project", "프로젝트 AGENTS.md (+ fallback CLAUDE.md)", "cli", "Codex 가 로드", evidence="external",
                   phase="load", item_id=_link(items, "setting", "config.toml 전체", "codex"),
                   condition="config.toml project_doc_fallback_filenames 참고"),
        ]
    elif engine == "antigravity":
        layers += [
            _layer("agy_wrap", "<ttakkari_system_instructions> 로 프롬프트 앞에 삽입", "engine", "agents/antigravity.py",
                   phase="delivery", condition="Workspace root·쓰기 루트가 그 뒤에 따라붙는다"),
            _layer("agy_sandbox", "--sandbox --mode accept-edits", "engine", "agents/antigravity.py", phase="guard",
                   item_id=_link(items, "guard", "엔진별 강제 수단")),
            _layer("agy_mcp", "ttakkari MCP (세션별 프로젝트 설정)", "engine", "agents/antigravity.py _session_project",
                   phase="tool", item_id=_link(items, "mcp", "ttakkari (MCP 서버)")),
            _layer("agy_gemini", "~/.gemini/GEMINI.md · .agents/rules", "cli", "Antigravity 가 로드", evidence="external",
                   phase="load", item_id=_link(items, "instruction", "GEMINI.md", "antigravity"),
                   condition="읽는지 확인 필요. 파일이 없으면 영향 없음"),
        ]
    elif engine == "muse":
        layers += [
            _layer("muse_spec", "spec.json 의 system_note 로 전달", "engine", "agents/muse.py", phase="delivery"),
            _layer("muse_hook", "muse_hook 가드(policy.json)", "engine", "security/muse_hook.py", phase="guard",
                   item_id=_link(items, "guard", "엔진별 강제 수단")),
            _layer("muse_mcp", "ttakkari MCP", "engine", "agents/muse.py", phase="tool",
                   item_id=_link(items, "mcp", "ttakkari (MCP 서버)")),
        ]
        notes.append("Muse 는 사용자 전역 CLAUDE.md·스킬을 읽지 않는다. 프롬프트 레이어가 전부다.")
    else:
        layers.append(_layer("qwen_sandbox", "Qwen 하네스 OS 샌드박스", "engine", "agents/qwen_local.py", phase="guard",
                             item_id=_link(items, "guard", "엔진별 강제 수단"),
                             condition="MCP·네트워크 없이 outbox·워크스페이스 안만 쓴다"))
    if session == "orchestrator":
        notes.append("부모 없는 orchestrator 는 옛 메인 오케스트레이터 경로다. 새 요청은 접수 → 기획으로 흐른다.")
    return layers, notes


def _desk(items: list[Item], engine: str, settings: Any) -> tuple[list[Layer], list[str]]:
    order = list(settings.desk_engine_order)
    notes = ["접수는 전역 하네스(CLAUDE.md·스킬·훅)와 공통 지침을 받지 않는다(runs/instructions.py)."]
    layers: list[Layer] = []
    qwen_on = bool(settings.desk_qwen_enabled)
    if engine == "qwen-local":
        sys_items = [i for i in items if i.category == "prompt" and i.group == "접수 로컬 라우터"]
        for i in sys_items:
            layers.append(Layer(f"desk_{i.name}", i.name, "code", i.path or "", applies=qwen_on, text=i.body,
                                chars=len(i.body), item_id=i.id, phase="prompt",
                                condition="" if qwen_on else "desk_qwen_enabled 꺼짐"))
        notes.append("1순위 로컬 Qwen 라우터. 실패·시간 초과·형식 오류면 엔진 순서로 넘어간다: " + " → ".join(order))
        return layers, notes
    used = engine in order
    desk_md = _find(items, "prompt", "harness|desk")
    layers.append(Layer("desk_system", "접수 지침(desk.md 또는 기본값)", "file" if desk_md and desk_md.source == "file" else "code",
                        desk_md.path or "" if desk_md else "", applies=used, text=desk_md.body if desk_md else "",
                        chars=len(desk_md.body) if desk_md else 0, item_id=desk_md.id if desk_md else None, phase="prompt",
                        condition="" if used else f"desk_engine_order={order} 에 없는 엔진"))
    if engine == "claude":
        layers += [
            Layer("desk_claude_flags", "--system-prompt(기본 프롬프트 대체) · --setting-sources local", "engine",
                  "runs/desk.py _claude_flags", applies=used, phase="delivery",
                  condition="disableAllHooks, --tools \"\", --strict-mcp-config, --no-session-persistence"),
        ]
    elif engine == "codex":
        layers += [
            Layer("desk_codex_flags", "-c developer_instructions · read-only · 훅·도구 비활성", "engine",
                  "runs/desk.py _codex_cmd", applies=used, phase="delivery",
                  condition="--disable hooks/shell_tool/plugins 등, 사용자 config 의 MCP 서버는 enabled=false"),
            Layer("desk_codex_agents", "~/.codex/AGENTS.md", "cli", "Codex 가 로드", applies=used, evidence="external",
                  phase="load", item_id=_link(items, "instruction", "AGENTS.md", "codex"),
                  condition="접수 명령이 이 파일 로드를 끄는지는 코드에 없다. 확인 필요"),
        ]
    else:
        notes.append("이 엔진은 접수에 쓰이지 않는다.")
    layers.append(Layer("desk_prompt", "사용자 프롬프트: [작업판] + [최근 대화] + 메시지", "generated", "runs/desk.py _prompt",
                        applies=used, phase="prompt", condition="요청마다 DB 에서 생성(본문 없음)"))
    notes.append("엔진 순서: " + " → ".join(order) + (" (1순위 로컬 Qwen 은 별도 선택)" if qwen_on else ""))
    return layers, notes
