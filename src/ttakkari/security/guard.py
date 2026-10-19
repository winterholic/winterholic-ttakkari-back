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

from ttakkari.broker.policy import is_sensitive, is_sensitive_name, is_within

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
# ${!x} 간접 참조·${a[0]} 배열 확장의 목적지는 정적 분석으로 알 수 없다.
# DROP을 담은 변수가 있고 클라이언트 인자에 이런 확장이 있으면 보수적으로 막는다.
_INDIRECT_REF_RE = re.compile(r"\$\{![A-Za-z_][A-Za-z0-9_]*\}|\$![A-Za-z_][A-Za-z0-9_]*")
_ARRAY_REF_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\[|\$\{[A-Za-z_][A-Za-z0-9_]*\[")
# x=SQL 같은 별칭 대입. 값이 DROP 변수명과 같으면 별칭도 DROP 변수로 본다.
_ALIAS_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)")

# MongoDB·Redis·전체 삭제. SQL DROP과 별도 정책이다.
_MONGO_CLIENTS = frozenset({"mongosh", "mongo"})
_MONGO_DROP_RE = re.compile(r"\.\s*(drop|dropDatabase|dropCollection)\s*\(", re.IGNORECASE)
_REDIS_FLUSH_RE = re.compile(r"\bFLUSH(ALL|DB)\b", re.IGNORECASE)
_TRUNCATE_RE = re.compile(r"\btruncate\s+(table\s+)?[^\s;]+", re.IGNORECASE)
_DELETE_FROM_RE = re.compile(r"\bdelete\s+from\s+[^\s;]+", re.IGNORECASE)
_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)

# 중첩 실행 추적용. 래퍼를 벗긴 뒤 실제 동사를 보고 -c·-e·--eval 문자열을 재귀 검사한다.
_WRAPPER_VERBS = frozenset({"sudo", "doas", "su", "nice", "timeout", "env", "command", "exec", "nohup", "time"})
_NESTED_SHELL_VERBS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_INTERPRETER_VERBS = frozenset({"python", "python3", "node", "nodejs", "perl"})
_CONTAINER_VERBS = frozenset({"docker", "podman", "kubectl", "nerdctl"})
_MAX_NESTING = 4
_CLIENT_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_QUOTED_LIT_RE = re.compile(r"""["']([^"'\\]{1,512})["']""")


def _strip_prefix(argv: list[str]) -> list[str]:
    while len(argv) > 1 and _ASSIGN_RE.match(argv[0]):
        argv = argv[1:]
    verb = Path(argv[0]).name
    if verb in ("env", "command", "exec", "nohup", "time"):
        argv = argv[1:] or argv
    return argv


def _unwrap_for_analysis(argv: list[str]) -> list[str]:
    """중첩·파이프 분석용으로만 래퍼를 벗긴다. 허용·차단 판정에는 쓰지 않는다.

    sudo를 벗겨도 바깥 check_bash의 catastrophic 검사는 원문 기준으로 먼저 돌았으므로
    sudo 자체 차단은 유지된다.
    """
    a = list(argv)
    while len(a) > 1:
        if _ASSIGN_RE.match(a[0]):
            a = a[1:]
            continue
        verb = Path(a[0]).name
        if verb not in _WRAPPER_VERBS:
            break
        a = a[1:]
        while len(a) > 1 and a[0].startswith("-") and a[0] != "-":
            if a[0] in ("-u", "--user", "-g", "--group") and len(a) > 2:
                a = a[2:]
            else:
                a = a[1:]
        if verb == "timeout":
            while len(a) > 1 and re.fullmatch(r"\d+[smhd]?", a[0]):
                a = a[1:]
    return a


def _split_stages(command: str) -> list[str]:
    """인용부호 안의 | 를 건드리지 않고 파이프 단계별 원문으로 나눈다."""
    stages: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            buf.append(ch)
            quote = ch
            continue
        if ch == "|":
            stages.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    stages.append("".join(buf))
    return stages


def _lex_stage(stage: str) -> list[str]:
    try:
        return shlex.split(stage, posix=True)
    except ValueError:
        return stage.split()


def _stage_invokes_client(argv: list[str]) -> bool:
    """이 단계가 DB 클라이언트를 직접 실행하는지. xargs·컨테이너 경유 포함."""
    a = _unwrap_for_analysis(argv)
    if not a:
        return False
    verb = Path(a[0]).name.lower()
    if verb in DB_CLIENT_VERBS:
        return True
    if verb == "xargs" or verb in _CONTAINER_VERBS:
        return any(Path(w).name.lower() in DB_CLIENT_VERBS for w in a[1:])
    return False


def _command_verbs(split: list[list[str]]) -> set[str]:
    verbs: set[str] = set()
    for argv in split:
        a = _unwrap_for_analysis(argv)
        if not a:
            continue
        verbs.add(Path(a[0]).name.lower())
        if Path(a[0]).name.lower() == "xargs" or Path(a[0]).name.lower() in _CONTAINER_VERBS:
            verbs.update(Path(w).name.lower() for w in a[1:])
    return verbs


def _mentioned_clients(text: str) -> set[str]:
    words = {w.lower() for w in _CLIENT_WORD_RE.findall(text)}
    return {c for c in DB_CLIENT_VERBS if c.lower() in words}


def _nested_payloads(argv: list[str]) -> list[str]:
    """bash -c·sh -c·eval이 실행하는 문자열. 래퍼 뒤에서 찾는다."""
    a = _unwrap_for_analysis(argv)
    if not a:
        return []
    verb = Path(a[0]).name
    if verb in _NESTED_SHELL_VERBS:
        if "-c" in a[1:]:
            i = a.index("-c")
            if i + 1 < len(a):
                return [a[i + 1]]
        return []
    if verb == "eval":
        return [" ".join(a[1:])] if len(a) > 1 else []
    return []


def _code_payloads(argv: list[str]) -> list[str]:
    """python/node/perl -c·-e·--eval 코드 문자열."""
    a = _unwrap_for_analysis(argv)
    if not a or Path(a[0]).name not in _INTERPRETER_VERBS:
        return []
    for flag in ("-c", "-e", "--eval"):
        if flag in a[1:]:
            i = a.index(flag)
            if i + 1 < len(a):
                return [a[i + 1]]
    return []


def _payload_refs_sensitive(payload: str, cwd: Path) -> str | None:
    """인터프리터 코드 안의 인용 문자열이 민감 파일을 가리키면 막는다."""
    for lit in _QUOTED_LIT_RE.findall(payload):
        if is_sensitive_name(Path(lit)):
            return f"민감 파일 읽기·반출 차단: {lit}"
        p = _resolve(lit, cwd)
        try:
            exists = p.is_file()
        except OSError:
            exists = False
        if exists and is_sensitive(p):
            return f"민감 파일 읽기·반출 차단: {lit}"
    return None


def _check_text_destruction(text: str, clients: set[str]) -> str | None:
    """단어 기준 파괴적 실행 판정. 인터프리터 코드처럼 셸 분해가 안 되는 텍스트용."""
    if not clients:
        return None
    if _DROP_DB_RE.search(text):
        return "고위험 명령 차단(DB 통째 삭제)"
    if (clients & _MONGO_CLIENTS) and _MONGO_DROP_RE.search(text):
        return "고위험 명령 차단(MongoDB 통째 삭제)"
    if "redis-cli" in clients and _REDIS_FLUSH_RE.search(text):
        return "고위험 명령 차단(Redis 전체 삭제)"
    if _TRUNCATE_RE.search(text):
        return "고위험 명령 차단(전체 테이블 삭제)"
    if _DELETE_FROM_RE.search(text) and not _WHERE_RE.search(text):
        return "고위험 명령 차단(조건 없는 전체 삭제)"
    return None


def _check_code_payload(payload: str, cwd: Path) -> str | None:
    if reason := _check_text_destruction(payload, _mentioned_clients(payload)):
        return reason
    return _payload_refs_sensitive(payload, cwd)


def _check_nosql_destructive(command: str, verbs: set[str]) -> str | None:
    if (verbs & _MONGO_CLIENTS) and _MONGO_DROP_RE.search(command):
        return "고위험 명령 차단(MongoDB 통째 삭제)"
    if "redis-cli" in verbs and _REDIS_FLUSH_RE.search(command):
        return "고위험 명령 차단(Redis 전체 삭제)"
    if verbs & DB_CLIENT_VERBS:
        if _TRUNCATE_RE.search(command):
            return "고위험 명령 차단(전체 테이블 삭제)"
        if _DELETE_FROM_RE.search(command) and not _WHERE_RE.search(command):
            return "고위험 명령 차단(조건 없는 전체 삭제)"
    return None


def _stage_has_input_redirect(argv: list[str]) -> bool:
    for tok in argv[1:]:
        if tok == "<" or (tok.startswith("<") and not tok.startswith("<<")):
            return True
    if argv and re.search(r"<(?!<)", Path(argv[0]).name):
        return True
    return False


def _check_client_stdin(command: str) -> str | None:
    """DB 클라이언트로 들어가는 파이프·입력 리다이렉션은 내용과 무관하게 막는다.

    파이프·파일 입력의 실제 SQL은 셸 문자열에서 안 보인다. printf 조각 조립·base64
    디코드·xargs 경유 같은 런타임 생성은 모양으로만 막고, 디코딩 내용까지는
    들여다보지 않는다(구조적 한계).
    """
    stages = [s for s in _split_stages(command) if s.strip()]
    for stage in stages:
        argv = _lex_stage(stage)
        a = _unwrap_for_analysis(argv)
        if a and Path(a[0]).name.lower() in DB_CLIENT_VERBS and _stage_has_input_redirect(argv):
            return "고위험 명령 차단(DB 클라이언트로 파일 입력 전달)"
        if a and Path(a[0]).name.lower() in _CONTAINER_VERBS and _stage_has_input_redirect(argv):
            if any(Path(w).name.lower() in DB_CLIENT_VERBS for w in a[1:]):
                return "고위험 명령 차단(DB 클라이언트로 파일 입력 전달)"
    if len(stages) > 1:
        for stage in stages[1:]:
            if _stage_invokes_client(_lex_stage(stage)):
                return "고위험 명령 차단(DB 클라이언트로 파이프 입력 전달)"
    if _DROP_DB_RE.search(command):
        for stage in stages[1:]:
            a = _unwrap_for_analysis(_lex_stage(stage))
            if a and Path(a[0]).name.lower() == "xargs":
                return "고위험 명령 차단(DB 통째 삭제)"
    return None


def _vars_assigned_with_drop(command: str) -> set[str]:
    """값 조각에 DROP이 든 대입의 변수명 모음. 배열 대입 a=(...)은 닫는 괄호까지가 값이다."""
    names: set[str] = set()
    for m in _ASSIGN_NAME_RE.finditer(command):
        end = len(command)
        depth = 0
        for i in range(m.end(), len(command)):
            ch = command[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif depth == 0 and ch in ";&|\n":
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
    drop_names = _vars_assigned_with_drop(command)
    if drop_names:
        aliases = {m.group(1) for m in _ALIAS_ASSIGN_RE.finditer(command) if m.group(2) in drop_names}
        targets = drop_names | aliases
        for argv in split:
            if not {Path(w).name for w in argv} & DB_CLIENT_VERBS:
                continue
            text = " ".join(argv)
            if any(_refers_to_var(text, name) for name in targets):
                return "고위험 명령 차단(DB 통째 삭제)"
            # ${!x}·${a[0]}의 실제 목적지는 알 수 없으니 DROP 변수와 함께면 막는다.
            if _INDIRECT_REF_RE.search(text) or _ARRAY_REF_RE.search(text):
                return "고위험 명령 차단(DB 통째 삭제)"
            if any(re.search(r"\$\{!" + re.escape(name) + r"\}", text) for name in aliases):
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


def check_bash(command: str, cwd: Path, cfg: GuardConfig, _depth: int = 0) -> str | None:
    for rx, why in _CATASTROPHIC_RE:
        if rx.search(command):
            return f"고위험 명령 차단({why})"
    if reason := _check_sql_drop(command):
        return reason
    split = _split_commands(command)
    if reason := _check_nosql_destructive(command, _command_verbs(split)):
        return reason
    if reason := _check_client_stdin(command):
        return reason
    if _depth < _MAX_NESTING:
        for argv in split:
            for payload in _nested_payloads(argv):
                if reason := check_bash(payload, cwd, cfg, _depth + 1):
                    return reason
            for payload in _code_payloads(argv):
                if reason := _check_code_payload(payload, cwd):
                    return reason
                if reason := check_bash(payload, cwd, cfg, _depth + 1):
                    return reason
    for argv in split:
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
            if (verb in READ_VERBS or verb in _INTERPRETER_VERBS) and is_sensitive(p):
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
