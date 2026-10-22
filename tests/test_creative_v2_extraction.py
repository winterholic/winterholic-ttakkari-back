import pytest

from ttakkari.creative.v2 import extraction


@pytest.mark.asyncio
async def test_llm_extraction_is_grounded_and_never_promotes_inference_to_hard(monkeypatch):
    async def complete(_brief):
        return {
            "selections": [
                {
                    "entry_id": "C01.tech_explainer",
                    "role": "primary",
                    "strength": "hard",
                    "source": "user_explicit",
                    "confidence": 0.91,
                    "evidence": [{"quote": "기술 설명"}],
                }
            ],
            "constraints": [
                {
                    "id": "duration",
                    "kind": "duration",
                    "hard": True,
                    "statement": "약 3분",
                    "source": "user_explicit",
                    "evidence": [{"quote": "3분 정도"}],
                }
            ],
            "duration": {"mode": "preferred", "seconds": 180},
            "open_questions": [],
        }

    monkeypatch.setattr(extraction, "_complete", complete)
    spec = await extraction.extract_intent("기술 설명을 3분 정도로 만들어줘")

    assert spec.extraction.method == "llm"
    assert spec.selections[0].source == "llm_inferred"
    assert spec.selections[0].strength == "soft"
    assert spec.constraints[0].source == "llm_inferred"
    assert spec.constraints[0].hard is False
    assert spec.open_questions


@pytest.mark.asyncio
async def test_llm_extraction_rejects_evidence_not_present_in_brief(monkeypatch):
    async def complete(_brief):
        return {
            "selections": [
                {
                    "entry_id": "C01.tech_explainer",
                    "role": "primary",
                    "strength": "soft",
                    "confidence": 0.8,
                    "evidence": [{"quote": "사용자가 SF라고 했다"}],
                }
            ]
        }

    monkeypatch.setattr(extraction, "_complete", complete)
    with pytest.raises(extraction.IntentExtractionRejected, match="evidence_not_in_brief"):
        await extraction.extract_intent("기술 설명 영상")
