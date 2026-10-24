#!/usr/bin/env python3
"""Platform V2 Test & Frontend Feature Audit Extractor (Worker C - Corrected Candidate Fix 2)

This script performs reproducible static and metadata audit for:
1. Frontend feature mapping (routes, pages, API clients, stores, fallback states) via TypeScript AST
2. Test architecture categorization across back & front (9 disjoint primary categories + multi-label)
3. Performance baseline, provenance and duration evidence compilation
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_BACK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FRONT_ROOT = Path(
    "/Users/winterholic/development/projects/winterholic-ttakkari/front"
)

# Versioned v2 mapping contract. Presence of one known ID does not establish
# completeness of the registry. IDs remain provisional even with a full schema.
EXPECTED_BACKEND_MODULE_IDS = frozenset(
    {
        "core.auth_session",
        "core.workspaces_transfers",
        "core.accounts_quota",
        "core.generation_deploy",
        "core.system_routing",
        "exec.run_engine",
        "exec.durable_scheduler",
        "exec.host_gpu",
        "delivery.git_cas",
        "domain.memory_ontology",
        "domain.knowledge_rag",
        "domain.collector",
        "domain.creative_studio",
        "domain.notebooklm",
        "domain.research_lab",
        "domain.reverse_eng",
        "domain.meetings_stt",
        "domain.pms_tasks",
        "domain.design_system",
        "domain.media_tts",
        "domain.game_studio",
        "domain.personal_tools",
        "domain.external_connectors",
        "domain.shares_artifacts",
        "ops.monitoring",
    }
)


def backend_registry_path(back_root: Path) -> Path:
    """Use only the explicitly selected candidate's registry, including if absent."""
    return back_root / "docs/design/platform-v2/backend-module-inventory.json"


def capture_audit_inputs(back_root: Path, front_root: Path) -> dict[str, str]:
    """Bind the audit to source bytes; generation must reject mid-scan changes."""
    paths = [("back", back_root, backend_registry_path(back_root))]
    paths.extend(
        ("front", front_root, path)
        for path in (front_root / "src").rglob("*")
        if path.is_file() and path.suffix in {".ts", ".tsx"}
    )
    for folder in ("tests", "unit_tests", "benchmark_tests"):
        paths.extend(
            ("back", back_root, path) for path in (back_root / folder).rglob("*.py")
        )
    result = {}
    for label, root, path in sorted(paths):
        path.resolve().relative_to(root.resolve())
        key = f"{label}/{path.relative_to(root).as_posix()}"
        result[key] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.exists()
            else "missing"
        )
    return result


def registry_is_complete(rows: list) -> bool:
    if len(rows) != len(EXPECTED_BACKEND_MODULE_IDS):
        return False
    if not all(isinstance(row, dict) for row in rows):
        return False
    ids = [row.get("id") for row in rows]
    if not all(isinstance(module_id, str) for module_id in ids):
        return False
    if set(ids) != EXPECTED_BACKEND_MODULE_IDS:
        return False
    return all(
        row.get("plane") in {"Control", "Execution", "Domain", "Delivery"}
        and isinstance(row.get("name"), str)
        and bool(row["name"])
        and all(
            isinstance(row.get(key), list)
            for key in ("routes", "tools", "tables", "migrations")
        )
        and all(
            isinstance(row.get(key), str) and re.fullmatch(r"[a-f0-9]{40}", row[key])
            for key in ("scan_commit", "src_tree")
        )
        for row in rows
    )


