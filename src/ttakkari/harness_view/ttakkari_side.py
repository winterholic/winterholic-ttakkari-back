"""따까리가 세션에 주입하는 지침·가드·정책·도구를 코드와 설정에서 그대로 꺼낸다.

문구는 실제 주입에 쓰는 상수·함수를 import 해서 가져오므로 코드와 어긋나지 않는다. 설정 값은 허용 목록의
필드만 보이고(비밀 필드는 목록에 없다), 한 번 더 마스킹한다.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
from typing import Any

from ttakkari.harness_view.model import Item
from ttakkari.harness_view.sources import Ctx, json_item, text_item
from ttakkari.runs import instructions as instructions_mod

ROLE_LABEL = {"common": "공통 지침(접수 제외 모든 실행)", "desk": "접수 지침", "lead": "기획 지침", "worker": "작업자 지침"}
ROLE_SESSIONS = {"common": ("lead", "worker", "orchestrator"), "desk": ("desk",), "lead": ("lead", "orchestrator"),
                 "worker": ("worker",)}
SRC = "back/src/ttakkari/"

# 설정 화면에 보여도 되는 필드. 비밀·경로 목록·DB 주소는 일부러 넣지 않는다.
POLICY_FIELDS = {
    "모델": ["orchestrator_fallback_models", "worker_default_models", "model_counterparts", "peer_models",
            "engine_models", "desk_models", "desk_engine_order", "context_review_percent", "context_replace_percent"],
    "접수": ["desk_qwen_enabled", "desk_qwen_model", "desk_qwen_timeout_seconds", "desk_qwen_max_prompt_chars",
            "desk_timeout_seconds", "desk_warm_pool"],
    "Qwen": ["qwen_local_enabled", "qwen_local_turns", "qwen_parallel_routing", "qwen_queue_retry_seconds",
            "qwen_flash_poll_seconds", "qwen_flash_monitor_model", "qwen_flash_monitor_port", "qwen35_shadow_enabled",
            "qwen35_shadow_sample_rate", "desk_qwen_manage", "desk_qwen_ctx", "desk_qwen_threads"],
    "실행·보안": ["claude_setting_sources", "engine_seatbelt", "engine_seatbelt_deny_keychain",
              "max_concurrent_desk", "max_concurrent_leads", "max_concurrent_workers", "run_timeout_seconds",
              "instructions_max_chars", "delivery_pipeline_enabled", "profile_cooldown_seconds", "env"],
}


def _setting(settings: Any, name: str) -> Any:
    value = getattr(settings, name, None)
    return value if not isinstance(value, Path) else str(value)


def collect_prompts(ctx: Ctx) -> list[Item]:
    from ttakkari.orchestrator import runtime
    from ttakkari.runs import capabilities, desk_qwen, manager

    items: list[Item] = []
    harness = ctx.harness_dir
    for role in ("common", "desk", "lead", "worker"):
        path = harness / instructions_mod.FILES[role]
        exists = path.is_file()
        text = instructions_mod.read(role)
        source = "file" if exists else "code"
        item = text_item("prompt", ROLE_LABEL[role], f"harness|{role}", text, scope="ttakkari", engine="ttakkari",
                         group="비서 지침 파일", source=source, path=ctx.disp(path) if exists else f"{SRC}runs/instructions.py DESK_DEFAULT",
                         sessions=ROLE_SESSIONS[role], kind="markdown",
                         description={"common": "비서 관리 > 지침의 공통 지침. 접수는 받지 않는다.",
                                      "desk": "접수 담당 시스템 프롬프트(없으면 코드 기본값).",
                                      "lead": "기획 세션 전용 추가 지침.",
                                      "worker": "작업 세션 전용 추가 지침."}[role],
                         active=bool(text), active_note=("저장된 파일" if exists else
                                                         "기본값 사용 중" if text else "파일 없음, 기본값 없음"))
        if exists:
            try:
                item.mtime = path.stat().st_mtime
            except OSError:
                pass
            if len(path.read_text(encoding="utf-8", errors="replace").strip()) > ctx.settings.instructions_max_chars:
                item.warnings.append(f"{ctx.settings.instructions_max_chars:,}자를 넘는 뒷부분은 주입되지 않는다")
        item.present = exists or bool(text)
        items.append(item)

    code_prompts: list[tuple[str, str, str, str, tuple[str, ...], str]] = [
        ("SYSTEM_NOTE", manager.SYSTEM_NOTE + "\n\n" + instructions_mod.access_note(Path("<outbox>")), "runs/manager.py",
         "기본 안내(출력 폴더·무인 실행·가드·보호 영역 조회 경로). {outbox} 는 Run 마다 채워진다.",
         ("lead", "worker", "orchestrator"), "모든 비접수 Run 의 첫 레이어"),
        ("AGENT_NOTE", runtime.AGENT_NOTE, "orchestrator/runtime.py", "ttakkari MCP 도구 사용·배포 방침 안내.", ("lead", "worker", "orchestrator"), "모든 비접수 Run"),
        ("ORCHESTRATOR_NOTE", runtime.ORCHESTRATOR_NOTE, "orchestrator/runtime.py", "옛 메인 오케스트레이터(부모 없음) 역할 안내.", ("orchestrator",), "부모 없는 orchestrator 세션"),
        ("LEAD_NOTE", runtime.LEAD_NOTE, "orchestrator/runtime.py", "기획 세션 역할 안내(접수가 넘긴 요청을 맡는다).", ("lead",), "부모가 있는 orchestrator 세션"),
        ("PLANNING_CONTRACT", instructions_mod.PLANNING_CONTRACT, "runs/instructions.py", "모든 기획 엔진의 전략·요구·완료·재개 계약.", ("lead", "orchestrator"), "역할 지침 뒤에 항상 주입"),
        ("DESK_DEFAULT", instructions_mod.DESK_DEFAULT, "runs/instructions.py", "접수 기본 지침. desk.md 가 있으면 대체된다.", ("desk",), "엔진 폴백 경로의 접수"),
        ("DESK_ROUTING_CONTRACT", instructions_mod.DESK_ROUTING_CONTRACT, "runs/instructions.py", "접수의 프로젝트·작업 연속성 계약. 저장된 접수 지침 뒤에도 항상 붙는다.", ("desk",), "엔진 폴백과 평가 경로의 접수"),
        ("MODEL_ROUTING_NOTE", runtime.model_routing_note(), "orchestrator/runtime.py model_routing_note()", "Antigravity 라우팅 정책 + 현재 단계표(코드에서 생성).", ("lead", "orchestrator"), "orchestrator 세션만"),
        ("capabilities.prompt_note()", capabilities.prompt_note(), "runs/capabilities.py", "엔진별 실행 환경 측정 요약(측정 파일에서 생성). detail 문자열은 넣지 않는다.", ("lead", "worker", "orchestrator"), "모든 비접수 Run"),
    ]
    for name, text, where, desc, sessions, cond in code_prompts:
        items.append(text_item("prompt", name, f"code|{name}", text, scope="ttakkari", engine="ttakkari", group="코드 상수",
                               path=SRC + where, description=desc, sessions=sessions, active=True, active_note=cond))

    desk_blocks = [("KIND_SYSTEM", desk_qwen.KIND_SYSTEM), ("SOCIAL_REPLY_SYSTEM", desk_qwen.SOCIAL_REPLY_SYSTEM),
                   ("FACT_REPLY_SYSTEM", desk_qwen.FACT_REPLY_SYSTEM), ("VERIFY_SYSTEM", desk_qwen.VERIFY_SYSTEM),
                   ("GUARD_SYSTEM", desk_qwen.GUARD_SYSTEM), ("LEAD_SYSTEM", desk_qwen.LEAD_SYSTEM)]
    qwen_on = bool(ctx.settings.desk_qwen_enabled)
    for name, text in desk_blocks:
        items.append(text_item("prompt", f"접수 Qwen · {name}", f"desk_qwen|{name}", text, scope="ttakkari",
                               engine="qwen-local", group="접수 로컬 라우터", path=SRC + "runs/desk_qwen.py",
                               description=f"접수 1순위 로컬 Qwen 라우터 프롬프트(버전 {desk_qwen.PROMPT_VERSION})",
                               sessions=("desk",), active=qwen_on,
                               active_note="desk_qwen_enabled" if qwen_on else "desk_qwen_enabled 꺼짐"))
    fewshot = {"KIND_FEWSHOT": desk_qwen.FEWSHOT, "LEAD_FEWSHOT": desk_qwen.LEAD_FEWSHOT}
    items.append(json_item("prompt", "접수 Qwen · few-shot", "desk_qwen|fewshot", {k: list(v) for k, v in fewshot.items()},
                           scope="ttakkari", engine="qwen-local", group="접수 로컬 라우터", path=SRC + "runs/desk_qwen.py",
                           source="code", sessions=("desk",), active=qwen_on,
                           description=f"예시 {len(desk_qwen.FEWSHOT)}+{len(desk_qwen.LEAD_FEWSHOT)}건"))

    from ttakkari.agents.qwen_local import QwenLocalAdapter
    short = QwenLocalAdapter.build_short_system_note(outbox="<outbox>", write_roots=["<write roots>"], kind="worker")
    contract = text_item("prompt", "Qwen 작업자 · 짧은 계약", "code|qwen_short_note", short, scope="ttakkari",
                         engine="qwen-local", group="엔진별 주입", path=SRC + "agents/qwen_local.py",
                         description="qwen-local 은 위 클라우드용 레이어를 모두 빼고 이 한 줄만 받는다.",
                         sessions=("worker",), active=True, active_note="engine=qwen-local 일 때")
    contract.hint_group, contract.hint_layer, contract.hint_concern = "qwen", "session", "instruction"
    items.append(contract)
    items.append(text_item("prompt", "엔진별 주입 방식", "code|injection", INJECTION_TABLE.format(
        setting_sources=ctx.settings.claude_setting_sources or "(비어 있음: 사용자 전역 설정 포함)"),
        scope="ttakkari", engine="ttakkari", group="엔진별 주입", path=SRC + "agents/*.py, runs/desk.py",
        description="Claude·Codex·Antigravity·Muse·Qwen 이 지침을 실제로 받는 방법", kind="markdown", active=True))
    return items


INJECTION_TABLE = """\
## 엔진별 지침 주입 방식 (코드 기준)

