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
    (r"\bdropdb\b|\bdrop\s+(database|schema)\b", "DB 통째 삭제"),
    (r"\bfind\s+(/|~|\$HOME)(\s|$)[^;&|]*(-delete|-exec\s+rm)", "광역 find 삭제"),
    (r"\bchmod\s+-R\s+\S+\s+(/|~|\$HOME)(\s|$)", "광역 권한 변경"),
    (r"\bchown\s+-R\b[^;&|]*\s(/|~|\$HOME)(\s|$)", "광역 소유자 변경"),
]
_CATASTROPHIC_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in CATASTROPHIC]

# shlex 로 인용부호를 벗긴 뒤의 명령 이름으로 다시 본다(s'u'do 같은 우회).
BLOCKED_VERBS = {"sudo", "doas", "su", "shutdown", "reboot", "halt", "poweroff", "killall", "csrutil", "nvram",
                 "systemsetup", "dropdb"}

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

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
    for argv in _split_commands(command):
        while len(argv) > 1 and _ASSIGN_RE.match(argv[0]):
            argv = argv[1:]
        verb = Path(argv[0]).name
        if verb == "env" or verb == "command" or verb == "exec" or verb == "nohup" or verb == "time":
            argv = argv[1:] or argv
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


def evaluate(tool: str, tool_input: dict, cwd: str | None, cfg: GuardConfig) -> str | None:
    base = Path(cwd or os.getcwd())
    if tool == "Bash":
        return check_bash(str(tool_input.get("command", "")), base, cfg)
    if tool in WRITE_TOOLS or tool in READ_TOOLS:
        return check_path_tool(tool, tool_input, base, cfg)
    return None
