"""초안 본문의 민감정보 탐지. 정규식 기반 보조 수단이며 회사 기밀·맥락상 비공개 내용은 찾지 못한다."""
from __future__ import annotations

import re
from dataclasses import dataclass

HIGH = "high"
MEDIUM = "medium"
MAX_FINDINGS = 200
_EXCERPT = 100

_PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\$\{[^}]*\}|\$[A-Z_]+|\{\{.*\}\}|\*{3,}|x{3,}|\.{3,}|…|-+|null|none|"
                          r"changeme|change-me|your[-_ ].*|example.*|placeholder.*|redacted.*)$", re.IGNORECASE)
_PRIVATE_HOST = (r"(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
                 r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|169\.254(?:\.\d{1,3}){2}|"
                 r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:internal|local|lan|corp|intra|intranet|home\.arpa))")


@dataclass(frozen=True)
class Rule:
    name: str
    category: str
    severity: str
    pattern: re.Pattern[str]
    secret: bool  # True 면 발췌에서 일치 값을 가린다.
    group: int = 0


def _r(name: str, category: str, severity: str, pattern: str, *, secret: bool, group: int = 0, flags: int = 0) -> Rule:
    return Rule(name, category, severity, re.compile(pattern, flags), secret, group)


RULES: tuple[Rule, ...] = (
    _r("private_key", "credential", HIGH, r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----", secret=True),
    _r("aws_access_key", "credential", HIGH, r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", secret=True),
    _r("github_token", "credential", HIGH, r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b", secret=True),
    _r("slack_token", "credential", HIGH, r"\bxox[baprs]-[A-Za-z0-9-]{10,}", secret=True),
    _r("api_key", "credential", HIGH, r"\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35})\b", secret=True),
    _r("jwt", "credential", HIGH, r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", secret=True),
    _r("bearer_token", "credential", HIGH, r"\bBearer\s+([A-Za-z0-9._~+/=-]{20,})", secret=True, group=1),
    _r("credential_assignment", "credential", HIGH,
       r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
       r"비밀번호|패스워드|암호)[\"']?\s*[:=]\s*[\"']?([^\s\"'`,;]{4,})",
       secret=True, group=1, flags=re.IGNORECASE),
    _r("url_credentials", "credential", HIGH, r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s@]+)@", secret=True, group=1,
       flags=re.IGNORECASE),
    _r("korean_rrn", "personal", HIGH, r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)", secret=True),
    _r("internal_url", "internal_url", HIGH, rf"\b(?:[a-z][a-z0-9+.-]*://)?{_PRIVATE_HOST}(?::\d{{1,5}})?(?:/[^\s)>\]\"']*)?",
       secret=False, flags=re.IGNORECASE),
    _r("local_path", "local_path", HIGH,
       r"(?<![\w/.])(?:/(?:Users|home|Volumes|private/var|var/folders|etc|opt|root|tmp)/[^\s)>\]\"'`]+|"
       r"[A-Za-z]:\\(?:Users|Windows|Program Files)[^\s)>\]\"'`]*|~/\.(?:ssh|aws|config|claude|codex|gnupg)[^\s)>\]\"'`]*|"
       r"file:///[^\s)>\]\"'`]+)",
       secret=False),
    _r("email", "personal", MEDIUM, r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", secret=True),
    _r("phone_kr", "personal", MEDIUM, r"(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)", secret=True),
)


@dataclass(frozen=True)
class Finding:
    rule: str
    category: str
    severity: str
    field: str
    document_id: str | None
    line: int
    excerpt: str


def _mask(line: str, start: int, end: int, rule: str) -> str:
    out = f"{line[:start]}«{rule}»{line[end:]}"
    return out[:_EXCERPT] + ("…" if len(out) > _EXCERPT else "")


def _plain(line: str, start: int) -> str:
    cut = max(0, start - 20)
    out = line[cut:cut + _EXCERPT]
    return ("…" if cut else "") + out + ("…" if len(line) > cut + _EXCERPT else "")


def scan_text(text: str, *, field: str = "content", document_id: str | None = None) -> list[Finding]:
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        masked_spans: list[tuple[int, int]] = []
        for rule in RULES:
            for m in rule.pattern.finditer(line):
                value = m.group(rule.group)
                if rule.name in {"credential_assignment", "bearer_token"} and _PLACEHOLDER.match(value):
                    continue
                span = m.span(rule.group)
                if rule.name == "email" and any(a <= m.start() < b for a, b in masked_spans):
                    continue  # URL 인증정보 안의 user:pass@host 는 url_credentials 가 이미 보고했다.
                if rule.secret:
                    masked_spans.append(span)
                excerpt = _mask(line, span[0], span[1], rule.name) if rule.secret else _plain(line, m.start())
                found.append(Finding(rule.name, rule.category, rule.severity, field, document_id, number, excerpt))
                if len(found) >= MAX_FINDINGS:
                    return found
    return found