| 엔진 | 지침이 들어가는 곳 | 코드 |
|---|---|---|
| claude | `--append-system-prompt <레이어 합본>`, `--settings`(PreToolUse guard 훅), `--setting-sources` = `{setting_sources}` | agents/claude.py |
| codex | `-c developer_instructions=<레이어 합본 + macOS 브라우저 안내>`, `sandbox_mode`·`approval_policy="never"` 덮어쓰기 | agents/codex.py |
| antigravity | 프롬프트 앞에 `<ttakkari_system_instructions>…</ttakkari_system_instructions>` 로 감싸 삽입, `--sandbox` | agents/antigravity.py |
| muse | `spec.json` 의 `system_note`, `muse_hook` 가드 | agents/muse.py |
| qwen-local | 레이어를 모두 빼고 짧은 계약만, MCP 없음, OS 샌드박스 | agents/qwen_local.py |
| 접수(desk) | 1순위 로컬 Qwen 라우터 프롬프트, 실패하면 codex(`developer_instructions`, 훅·도구 끔)·claude(`--system-prompt`, `--setting-sources local`, 도구 없음) | runs/desk.py, runs/desk_qwen.py |

레이어 합본은 `runs/manager.py` 의 `_system_note_parts` 가 만든다.
"""


def collect_guard(ctx: Ctx) -> list[Item]:
    from ttakkari.broker import policy as broker_policy
    from ttakkari.security import guard

    items: list[Item] = []
    sess = ("lead", "worker", "orchestrator")
    for i, (pattern, why) in enumerate(guard.CATASTROPHIC):
        items.append(json_item("guard", f"차단: {why}", f"guard|catastrophic|{i}", {"pattern": pattern, "reason": why},
                               scope="ttakkari", engine="claude", group="고위험 명령 정규식", source="code",
                               path=SRC + "security/guard.py CATASTROPHIC", sessions=sess, active=True,
                               description=pattern[:200], active_note="Claude PreToolUse 훅·Muse 훅이 적용"))
    items.append(json_item("guard", "차단 명령 이름", "guard|verbs", sorted(guard.BLOCKED_VERBS), scope="ttakkari",
                           engine="claude", group="명령 이름", source="code", path=SRC + "security/guard.py BLOCKED_VERBS",
                           sessions=sess, active=True, description=", ".join(sorted(guard.BLOCKED_VERBS))))
    items.append(json_item("guard", "DB 클라이언트 DROP·TRUNCATE 검사", "guard|dbclients", sorted(guard.DB_CLIENT_VERBS),
                           scope="ttakkari", engine="claude", group="명령 이름", source="code",
                           path=SRC + "security/guard.py DB_CLIENT_VERBS", sessions=sess, active=True,
                           description="이 클라이언트에서 DROP DATABASE·WHERE 없는 DELETE 등을 막는다"))
    items.append(json_item("guard", "민감 파일 이름", "guard|sensitive_names", list(broker_policy.SENSITIVE_NAME_GLOBS),
                           scope="ttakkari", engine="claude", group="경로 정책", source="code",
                           path=SRC + "broker/policy.py SENSITIVE_NAME_GLOBS", sessions=sess, active=True,
                           description="읽기·반출을 막는 파일 이름 패턴"))
    items.append(json_item("guard", "민감 폴더", "guard|sensitive_dirs", list(broker_policy.SENSITIVE_DIR_NAMES),
                           scope="ttakkari", engine="claude", group="경로 정책", source="code",
                           path=SRC + "broker/policy.py SENSITIVE_DIR_NAMES", sessions=sess, active=True,
                           description=", ".join(broker_policy.SENSITIVE_DIR_NAMES)))
    settings = ctx.settings
    items.append(json_item("guard", "경로 경계(쓰기 루트·보호 영역)", "guard|paths", {
        "protected": "~/.ttakkari 전체(TTAKKARI_GUARD_PROTECTED = 따까리 홈 + 추가 보호)",
        "write_roots": "워크스페이스 루트, Run outbox, uploads, inbox, 워크스페이스 extra_writable_roots, 임시 폴더",
        "creative_intent": "creative_intent 작업은 outbox 만 쓸 수 있다",
        "allowed_roots": [str(p) for p in settings.allowed_roots],
        "default_writable_roots": [str(p) for p in settings.default_writable_roots]},
        scope="ttakkari", engine="ttakkari", group="경로 정책", source="code",
        path=SRC + "runs/manager.py write_roots_for_session, _child_env", sessions=sess, active=True,
        description="Run 마다 가드 훅에 넘기는 환경변수의 구성"))
    items.append(text_item("guard", "엔진별 강제 수단", "guard|enforcement", ENFORCEMENT, scope="ttakkari",
                           engine="ttakkari", group="엔진별", path=SRC + "security/*.py, agents/*.py",
                           description="어느 엔진에서 무엇이 실제로 막는가(셸 분석은 휴리스틱이다)",
                           kind="markdown", sessions=sess, active=True,
                           active_note=f"engine_seatbelt={list(settings.engine_seatbelt) or '없음'}"))
    items.append(json_item("guard", "로그인 제한(브루트포스)", "guard|login", {
        "module": "security/login_throttle.py", "note": "소유자 로그인 시도 제한. 임계값은 설정(login_*)"},
        scope="ttakkari", engine="ttakkari", group="인증", source="code", path=SRC + "security/login_throttle.py",
        active=True, description="API 소유자 인증과 에이전트 토큰 분리는 라우트 인벤토리 시험이 지킨다"))
    return items


ENFORCEMENT = """\
## 실행 가드가 실제로 막는 방법