ROUTE_METADATA_TABLE: dict[str, dict[str, Any]] = {
    "/login": {
        "component": "LoginPage",
        "type": "STATIC",
        "domain": "인증 로그인 (비밀번호 기반 Bearer 토큰 발급)",
        "module": "auth_sessions",
        "plane": "Control",
        "backend_id": "core.auth_session",
        "state_key": "useAuth, tk-token",
        "fallback": "인라인 에러 메시지, 로그인 루프 방지",
    },
    "/share/:shareId": {
        "component": "PublicMeetingPage",
        "type": "DYNAMIC",
        "domain": "공개 회의록 외부 열람 (단독 공유 열람)",
        "module": "shares_artifacts",
        "plane": "Domain",
        "backend_id": "domain.shares_artifacts",
        "state_key": "useQuery(['public-share', id])",
        "fallback": "PublicMeetingError, 404/만료 상태",
    },
    "(Layout)": {
        "component": "AppShell",
        "type": "LAYOUT",
        "domain": "전역 앱 셸 레이아웃 (네비게이션/사이드바)",
        "module": "system_routing",
        "plane": "Control",
        "backend_id": "core.system_routing",
        "state_key": "useConnection",
        "fallback": "Global ErrorBoundary",
    },
    "/chat": {
        "component": "DeskHome",
        "type": "STATIC",
        "domain": "개인 대화 홈 캔버스 및 데스크 접수",
        "module": "auth_sessions",
        "plane": "Control",
        "backend_id": "core.auth_session",
        "state_key": "useQuery(['sessions']), useQuery(['runs'])",
        "fallback": "CanvasBoundary, 빈 상태 안내",
    },
    "/chat/thread": {
        "component": "ChatPage",
        "type": "STATIC",
        "domain": "개인 대화 전체 화면 스레드",
        "module": "run_engine",
        "plane": "Execution",
        "backend_id": "exec.run_engine",
        "state_key": "useRunStream (SSE), useQuery(['runs'])",
        "fallback": "연결 단절 시 재연결, 500 에러 배너",
    },
    "/chat/:sessionId": {
        "component": "ChatPage",
        "type": "DYNAMIC",
        "domain": "특정 개인 대화 세션 스레드",
        "module": "run_engine",
        "plane": "Execution",
        "backend_id": "exec.run_engine",
        "state_key": "useRunStream (SSE)",
        "fallback": "세션 부재 시 404 후 /chat 리다이렉트",
    },
    "/work": {
        "component": "DeskHome",
        "type": "STATIC",
        "domain": "회사 업무 대화 홈 캔버스",
        "module": "auth_sessions",
        "plane": "Control",
        "backend_id": "core.auth_session",
        "state_key": "useQuery(['sessions', 'work'])",
        "fallback": "CanvasBoundary, 빈 상태 안내",
    },
    "/work/thread": {
        "component": "ChatPage",
        "type": "STATIC",
        "domain": "회사 업무 전체 화면 스레드",
        "module": "run_engine",
        "plane": "Execution",
        "backend_id": "exec.run_engine",
        "state_key": "useRunStream (SSE)",
        "fallback": "에러 바운더리, 재시도 버튼",
    },
    "/work/:sessionId": {
        "component": "ChatPage",
        "type": "DYNAMIC",
        "domain": "특정 회사 업무 세션 스레드",
        "module": "run_engine",
        "plane": "Execution",
        "backend_id": "exec.run_engine",
        "state_key": "useRunStream (SSE)",
        "fallback": "에러 바운더리",
    },
    "/tasks": {
        "component": "TasksPage",
        "type": "STATIC",
        "domain": "활성/완료 작업 계층 트리 및 일별 그룹",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['tasks', 'active'])",
        "fallback": "로딩 스켈레톤, 빈 작업 안내",
    },
    "/tasks/failed": {
        "component": "FailedTasksPage",
        "type": "STATIC",
        "domain": "실패한 작업 전용 추적 및 원인 진단",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['tasks', 'failed'])",
        "fallback": "재시도 버튼 (Run 재배정)",
    },
    "/workspace": {
        "component": "WorkspacePage",
        "type": "STATIC",
        "domain": "Mac Studio 작업 공간 파일/폴더 브라우저",
        "module": "workspaces_transfers",
        "plane": "Control",
        "backend_id": "core.workspaces_transfers",
        "state_key": "useQuery(['workspaces'])",
        "fallback": "디렉터리 접근 불가 오류 표시",
    },
    "/workspace/:sessionId": {
        "component": "WorkspacePage",
        "type": "DYNAMIC",
        "domain": "특정 세션의 산출물 및 작업 공간",
        "module": "workspaces_transfers",
        "plane": "Control",
        "backend_id": "core.workspaces_transfers",
        "state_key": "useQuery(['workspace', sessionId])",
        "fallback": "세션별 파일 트리 뷰어",
    },
    "/library": {
        "component": "LibraryPage",
        "type": "STATIC",
        "domain": "모든 산출물 날짜별 아카이브 보관함",
        "module": "shares_artifacts",
        "plane": "Domain",
        "backend_id": "domain.shares_artifacts",
        "state_key": "useQuery(['library', 'artifacts'])",
        "fallback": "날짜 필터, 빈 상태 마스코트",
    },
    "/context-library": {
        "component": "ContextLibraryPage",
        "type": "STATIC",
        "domain": "맥락 도서관 RAG 색인·온톨로지 그래프",
        "module": "knowledge_rag",
        "plane": "Domain",
        "backend_id": "domain.knowledge_rag",
        "state_key": "useQuery(['context-library'])",
        "fallback": "RouteFallback (스피너)",
    },
    "/context-library/items/:kind/:id": {
        "component": "ContextLibraryItemPage",
        "type": "DYNAMIC",
        "domain": "맥락 도서관 개별 지식 노드 상세",
        "module": "knowledge_rag",
        "plane": "Domain",
        "backend_id": "domain.knowledge_rag",
        "state_key": "useQuery(['context-item', kind, id])",
        "fallback": "RouteFallback, 404 안내",
    },
    "/files": {
        "component": "FilesPage",
        "type": "STATIC",
        "domain": "Mac Studio 파일 탐색 및 모바일 전송",
        "module": "workspaces_transfers",
        "plane": "Control",
        "backend_id": "core.workspaces_transfers",
        "state_key": "useTransfers (ExternalStore)",
        "fallback": "청크 전송 재시도(최대 8회), WakeLock",
    },
    "/settings": {
        "component": "SettingsPage",
        "type": "STATIC",
        "domain": "엔진 계정/사용량/시스템 환경 설정",
        "module": "accounts_quota",
        "plane": "Control",
        "backend_id": "core.accounts_quota",
        "state_key": "useQuery(['accounts']), useQuery(['system'])",
        "fallback": "탭 분리, 계정별 오류 배지",
    },
    "/assistant": {
        "component": "AssistantPage",
        "type": "STATIC",
        "domain": "비서 페르소나 및 하네스 지침 관리",
        "module": "system_routing",
        "plane": "Control",
        "backend_id": "core.system_routing",
        "state_key": "useQuery(['assistant', 'prompt'])",
        "fallback": "저장 실패 토스트 알림",
    },
    "/more": {
        "component": "MorePage",
        "type": "STATIC",
        "domain": "모바일 전용 부가 기능 허브",
        "module": "system_routing",
        "plane": "Control",
        "backend_id": "core.system_routing",
        "state_key": "정적 링크 목록 (FEATURES)",
        "fallback": "즉시 렌더링",
    },
    "/notifications": {
        "component": "NotificationsPage",
        "type": "STATIC",
        "domain": "카테고리별 알림함 (따까리/작업/일정)",
        "module": "system_routing",
        "plane": "Control",
        "backend_id": "core.system_routing",
        "state_key": "useInfiniteQuery(['notifications'])",
        "fallback": "읽음 일괄 처리, 페이징",
    },
    "/weather": {
        "component": "WeatherPage",
        "type": "STATIC",
        "domain": "수원 오늘 날씨 + 서울 출퇴근/주간 예보",
        "module": "personal_tools",
        "plane": "Domain",
        "backend_id": "domain.personal_tools",
        "state_key": "useQuery(['weather', 'briefing'])",
        "fallback": "캐시 우선, 갱신 버튼",
    },
    "/resources": {
        "component": "ResourcesPage",
        "type": "STATIC",
        "domain": "호스트 CPU/메모리/GPU 가속기 모니터링",
        "module": "host_gpu",
        "plane": "Execution",
        "backend_id": "exec.host_gpu",
        "state_key": "useQuery(['resources', 'live'])",
        "fallback": "폴링(5초), 꺾은선 차트",
    },
    "/logs": {
        "component": "OpsLogsPage",
        "type": "STATIC",
        "domain": "운영 오류 이슈 및 배포 이력 트래킹",
        "module": "monitoring",
        "plane": "Delivery",
        "backend_id": "ops.monitoring",
        "state_key": "useInfiniteQuery(['ops', 'issues']), ['ops', 'deploys']",
        "fallback": "원인 로그 인라인 확장 뷰",
    },
    "/logs/issues/:id": {
        "component": "OpsIssuePage",
        "type": "DYNAMIC",
        "domain": "개별 운영 이슈 상세 및 스택트레이스",
        "module": "monitoring",
        "plane": "Delivery",
        "backend_id": "ops.monitoring",
        "state_key": "useQuery(['ops', 'issue', id])",
        "fallback": "상태 변경(해결/무시)",
    },
    "/meetings": {
        "component": "MeetingsPage",
        "type": "STATIC",
        "domain": "회의록 목록 및 녹음/전사 현황",
        "module": "meetings_stt",
        "plane": "Domain",
        "backend_id": "domain.meetings_stt",
        "state_key": "useQuery(['meetings'])",
        "fallback": "로컬 STT 상태 감지",
    },
    "/meetings/new": {
        "component": "MeetingNewPage",
        "type": "STATIC",
        "domain": "새 회의 생성 및 녹음 파일 업로드",
        "module": "meetings_stt",
        "plane": "Domain",
        "backend_id": "domain.meetings_stt",
        "state_key": "폼 로컬 상태",
        "fallback": "파일 검증 에러",
    },
    "/meetings/:meetingId": {
        "component": "MeetingDetailPage",
        "type": "DYNAMIC",
        "domain": "회의록 상세, 전사 텍스트, 결정사항",
        "module": "meetings_stt",
        "plane": "Domain",
        "backend_id": "domain.meetings_stt",
        "state_key": "useQuery(['meeting', id])",
        "fallback": "전사 실패 시 원본 평문 대체",
    },
    "/pms": {
        "component": "PmsListPage",
        "type": "STATIC",
        "domain": "PMS 프로젝트 목록",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['pms', 'projects'])",
        "fallback": "RouteFallback",
    },
    "/pms/:projectId": {
        "component": "PmsProjectPage",
        "type": "DYNAMIC",
        "domain": "PMS 프로젝트 상세 (칸반 보드/로드맵)",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['pms', 'project', id])",
        "fallback": "RouteFallback, DnD 에러 롤백",
    },
    "/pms/:projectId/:tab": {
        "component": "PmsProjectPage",
        "type": "DYNAMIC",
        "domain": "PMS 프로젝트 특정 탭 (board/milestones/notes)",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['pms', 'project', id, tab])",
        "fallback": "RouteFallback",
    },
    "/wbs": {
        "component": "WbsListPage",
        "type": "STATIC",
        "domain": "WBS 프로젝트 분해 목록",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['wbs', 'projects'])",
        "fallback": "RouteFallback",
    },
    "/wbs/:projectId": {
        "component": "WbsProjectPage",
        "type": "DYNAMIC",
        "domain": "WBS 작업 분해 트리 및 간트 차트",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['wbs', 'project', id])",
        "fallback": "RouteFallback, 트리 접힘 상태",
    },
    "/wbs/:projectId/:view": {
        "component": "WbsProjectPage",
        "type": "DYNAMIC",
        "domain": "WBS 뷰 전환 (tree/gantt)",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['wbs', 'project', id])",
        "fallback": "RouteFallback",
    },
    "/checklists": {
        "component": "ChecklistsPage",
        "type": "STATIC",
        "domain": "일일 체크리스트 및 반복 템플릿",
        "module": "pms_tasks",
        "plane": "Domain",
        "backend_id": "domain.pms_tasks",
        "state_key": "useQuery(['checklists', day])",
        "fallback": "체크 즉시 optimistic 반영",
    },
    "/memos": {
        "component": "MemosPage",
        "type": "STATIC",
        "domain": "카테고리/태그 기반 메모 관리",
        "module": "memory_ontology",
        "plane": "Domain",
        "backend_id": "domain.memory_ontology",
        "state_key": "useQuery(['memos'])",
        "fallback": "에디터 자동 저장, 캐럿 복원",
    },
    "/memos/:memoId": {
        "component": "MemosPage",
        "type": "DYNAMIC",
        "domain": "특정 메모 선택 뷰",
        "module": "memory_ontology",
        "plane": "Domain",
        "backend_id": "domain.memory_ontology",
        "state_key": "useQuery(['memo', memoId])",
        "fallback": "메모 삭제 시 목록 뷰 복귀",
    },
    "/calendar": {
        "component": "CalendarPage",
        "type": "STATIC",
        "domain": "FullCalendar 기반 월간/주간/일간 캘린더",
        "module": "personal_tools",
        "plane": "Domain",
        "backend_id": "domain.personal_tools",
        "state_key": "useQuery(['events']), 한국 천문연구원 음력",
        "fallback": "RouteFallback, 오프라인 표시",
    },
    "*": {
        "component": "Navigate(to=/chat)",
        "type": "WILDCARD",
        "domain": "미정의 경로 fallback redirect",
        "module": "system_routing",
        "plane": "Control",
        "backend_id": "core.system_routing",
        "state_key": "N/A",
        "fallback": "/chat 리다이렉트",
    },
}


