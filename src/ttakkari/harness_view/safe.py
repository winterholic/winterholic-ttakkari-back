"""하네스 조회 전용 안전 읽기. 클라이언트는 경로를 주지 않고, 여기서도 허용 루트·패턴 밖은 읽지 않는다.

- 루트와 상대 패턴을 코드에 고정한다(Root). 심볼릭 링크는 따라간 실제 경로가 허용 루트·패턴 안일 때만 읽는다.
- 비밀 파일(.env, 로그인 토큰, 히스토리)은 패턴에 없을 뿐 아니라 이름으로도 한 번 더 거른다.
- 본문과 설정 값은 응답 전에 마스킹한다. 값 대신 키 이름만 남기는 것이 원칙이다.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ttakkari.broker.policy import is_sensitive_name, is_within
from ttakkari.oplog.scrub import MASK, scrub, scrub_prose

MAX_FILE_BYTES = 256 * 1024
# 패턴이 허용해도 읽지 않는 이름. 로그인 토큰·대화 기록·계정 상태 파일.
NEVER_READ_NAMES = frozenset({
    ".credentials.json", "credentials.json", "auth.json", "antigravity-oauth-token", "history.jsonl",
    ".env", "oauth_creds.json", "keychain", "id_rsa", "id_ed25519",
})
# 통째로 읽지 않고 지정한 키만 꺼내는 파일. 나머지 키(계정 정보 등)는 응답에 싣지 않는다.
SUBSET_ONLY_NAMES = frozenset({".claude.json"})
_SECRET_KEY = re.compile(
    r"secret|passw|api[_-]?key|access[_-]?key|private[_-]?key|credential|cookie|authorization|bearer|jwt|"
    r"session[_-]?id|(?<![a-z])token(?!s\b)(?!_?(?:count|limit|usage|budget))|(?<![a-z])auth(?!or)", re.IGNORECASE)
# 값 전체를 가리는 컨테이너 키. 안의 키 이름만 남긴다.
_OPAQUE_KEYS = frozenset({"env", "headers", "http_headers", "environment", "env_vars", "envs", "secrets"})
_PROSE_KEYS = frozenset({"description", "title"})
_SECRET_FLAG = re.compile(r"^--?[\w-]*(secret|passw|token|key|auth|cookie|bearer)[\w-]*$", re.IGNORECASE)


class Denied(Exception):
    """허용 루트·패턴 밖이거나 비밀 파일이라 읽지 않는다."""


@dataclass(frozen=True)
class Root:
    path: Path
    patterns: tuple[str, ...]
    # False 면 읽기 패턴에만 쓰고 존재 확인(깨진 참조 검사)에는 쓰지 않는다. 홈처럼 넓은 루트를 존재 탐색에 열지 않기 위해서다.
    probe: bool = True


def _match(rel: str, pattern: str) -> bool:
    # fnmatch 의 * 는 / 도 넘어가므로 구간 수를 맞춰 폴더 경계를 지킨다.
    return rel.count("/") == pattern.count("/") and fnmatch.fnmatchcase(rel, pattern)


class SafeFiles:
    def __init__(self, roots: list[Root]) -> None:
        self.roots = [Root(Path(os.path.realpath(r.path)), r.patterns, r.probe) for r in roots]

    def _under(self, path: Path, *, probe_only: bool = False) -> list[tuple[Root, str]]:
        return [(root, path.relative_to(root.path).as_posix())
                for root in self.roots
                if (root.probe or not probe_only) and is_within(path, root.path) and path != root.path]

    def allowed(self, path: Path) -> bool:
        return any(any(_match(rel, p) for p in root.patterns) for root, rel in self._under(path))

    def resolve(self, path: Path | str) -> Path:
        """읽어도 되는 일반 파일의 실제 경로. 아니면 Denied."""
        lexical = Path(os.path.abspath(os.path.expanduser(str(path))))
        real = Path(os.path.realpath(lexical))
        # 루트가 심볼릭 링크일 수 있어 lexical 은 루트의 실제 경로 기준으로 다시 비교한다.
        if not (self.allowed(lexical) or self.allowed(real)):
            raise Denied("허용 목록에 없는 경로")
        if not self.allowed(real):
            raise Denied("심볼릭 링크가 허용 범위 밖을 가리킨다")
        for candidate in (lexical, real):
            if candidate.name.lower() in NEVER_READ_NAMES or is_sensitive_name(candidate):
                raise Denied("비밀 파일")
        try:
            mode = real.lstat().st_mode
        except OSError as exc:
            raise Denied("읽을 수 없다") from exc
        if not stat.S_ISREG(mode):
            raise Denied("일반 파일이 아니다")
        return real

    def exists(self, path: Path | str) -> bool | None:
        """허용 루트 안의 경로만 존재 여부를 본다. 밖이면 None(판정 안 함). 내용은 읽지 않는다."""
        lexical = Path(os.path.abspath(os.path.expanduser(str(path))))
        real = Path(os.path.realpath(lexical))
        if not self._under(real, probe_only=True):
            return None
        return real.exists()

    def read_text(self, path: Path | str, limit: int = MAX_FILE_BYTES) -> tuple[str, bool]:
        """(본문, 잘림 여부). 마스킹 전의 원문이므로 호출자가 mask_text 를 거쳐야 한다."""
        real = self.resolve(path)
        if real.name in SUBSET_ONLY_NAMES:
            raise Denied("일부 키만 읽는 파일")
        return self._read(real, limit)

    @staticmethod
    def _read(real: Path, limit: int) -> tuple[str, bool]:
        try:
            fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise Denied("읽을 수 없다") from exc
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise Denied("일반 파일이 아니다")
            raw = handle.read(limit + 1)
        return raw[:limit].decode("utf-8", errors="replace"), len(raw) > limit

    def read_json_subset(self, path: Path | str, keys: tuple[str, ...]) -> dict[str, Any]:
        real = self.resolve(path)
        text, truncated = self._read(real, 4 * 1024 * 1024)
        if truncated:
            raise Denied("파일이 너무 크다")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise Denied("JSON 이 아니다") from exc
        return {k: data[k] for k in keys if isinstance(data, dict) and k in data}

    def glob(self, root: Path, pattern: str) -> Iterator[Path]:
        """루트 아래 패턴 후보. 허용 여부는 읽을 때 resolve 가 다시 판단한다."""
        yield from sorted(Path(root).glob(pattern))


def is_secret_key(name: str) -> bool:
    return bool(_SECRET_KEY.search(name))


def mask_text(text: str) -> tuple[str, int]:
    """(마스킹된 본문, 가려진 곳의 수). 같은 규칙을 한 번 더 적용해도 결과가 같다."""
    out = scrub_prose(text)
    return out, max(0, out.count(MASK) - text.count(MASK))


def _mask_args(args: list[Any]) -> list[Any]:
    out: list[Any] = []
    hide_next = False
    for arg in args:
        if hide_next and isinstance(arg, str) and not arg.startswith("-"):
            out.append(MASK)
            hide_next = False
            continue
        hide_next = isinstance(arg, str) and bool(_SECRET_FLAG.match(arg)) and "=" not in arg
        out.append(scrub(arg) if isinstance(arg, str) else arg)
    return out


def mask_config(value: Any, key: str = "") -> Any:
    """설정 구조를 마스킹한다. env·headers 는 키 이름만 남기고, 비밀스러운 키의 값과 인자의 값은 가린다."""
    if isinstance(value, dict):
        if key.lower() in _OPAQUE_KEYS:
            return {k: MASK for k in value}
        return {k: (MASK if is_secret_key(str(k)) and not isinstance(v, dict | list) else mask_config(v, str(k)))
                for k, v in value.items()}
    if isinstance(value, list):
        if key.lower() == "args":
            return _mask_args(value)
        return [mask_config(v, key) for v in value]
    if isinstance(value, str):
        # description 은 도구·스키마 설명문(산문)이라 문서용 규칙을 쓰고, 명령·인자 같은 값은 엄격한 규칙을 쓴다.
        return scrub_prose(value) if key in _PROSE_KEYS else scrub(value)
    return value


def count_masked(original: Any, masked: Any) -> int:
    dump = lambda v: json.dumps(v, ensure_ascii=False)
    return max(0, dump(masked).count(MASK) - dump(original).count(MASK))