- **Claude**: `--settings` 로 주입한 PreToolUse 훅(`security/claude_hook.py`)이 도구 호출마다 `guard.evaluate` 를 돈다. 오류가 나면 차단(fail-closed). `--dangerously-skip-permissions` 와 함께 쓴다.
- **Codex**: OS 샌드박스 `workspace-write`(+ `sandbox_workspace_write.writable_roots`). 가드 훅은 없다.
- **Antigravity**: `--sandbox` 와 `--mode accept-edits`.
- **Muse**: `security/muse_hook.py` 가 policy.json 의 쓰기 루트·보호 경로를 적용.
- **Qwen local**: 별도 하네스의 OS 샌드박스, MCP 없음.
- **`engine_seatbelt`** 가 켜진 엔진은 `sandbox-exec` 로 DB 포트·소켓과 비밀 경로를 커널에서 막는다(`security/seatbelt.py`).
- 셸 문자열 분석은 우회 가능한 휴리스틱이라 샌드박스가 아니다. 진짜 경계는 OS 샌드박스·git 체크포인트·File Broker 다(ADR 0003).
"""


def collect_policy(ctx: Ctx) -> list[Item]:
    from ttakkari.runs import worker_routing

    s = ctx.settings
    items: list[Item] = []
    routing = worker_routing.policy()
    items.append(json_item("policy", "작업자 라우팅 단계표", "policy|routes", routing, scope="ttakkari", engine="ttakkari",
                           group="라우팅", source="code", path=SRC + "runs/worker_routing.py ROUTES",
                           sessions=("lead", "orchestrator"), active=True,
                           description="task_type 별 엔진·모델 우선순서(같은 단계는 부하로 분산). 켜진 엔진만 표시"))
    for group, fields in POLICY_FIELDS.items():
        data = {f: _setting(s, f) for f in fields if hasattr(s, f)}
        setting = json_item("policy", f"설정 · {group}", f"policy|settings|{group}", data, scope="ttakkari",
                            engine="qwen-local" if group == "Qwen" else "ttakkari", group="설정(config)", source="config",
                            path="~/.ttakkari/config.env 의 TTAKKARI_* (표시는 허용 목록 필드만)", active=True,
                            description=", ".join(data)[:200])
        if group == "Qwen":
            setting.hint_group, setting.hint_layer, setting.hint_concern = "qwen", "session", "setting"
        items.append(setting)
    q_row = ctx.app_settings.get("qwen_worker_activation")
    activation = json_item("policy", "Qwen 작업자 활성화(DB)", "policy|qwen_activation", {
        "app_settings.qwen_worker_activation": q_row, "config_locked": "qwen_local_enabled" in s.model_fields_set,
        "qwen_local_enabled(config)": s.qwen_local_enabled},
        scope="ttakkari", engine="qwen-local", group="Qwen", source="db", path="DB app_settings[qwen_worker_activation]",
        active=bool(s.qwen_local_enabled or (q_row or {}).get("enabled")),
        description="소유자가 비서 관리에서 켠다. 설정에 명시되면 설정이 이긴다")
    activation.hint_group, activation.hint_layer, activation.hint_concern = "qwen", "session", "setting"
    items.append(activation)
    for acc in ctx.accounts:
        acc["view"] = {**acc["view"], **_account_config(ctx, acc)}
        item = json_item("policy", f"계정 · {acc['name']}", f"policy|account|{acc['id']}", acc["view"], scope="account",
                         engine=acc["engine"], group="엔진 계정(DB)", source="db", path="DB engine_accounts",
                         active=bool(acc["view"].get("enabled")),
                         description=f"{acc['engine']} · {acc['view'].get('pool')} · 우선순위 {acc['view'].get('priority')}")
        item.warnings.extend(acc.get("warnings", []))
        items.append(item)
    if not ctx.accounts:
        items.append(text_item("policy", "엔진 계정", "policy|accounts_empty", "등록된 엔진 계정 없음", scope="account",
                               engine="ttakkari", group="엔진 계정(DB)", source="db", path="DB engine_accounts",
                               active=False))
    return items


def _account_config(ctx: Ctx, acc: dict[str, Any]) -> dict[str, Any]:
    """계정의 config_dir 에 사용자 전역 하네스가 어떻게 이어지는지(심볼릭 링크·자체 파일·없음). 내용은 읽지 않는다."""
    cfg = acc["view"].get("config_dir")
    engine = acc["engine"]
    if engine not in ("claude", "codex"):
        return {}
    global_dir = ctx.claude if engine == "claude" else ctx.codex
    if not cfg:
        return {"harness_source": f"기본 위치({ctx.disp(global_dir)})"}
    base = Path(cfg).expanduser()
    managed = ctx.ttakkari_home / "accounts"
    if not any(os.path.realpath(base).startswith(str(os.path.realpath(r)) + os.sep) or os.path.realpath(base) == str(os.path.realpath(r))
               for r in (managed, global_dir)):
        return {"harness_source": "허용 루트 밖이라 확인하지 않음"}
    names = ("CLAUDE.md", "settings.json", "skills") if engine == "claude" else ("AGENTS.md", "config.toml", "skills")
    status = {}
    for name in names:
        target = base / name
        if not (target.exists() or target.is_symlink()):
            status[name] = "없음"
        elif os.path.realpath(target) == os.path.realpath(global_dir / name):
            status[name] = "전역 파일과 같음(링크)"
        else:
            status[name] = "계정 전용"
    return {"harness_source": ctx.disp(base), "files": status}


def collect_capabilities(ctx: Ctx) -> list[Item]:
    from ttakkari.runs import capabilities

    items = []
    for engine in capabilities.ENGINES:
        view = capabilities.view_engine(engine, now=ctx.now)
        names = [n for n, v in view["items"].items() if v["status"] == "blocked"]
        measured = view["measured_at"]
        item = json_item("capability", f"{engine} 실행 환경", f"capability|{engine}", view, scope="ttakkari",
                         engine=engine if engine != "host" else "ttakkari", group="probe 측정", source="generated",
                         path="~/.ttakkari/data/execution-capabilities.json (백엔드가 읽음)", active=measured is not None,
                         active_note="측정 기록 있음" if measured else "아직 측정 안 됨",
                         description=("blocked: " + ", ".join(names)) if names else ("blocked 없음" if measured else "측정 안 됨"))
        if view.get("stale_items"):
            item.warnings.append("오래된 측정 항목: " + ", ".join(view["stale_items"]))
        items.append(item)
    return items


def collect_ttakkari_tools(tools: list[Any], orchestrator_only: frozenset[str]) -> list[Item]:
    from ttakkari.orchestrator import tool_policy

    groups = {name: label for label, names in (
        ("읽기 전용", tool_policy.READ_ONLY_TOOLS), ("PMS", tool_policy.PMS_TOOLS), ("배포", tool_policy.DEPLOY_TOOLS),
        ("체크리스트", tool_policy.CHECKLIST_TOOLS), ("비서 쓰기", tool_policy.ASSISTANT_WRITE_TOOLS),
        ("운영 로그", tool_policy.OPS_LOG_TOOLS), ("NotebookLM", tool_policy.NOTEBOOKLM_TOOLS),
        ("호스트 환경", tool_policy.HOST_ENV_TOOLS), ("호스트 운영", tool_policy.HOST_OPERATION_TOOLS),
        ("작업자 관리", tool_policy.WORKER_MANAGEMENT_TOOLS),
        ("벤치마크", tool_policy.BENCHMARK_TOOLS), ("리버스", tool_policy.REVERSE_TOOLS),
        ("연구", tool_policy.RESEARCH_TOOLS), ("예약", tool_policy.SCHEDULE_TOOLS),
        ("크리에이티브", tool_policy.CREATIVE_TOOLS)) for name in names}
    items = []
    for tool in tools:
        name = tool.name
        schema = getattr(tool, "inputSchema", None) or {}
        params = list((schema.get("properties") or {}).keys())
        only = name in orchestrator_only
        read_only = name in tool_policy.READ_ONLY_TOOLS
        approval = groups.get(name)
        item = json_item("mcp", name, f"tool|{name}", {"description": tool.description, "inputSchema": schema,
                                                       "read_only": read_only, "approval_group": approval},
                         scope="ttakkari", engine="ttakkari", group="따까리 MCP 도구", source="code",
                         path=SRC + "orchestrator/mcp_server.py", active=True,
                         sessions=("lead", "orchestrator") if only else ("lead", "worker", "orchestrator"),
                         description=" ".join((tool.description or "").split())[:240],
                         active_note="orchestrator 전용" if only else "모든 세션")
        item.meta.update({"params": params, "read_only": read_only, "approval_group": approval, "orchestrator_only": only})
        if approval is None:
            item.warnings.append("tool_policy 자동 승인 목록에 없다")
        items.append(item)
    server = json_item("mcp", "ttakkari (MCP 서버)", "tool|server", {
        "command": "python -m ttakkari.orchestrator.mcp_server",
        "env": {"TTK_ORCH_TOKEN": "세션·Run 한정 토큰", "TTK_API_URL": "백엔드 주소", "TTK_ROLE": "agent | orchestrator"},
        "roles": {"agent": "공통 도구", "orchestrator": "공통 + 작업 세션 도구"}, "tool_count": len(tools)},
        scope="ttakkari", engine="ttakkari", group="따까리 MCP 서버", source="code",
        path=SRC + "orchestrator/runtime.py mcp_server_spec", active=True,
        description=f"엔진마다 stdio 로 띄운다. 도구 {len(tools)}개, orchestrator 전용 {len(orchestrator_only)}개",
        sessions=("lead", "worker", "orchestrator"))
    server.meta["secret_keys"] = ["TTK_ORCH_TOKEN"]
    return [server, *items]


def orchestrator_only_names() -> frozenset[str]:
    from ttakkari.orchestrator import mcp_server

    tree = ast.parse(inspect.getsource(mcp_server))
    return frozenset(n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                     for d in n.decorator_list if isinstance(d, ast.Name) and d.id == "orchestrator_tool")