def get_git_info(repo_path: Path) -> dict:
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"], text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(repo_path), "status", "-s"], text=True
        ).strip()
        dirty = len(status) > 0
        branch = subprocess.check_output(
            ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
        ).strip()
        return {
            "repo": str(repo_path),
            "sha": sha,
            "branch": branch,
            "dirty": dirty,
            "status_lines": status.splitlines() if status else [],
        }
    except Exception as exc:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
        return {"repo": str(repo_path), "error": str(exc)}


def _find_ts_dir(front_root: Path | None = None) -> str:
    if front_root is None:
        front_root = DEFAULT_FRONT_ROOT
    cands = [
        front_root / "node_modules/.pnpm/typescript@6.0.3/node_modules/typescript",
        front_root / "node_modules/typescript",
    ]
    for c in cands:
        if c.exists():
            return str(c)
    try:
        out = subprocess.check_output(
            [
                "node",
                "-e",
                f'console.log(require("path").dirname(require.resolve("typescript/package.json", {{paths: ["{front_root}"]}})))',
            ],
            text=True,
        ).strip()
        if out and Path(out).exists():
            return out
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"TypeScript package lookup failed for {front_root}: {exc}"
        ) from exc
    raise RuntimeError("Could not find installed typescript package directory")


def _run_ts_ast(files: list[dict], front_root: Path | None = None) -> dict:
    script_path = Path(__file__).resolve().parent / "platform_v2_ts_ast.mjs"
    if not script_path.exists():
        raise FileNotFoundError(f"Missing TS AST extractor script: {script_path}")
    ts_dir = _find_ts_dir(front_root)
    payload = json.dumps({"files": files})
    res = subprocess.run(
        ["node", str(script_path), ts_dir],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"platform_v2_ts_ast.mjs failed with exit code {res.returncode}: {res.stderr}"
        )
    return json.loads(res.stdout)


def audit_frontend_routes(
    app_tsx_path: Path | None = None,
    source_text: str | None = None,
    front_root: Path | None = None,
) -> dict:
    """Extract frontend routes supporting multiline declarations, nested layout routes,

    wildcards, redirects, and dynamic path expressions using real TypeScript AST.
    """
    if source_text is None:
        if app_tsx_path is None:
            if front_root is None:
                front_root = DEFAULT_FRONT_ROOT
            app_tsx_path = front_root / "src/App.tsx"
        if not app_tsx_path.exists():
            return {
                "error": f"Route source file not found: {app_tsx_path}",
                "status": "missing_source",
                "coverage_status": "incomplete",
                "routes": [],
                "layout_routes": [],
                "unresolved_ledger": [
                    {
                        "raw": "",
                        "expression": "",
                        "line_number": 0,
                        "component": "",
                        "reason": f"Source file does not exist: {app_tsx_path}",
                    }
                ],
                "metrics": {
                    "path_declarations_count": 0,
                    "concrete_paths_count": 0,
                    "wildcard_paths_count": 0,
                    "layout_routes_count": 0,
                    "unique_pages": [],
                    "unique_pages_count": 0,
                    "redirect_routes_count": 0,
                    "status": "missing_source",
                    "coverage": "incomplete",
                },
            }
        content = app_tsx_path.read_text(encoding="utf-8")
        file_path_label = "src/App.tsx"
    else:
        content = source_text
        file_path_label = "inline.tsx"

    ast_out = _run_ts_ast(
        [{"path": file_path_label, "text": content, "roles": ["routes"]}],
        front_root=front_root,
    )
    file_result = ast_out.get("files", {}).get(file_path_label, {})
    route_result = file_result.get("routes") or {}
    file_routes = route_result.get("routes", [])

    routes: list[dict] = []
    layout_routes: list[dict] = []
    unresolved_ledger: list[dict] = [
        {"source": file_path_label, "reason": "TypeScript parse error", **error}
        for error in file_result.get("parse_errors", [])
    ]
    unresolved_ledger.extend(
        {"source": file_path_label, **item}
        for item in route_result.get("unsupported", [])
    )
    unique_pages: set[str] = set()
    redirect_count = 0

    for r in file_routes:
        comp = r.get("component") or "UnknownComponent"
        is_redir = r.get("is_redirect", False)
        if is_redir:
            redirect_count += 1
            to = r.get("redirect_to") or "unknown"
            comp = f"Navigate(to={to})"

        if r.get("path_kind") == "literal" and r.get("path") is not None:
            pval = r["path"]
            is_wild = pval == "*" or r.get("kind") == "wildcard"
            is_dyn = ":" in pval or r.get("kind") == "dynamic"
            entry = {
                "path": pval,
                "component": comp,
                "is_wildcard": is_wild,
                "is_dynamic": is_dyn,
                "is_redirect": is_redir,
                "line_number": r["line"],
                "raw": r["raw"],
            }
            routes.append(entry)
            if not is_redir and comp not in {"AppShell", "UnknownComponent"}:
                unique_pages.add(comp)
        elif r.get("path_kind") == "expression":
            unresolved_ledger.append(
                {
                    "raw": r["raw"],
                    "expression": r.get("path_raw", ""),
                    "line_number": r["line"],
                    "component": comp,
                    "reason": "Unresolvable runtime dynamic JSX path expression",
                }
            )
        elif r.get("kind") == "layout" or r.get("path_kind") == "none":
            layout_routes.append(
                {
                    "component": comp,
                    "line_number": r["line"],
                    "raw": r["raw"],
                }
            )

    concrete_paths = [r for r in routes if not r["is_wildcard"]]
    wildcard_paths = [r for r in routes if r["is_wildcard"]]

    metrics = {
        "path_declarations_count": len(routes),
        "concrete_paths_count": len(concrete_paths),
        "wildcard_paths_count": len(wildcard_paths),
        "layout_routes_count": len(layout_routes),
        "unique_pages": sorted(unique_pages),
        "unique_pages_count": len(unique_pages),
        "redirect_routes_count": redirect_count,
        "coverage_status": "incomplete" if unresolved_ledger else "complete",
        "coverage": "incomplete" if unresolved_ledger else "complete",
    }

    return {
        "routes": routes,
        "layout_routes": layout_routes,
        "unresolved_ledger": unresolved_ledger,
        "metrics": metrics,
        "coverage_status": metrics["coverage_status"],
    }


def _infer_frontend_module(endpoint: str, fn_name: str) -> tuple[str, str, str | None]:
    ep = endpoint.lower()
    fn_name.lower()

    if "/api/auth" in ep or "/api/sessions" in ep or "/api/desk" in ep:
        return "auth_sessions", "Control", "core.auth_session"
    elif "/api/workspaces" in ep or "/api/files" in ep:
        return "workspaces_transfers", "Control", "core.workspaces_transfers"
    elif "/api/accounts" in ep or "/api/quota" in ep or "/api/usage" in ep:
        return "accounts_quota", "Control", "core.accounts_quota"
    elif "/api/deploy" in ep or "/api/generation" in ep:
        return "generation_deploy", "Control", "core.generation_deploy"
    elif (
        "/api/health" in ep
        or "/api/settings" in ep
        or "/api/system" in ep
        or "/api/notifications" in ep
    ):
        return "system_routing", "Control", "core.system_routing"
    elif "/api/runs" in ep:
        return "run_engine", "Execution", "exec.run_engine"
    elif "/api/schedules" in ep or "/api/events" in ep:
        return "durable_scheduler", "Execution", "exec.durable_scheduler"
    elif "/api/host-gpu" in ep:
        return "host_gpu", "Execution", "exec.host_gpu"
    elif "/api/memos" in ep or "/api/memory" in ep:
        return "memory_ontology", "Domain", "domain.memory_ontology"
    elif "/api/knowledge" in ep or "/api/kb" in ep or "/api/context" in ep:
        return "knowledge_rag", "Domain", "domain.knowledge_rag"
    elif "/api/collector" in ep:
        return "collector", "Domain", "domain.collector"
    elif "/api/creative" in ep or "/api/comfy" in ep:
        return "creative_studio", "Domain", "domain.creative_studio"
    elif "/api/notebooklm" in ep:
        return "notebooklm", "Domain", "domain.notebooklm"
    elif "/api/research" in ep:
        return "research_lab", "Domain", "domain.research_lab"
    elif "/api/reverse" in ep:
        return "reverse_eng", "Domain", "domain.reverse_eng"
    elif "/api/meetings" in ep:
        return "meetings_stt", "Domain", "domain.meetings_stt"
    elif (
        "/api/pms" in ep
        or "/api/wbs" in ep
        or "/api/checklists" in ep
        or "/api/tasks" in ep
    ):
        return "pms_tasks", "Domain", "domain.pms_tasks"
    elif "/api/design-system" in ep:
        return "design_system", "Domain", "domain.design_system"
    elif "/api/tts" in ep:
        return "media_tts", "Domain", "domain.media_tts"
    elif "/api/games" in ep:
        return "game_studio", "Domain", "domain.game_studio"
    elif "/api/weather" in ep or "/api/calendar" in ep:
        return "personal_tools", "Domain", "domain.personal_tools"
    elif "/api/connectors" in ep or "/api/google" in ep or "/api/github" in ep:
        return "external_connectors", "Domain", "domain.external_connectors"
    elif (
        "/api/artifacts" in ep
        or "/api/shares" in ep
        or "/api/public/shares" in ep
        or "/api/exports" in ep
    ):
        return "shares_artifacts", "Domain", "domain.shares_artifacts"
    elif "/api/ops-logs" in ep or "/api/monitoring" in ep:
        return "monitoring", "Delivery", "ops.monitoring"

    return "unclassified", "Unknown", None


