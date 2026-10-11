"""엔진 프로필(계정) 우선순위와 사용량 한도 폴백(ADR 0006)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from ttakkari.config import get_settings

ENGINES = {"claude", "codex"}
# 엔진이 사용량·속도 한도로 거절할 때의 문구. 문구가 바뀌면 폴백 없이 그냥 실패한다(ADR 0006 한계).
USAGE_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate.?limit|quota|too many requests|\b429\b|"
    r"credit balance|out of credits|overloaded",
    re.IGNORECASE,
)

_cooldown: dict[str, float] = {}


@dataclass(frozen=True)
class Profile:
    name: str
    engine: str
    config_dir: Path | None = None

    def env(self) -> dict[str, str]:
        if self.config_dir is None:
            return {}
        return {"CLAUDE_CONFIG_DIR" if self.engine == "claude" else "CODEX_HOME": str(self.config_dir)}


def load() -> list[Profile]:
    out: list[Profile] = []
    for raw in get_settings().engine_profiles:
        engine = raw.get("engine", "")
        name = raw.get("name") or engine
        if engine not in ENGINES or any(p.name == name for p in out):
            continue
        cd = raw.get("config_dir")
        out.append(Profile(name=name, engine=engine, config_dir=Path(cd).expanduser() if cd else None))
    return out


def candidates(session_engine: str) -> list[Profile]:
    """세션 엔진에 맞는 프로필을 우선순위대로. 쉬는 프로필은 뒤로 보낸다(전부 쉬면 순서대로 다시 시도)."""
    all_ = load()
    pool = all_ if session_engine == "auto" else [p for p in all_ if p.engine == session_engine]
    if not pool and session_engine in ENGINES:
        pool = [Profile(name=session_engine, engine=session_engine)]
    now = time.monotonic()
    ready = [p for p in pool if _cooldown.get(p.name, 0) <= now]
    cooling = [p for p in pool if _cooldown.get(p.name, 0) > now]
    return ready + cooling


def cool_down(name: str) -> None:
    _cooldown[name] = time.monotonic() + get_settings().profile_cooldown_seconds


def reset_cooldowns() -> None:
    _cooldown.clear()


def is_usage_limit(*texts: str | None) -> bool:
    return any(t and USAGE_LIMIT_RE.search(t) for t in texts)
