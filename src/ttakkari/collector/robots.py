"""Collector Platform: RFC 9309 robots.txt parsing and matching (CP-S2-04).

Python's urllib.robotparser is first-match and has no wildcard support, so this implements the RFC rules:
- a crawler uses the group whose product token is the longest case-insensitive prefix match, else `*`;
  several groups for the same token are merged
- the longest matching path pattern wins, `Allow` wins a tie, no match means allowed
- `*` matches any run of characters and a trailing `$` anchors the end
- only the first 500 KiB are parsed

robots.txt is a politeness signal, not a grant of rights: the caller still needs the source policy to allow the
purpose (policy.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

MAX_ROBOTS_BYTES = 500 * 1024
MAX_CRAWL_DELAY_SECONDS = 30.0
_UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str] = field(compare=False, repr=False)


@dataclass(frozen=True)
class Group:
    agents: tuple[str, ...]
    rules: tuple[Rule, ...]
    crawl_delay: float | None


def _canon(path: str) -> str:
    """Compare percent-encoded paths in one form: decode, then re-encode everything outside the unreserved set."""
    return quote(unquote(path), safe=_UNRESERVED + "/*$?=&:@!,;+'()")


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(regex + ("$" if anchored else ""))


def parse(text: str) -> list[Group]:
    groups: list[dict] = []
    current: dict | None = None
    last_was_agent = False
    for raw in text[:MAX_ROBOTS_BYTES].splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not last_was_agent or current is None:
                current = {"agents": [], "rules": [], "delay": None}
                groups.append(current)
            current["agents"].append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if current is None:
            continue
        if key in ("allow", "disallow"):
            if value == "":
                continue  # an empty Disallow allows everything and an empty Allow adds nothing: no rule either way
            pattern = _canon(value)
            current["rules"].append(Rule(key == "allow", pattern, _compile(pattern)))
        elif key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                current["delay"] = min(delay, MAX_CRAWL_DELAY_SECONDS)
    return [Group(tuple(g["agents"]), tuple(g["rules"]), g["delay"]) for g in groups]


@dataclass(frozen=True)
class RobotsRules:
    """The rules that apply to one crawler token. `unavailable` means the file could not be fetched safely."""

    rules: tuple[Rule, ...] = ()
    crawl_delay: float | None = None
    unavailable: bool = False
    matched_agent: str | None = None

    def allowed(self, target: str) -> bool:
        """`target` is the request path plus query. /robots.txt itself is always fetchable."""
        if self.unavailable:
            return False
        path = _canon(target if target.startswith("/") else "/" + target)
        if path.split("?", 1)[0] == "/robots.txt":
            return True
        best: Rule | None = None
        for rule in self.rules:
            if rule.regex.match(path) is None:
                continue
            if best is None or len(rule.pattern) > len(best.pattern) or (
                len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow
            ):
                best = rule
        return True if best is None else best.allow


UNAVAILABLE = RobotsRules(unavailable=True)
ALLOW_ALL = RobotsRules()


def rules_for(groups: list[Group], product_token: str) -> RobotsRules:
    """Merge the groups that name the longest matching token, or `*` when none matches."""
    token = product_token.lower()
    best_len = -1
    chosen: list[Group] = []
    star: list[Group] = []
    for group in groups:
        for agent in group.agents:
            if agent == "*":
                star.append(group)
                break
            if agent and token.startswith(agent):
                if len(agent) > best_len:
                    best_len, chosen = len(agent), [group]
                elif len(agent) == best_len and group not in chosen:
                    chosen.append(group)
                break
    use = chosen or star
    if not use:
        return ALLOW_ALL
    delays = [g.crawl_delay for g in use if g.crawl_delay is not None]
    return RobotsRules(
        rules=tuple(r for g in use for r in g.rules),
        crawl_delay=delays[0] if delays else None,
        matched_agent=(token[:best_len] if chosen else "*"),
    )


def classify_status(status: int) -> str:
    """Map the robots.txt HTTP status to a fetch outcome.

    - 2xx parse the body
    - 404/410 and other 4xx: no rules (RFC 9309 2.3.1.3), except 401/403 which this collector treats as a closed door
    - 429 and 5xx: unreachable, so deny all for now (RFC 9309 2.3.1.4)
    """
    if 200 <= status < 300:
        return "parse"
    if status in (401, 403):
        return "forbidden"
    if status == 429 or status >= 500:
        return "unreachable"
    if 400 <= status < 500:
        return "no_rules"
    return "unreachable"