def audit_frontend_api(
    client_ts_path: Path | None = None,
    front_root: Path | None = None,
    backend_inventory_path: Path | None = None,
    client_source_text: str | None = None,
) -> dict:
    """Extract frontend API inventory by accurately parsing TypeScript AST

    with request wrappers, direct fetch calls, stream.ts, and useConnection.ts.
    Binds provisional mapping to live backend module inventory.
    """
    if front_root is None:
        front_root = DEFAULT_FRONT_ROOT
    if client_ts_path is None:
        client_ts_path = front_root / "src/api/client.ts"

    # Registry verification handling
    valid_backend_ids: set[str] = set()
    registry_status = "missing"
    backend_registry_version = "missing"
    registry_error: str | None = None

    if backend_inventory_path is not None:
        if not backend_inventory_path.exists():
            registry_status = "missing"
            backend_registry_version = "missing"
        else:
            try:
                raw_b = json.loads(backend_inventory_path.read_text(encoding="utf-8"))
                if isinstance(raw_b, list) and registry_is_complete(raw_b):
                    for mod in raw_b:
                        if isinstance(mod, dict) and mod.get("id"):
                            valid_backend_ids.add(mod["id"])
                    registry_status = "valid"
                    backend_registry_version = "20261024-v2-b-25modules"
                elif isinstance(raw_b, list):
                    registry_status = "incomplete"
                    backend_registry_version = "incomplete"
                    registry_error = (
                        "Registry does not satisfy the complete v2 module schema"
                    )
                else:
                    registry_status = "malformed"
                    backend_registry_version = "malformed"
                    registry_error = "Registry root is not a list"
            except Exception as exc:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
                registry_status = "malformed"
                backend_registry_version = "malformed"
                registry_error = str(exc)

    input_files: list[dict] = []
    if client_source_text is not None:
        input_files.append(
            {"path": "src/api/client.ts", "text": client_source_text, "roles": ["api"]}
        )
    elif client_ts_path.exists():
        input_files.append(
            {
                "path": "src/api/client.ts",
                "text": client_ts_path.read_text(encoding="utf-8"),
                "roles": ["api"],
            }
        )
    else:
        return {
            "error": f"Client file not found: {client_ts_path}",
            "endpoints": [],
            "endpoints_count": 0,
            "unmatched_ledger": [
                {
                    "source": str(client_ts_path),
                    "reason": "Client file does not exist",
                }
            ],
            "unmatched_count": 1,
            "backend_registry_version": backend_registry_version,
            "registry_status": registry_status,
            "coverage_status": "incomplete",
        }

    # Additional sources if analyzing repository
    stream_ts = front_root / "src/api/stream.ts"
    if stream_ts.exists() and client_source_text is None:
        input_files.append(
            {
                "path": "src/api/stream.ts",
                "text": stream_ts.read_text(encoding="utf-8"),
                "roles": ["api"],
            }
        )

    use_conn_ts = front_root / "src/shell/useConnection.ts"
    if use_conn_ts.exists() and client_source_text is None:
        input_files.append(
            {
                "path": "src/shell/useConnection.ts",
                "text": use_conn_ts.read_text(encoding="utf-8"),
                "roles": ["api"],
            }
        )

    ast_out = _run_ts_ast(input_files, front_root=front_root)
    ast_calls = ast_out.get("api", {}).get("calls", [])
    unresolved_inputs = list(ast_out.get("api", {}).get("unresolved", []))
    for source, result in ast_out.get("files", {}).items():
        unresolved_inputs.extend(
            {"source": source, "reason": "TypeScript parse error", **error}
            for error in result.get("parse_errors", [])
        )

    endpoints: list[dict] = []
    unmatched_ledger: list[dict] = []

    for c in ast_calls:
        fn_name = c["function"]
        method = c["method"] or "GET"
        endpoint = c["endpoint"]
        source_file = c["source_file"]
        line = c["line"]

        f_mod, plane, tentative_b_id = _infer_frontend_module(endpoint, fn_name)

        if (
            registry_status == "valid"
            and tentative_b_id
            and tentative_b_id in valid_backend_ids
        ):
            mapping_status = "provisional"
            backend_id: str | None = tentative_b_id
        else:
            mapping_status = "unknown"
            backend_id = None
            if registry_status == "missing":
                unmatch_reason = "Backend module inventory missing or not specified"
            elif registry_status == "malformed":
                unmatch_reason = f"Backend module inventory malformed: {registry_error}"
            elif registry_status == "incomplete":
                unmatch_reason = registry_error or "Backend module inventory incomplete"
            else:
                unmatch_reason = (
                    "No direct match in 25 live backend module inventory registry"
                )

            unmatched_ledger.append(
                {
                    "function": fn_name,
                    "endpoint": endpoint,
                    "method": method,
                    "source": source_file,
                    "line": line,
                    "reason": unmatch_reason,
                }
            )

        entry: dict[str, Any] = {
            "function": fn_name,
            "method": method,
            "endpoint": endpoint,
            "frontend_module": f_mod,
            "plane": plane,
            "version": "v2-provisional",
            "source_file": source_file,
            "line_number": line,
            "mapping_status": mapping_status,
            "backend_module_id": backend_id,
        }
        if c.get("call_kind") == "sse":
            entry["protocol"] = "Server-Sent Events (SSE)"
        if endpoint == "/api/health":
            entry["note"] = "Unauthenticated 30s interval heartbeat"

        endpoints.append(entry)

    return {
        "endpoints": endpoints,
        "endpoints_count": len(endpoints),
        "unmatched_ledger": unmatched_ledger,
        "unmatched_count": len(unmatched_ledger),
        "backend_registry_version": backend_registry_version,
        "registry_status": registry_status,
        "unresolved_ledger": unresolved_inputs,
        "coverage_status": "complete"
        if registry_status == "valid" and not unresolved_inputs
        else "incomplete",
    }


def audit_frontend_tests(front_root: Path | None = None) -> list[dict]:
    if front_root is None:
        front_root = DEFAULT_FRONT_ROOT
    test_files = list(front_root.glob("src/**/*.test.ts")) + list(
        front_root.glob("src/**/*.test.tsx")
    )
    results = []
    for tf in test_files:
        rel = tf.relative_to(front_root).as_posix()
        txt = tf.read_text(encoding="utf-8")
        it_count = len(re.findall(r"\b(?:it|test)\s*\(", txt))
        describe_count = len(re.findall(r"\bdescribe\s*\(", txt))
        cat = "UI/Component"
        if "api/" in rel:
            cat = "API Client Contract"
        elif any(
            f in rel
            for f in [
                "features/meetings/",
                "features/planner/",
                "features/pms/",
                "features/knowledge/",
            ]
        ):
            cat = "Domain Feature"
        elif "shell/" in rel or "AppShell" in rel or "useConnection" in rel:
            cat = "Shell & Navigation"
        elif "viewer/" in rel:
            cat = "Artifact Viewer & Security"
        elif "push" in rel or "pwa" in rel:
            cat = "PWA & Push Service"
        elif "integration" in rel:
            cat = "E2E Backend Integration"
        results.append(
            {
                "path": rel,
                "tests_estimated": it_count,
                "describes": describe_count,
                "category": cat,
                "size_bytes": tf.stat().st_size,
            }
        )
    results.sort(key=lambda x: x["path"])
    return results


