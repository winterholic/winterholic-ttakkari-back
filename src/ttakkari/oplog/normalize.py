"""메시지 정규화와 fingerprint(이슈 그룹 키). 값만 다른 같은 오류는 같은 이슈가 된다."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<uuid>"),
    (re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"), "<ts>"),
    (re.compile(r"\d{4}-\d\d-\d\d"), "<date>"),
    (re.compile(r"/Users/[^/\s]+"), "~"),
    (re.compile(r"/(?:private/)?(?:var/folders|tmp)/[^\s'\")]+"), "<tmp>"),
    (re.compile(r"\bttk-it-\w+"), "<tmp>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b"), "<sha>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|MB|KB|GB|%)?(?![\w.])"), "<n>"),
]
_WS = re.compile(r"\s+")
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
_EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure|Timeout)\w*)(?::\s?(?P<msg>.*))?$")
_SITE = re.compile(r"(?:^|/)(?:site-packages|lib/python[\d.]+)/")
LEVELS = ("debug", "info", "warning", "error", "fatal")


def normalize_message(text: str, limit: int = 300) -> str:
    text = _ANSI.sub("", text or "").strip().splitlines()[0] if (text or "").strip() else ""
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return _WS.sub(" ", text).strip()[:limit]


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


@dataclass
class ParsedTrace:
    exc_type: str | None = None
    exc_message: str = ""
    frames: list[str] = field(default_factory=list)  # "module/path.py:function", 줄 번호 제외

    causes: list[ParsedTrace] = field(default_factory=list)

    @property
    def app_frames(self) -> list[str]:
        own = [f for f in self.frames if not _SITE.search(f)]
        return (own or self.frames)[-3:]


def parse_traceback(text: str) -> ParsedTrace | None:
    """최종 예외를 표시하되 명시적 cause/context 체인은 그룹핑에도 보존한다."""
    lines = _ANSI.sub("", text).splitlines()
    if not any(l.startswith("Traceback (most recent call last)") for l in lines):
        return None
    start = max(i for i, l in enumerate(lines) if l.startswith("Traceback (most recent call last)"))
    trace = ParsedTrace()
    for line in lines[start + 1:]:
        m = _FRAME.match(line)
        if m:
            path = re.sub(r".*/src/", "", m["file"])
            path = re.sub(r"^.*/site-packages/", "site-packages/", path)
            trace.frames.append(f"{path}:{m['func']}")
            continue
        if line and not line[0].isspace():
            e = _EXC_LINE.match(line.strip())
            if e:
                trace.exc_type, trace.exc_message = e["type"], e["msg"] or ""
            elif trace.exc_type is None:
                trace.exc_type = line.split(":", 1)[0].strip()[:80]
                trace.exc_message = line.partition(":")[2].strip()
    if trace.exc_type is None:
        return None
    if start:
        prefix = "\n".join(lines[:start]).rstrip()
        if prefix.endswith(("During handling of the above exception, another exception occurred:",
                            "The above exception was the direct cause of the following exception:")):
            cause = parse_traceback(prefix)
            if cause is not None:
                trace.causes = [cause]
    return trace


def exception_fingerprint(trace: ParsedTrace) -> str:
    if trace.causes:
        # 단일 예외 fingerprint는 그대로 유지한다. 체인만 버전 있는 키로 분리한다.
        single = ParsedTrace(trace.exc_type, trace.exc_message, trace.frames)
        return fingerprint("exc-chain-v1", *(exception_fingerprint(c) for c in trace.causes), exception_fingerprint(single))
    frames = trace.app_frames
    if frames:
        return fingerprint("exc", trace.exc_type or "", *frames)
    return fingerprint("exc", trace.exc_type or "", normalize_message(trace.exc_message))


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0
