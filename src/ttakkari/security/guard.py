"""에이전트 자동 실행용 보험 규칙.

에이전트는 승인 없이 돈다. 여기서는 "되돌릴 수 없는 사고"만 막는다.
셸 문자열 분석은 우회 가능한 휴리스틱이라 샌드박스가 아니다. 진짜 경계는 codex OS 샌드박스와
git 체크포인트, File Broker 반출 정책이다(docs/adr/0003 참고).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ttakkari.broker.policy import is_sensitive, is_within

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS"}

# (정규식, 사람이 읽을 이유). 단어 경계 기준, 대소문자 무시.
CATASTROPHIC = [
    (r"(^|[;&|(\s])(sudo|doas|su)\s", "관리자 권한 실행"),
    (r"\bmkfs(\.\w+)?\b", "파일시스템 포맷"),
    (r"\bdiskutil\s+(erase\w*|partitiondisk|reformat|zerodisk|securerase|randomdisk|apfs\s+(delete\w*|erase\w*))", "디스크 삭제·파티션 변경"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "블록 장치 직접 쓰기"),
    (r">\s*/dev/(r?disk|sd[a-z])", "블록 장치 직접 쓰기"),
    (r"(^|[;&|(\s])(shutdown|reboot|halt|poweroff)\b", "시스템 종료"),
    (r"\blaunchctl\s+(bootout|unload|remove|disable)\b", "시스템 서비스 해제"),
    (r"\b(csrutil|nvram|systemsetup)\b", "시스템 보안 설정 변경"),
    (r"\bspctl\s+--master-disable", "Gatekeeper 해제"),
    (r"\btmutil\s+(delete\w*|disable)", "Time Machine 백업 삭제"),
    (r"\bcrontab\s+-r\b", "crontab 전체 삭제"),
    (r"\bsecurity\s+(find-(generic|internet)-password|dump-keychain|export|delete-\w+)", "키체인 비밀 접근"),
    (r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b|\s--mirror\b|\s--delete\b|\s-d\b|\s\+\S)", "git 강제 푸시·원격 삭제"),
    (r"\bgit\s+push\b[^;&|]*\s:\S", "git 원격 브랜치 삭제"),
    (r"\bgit\s+(filter-branch|filter-repo)\b", "git 히스토리 재작성"),
    (r"\bgit\s+clean\b[^;&|]*\s-\w*f", "git 미추적 파일 일괄 삭제"),
    (r"\bgit\s+update-ref\s+-d\s+refs/ttakkari", "ttakkari 체크포인트 삭제"),
    (r"\bgh\s+(repo\s+delete|release\s+delete|api\s+.*(-X|--method)\s*DELETE)", "GitHub 원격 리소스 삭제"),
    (r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b", "원격 스크립트 파이프 실행"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;", "fork bomb"),
    (r"\bkillall\b", "프로세스 일괄 종료"),
    (r"\bpkill\b[^;&|]*(ttakkari|uvicorn|postgres|claude|codex)", "ttakkari 런타임 종료"),
    (r"\bbrew\s+services\s+(stop|kill)\s+postgres", "DB 서버 중지"),
    # NOTE: "DROP DATABASE/SCHEMA" 전역 정규식은 heredoc·스크립트 본문·grep까지
    # 막는 과차단(S-2·S-3·S-5·S-8)이라 삭제했다. 실제 실행은 아래 DB_CLIENT_VERBS
    # 검사(_check_sql_drop)가 맡는다. dropdb 명령어는 BLOCKED_VERBS에서 계속 차단.
    (r"\bfind\s+(/|~|\$HOME)(\s|$)[^;&|]*(-delete|-exec\s+rm)", "광역 find 삭제"),
    (r"\bchmod\s+-R\s+\S+\s+(/|~|\$HOME)(\s|$)", "광역 권한 변경"),
    (r"\bchown\s+-R\b[^;&|]*\s(/|~|\$HOME)(\s|$)", "광역 소유자 변경"),
]
_CATASTROPHIC_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in CATASTROPHIC]

# shlex 로 인용부호를 벗긴 뒤의 명령 이름으로 다시 본다(s'u'do 같은 우회).
BLOCKED_VERBS = {"sudo", "doas", "su", "shutdown", "reboot", "halt", "poweroff", "killall", "csrutil", "nvram",
                 "systemsetup", "dropdb"}

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# SQL을 직접 실행하는 DB 클라이언트. DROP 검사는 이들 동사가 명령에 실제로
# 쓰일 때만 차단한다. 파일 내용(-f 스크립트 등)은 셸 문자열에서 안 보이므로
# 검사 범위 밖이다 (가드는 휴리스틱, 샌드박스가 아님).
DB_CLIENT_VERBS = frozenset({
    "mysql", "mariadb", "psql", "sqlite3", "duckdb", "clickhouse",
    "mongosh", "mongo", "redis-cli", "sqlcmd", "isql",
})
_DROP_DB_RE = re.compile(r"\bdrop\s+(database|schema)\b", re.IGNORECASE)
# 파이프 뒤 env·command 같은 래퍼(| env mysql 우회)를 허용한다. 직접·기본 파이프는 그대로 막힌다.
_PIPE_PREFIX_RE = r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+|sudo\s+|env\s+|command\s+|exec\s+|nohup\s+|time\s+)*"
_PIPE_TO_CLIENT_RE = re.compile(
    r"\|\s*" + _PIPE_PREFIX_RE + r"(" + "|".join(sorted(DB_CLIENT_VERBS)) + r")\b",
    re.IGNORECASE,
)
# SQL='DROP DATABASE ...'; mysql -e "$SQL" 같은 변수 경유 우회용.
# 대입이 속한 셸 조각에 DROP이 있으면 그 변수명을 기록한다 (휴리스틱).
_ASSIGN_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\+?=")


def _strip_prefix(argv: list[str]) -> list[str]:
    while len(argv) > 1 and _ASSIGN_RE.match(argv[0]):
        argv = argv[1:]
    verb = Path(argv[0]).name
    if verb in ("env", "command", "exec", "nohup", "time"):
        argv = argv[1:] or argv
    return argv


def _vars_assigned_with_drop(command: str) -> set[str]:
    """값 조각에 DROP이 든 대입의 변수명 모음. 개행·;&|()까지를 한 조각으로 본다."""
    names: set[str] = set()
    for m in _ASSIGN_NAME_RE.finditer(command):
        end = len(command)
        for i in range(m.end(), len(command)):
            if command[i] in ";&|()\n":
                end = i
                break
        if _DROP_DB_RE.search(command[m.start() : end]):
            names.add(m.group(1))
    return names


def _refers_to_var(text: str, name: str) -> bool:
    return bool(
        re.search(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])" + r"|\$\{" + re.escape(name) + r"(?![A-Za-z0-9_])", text)
    )


def _check_sql_drop(command: str) -> str | None:
    """DROP DATABASE/SCHEMA 문자열이 DB 클라이언트 실행과 붙을 때만 막는다.

    허용: heredoc·스크립트 본문, grep/log 검색, -f 파일 지정(내용 미확인).
    차단: mysql -e "..." 직접 실행, 파이프로 클라이언트에 먹이기, dropdb는 별도,
    변수에 담은 DROP을 클라이언트 인자로 넘기기("$SQL" 경유).
    """
    if not _DROP_DB_RE.search(command):
        return None
    split = [_strip_prefix(argv) for argv in _split_commands(command)]
    for argv in split:
        if _DROP_DB_RE.search(" ".join(argv)) and {Path(w).name for w in argv} & DB_CLIENT_VERBS:
            return "고위험 명령 차단(DB 통째 삭제)"
    for name in _vars_assigned_with_drop(command):
        for argv in split:
            if {Path(w).name for w in argv} & DB_CLIENT_VERBS and _refers_to_var(" ".join(argv), name):
                return "고위험 명령 차단(DB 통째 삭제)"
    if _PIPE_TO_CLIENT_RE.search(command):
        return "고위험 명령 차단(DB 통째 삭제)"
    return None

READ_VERBS = {"cat", "less", "more", "head", "tail", "bat", "strings", "base64", "xxd", "od", "hexdump", "cp", "scp",
              "rsync", "curl", "wget", "nc", "tar", "zip", "open", "grep", "rg", "awk", "sed", "source", "."}


@dataclass
class GuardConfig:
    write_roots: list[Path] = field(default_factory=list)
    protected: list[Path] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> GuardConfig:
        def paths(var: str) -> list[Path]:
            return [Path(p).resolve() for p in os.environ.get(var, "").split(os.pathsep) if p]

        return cls(write_roots=paths("TTAKKARI_GUARD_WRITE_ROOTS"), protected=paths("TTAKKARI_GUARD_PROTECTED"))


def _resolve(raw: str, cwd: Path) -> Path:
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = cwd / p
    return Path(os.path.realpath(p))


def _in_write_roots(p: Path, cfg: GuardConfig) -> bool:
    return any(is_within(p, r) for r in cfg.write_roots)


def _protected(p: Path, cfg: GuardConfig) -> bool:
    return any(is_within(p, r) for r in cfg.protected) and not _in_write_roots(p, cfg)


def check_path_tool(tool: str, tool_input: dict, cwd: Path, cfg: GuardConfig) -> str | None:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not raw or not isinstance(raw, str):
        return None
    p = _resolve(raw, cwd)
    if _protected(p, cfg):
        return f"ttakkari 보호 영역 접근 차단: {raw}"
    if is_sensitive(p):
        return f"민감 파일 접근 차단: {raw}"
    if tool in WRITE_TOOLS and cfg.write_roots and not _in_write_roots(p, cfg):
        return f"워크스페이스 밖 쓰기 차단: {raw}"
    return None


def _split_commands(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    cmds: list[list[str]] = [[]]
    try:
        for tok in lexer:
            if tok and set(tok) <= set(";&|()"):
                cmds.append([])
            else:
                cmds[-1].append(tok)
    except ValueError:
        return [command.split()]
    return [c for c in cmds if c]


def _check_rm(argv: list[str], cwd: Path, cfg: GuardConfig) -> str | None:
    flags = [a for a in argv[1:] if a.startswith("-")]
    recursive = any(a in ("--recursive",) or (not a.startswith("--") and ("r" in a or "R" in a)) for a in flags)
    if not recursive:
        return None
    targets = [a for a in argv[1:] if not a.startswith("-")]
    for t in targets:
        if any(ch in t for ch in "$`") or t in ("*", "/*", "~", "~/", "/", ".", "./", ".."):
            return f"해석 불가하거나 광역인 재귀 삭제 대상: {t}"
        p = _resolve(t, cwd)
        if not _in_write_roots(p, cfg):
            return f"워크스페이스 밖 재귀 삭제 차단: {t}"
        if any(p == r for r in cfg.write_roots):
            return f"워크스페이스 루트 자체 삭제 차단: {t}"
    return None


def check_bash(command: str, cwd: Path, cfg: GuardConfig) -> str | None:
    for rx, why in _CATASTROPHIC_RE:
        if rx.search(command):
            return f"고위험 명령 차단({why})"
    if reason := _check_sql_drop(command):
        return reason
    for argv in _split_commands(command):
        argv = _strip_prefix(argv)
        verb = Path(argv[0]).name
        if verb in BLOCKED_VERBS or verb.startswith("mkfs"):
            return f"고위험 명령 차단({verb})"
        if argv[0].startswith("$") or "`" in argv[0]:
            return "명령 이름을 변수·치환으로 구성한 실행 차단"
        if verb == "rm":
            reason = _check_rm(argv, cwd, cfg)
            if reason:
                return reason
        for tok in argv[1:]:
            if tok.startswith("-") or "/" not in tok and "." not in tok:
                continue
            p = _resolve(tok.split("=", 1)[-1], cwd)
            if _protected(p, cfg):
                return f"ttakkari 보호 영역 접근 차단: {tok}"
            if verb in READ_VERBS and is_sensitive(p):
                return f"민감 파일 읽기·반출 차단: {tok}"
    return None


def check_mcp_paths(tool_input: object, cwd: Path, cfg: GuardConfig, depth: int = 0) -> str | None:
    """ArtCraft MCP 도구 인자의 절대 경로가 보호 영역·민감 파일이면 막는다. 경로를 가두는 옵션이 없는 앱의 보험이다."""
    if depth > 6:
        return None
    if isinstance(tool_input, str):
        if "\x00" in tool_input or not (tool_input.startswith(("/", "~/")) and len(tool_input) < 4096):
            return None
        p = _resolve(tool_input, cwd)
        if _protected(p, cfg):
            return f"ttakkari 보호 영역 접근 차단: {tool_input}"
        if is_sensitive(p):
            return f"민감 파일 접근 차단: {tool_input}"
    elif isinstance(tool_input, dict):
        for value in tool_input.values():
            if reason := check_mcp_paths(value, cwd, cfg, depth + 1):
                return reason
    elif isinstance(tool_input, list):
        for value in tool_input[:200]:
            if reason := check_mcp_paths(value, cwd, cfg, depth + 1):
                return reason
    return None


def evaluate(tool: str, tool_input: dict, cwd: str | None, cfg: GuardConfig) -> str | None:
    base = Path(cwd or os.getcwd())
    if tool.startswith("mcp__artcraft_"):
        return check_mcp_paths(tool_input, base, cfg)
    if tool == "Bash":
        return check_bash(str(tool_input.get("command", "")), base, cfg)
    if tool in WRITE_TOOLS or tool in READ_TOOLS:
        return check_path_tool(tool, tool_input, base, cfg)
    return None