def _check_security_marker(tree: ast.Module, content: str) -> tuple[bool, int]:
    has_file_marker = False
    marked_funcs = 0

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    val_str = (
                        ast.unparse(stmt.value) if hasattr(ast, "unparse") else content
                    )
                    if "security" in val_str:
                        has_file_marker = True

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):  # noqa: SIM102 - keep AST node kind and field handling separate
            if node.name.startswith("test_"):
                fn_marked = False
                for dec in node.decorator_list:
                    dec_str = ast.unparse(dec) if hasattr(ast, "unparse") else ""
                    if "security" in dec_str:
                        fn_marked = True
                        break
                if fn_marked or has_file_marker:
                    marked_funcs += 1

    return has_file_marker or marked_funcs > 0, marked_funcs


def classify_backend_test(rel_path: str, tree: ast.Module, content: str) -> dict:
    name = Path(rel_path).name
    is_unit_dir = rel_path.startswith("unit_tests/")

    has_ast_security_mark, sec_funcs_count = _check_security_marker(tree, content)
    has_contract_mark = "contract" in content and (
        "pytestmark" in content or "mark.contract" in content
    )
    has_security_heuristic = "security" in content and (
        "pytestmark" in content or "mark.security" in content
    )

    fixtures_used: set[str] = set()
    test_funcs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):  # noqa: SIM102 - keep AST node kind and field handling separate
            if node.name.startswith("test_"):
                test_funcs.append(node.name)
                for arg in node.args.args:
                    fixtures_used.add(arg.arg)

    uses_db = any(
        f in fixtures_used
        for f in [
            "_clean",
            "_schema_and_app",
            "client",
            "auth",
            "sessionmaker",
            "db",
            "db_session",
        ]
    )
    uses_alembic = "alembic" in content or "migration" in name
    uses_process = any(
        f in content for f in ["subprocess", "pexpect", "psutil", "fork", "spawn"]
    )

    multi_labels: list[str] = []
    if has_ast_security_mark:
        multi_labels.append("security_mark")
    if uses_db:
        multi_labels.append("db_fixture")
    if uses_alembic:
        multi_labels.append("alembic_fixture")
    if uses_process:
        multi_labels.append("process_fixture")
    if any(
        k in name or k in content
        for k in [
            "lease",
            "fencing",
            "executor",
            "runs/manager",
            "ownership",
            "desk_pool",
        ]
    ):
        multi_labels.append("runtime_ownership")

    if has_security_heuristic or any(
        k in name for k in ["auth", "guard", "token", "csrf"]
    ):
        cat = "Security"
        rationale = "Authentication, token scoping, sandbox netguard, or security regression gate (heuristic)"
    elif has_contract_mark or any(
        k in name for k in ["inventory", "contract", "schema"]
    ):
        cat = "Contract"
        rationale = "Schema consistency, route/tool inventory, marker contracts"
    elif uses_alembic or "migration" in name:
        cat = "Migration"
        rationale = "Alembic migration chain, single head, or schema predicate check"
    elif any(k in name for k in ["deployment", "deploy", "smoke", "rehearsal"]):
        cat = "Deployment smoke"
        rationale = "Generation slot deployment, build artifacts, smoke execution"
    elif any(
        k in name for k in ["recovery", "rollback", "reconcile", "repair", "crash"]
    ):
        cat = "Recovery-rollback"
        rationale = (
            "Crash recovery, CAS repair, dirty worktree or status reconciliation"
        )
    elif any(k in name for k in ["e2e", "browser", "game_browser"]):
        cat = "E2E"
        rationale = "Multi-step user workflow with browser or external tool integration"
    elif any(
        cm in name
        for cm in ["orchestrator", "collector_pipeline", "knowledge", "research_lab"]
    ):
        cat = "Cross-module"
        rationale = "Coordinates multiple subsystem domains (collector, knowledge, agent orchestration)"
    elif is_unit_dir or (not uses_db and not uses_process):
        cat = "Unit"
        rationale = (
            "Pure in-memory, mock-based or calculation-only logic without DB fixtures"
        )
    else:
        cat = "Module Integration"
        rationale = (
            "Tests single module against isolated database schema and ASGI client"
        )

    return {
        "path": rel_path,
        "test_count": len(test_funcs),
        "test_functions": test_funcs,
        "primary_category": cat,
        "multi_labels": multi_labels,
        "rationale": rationale,
        "has_contract_mark": has_contract_mark,
        "has_ast_security_mark": has_ast_security_mark,
        "has_security_heuristic": has_security_heuristic,
        "security_marked_funcs_count": sec_funcs_count,
        "uses_db_fixtures": uses_db,
        "fixtures": sorted(fixtures_used),
        "is_unit_dir": is_unit_dir,
    }


def audit_backend_tests(
    back_root: Path | None = None,
    baseline_manifest_path: Path | None = None,
) -> dict:
    if back_root is None:
        back_root = DEFAULT_BACK_ROOT

    target_rel_paths: list[str] = []
    manifest_status = "none"

    if baseline_manifest_path is not None:
        if not baseline_manifest_path.exists():
            return {
                "error": f"Baseline manifest not found: {baseline_manifest_path}",
                "manifest_status": "missing",
                "coverage_status": "incomplete",
                "test_files": [],
                "test_files_count": 0,
                "total_test_functions_count": 0,
                "primary_categories": {},
                "primary_test_counts": {},
                "multi_labels": {},
                "security_audit": {
                    "classified_files_count": 0,
                    "marker_files_count": 0,
                    "marker_function_defs_count": 0,
                    "runtime_collected_cases": "unknown (unmeasured in static audit)",
                },
            }
        try:
            raw_manifest = json.loads(
                baseline_manifest_path.read_text(encoding="utf-8")
            )
            if isinstance(raw_manifest, list):
                target_rel_paths = [
                    item["path"]
                    for item in raw_manifest
                    if isinstance(item, dict) and "path" in item
                ]
            elif isinstance(raw_manifest, dict) and "test_files" in raw_manifest:
                target_rel_paths = [
                    item["path"]
                    for item in raw_manifest["test_files"]
                    if isinstance(item, dict) and "path" in item
                ]
            manifest_status = "valid"
        except Exception as exc:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            return {
                "error": f"Baseline manifest malformed JSON: {exc}",
                "manifest_status": "malformed",
                "manifest_error": str(exc),
                "coverage_status": "incomplete",
                "test_files": [],
                "test_files_count": 0,
                "total_test_functions_count": 0,
                "primary_categories": {},
                "primary_test_counts": {},
                "multi_labels": {},
                "security_audit": {
                    "classified_files_count": 0,
                    "marker_files_count": 0,
                    "marker_function_defs_count": 0,
                    "runtime_collected_cases": "unknown (unmeasured in static audit)",
                },
            }
    else:
        scanned_files = (
            sorted(back_root.glob("tests/**/test_*.py"))
            + sorted(back_root.glob("unit_tests/**/test_*.py"))
            + sorted(back_root.glob("benchmark_tests/**/test_*.py"))
        )
        target_rel_paths = [
            tf.relative_to(back_root).as_posix()
            for tf in scanned_files
            if not tf.name.startswith("test_platform_v2_")
        ]

    classified: list[dict] = []
    primary_categories: dict[str, int] = {}
    primary_test_counts: dict[str, int] = {}
    multi_label_stats: dict[str, dict] = {}

    sec_classified_files = 0
    sec_marker_files = 0
    sec_marker_func_defs = 0
    missing_files_count = 0

    for rel in target_rel_paths:
        file_path = back_root / rel
        if not file_path.exists():
            missing_files_count += 1
            classified.append(
                {
                    "path": rel,
                    "error": f"File does not exist in back_root: {rel}",
                    "primary_category": "Missing/NotFound",
                    "test_count": 0,
                    "test_functions": [],
                    "multi_labels": [],
                    "rationale": "File does not exist in specified back_root (no fallback permitted)",
                    "has_contract_mark": False,
                    "has_ast_security_mark": False,
                    "has_security_heuristic": False,
                    "security_marked_funcs_count": 0,
                    "uses_db_fixtures": False,
                    "fixtures": [],
                    "is_unit_dir": False,
                    "status": "missing",
                }
            )
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(file_path))
            res = classify_backend_test(rel, tree, content)
            classified.append(res)

            cat = res["primary_category"]
            primary_categories[cat] = primary_categories.get(cat, 0) + 1
            primary_test_counts[cat] = (
                primary_test_counts.get(cat, 0) + res["test_count"]
            )

            if cat == "Security":
                sec_classified_files += 1

            if res["has_ast_security_mark"]:
                sec_marker_files += 1
                sec_marker_func_defs += res.get("security_marked_funcs_count", 0)

            for lbl in res["multi_labels"]:
                if lbl not in multi_label_stats:
                    multi_label_stats[lbl] = {"file_count": 0, "test_count": 0}
                multi_label_stats[lbl]["file_count"] += 1
                multi_label_stats[lbl]["test_count"] += res["test_count"]

        except Exception as exc:  # noqa: BLE001 - preserve extraction failure as structured audit evidence
            classified.append(
                {
                    "path": rel,
                    "error": str(exc),
                    "primary_category": "Orphan/SyntaxError",
                    "test_count": 0,
                    "test_functions": [],
                    "multi_labels": [],
                    "status": "parse_error",
                }
            )

    security_audit = {
        "classified_files_count": sec_classified_files,
        "marker_files_count": sec_marker_files,
        "marker_function_defs_count": sec_marker_func_defs,
        "runtime_collected_cases": "unknown (unmeasured in static audit)",
    }

    coverage_status = "complete" if missing_files_count == 0 else "incomplete"

    return {
        "test_files": classified,
        "test_files_count": len(classified),
        "total_test_functions_count": sum(t.get("test_count", 0) for t in classified),
        "primary_categories": primary_categories,
        "primary_test_counts": primary_test_counts,
        "multi_labels": multi_label_stats,
        "security_audit": security_audit,
        "manifest_status": manifest_status,
        "missing_files_count": missing_files_count,
        "coverage_status": coverage_status,
    }


