import uuid

import pytest

from ttakkari.api.accounts import AccountUsageOut, UsageWindowOut
from ttakkari.runs import usage


@pytest.mark.asyncio
async def test_fetch_keeps_zero_ai_credits_distinct_from_unknown(monkeypatch):
    responses = {
        "usage": {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
            {"name": "Gemini Models", "buckets": [{"window": "weekly", "remaining_fraction": 0.75}]}
        ]}}},
        "credits": {"status": "SUCCESS", "command": {"name": "credits", "data": {"remaining_credits": 0}}},
    }

    async def fake_command(name):
        return responses[name]

    monkeypatch.setattr(usage, "_agy_command", fake_command)
    result = await usage._fetch_antigravity()

    assert result.source == "antigravity_cli"
    assert result.credits_balance == 0
    assert result.credits_error is None
    assert result.windows[0].remaining_percent == 75


def test_api_dto_keeps_exact_remaining_and_separate_credit_balance():
    dto = AccountUsageOut(
        id=uuid.uuid4(), name="Antigravity", engine="antigravity", pool="personal", enabled=True, rank=1,
        max_used_percent=100, remaining_to_limit_percent=None, stale=False, work_enabled=True, work_rank=None,
        login="unknown", cooldown_seconds=0,
        windows=[UsageWindowOut(key="agy_gemini_models_weekly", label="Gemini Models · 주간", percent=25,
                                resets_at=1_792_675_298, remaining_percent=75.25)],
        credits_balance=0, credits_error=None, source="antigravity_cli", plan=None, error=None, updated_at=1_792_088_498,
    )
    payload = dto.model_dump()

    assert payload["windows"][0]["remaining_percent"] == 75.25
    assert payload["credits_balance"] == 0
