"""A valid session ID alone must not authorize reusing an unrelated conversation."""
from __future__ import annotations

import json

import httpx
import pytest

from ttakkari.runs import desk, desk_qwen


@pytest.fixture(autouse=True)
def _endpoint(monkeypatch):
    monkeypatch.setattr(desk_qwen, "endpoint", lambda: "http://qwen.test")


@pytest.mark.parametrize("same_project,same_task", [("no", "yes"), ("yes", "no"), (None, None)])
async def test_picker_rejects_valid_id_without_both_continuity_checks(same_project, same_task):
    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "lead_id": "aip", "same_project": same_project, "same_task": same_task,
        })}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        picked = await desk_qwen._pick_lead(
            client, "- lead_id=aip 「AIP 1.0 배포까지 남은 작업 확인」 상태: 진행 중",
            "사용자: AIP 배포·테스트 개선해줘", "따까리 배포·테스트 병목을 분석해줘", ["aip"],
        )
    assert picked is None


async def test_picker_uses_session_title_as_identity_instead_of_contaminated_result():
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "lead_id": "ttk", "same_project": "yes", "same_task": "yes",
        })}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        picked = await desk_qwen._pick_lead(
            client, "- lead_id=ttk 「따까리 접수기 라우팅 보완」 상태: 진행 중 · 최근 결과: UNRELATED_AIP_RESULT",
            "사용자: AIP 진행률을 알려줘", "따까리 접수기 오분류 회귀도 추가해줘", ["ttk"],
        )
    assert picked == "ttk"
    content = calls[0]["messages"][-1]["content"]
    assert "UNRELATED_AIP_RESULT" not in content
    assert "따까리 접수기 라우팅 보완" in content
    assert "사용자: AIP 진행률을 알려줘" in content  # pronoun follow-ups can still use history
    props = calls[0]["response_format"]["json_schema"]["schema"]["properties"]
    assert props["same_project"]["enum"] == ["yes", "no"]
    assert props["same_task"]["enum"] == ["yes", "no"]


@pytest.mark.parametrize("checks", [{}, {"same_project": "no", "same_task": "yes"},
                                    {"same_project": "yes", "same_task": "no"}])
def test_cli_fallback_also_requires_continuity_and_bypasses_title_dedup(checks):
    decision = desk._parse(json.dumps({"action": "delegate", "reply": "넘길게요", "lead_id": "aip", **checks}))
    assert decision is not None and decision.lead_id is None and decision.new_session


def test_cli_fallback_preserves_verified_continuation():
    decision = desk._parse(json.dumps({"action": "delegate", "reply": "이어서", "lead_id": "ttk",
                                      "same_project": "yes", "same_task": "yes"}))
    assert decision is not None and decision.lead_id == "ttk" and not decision.new_session
