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


def thinking_level(model: str) -> str:
    return model.rsplit("-", 1)[-1]


def quota_group(model: str | None) -> str | None:
    if model not in ANTIGRAVITY_MODELS:
        return None
    if model.startswith(("claude-", "gpt-oss-")):
        return "agy_claude_and_gpt_models"
    if model.startswith("gemini-4-argon-"):
        # The user's paid Pro snapshot exposes a separate allowance. A missing
        # Argon window is unknown, not proof of a shared pool or an empty quota.
        return "agy_gemini_4_argon"
    return "agy_gemini_models"
