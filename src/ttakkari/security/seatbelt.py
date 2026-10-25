"""엔진 프로세스를 macOS Seatbelt(sandbox-exec) 아래에서 띄우는 OS 수준 경계(docs/security/adr-engine-seatbelt.md).

guard.py 는 명령 문자열 블록리스트라 `bash -c`, `eval`, Python 같은 실행기를 거치면 뚫린다
(/tmp/ttk-probe-report.md). 여기서는 커널이 판정하므로 실행 경로와 상관없이 막힌다.

- 기본 허용(allow default) 위에 거부 규칙을 얹는다. 엔진 CLI 가 쓰는 mach 서비스·키체인·
  노드 런타임을 일일이 열지 않기 위해서다. 남은 위험은 ADR 에 적었다.
- SBPL 은 나중 규칙이 이긴다. 그래서 순서가 의미를 가진다: ttakkari 홈 거부 → 허용 예외 →
  비밀 경로 거부(어떤 예외보다 뒤라 항상 이김) → .env 예시 파일 재허용.
- codex·antigravity·qwen-local·muse 는 자체 샌드박스를 건다. 바깥에서 한 번 더 감싸면 안쪽
  sandbox_apply 가 EPERM 으로 실패하므로(codex 실측, 나머지는 코드 근거) 감싸지 않는다.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# 자체 샌드박스가 없는 엔진만. muse 는 네이티브 샌드박스가 available 이어야 뜨므로(.muse-runtime/contract.mjs) 뺐다.
WRAPPABLE_ENGINES = frozenset({"claude"})

# PostgreSQL, MySQL/MariaDB, Redis, MongoDB. 원격 호스트도 같은 포트면 막는다.
DB_PORTS = (5432, 5433, 3306, 6379, 27017)

DB_SOCKET_REGEXES = (
    r"/\.s\.PGSQL\.[0-9]+$",
    r"/mysqld?\.sock$",
    r"/redis[^/]*\.sock$",
    r"/mongodb-[0-9]+\.sock$",
)

# 홈 기준 상대 경로. 디렉터리는 subpath, 파일은 literal 로 쓴다.
SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".kube", ".config/gh", ".config/gcloud", ".azure", ".password-store",
               ".gemini", ".codex", ".docker", ".opencode")
SECRET_FILES = (".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc", ".my.cnf")
# 계정 폴더 없이 뜨는 기본 claude 계정은 이 파일로 인증한다. 그때만 열어 둔다.
CLAUDE_DEFAULT_CREDENTIALS = ".claude/.credentials.json"
KEYCHAIN_DIR = "Library/Keychains"

ENV_FILE_REGEX = r"/\.env(\.[^/]+)?$"
ENV_EXAMPLE_REGEX = r"/\.env\.(example|sample|template|dist)$"


@dataclass
class SeatbeltPolicy:
    home: Path
    ttakkari_home: Path
    # ttakkari 홈 안에서 읽기·쓰기를 다시 여는 디렉터리와 낱개 파일.
    allowed: list[Path] = field(default_factory=list)
    allowed_files: list[Path] = field(default_factory=list)
    # 배포된 현재 릴리스의 Python·모듈만 읽는다. 계정/설정이나 다른 릴리스는 열지 않는다.
    runtime_root: Path | None = None
    # 워크스페이스의 전용 테스트 DB 디렉터리만 허용한다. 일반 쓰기 루트는 쓰지 않는다.
    socket_roots: list[Path] = field(default_factory=list)
    deny_keychain: bool = False
    secret_files: tuple[str, ...] = SECRET_FILES


def _q(path: Path | str) -> str:
    """SBPL 문자열 리터럴. 이스케이프 규칙에 기대지 않고 위험 문자를 아예 거부한다."""
    s = str(path)
    if not s.startswith("/") or any(c in s for c in '"\\\n\r\0'):
        raise ValueError(f"Seatbelt 프로파일에 넣을 수 없는 경로: {s!r}")
    return f'"{s}"'


def _real(path: Path) -> Path:
    # Seatbelt 는 실제 경로로 판정한다(/tmp → /private/tmp). 상대 경로는 백엔드 cwd 에 붙어 엉뚱한 곳을 연다.
    if not Path(path).is_absolute():
        raise ValueError(f"Seatbelt 프로파일에 상대 경로를 넣을 수 없다: {path!r}")
    return Path(os.path.realpath(path))


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def render_profile(policy: SeatbeltPolicy) -> str:
    home = _real(policy.home)
    ttk = _real(policy.ttakkari_home)
    lines = [
        "(version 1)",
        "(allow default)",
        ";; DB 네트워크",
        "(deny network-outbound",
        *(f'  (remote ip "*:{port}")' for port in DB_PORTS),
        ")",
        "(deny network-outbound (remote unix-socket",
        *(f'  (regex #"{rx}")' for rx in DB_SOCKET_REGEXES),
        "))",
    ]
    roots = sorted({_real(p) for p in policy.socket_roots})
    if roots:
        lines.append("(allow network-outbound (remote unix-socket")
        lines += [f"  (subpath {_q(r)})" for r in roots]
        lines.append("))")

    lines += [";; ttakkari 홈: 다른 계정 인증, config.env, 백업", f"(deny file-read-data file-write* (subpath {_q(ttk)}))"]
    allowed = sorted({p for p in (_real(a) for a in policy.allowed) if _under(p, ttk) and p != ttk})
    allowed_files = sorted({p for p in (_real(a) for a in policy.allowed_files) if _under(p, ttk) and p != ttk})
    if allowed or allowed_files:
        lines.append("(allow file-read-data file-write*")
        lines += [f"  (subpath {_q(p)})" for p in allowed]
        lines += [f"  (literal {_q(p)})" for p in allowed_files]
        lines.append(")")
    if policy.runtime_root is not None:
        lines.append(f"(allow file-read-data (subpath {_q(_real(policy.runtime_root))}))")

    # 심링크로 옮긴 저장소도 실제 대상 경로를 막는다. 기존 이름도 함께 닫아 둔다.
    secret_dirs = {p for d in SECRET_DIRS for p in (home / d, _real(home / d))}
    if policy.deny_keychain:
        secret_dirs.update((home / KEYCHAIN_DIR, _real(home / KEYCHAIN_DIR)))
    secret_files = {p for f in policy.secret_files for p in (home / f, _real(home / f))}
    lines += [
        ";; 비밀 경로: 위 예외보다 뒤에 둬서 항상 거부",
        "(deny file-read-data file-write*",
        *(f"  (subpath {_q(d)})" for d in sorted(secret_dirs)),
        *(f"  (literal {_q(p)})" for p in sorted(secret_files)),
        f'  (regex #"{ENV_FILE_REGEX}")',
        ")",
        # 예시 파일 예외로 다른 계정/자격 증명 디렉터리를 다시 열지 않는다.
        f'(allow file-read-data file-write* (require-all (regex #"{ENV_EXAMPLE_REGEX}")',
        *(f"  (require-not (subpath {_q(d)}))" for d in sorted(secret_dirs | {ttk})),
        *(f"  (require-not (literal {_q(p)}))" for p in sorted(secret_files)),
        "))",
    ]
    return "\n".join(lines) + "\n"


def wrap(cmd: list[str], profile: str) -> list[str]:
    # -p 로 넘겨 임시 파일 정리를 없앤다. 프로파일에는 경로만 있고 비밀은 없다.
    return [SANDBOX_EXEC, "-p", profile, *cmd]


def policy_for(*, home: Path, ttakkari_home: Path, outbox: Path, write_roots: Iterable[Path],
               env: Mapping[str, str], engine: str = "", deny_keychain: bool = False,
               workspace: Path | None = None) -> SeatbeltPolicy:
    home, ttk, outbox = _real(home), _real(ttakkari_home), _real(outbox)
    write_roots = list(write_roots)
    # write_roots/XDG/CODEX_HOME 은 다른 계정이나 설정을 여는 근거가 아니다.
    # ttakkari 안에서는 이 Run의 산출물과 업로드/인박스만 디렉터리로 다시 연다.
    transfer_roots = (ttk / "data/outbox", ttk / "data/uploads", ttk / "inbox")
    if _under(outbox, ttk) and not (_under(outbox, transfer_roots[0]) and outbox != transfer_roots[0]):
        raise ValueError("Seatbelt: ttakkari 내부 outbox는 data/outbox/<run> 아래여야 한다")
    allowed = [p for p in map(_real, [outbox, *write_roots]) if
               (_under(p, outbox) or any(_under(p, r) for r in transfer_roots[1:]))
               and _under(p, ttk) and p != ttk]
    config_dir = _real(Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")) if engine == "claude" else None
    if config_dir is not None:
        accounts = ttk / "accounts"
        protected = [home, ttk, accounts, *(_real(home / d) for d in SECRET_DIRS)]
        if any(_under(p, config_dir) for p in protected) or any(
            _under(config_dir, _real(home / d)) for d in SECRET_DIRS
        ) or (_under(config_dir, ttk) and not _under(config_dir, accounts)):
            raise ValueError("Seatbelt: CLAUDE_CONFIG_DIR는 독립된 Claude 계정 폴더여야 한다")
        allowed.append(config_dir)
    allowed_files = []
    if env.get("TTAKKARI_GUARD_LOG"):
        # PreToolUse 훅이 엔진의 자식이라 같은 샌드박스에서 가드 로그를 쓴다.
        log = _real(Path(env["TTAKKARI_GUARD_LOG"]))
        if _under(log, ttk) and log != ttk / "data/guard.log":
            raise ValueError("Seatbelt: ttakkari 내부 가드 로그는 data/guard.log여야 한다")
        allowed_files.append(log)
    secret_files = SECRET_FILES
    if config_dir != _real(home / ".claude"):
        secret_files += (CLAUDE_DEFAULT_CREDENTIALS,)
    # 훅과 stdio MCP는 이 백엔드의 Python으로 뜬다. .ttakkari 전체 거부가 배포
    # 릴리스의 venv/site-packages까지 막지 않도록 현재 코드 위치에서만 예외를 만든다.
    # 환경 변수·write_roots에서 런타임 경로를 받으면 다른 계정을 열 수 있으므로 사용하지 않는다.
    runtime_root = _real(Path(__file__).parents[3])
    if _under(runtime_root, ttk):
        if runtime_root.parent != ttk / "deploy/releases" or not re.fullmatch(r"[0-9a-f]{40}", runtime_root.name):
            raise ValueError("Seatbelt: ttakkari 내부 runtime은 deploy/releases/<SHA>여야 한다")
    else:
        runtime_root = None
    socket_roots = []
    if workspace is not None:
        workspace = _real(workspace)
        socket_root = _real(workspace / ".ttakkari-db-sockets")
        if not _under(socket_root, workspace) or socket_root == workspace:
            raise ValueError("Seatbelt: test DB socket 디렉터리는 workspace 안이어야 한다")
        socket_roots.append(socket_root)
    return SeatbeltPolicy(home=home, ttakkari_home=ttk, allowed=allowed, allowed_files=allowed_files,
                          runtime_root=runtime_root, socket_roots=socket_roots,
                          deny_keychain=deny_keychain, secret_files=secret_files)


def maybe_wrap(engine: str, cmd: list[str], *, enabled_engines: Iterable[str], home: Path,
               ttakkari_home: Path, outbox: Path, write_roots: Iterable[Path],
               env: Mapping[str, str], deny_keychain: bool = False, workspace: Path | None = None) -> list[str]:
    """기능 플래그에 든 엔진이면 sandbox-exec 로 감싼다. 감쌀 수 없으면 샌드박스 없이 띄우지 않는다."""
    if engine not in set(enabled_engines):
        return cmd
    if engine not in WRAPPABLE_ENGINES:
        raise RuntimeError(f"Seatbelt 래퍼를 {engine} 에 적용할 수 없다(자체 샌드박스와 중첩 불가). "
                           "TTAKKARI_ENGINE_SEATBELT 에서 빼라")
    if not Path(SANDBOX_EXEC).is_file():
        raise RuntimeError("sandbox-exec 가 없어 Seatbelt 를 적용할 수 없다")
    policy = policy_for(home=home, ttakkari_home=ttakkari_home, outbox=outbox, write_roots=write_roots,
                        env=env, engine=engine, deny_keychain=deny_keychain, workspace=workspace)
    return wrap(cmd, render_profile(policy))
