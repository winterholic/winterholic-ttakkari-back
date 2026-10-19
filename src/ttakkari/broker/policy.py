"""File Broker 경로 정책.

모든 파일 접근은 여기서 (루트, 상대경로) → 검증된 실제 경로로 바뀐다.
에이전트나 클라이언트가 준 경로 문자열은 신뢰하지 않는다.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# 반출(다운로드·외부 전송) 기본 차단 대상. 소문자 경로 조각 기준으로 매칭한다.
SENSITIVE_NAME_GLOBS = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    # NOTE: "*.pem"은 이름만으로 막지 않는다. 인증서 .pem 읽기까지 막히는
    # 과차단(S-2·S-7) 때문에 내용 판정(_pem_holds_private_key)으로 분리했다.
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
    # NOTE: 과거 "*secret*" 부분일치는 정상 소스(secret-v2-bridge.ts 등)까지 막아
    # 실제 비밀 파일명으로 좁혔다 (S-6b·S-6c).
    "secret.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.toml",
    "secrets.env",
    "secrets.txt",
    ".secrets",
    "secrets",
    "*.tfstate",
    "*.tfvars",
)
# 경로 조각 연속 일치. ".codex/auth.json" 처럼 파일까지 적으면 그 파일만 막는다(하네스 폴더를 권한 폴더로 열어도 로그인 토큰은 못 읽게).
SENSITIVE_DIR_NAMES = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".gcloud", ".config/gcloud", ".git",
                       ".codex/auth.json")


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


# .pem은 인증서와 개인키가 같은 확장자를 쓴다. 이름이 아니라 내용으로 가린다:
# 개인키 표식이 있거나, 인증서 표식조차 없으면(판독 불가·DER 등 포함) 민감으로 본다.
# 순수 인증서 .pem만 비민감이다.
_PRIVATE_KEY_MARKER = "PRIVATE KEY"
_CERTIFICATE_MARKER = "CERTIFICATE"


def _pem_holds_private_key(path: Path) -> bool:
    # NOTE: 앞 8KiB만 읽으면 인증서 뒤에 붙은 개인키를 놓친다. 파일 전체를
    # 청크 순회로 검사한다(개인키 표식을 찾으면 즉시 중단. 메모리 상한 유지).
    try:
        if not path.is_file():
            return True
        found_cert = False
        tail = ""
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                text = tail + chunk.decode("utf-8", errors="ignore").upper()
                if _PRIVATE_KEY_MARKER in text:
                    return True
                if _CERTIFICATE_MARKER in text:
                    found_cert = True
                tail = text[-32:]
    except OSError:
        return True
    return not found_cert


# my-secrets.json·prod-secrets.yml 같은 실제 비밀 파일명 변형용.
# 데이터 확장자 + secret/secrets가 구분자(-_.)로 떨어진 단어일 때만 민감으로 본다.
# secret-v2-bridge.ts(.ts)·my-secret-notes.md(.md) 같은 일반 파일은 계속 허용된다.
_SECRET_VARIANT_EXTS = frozenset({".json", ".yaml", ".yml", ".toml", ".env", ".txt"})
_SECRET_WORD_RE = re.compile(r"(?<![a-z0-9])secrets?(?![a-z0-9])")


def _looks_like_secret_variant(name: str) -> bool:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or stem.strip("._-") == "":
        return False
    return ("." + ext) in _SECRET_VARIANT_EXTS and _SECRET_WORD_RE.search(stem) is not None


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
    if name.endswith(".pem"):
        return _pem_holds_private_key(path)
    if _looks_like_secret_variant(name):
        return True
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
