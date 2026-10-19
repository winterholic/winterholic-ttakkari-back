"""Antigravity CLI model IDs observed with `agy models` on 2026-10-16.

Native Claude aliases remain distinct from these Antigravity model IDs.
Quota pools are baseline allowances, independent of purchased AI credits.
"""

ANTIGRAVITY_MODELS = [
    f"{family}-{level}"
    for family in ("claude-sonnet-5-5", "claude-opus-5-5", "gemini-4-argon", "gemini-3.8-flash")
    for level in ("low", "medium", "high")
] + ["gpt-oss-120b-medium"]

ANTIGRAVITY_DEFAULT = "claude-sonnet-5-5-high"
ANTIGRAVITY_QUOTA_GROUPS = ("agy_claude_and_gpt_models", "agy_gemini_models", "agy_gemini_4_argon")


# Muse 는 생각 수준을 max 로 고정한다(사용자 결정 2026-10-19). 세션·Run·API 요청이 다른 값을 줘도 실행 직전에 이 값으로 바꾼다.
MUSE_EFFORT = "max"


def thinking_level(model: str) -> str:
    return model.rsplit("-", 1)[-1]


def supports_effort(engine: str, model: str | None) -> bool:
    """Whether effort is a separate CLI option for this engine/model pair."""
    # Antigravity model IDs already encode their thinking level. The CLI rejects
    # --effort for these IDs, even when the value matches the suffix.
    return not (engine == "antigravity" and model in ANTIGRAVITY_MODELS)


def quota_group(model: str | None) -> str | None:
    if model not in ANTIGRAVITY_MODELS:
        return None
    if model.startswith(("claude-", "gpt-oss-")):
        return "agy_claude_and_gpt_models"
    if model.startswith("gemini-4-argon-"):
        # The observed snapshot exposes a separate allowance. A missing
        # Argon window is unknown, not proof of a shared pool or an empty quota.
        return "agy_gemini_4_argon"
    return "agy_gemini_models"