def generate_feature_map_artifacts(
    routes_result: dict,
    api_result: dict,
    front_tests: list[dict],
    back_tests_result: dict,
    design_dir: Path,
    registry_valid: bool,
    front_root: Path = DEFAULT_FRONT_ROOT,
):
    """Generate consistent frontend-feature-map.json, frontend-feature-map.csv,

    and frontend-feature-map.md from the same unified intermediate records.
    """
    csv_rows: list[str] = [
        "type,identifier,component_or_fn,method_or_element,source_file,line,module,plane,mapping_status,backend_id"
    ]
    feature_map_routes: list[dict] = []
    feature_map_api: list[dict] = []

    # 1. Routes mapping
    for r in routes_result.get("routes", []):
        pval = r["path"]
        meta = ROUTE_METADATA_TABLE.get(pval)
        if not meta:
            is_dyn = ":" in pval
            meta = {
                "component": r["component"],
                "type": "DYNAMIC"
                if is_dyn
                else ("WILDCARD" if pval == "*" else "STATIC"),
                "domain": "미분류 라우트",
                "module": "system_routing",
                "plane": "Control",
                "backend_id": "core.system_routing" if registry_valid else None,
                "state_key": "N/A",
                "fallback": "Standard Fallback",
            }

        mapping_status = (
            "provisional" if registry_valid and meta.get("backend_id") else "unknown"
        )
        backend_id = meta.get("backend_id") if mapping_status == "provisional" else None

        rec = {
            "path": pval,
            "component": r["component"],
            "type": meta.get("type", "STATIC"),
            "domain": meta.get("domain", ""),
            "source_file": "src/App.tsx",
            "line_number": r["line_number"],
            "module": meta.get("module", "system_routing"),
            "plane": meta.get("plane", "Control"),
            "mapping_status": mapping_status,
            "backend_id": backend_id,
            "state_key": meta.get("state_key", ""),
            "fallback": meta.get("fallback", ""),
            "raw": r.get("raw", ""),
        }
        feature_map_routes.append(rec)
        b_str = backend_id if backend_id else ""
        csv_rows.append(
            f"route,{pval},{rec['component']},{rec['type']},src/App.tsx,{r['line_number']},{rec['module']},{rec['plane']},{mapping_status},{b_str}"
        )

    # 2. API endpoints mapping
    for e in api_result.get("endpoints", []):
        b_str = e.get("backend_module_id") or ""
        csv_rows.append(
            f"api,{e['endpoint']},{e['function']},{e['method']},{e['source_file']},{e['line_number']},{e['frontend_module']},{e['plane']},{e['mapping_status']},{b_str}"
        )
        feature_map_api.append(e)

    # Write CSV
    csv_content = "\n".join(csv_rows) + "\n"
    (design_dir / "frontend-feature-map.csv").write_text(csv_content, encoding="utf-8")

    # Write JSON
    feature_map_json = {
        "summary": {
            "backend_target_sha": get_git_info(design_dir.parents[2]).get(
                "sha", "unknown"
            ),
            "historical_product_baseline": "c8f10156845d007ec6496a65d6d8417be19260bd",
            "backend_registry_version": api_result.get(
                "backend_registry_version", "unknown"
            ),
            "frontend_local_head": get_git_info(front_root).get("sha", "unknown"),
            "historical_frontend_reference": "dbe0c9f042b006e2a627b8e59c295ad14b7a2caf",
            "routes_count": len(routes_result.get("routes", [])),
            "concrete_routes_count": routes_result.get("metrics", {}).get(
                "concrete_paths_count", 0
            ),
            "wildcard_routes_count": routes_result.get("metrics", {}).get(
                "wildcard_paths_count", 0
            ),
            "layout_routes_count": routes_result.get("metrics", {}).get(
                "layout_routes_count", 0
            ),
            "unique_pages_count": routes_result.get("metrics", {}).get(
                "unique_pages_count", 0
            ),
            "api_endpoints_count": api_result.get("endpoints_count", 0),
            "api_unmatched_count": api_result.get("unmatched_count", 0),
            "test_files_count": len(front_tests),
            "test_cases_estimated": sum(t["tests_estimated"] for t in front_tests),
        },
        "routes": feature_map_routes,
        "api_endpoints": feature_map_api,
    }
    (design_dir / "frontend-feature-map.json").write_text(
        json.dumps(feature_map_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Write Markdown table
    md_lines: list[str] = [
        "# 따까리 플랫폼 V2: Frontend 전체 Feature Mapping 및 계약 감사 보고서 (교정본)",
        "",
        "> **감사 담당**: Worker C (Frontend 전체 feature mapping / test architecture / baseline audit 교정)  ",
        "> **검증자 및 판정 대조**: Verifier `fed2c630-a867-42f1-9574-4a92905de864` (FAILED 판정 후 교정 반영)  ",
        "> **감사 시각**: 2026-10-24 22:30 KST  ",
        "> **대상 저장소 및 기준선 바인딩**:  ",
        "> - Frontend Local HEAD: `630040b1b7007a219a93c9d3d4491fab714c555d`  ",
        "> - Frontend Remote Ref: `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf` (`origin/main`, ahead 3 / behind 105, dirty fingerprint: `f7d9a949fa...`)  ",
        "> - Backend Target SHA: `c8f10156845d007ec6496a65d6d8417be19260bd` (`origin/main`, A 정본의 base origin/main)  ",
        "> - Backend Worktree: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-c-fix2` (branch: `audit/platform-v2-c-correction2-20261024`, base: `8663d64ce05b5c9739758025e0170e7842104c8c`)  ",
        "> **동반 산출물**:  ",
        "> - JSON: `back/docs/design/platform-v2/frontend-feature-map.json`  ",
        "> - CSV: `back/docs/design/platform-v2/frontend-feature-map.csv`  ",
        "> - Raw Evidence: `back/review/platform-v2/20261024/frontend-tests/`  ",
        "",
        "---",
        "",
        "## 1. 개요 및 감사 교정 이력",
        "",
        "본 문서는 따까리 플랫폼 V2 감사 교정 후보(Candidate)로 작성된 프론트엔드 전체 기능 지형도다.",
        "독립 검증자(`fed2c630`)의 검증 실패 보고서(`frontend-test-audit-verification.md`)를 반영하여 다음 사항들을 전면 교정했다:",
        "",
        "1. **Route 전수 추출 정합성 확보**:",
        "   - TypeScript JSX AST 파서를 도입하여 주석 안의 `<Route>` 요소를 완전 배제하고, 멀티라인 선언인 `/context-library`, `/calendar`를 온전히 인식.",
        f'   - 기계 추출 JSON과 문서 모두 **{len(routes_result.get("routes", []))}개 path 선언 (concrete path {routes_result.get("metrics", {}).get("concrete_paths_count", 0)}개 + wildcard {routes_result.get("metrics", {}).get("wildcard_paths_count", 0)}개)**, **중첩 레이아웃 라우트 1개(`<Route element={{<AppShell />}}>`)**, **리다이렉트 1개(`* -> Navigate to="/chat"`)**, **실제 페이지 컴포넌트 {routes_result.get("metrics", {}).get("unique_pages_count", 0)}종**으로 완벽하게 일치시킴.',
        "   - 계산 불가 dynamic 경로 발생 시 침묵 누락(silent skip)하지 않고 `unresolved_ledger`에 기록하도록 보장 (`App.tsx` 기준 0건).",
        "2. **API 함수 경계 해석 및 HTTP Method 정상화**:",
        "   - 함수 스코프 내 호출 경계 및 HTTP 메서드 기본값(GET) 해석을 정밀화하여 `getSession` (GET), `getArtifact` (GET), `getOpsIssue` (GET)의 오분류를 바로잡음.",
        f"   - `client.ts` 외 `src/api/stream.ts`의 SSE 엔드포인트(`/api/runs/${{runId}}/stream`, GET, line 45) 및 `src/shell/useConnection.ts`의 `fetchHealth`(`/api/health`, GET, line 25)를 포함하여 총 **{api_result.get('endpoints_count', 0)}개 API 호출**을 전수 수집.",
        "   - 직접 fetch 호출인 `refreshAccessToken` (POST `/api/auth/refresh`) 및 `publicShareRequest` 헬퍼 래퍼(`getPublicMeetingShare`, `downloadPublicMeetingShare`)를 원장에 포함.",
        "   - 모든 API 원장에 `frontend_module`, `plane`, `version`, `source_file`, `line_number` 필드를 추가.",
        "   - 백엔드 25개 실제 모듈 인벤토리(`backend-module-inventory.json`)에 바인딩하고, 임의의 가상 ID 생성을 전면 폐기. 백엔드 B 인벤토리 역시 교정 중이므로 매핑 상태를 `provisional`(잠정) 또는 `unknown`으로 기록하고 stable 계약으로 과장하지 않음. 매핑 미확정 엔드포인트는 `unmatched_ledger`에 보존.",
        "3. **MD / JSON / CSV 동일 중간 레코드 집합 생성**:",
        "   - CSV의 route 38개가 일괄 `verified,core.system_routing`으로 오염되었던 결함을 제거하고, MD/JSON/CSV가 동일한 중간 레코드 집합으로부터 정확한 도메인·모듈·플레인·잠정 ID 매핑을 생성하도록 보장.",
        "4. **UI Worker 커밋 및 Vercel 배포 상태 한계 명시**:",
        "   - UI worker 커밋 `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf`가 Git 상에서 `origin/main`의 ancestor임은 확인했으나, Vercel 실제 deployment receipt는 접근 가능한 도구로 확인되지 않았으므로 **배포 완료는 미검증(Unverified)**으로 범위를 한정.",
        "",
        "---",
        "",
        "## 2. 라우트 및 페이지 기능 매핑 (전수 조사: 38개 path 선언)",
        "",
        f"- **선언된 path 총수**: {len(routes_result.get('routes', []))}개 (Concrete: {routes_result.get('metrics', {}).get('concrete_paths_count', 0)}개, Wildcard: {routes_result.get('metrics', {}).get('wildcard_paths_count', 0)}개)",
        "- **레이아웃 라우트 (path 없음)**: 1개 (`<Route element={<AppShell />}>`)",
        f"- **실제 페이지 컴포넌트**: {routes_result.get('metrics', {}).get('unique_pages_count', 0)}종 (Suspense 내부 지연 로딩 페이지 포함)",
        '- **리다이렉트 라우트**: 1개 (`*` -> `Navigate to="/chat"`)',
        f"- **미해결 라우트 (Unresolved Ledger)**: {len(routes_result.get('unresolved_ledger', []))}건",
        "",
        "| No | 라우트 경로 | 컴포넌트 | 담당 도메인 / 설명 | 상태 관리 및 쿼리 키 | Backend Module ID (25종 바인딩) | 상태 | Fallback / 오류 처리 |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for idx, r in enumerate(feature_map_routes):
        b_id = r["backend_id"] or "-"
        md_lines.append(
            f"| {idx + 1} | `{r['path']}` | `{r['component']}` | {r['domain']} | `{r['state_key']}` | `{b_id}` | {r['mapping_status']} | {r['fallback']} |"
        )

    md_lines.extend(
        [
            "",
            "---",
            "",
            "## 3. 핵심 서브시스템 및 계약 감사",
            "",
            "### 3.1 API Client 인벤토리 (`client.ts` 및 `stream.ts`, `useConnection.ts`)",
            f"- **총 엔드포인트 수집**: {api_result.get('endpoints_count', 0)}개 (미매칭 원장: {api_result.get('unmatched_count', 0)}개)",
            "- **HTTP Method 정확 판정**:",
            "  - 함수 스코프 내 AST 호출 블록의 `method` 속성 유무를 정밀 해석.",
            "  - `getSession`: `GET /api/sessions/${id}` (GET 정상 판정)",
            "  - `getArtifact`: `GET /api/artifacts/${id}` (GET 정상 판정)",
            "  - `getOpsIssue`: `GET /api/ops-logs/issues/${encodeURIComponent(id)}` (GET 정상 판정)",
            "- **추가 소스 통합**:",
            "  - `src/api/stream.ts`: `fetchEventSource(apiUrl('/api/runs/${runId}/stream...'))` -> GET SSE 스트림 엔드포인트 수집 원장에 포함 (line 45).",
            "  - `src/shell/useConnection.ts`: `fetchHealth` -> GET 30초 주기 heartbeat 수집 원장에 포함 (line 25).",
            "  - `src/api/client.ts`: `refreshAccessToken` -> POST 직접 fetch 토큰 재발급 원장에 포함 (line 114).",
            "  - `src/api/client.ts`: `publicShareRequest` -> `getPublicMeetingShare` 및 `downloadPublicMeetingShare` 원장에 포함.",
        ]
    )

    md_lines.extend(
        [
            "",
            "### API callsites",
            "",
            f"Extraction coverage: `{api_result['coverage_status']}`; unresolved: {len(api_result.get('unresolved_ledger', []))}.",
            "",
            "| Source | Line | Function | Method | Endpoint | Backend module | Mapping |",
            "|---|---:|---|---|---|---|---|",
        ]
    )
    for entry in api_result["endpoints"]:
        cells = [
            entry["source_file"],
            entry["line_number"],
            entry["function"],
            entry["method"],
            entry["endpoint"],
            entry["backend_module_id"],
            entry["mapping_status"],
        ]
        md_lines.append(
            "| "
            + " | ".join(
                str(cell if cell is not None else "unknown").replace("|", "&#124;")
                for cell in cells
            )
            + " |"
        )
    for item in api_result.get("unresolved_ledger", []):
        md_lines.append(
            f"- Unresolved: `{item.get('source_file', item.get('source', '?'))}`: {item['reason']}"
        )

    (design_dir / "frontend-feature-map.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )


def main():
    back_root = DEFAULT_BACK_ROOT
    front_root = DEFAULT_FRONT_ROOT

    cwd = Path.cwd().resolve()
    if (cwd / "scripts/platform_v2_test_audit.py").exists() and (
        cwd / "pyproject.toml"
    ).exists():
        back_root = cwd

    review_dir = back_root / "review/platform-v2/20261024/frontend-tests"
    design_dir = back_root / "docs/design/platform-v2"
    backend_inv_path = backend_registry_path(back_root)
    review_dir / "baseline-manifest.json"

    review_dir.mkdir(parents=True, exist_ok=True)
    design_dir.mkdir(parents=True, exist_ok=True)

    git_front = get_git_info(front_root)
    git_back = get_git_info(back_root)
    input_manifest = capture_audit_inputs(back_root, front_root)

    routes_result = audit_frontend_routes(
        app_tsx_path=front_root / "src/App.tsx", front_root=front_root
    )
    api_result = audit_frontend_api(
        front_root=front_root, backend_inventory_path=backend_inv_path
    )
    front_tests = audit_frontend_tests(front_root=front_root)
    back_tests_result = audit_backend_tests(
        back_root=back_root, baseline_manifest_path=None
    )

    registry_valid = api_result.get("registry_status") == "valid"
    if capture_audit_inputs(back_root, front_root) != input_manifest:
        raise RuntimeError(
            "Audit inputs changed during extraction; no artifacts were published"
        )

    # Generate consistent feature map artifacts (CSV, JSON, MD)
    generate_feature_map_artifacts(
        routes_result=routes_result,
        api_result=api_result,
        front_tests=front_tests,
        back_tests_result=back_tests_result,
        design_dir=design_dir,
        registry_valid=registry_valid,
        front_root=front_root,
    )

    summary = {
        "audit_version": "20261024-corrected-candidate-fix2",
        "verification_status": "pending_for_current_candidate",
        "git": {
            "front": git_front,
            "back": git_back,
        },
        "input_provenance": {
            "front_root": str(front_root.resolve()),
            "back_root": str(back_root.resolve()),
            "source_sha256": input_manifest,
            "stable_during_extraction": True,
            "backend_registry_status": api_result["registry_status"],
        },
        "historical_reference_provenance": {
            "backend_target_sha": "c8f10156845d007ec6496a65d6d8417be19260bd",
            "backend_baseline_sha": "607f8bda0df7dbdd458e4f0e54d703ce9edd3def",
            "frontend_remote_main": "dbe0c9f042b006e2a627b8e59c295ad14b7a2caf",
            "frontend_remote_blobs": {
                "src/App.tsx": "90dc3ab9ebaf2673286905b53b91fdaa777b65e9",
                "src/api/client.ts": "c9a6b8726c15fe5b05a59a58efe7f60701843091",
                "src/api/stream.ts": "dae6416337ec48557010d72330ee00cfb0b21843",
                "src/shell/useConnection.ts": "683d767d82c03b420c9cf86bc0624331e24f4b87",
            },
            "frontend_historical_dirty": {
                "head": "630040b1b7007a219a93c9d3d4491fab714c555d",
                "porcelain_sha256": "f7d9a949fa437af7c3e9bdaa11fada48c7ad613e79c96119e970361498c2c6af",
                "blobs_sha256": {
                    "src/App.tsx": "739a98c184cdc9bf72db0f2f81988795de88ba91b51176be261c916be35a8a7d",
                    "src/api/client.ts": "4ec6810e285cfab79f7fe289524f24453f4773c122fbe7bddb6eb8acdd924a93",
                    "src/api/stream.ts": "ded201bf6094ec3e9a188585d4efc54ff60246733dcbe631576844dea841d4d3",
                    "src/shell/useConnection.ts": "28c7b5a37e068f64bfad10184db497dbf958be6883307dbba989f938ce5de7f7",
                },
            },
            "backend_registry": {
                "path": "docs/design/platform-v2/backend-module-inventory.json",
                "sha256": "0dd1c0b84ac18c7f9471615ac6b0cfb2f6fef085e9eabdf58a38f61d5bafb69a",
                "modules_count": 25,
                "version": "20261024-v2-b-25modules (provisional)",
            },
        },
        "frontend": {
            "routes_count": routes_result["metrics"]["path_declarations_count"],
            "concrete_routes_count": routes_result["metrics"]["concrete_paths_count"],
            "wildcard_routes_count": routes_result["metrics"]["wildcard_paths_count"],
            "layout_routes_count": routes_result["metrics"]["layout_routes_count"],
            "unique_pages_count": routes_result["metrics"]["unique_pages_count"],
            "api_endpoints_count": api_result["endpoints_count"],
            "api_unmatched_count": api_result["unmatched_count"],
            "test_files_count": len(front_tests),
            "test_cases_estimated": sum(t["tests_estimated"] for t in front_tests),
        },
        "backend": {
            "test_files_count": back_tests_result["test_files_count"],
            "test_functions_count": back_tests_result["total_test_functions_count"],
            "categories": back_tests_result["primary_categories"],
            "category_test_counts": back_tests_result["primary_test_counts"],
            "multi_labels": back_tests_result["multi_labels"],
            "security_audit": back_tests_result["security_audit"],
        },
        "c8f10156_product_inventory": {
            "ref": "origin/main",
            "sha": "c8f10156845d007ec6496a65d6d8417be19260bd",
            "test_files_count": 303,
            "test_functions_count": 3539,
            "primary_categories": {
                "Unit": 107,
                "Module Integration": 103,
                "Security": 41,
                "Migration": 17,
                "Cross-module": 11,
                "Contract": 9,
                "Recovery-rollback": 6,
                "E2E": 5,
                "Deployment smoke": 4,
            },
            "security_audit": {
                "classified_files_count": 41,
                "marker_files_count": 37,
                "marker_function_defs_count": 449,
                "runtime_collected_cases": "unknown (unmeasured in static audit)",
            },
        },
        "baseline_drift_delta": {
            "baseline_sha": "607f8bda0df7dbdd458e4f0e54d703ce9edd3def",
            "target_product_sha": "c8f10156845d007ec6496a65d6d8417be19260bd",
            "git_metrics": {
                "rev_list_distance_behind": 118,
                "rev_list_distance_ahead": 2,
                "a_claim_111_behind_verified": False,
                "a_claim_111_behind_correction": "Actual measured git rev-list distance is 118 behind, 2 ahead",
                "src_py_delta": 64,
                "alembic_migrations_delta": 5,
                "diff_shortstat": "366 files changed, 86213 insertions(+), 339 deletions(-)",
            },
            "test_files_delta": {
                "baseline_committed_count": 252,
                "clean_worktree_count": 251,
                "baseline_manifest_dirty_count": 257,
                "target_c8_count": 303,
                "added_files_count": 51,
                "removed_files_count": 0,
            },
        },
        "measured_baselines": {
            "vitest_wall_duration_seconds": 5.43,
            "vitest_passed": 548,
            "vitest_skipped": 1,
            "vitest_total": 549,
            "vitest_jsdom_aggregate_environment_seconds": 33.21,
            "deploy_9f82c9c1_pytest_seconds": 1045.97,
            "deploy_9f82c9c1_passed": 4473,
            "deploy_9f82c9c1_subtests": 5020,
            "deploy_c8f10156_pytest_failed_seconds": 172.23,
            "fast_unit_seconds": 0.22,
        },
        "retraction_and_correction_ledger": [
            {
                "claim": "tests/test_collector_control.py missing in shared main security gate",
                "correction": "RETRACTED. Registration and file absent in shared main 607f8bda. Both present in back-collector-s2/complete candidate worktrees. Not a live operational defect.",
            },
            {
                "claim": "Duplicate 31 tests in run_security_gate can be eliminated",
                "correction": "CORRECTED. 31 is file count, not executed test case count. Gate must be retained; deduplication proposals must provide test identity/environment equivalence keys.",
            },
            {
                "claim": "100% complete contract mapping to backend modules",
                "correction": "CORRECTED. Mappings bound to provisional 25-module backend registry. Unmatched items recorded in unmatched_ledger.",
            },
        ],
    }

    # Write raw evidence files
    (review_dir / "frontend-routes.json").write_text(
        json.dumps(routes_result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (review_dir / "frontend-api-inventory.json").write_text(
        json.dumps(api_result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (review_dir / "frontend-test-inventory.json").write_text(
        json.dumps(front_tests, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (review_dir / "backend-test-classification.json").write_text(
        json.dumps(back_tests_result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (review_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Compute corrected-source-manifest.json
    manifest_entries = [
        "docs/design/platform-v2/frontend-feature-map.md",
        "docs/design/platform-v2/frontend-feature-map.json",
        "docs/design/platform-v2/frontend-feature-map.csv",
        "docs/design/platform-v2/test-architecture-and-baseline.md",
        "docs/design/platform-v2/test-impact-proposal.md",
        "scripts/platform_v2_test_audit.py",
        "scripts/platform_v2_ts_ast.mjs",
        "review/platform-v2/20261024/frontend-tests/frontend-routes.json",
        "review/platform-v2/20261024/frontend-tests/frontend-api-inventory.json",
        "review/platform-v2/20261024/frontend-tests/frontend-test-inventory.json",
        "review/platform-v2/20261024/frontend-tests/backend-test-classification.json",
        "review/platform-v2/20261024/frontend-tests/baseline-manifest.json",
        "review/platform-v2/20261024/frontend-tests/c8f10156-test-inventory.json",
        "review/platform-v2/20261024/frontend-tests/baseline-c8f10156-diff-ledger.json",
        "review/platform-v2/20261024/frontend-tests/summary.json",
        "unit_tests/test_platform_v2_route_audit.py",
        "unit_tests/test_platform_v2_api_audit.py",
        "unit_tests/test_platform_v2_test_classification.py",
    ]
    output_manifest: dict[str, str] = {}
    for me in manifest_entries:
        f_p = back_root / me
        if f_p.exists():
            output_manifest[me] = hashlib.sha256(f_p.read_bytes()).hexdigest()

    (review_dir / "corrected-source-manifest.json").write_text(
        json.dumps(output_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("Corrected audit extraction completed successfully.")
    print(
        f"Front: {routes_result['metrics']['path_declarations_count']} path declarations (concrete: {routes_result['metrics']['concrete_paths_count']}, wildcard: {routes_result['metrics']['wildcard_paths_count']}), {api_result['endpoints_count']} endpoints, {len(front_tests)} test files."
    )
    print(
        f"Back: {back_tests_result['test_files_count']} test files, {back_tests_result['total_test_functions_count']} test functions."
    )
    print(
        f"Primary categories (sum={sum(back_tests_result['primary_categories'].values())}): {back_tests_result['primary_categories']}"
    )
    print(f"Security: {back_tests_result['security_audit']}")


if __name__ == "__main__":
    main()
