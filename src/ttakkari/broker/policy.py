"""File Broker 경로 정책.

모든 파일 접근은 여기서 (루트, 상대경로) → 검증된 실제 경로로 바뀐다.
에이전트나 클라이언트가 준 경로 문자열은 신뢰하지 않는다.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# 반출(다운로드·외부 전송) 기본 차단 대상. 소문자 경로 조각 기준으로 매칭한다.
SENSITIVE_NAME_GLOBS = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "*.kdbx",
    "*.keychain",
    "*.keychain-db",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "*credentials*.json",
    "service-account*.json",
    "*secret*",
    "*.tfstate",
    "*.tfvars",
)
SENSITIVE_DIR_NAMES = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".gcloud", ".config/gcloud", ".git")


class BrokerError(Exception):
    """정책 위반. 메시지는 사용자에게 그대로 보여도 되는 수준으로만 쓴다."""

    def __init__(self, message: str, code: str = "forbidden"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedPath:
    root: Path
    path: Path

    @property
    def rel(self) -> str:
        return self.path.relative_to(self.root).as_posix() or "."


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


# 예시·템플릿 env 파일은 커밋 대상이라 비밀이 아니다.
SAFE_NAMES = (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults")


def is_sensitive(path: Path) -> bool:
    if path.name.lower() in SAFE_NAMES:
        return False
    parts = [p.lower() for p in path.parts]
    for d in SENSITIVE_DIR_NAMES:
        seg = d.split("/")
        for i in range(len(parts) - len(seg) + 1):
            if parts[i : i + len(seg)] == seg:
                return True
    name = parts[-1] if parts else ""
    return any(fnmatch.fnmatchcase(name, g) for g in SENSITIVE_NAME_GLOBS)


def _validate_rel(rel: str) -> PurePosixPath:
    if "\x00" in rel:
        raise BrokerError("잘못된 경로입니다.", "bad_path")
    rel = rel.replace("\\", "/").strip()
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise BrokerError("절대 경로는 받지 않습니다.", "bad_path")
    if any(part == ".." for part in p.parts):
        raise BrokerError("상위 경로(..)는 허용되지 않습니다.", "bad_path")
    return p


def resolve_in_root(root: Path, rel: str, *, must_exist: bool = True) -> ResolvedPath:
    """root 아래 상대경로를 실제 경로로 바꾼다. 심볼릭 링크가 root 밖을 가리키면 거부."""
    root = root.resolve(strict=True)
    p = _validate_rel(rel)
    candidate = root.joinpath(*p.parts) if p.parts and p.parts != (".",) else root
    try:
        real = candidate.resolve(strict=must_exist)
    except FileNotFoundError as e:
        raise BrokerError("파일을 찾을 수 없습니다.", "not_found") from e
    except (OSError, RuntimeError) as e:  # 심링크 루프 등
        raise BrokerError("경로를 해석할 수 없습니다.", "bad_path") from e
    if not is_within(real, root):
        raise BrokerError("허용된 영역 밖의 경로입니다.", "outside_root")
    return ResolvedPath(root=root, path=real)


def validate_absolute_in_roots(path: str | Path, roots: list[Path]) -> ResolvedPath:
    """에이전트가 보고한 절대경로용. 어떤 허용 루트에도 실제로 속해야 통과."""
    raw = Path(path)
    if not raw.is_absolute():
        raise BrokerError("절대 경로가 아닙니다.", "bad_path")
    try:
        real = raw.resolve(strict=True)
    except FileNotFoundError as e:
        raise BrokerError("파일을 찾을 수 없습니다.", "not_found") from e
    except (OSError, RuntimeError) as e:
        raise BrokerError("경로를 해석할 수 없습니다.", "bad_path") from e
    for root in roots:
        try:
            r = root.resolve(strict=True)
        except OSError:
            continue
        if is_within(real, r):
            return ResolvedPath(root=r, path=real)
    raise BrokerError("허용된 영역 밖의 경로입니다.", "outside_root")


def ensure_regular_file(path: Path) -> os.stat_result:
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise BrokerError("일반 파일이 아닙니다.", "not_a_file")
    return st


def validate_workspace_root(path: str, allowed_roots: list[Path]) -> Path:
    if "\x00" in path:
        raise BrokerError("잘못된 경로입니다.", "bad_path")
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise BrokerError("워크스페이스 경로는 절대 경로여야 합니다.", "bad_path")
    try:
        real = raw.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise BrokerError("디렉터리를 찾을 수 없습니다.", "not_found") from e
    if not real.is_dir():
        raise BrokerError("디렉터리가 아닙니다.", "not_a_dir")
    if is_sensitive(real):
        raise BrokerError("민감한 디렉터리는 워크스페이스로 등록할 수 없습니다.", "sensitive")
    for root in allowed_roots:
        # 허용 루트 자체(예: ~/development 전체)도 등록은 허용한다. 홈 디렉터리 전체는 allowed_roots 에 없으면 막힌다.
        if is_within(real, root):
            return real
    raise BrokerError("허용된 상위 경로(allowed_roots) 밖입니다.", "outside_root")
