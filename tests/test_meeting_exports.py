import csv
import io
import json
from datetime import UTC, datetime

import pytest

from ttakkari.sharing.delivery import FORMATTERS, format_options
from ttakkari.sharing.schemas import MeetingDocumentV1, Transcript


def document(**overrides):
    value = {
        "title": "제목, `x`\n# 위험한 제목",
        "held_at": datetime(2026, 10, 15, tzinfo=UTC),
        "included_sections": ["summary", "actions", "transcript"],
        "section_states": {"summary": "available", "decisions": "not_selected", "actions": "available",
            "discussions": "not_selected", "open_issues": "not_selected", "transcript": "available"},
        "analysis_state": "available", "participants": [], "summary": "요약\n다음 줄",
        "decisions": [], "actions": [{"item_id": "00000000-0000-4000-8000-000000000001", "position": 1,
            "text": "=cmd", "detail": "  +formula", "status": "open", "evidence_quote": None,
            "evidence_start": None, "evidence_utterance_id": None, "owner": None, "due_text": None}],
        "discussions": [], "open_issues": [],
        "transcript": Transcript(adopted_stt_job_id="00000000-0000-4000-8000-000000000002",
            source="local_whisper", timed=True, duration_seconds=3.0, text="ignored redundant projection",
            utterances=[{"utterance_id": "u1", "start": 0, "end": 1, "speaker_id": None,
                "speaker_label": "화자 1", "speaker_name": None, "text": '말, "그대로"\n이어짐',
                "verification_status": "needs_review", "overlap": None}],
            review={"status": "needs_review", "review_utterance_count": 1, "unknown_speaker_count": 1}),
    }
    value.update(overrides)
    return MeetingDocumentV1(**value)


@pytest.mark.asyncio
async def test_all_four_pure_formatters_are_registered_and_return_utf8_bytes():
    assert set(FORMATTERS) == {"txt", "md", "json", "csv"}
    doc = document()
    for fmt in ("txt", "md", "json"):
        body = await FORMATTERS[fmt](doc.model_copy(deep=True), "document")
        assert isinstance(body, bytes)
        assert "요약" in body.decode("utf-8")
        assert "not_selected" not in body.decode("utf-8") if fmt != "json" else True


@pytest.mark.asyncio
async def test_json_is_exact_projected_document_with_nulls_and_real_zero():
    body = await FORMATTERS["json"](document(), "document")
    parsed = json.loads(body)
    assert parsed["schema_version"] == 1
    assert parsed["transcript"]["utterances"][0]["start"] == 0
    assert parsed["transcript"]["utterances"][0]["overlap"] is None
    assert parsed["actions"][0]["owner"] is None
    assert "PRIVATE" not in body.decode()


@pytest.mark.asyncio
async def test_csv_keeps_columns_quoting_bom_nulls_numbers_and_guards_text_formulas():
    body = await FORMATTERS["csv"](document(), "actions")
    assert body.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in body
    assert body.decode("utf-8-sig").splitlines()[0] == (
        "item_id,position,text,detail,status,owner,due_text,evidence_quote,evidence_start,evidence_utterance_id")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline="")))
    assert rows[0]["evidence_quote"] == ""
    assert rows[0]["position"] == "1"
    assert rows[0]["text"] == "'=cmd"
    assert rows[0]["detail"] == "'  +formula"


@pytest.mark.asyncio
@pytest.mark.parametrize("danger", ["=1+1", "   +SUM(A1)", "\t@cmd", "\r=cmd", "\n-cmd", "\ufeff=cmd"])
async def test_csv_text_formula_guard_checks_leading_whitespace_and_controls(danger):
    doc = document(actions=[{"item_id": "00000000-0000-4000-8000-000000000001", "position": 1,
        "text": danger, "detail": None, "status": "open", "evidence_quote": None,
        "evidence_start": None, "evidence_utterance_id": None, "owner": None, "due_text": None}])
    body = await FORMATTERS["csv"](doc, "actions")
    row = next(csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline="")))
    assert row["text"] == "'" + danger


@pytest.mark.asyncio
async def test_csv_completed_empty_section_is_header_only_and_transcript_zero_is_retained():
    doc = document(actions=[])
    body = await FORMATTERS["csv"](doc, "actions")
    assert len(body.decode("utf-8-sig").splitlines()) == 1
    body = await FORMATTERS["csv"](doc, "utterances")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline="")))
    assert rows[0]["start_seconds"] == "0"
    assert rows[0]["overlap"] == ""
    assert rows[0]["text"] == '말, "그대로"\n이어짐'


@pytest.mark.asyncio
async def test_csv_keeps_boolean_text_values_as_lowercase_without_formula_rewriting():
    doc = document(transcript=Transcript(adopted_stt_job_id=None, source="paste", timed=True,
        duration_seconds=1, text="", utterances=[{"utterance_id": "1", "start": 0, "end": 1,
            "speaker_id": None, "speaker_label": None, "speaker_name": None, "text": "normal",
            "verification_status": "unverified", "overlap": True}],
        review={"status": "unverified", "review_utterance_count": 0, "unknown_speaker_count": 1}))
    body = await FORMATTERS["csv"](doc, "utterances")
    row = next(csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline="")))
    assert row["adopted_stt_job_id"] == ""
    assert row["overlap"] == "true"
    assert row["text"] == "normal"


def test_format_options_distinguish_enabled_formats_from_unanalyzed_parts():
    doc = document(analysis_state="not_generated", included_sections=["actions"], transcript=None,
        section_states={"summary": "not_selected", "decisions": "not_selected", "actions": "not_generated",
            "discussions": "not_selected", "open_issues": "not_selected", "transcript": "not_selected"})
    options = {item["format"]: item for item in format_options(doc)}
    assert options["txt"]["enabled"] and options["json"]["applicable"]
    assert options["csv"] == {"format": "csv", "enabled": True, "applicable": False,
        "parts": [], "reason": "analysis_not_generated"}
    assert options["pdf"] == {"format": "pdf", "enabled": False, "applicable": False,
        "parts": [], "reason": "format_not_enabled"}


@pytest.mark.asyncio
async def test_text_and_markdown_exclude_unselected_sections_and_escape_markdown_title():
    doc = document(decisions=[{"item_id": "00000000-0000-4000-8000-000000000003", "position": 1,
        "text": "SECRET_UNSELECTED", "detail": None, "status": "open", "evidence_quote": None,
        "evidence_start": None, "evidence_utterance_id": None}],
        section_states={"summary": "available", "decisions": "not_selected", "actions": "available",
            "discussions": "not_selected", "open_issues": "not_selected", "transcript": "available"})
    for fmt in ("txt", "md"):
        body = (await FORMATTERS[fmt](doc, "document")).decode()
        assert "SECRET_UNSELECTED" not in body
        assert "not_selected" not in body
        if fmt == "md":
            assert "\\# 위험한 제목" in body


@pytest.mark.asyncio
async def test_csv_rejects_unknown_part_and_json_or_text_reject_non_document_parts():
    doc = document()
    with pytest.raises(ValueError):
        await FORMATTERS["csv"](doc, "summary")
    for fmt in ("txt", "md", "json"):
        with pytest.raises(ValueError):
            await FORMATTERS[fmt](doc, "actions")
