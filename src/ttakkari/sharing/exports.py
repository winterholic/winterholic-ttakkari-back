"""Pure meeting export formatters. Inputs are approved DTOs, never ORM rows."""
from __future__ import annotations

import csv
import io
import json
import unicodedata
from collections.abc import Callable

from ttakkari.sharing.schemas import MeetingDocumentV1

CSV_COLUMNS = {
    "utterances": ("adopted_stt_job_id", "utterance_id", "start_seconds", "end_seconds", "speaker_id",
        "speaker_label", "speaker_name", "text", "verification_status", "overlap"),
    "decisions": ("item_id", "position", "text", "detail", "status", "evidence_quote", "evidence_start",
        "evidence_utterance_id"),
    "discussions": ("item_id", "position", "text", "detail", "status", "evidence_quote", "evidence_start",
        "evidence_utterance_id"),
    "open_issues": ("item_id", "position", "text", "detail", "status", "evidence_quote", "evidence_start",
        "evidence_utterance_id"),
    "actions": ("item_id", "position", "text", "detail", "status", "owner", "due_text", "evidence_quote",
        "evidence_start", "evidence_utterance_id"),
}
SECTION_LABELS = {"summary": "요약", "decisions": "결정", "actions": "할 일", "discussions": "논의",
    "open_issues": "미해결 항목", "transcript": "전사"}


def _markdown(value: str) -> str:
    """Escape Markdown syntax per line so user text cannot inject headings/links."""
    reserved = set("\\`*_{}[]<>()#+-.!|~")
    lines = value.splitlines() or [""]
    return "\n".join("".join("\\" + char if char in reserved else char for char in line) for line in lines)


def _title(doc: MeetingDocumentV1, *, markdown: bool) -> list[str]:
    title = _markdown(doc.title) if markdown else doc.title.replace("\r", " ").replace("\n", " ")
    date = doc.held_at.isoformat() if doc.held_at else None
    return ([f"# {title}"] if markdown else [title]) + ([date] if date else [])


def _item_lines(doc: MeetingDocumentV1, section: str, *, markdown: bool) -> list[str]:
    items = getattr(doc, section)
    if not items:
        return ["항목 없음"]
    result = []
    for item in items:
        text = _markdown(item.text) if markdown else item.text
        result.append(f"- {text}" if markdown else f"• {text}")
        if item.detail:
            result.append(f"  - {_markdown(item.detail) if markdown else item.detail}")
        if section == "actions" and item.owner:
            result.append(f"  - 담당: {_markdown(item.owner) if markdown else item.owner}")
        if section == "actions" and item.due_text:
            result.append(f"  - 기한: {_markdown(item.due_text) if markdown else item.due_text}")
        if item.evidence_quote:
            result.append(f"  - 근거: {_markdown(item.evidence_quote) if markdown else item.evidence_quote}")
    return result


def _transcript_paragraphs(doc: MeetingDocumentV1) -> list[str]:
    transcript = doc.transcript
    if transcript is None:
        return []
    if not transcript.timed:
        return [transcript.text] if transcript.text else []
    utterances = transcript.utterances
    groups: list[list] = []
    for utterance in utterances:
        if groups:
            previous = groups[-1][-1]
            previous_speaker = previous.speaker_name or previous.speaker_label or "화자 미상"
            speaker = utterance.speaker_name or utterance.speaker_label or "화자 미상"
            if (speaker == previous_speaker
                    and utterance.verification_status == previous.verification_status
                    and utterance.overlap == previous.overlap
                    and utterance.start - previous.end <= 2):
                groups[-1].append(utterance)
                continue
        groups.append([utterance])
    result = []
    for group in groups:
        first, last = group[0], group[-1]
        speaker = first.speaker_name or first.speaker_label or "화자 미상"
        marker = "검증 필요" if first.verification_status == "needs_review" else "미검수"
        result.append(f"[{first.start:.2f}–{last.end:.2f}] {speaker} [{marker}]: "
                      + " ".join(item.text for item in group))
    return result


def _render_document(doc: MeetingDocumentV1, *, markdown: bool) -> str:
    lines = _title(doc, markdown=markdown)
    for section in doc.included_sections:
        lines += ["", (f"## {SECTION_LABELS[section]}" if markdown else SECTION_LABELS[section])]
        if section == "summary":
            content = doc.summary if doc.analysis_state == "available" else "분석되지 않음"
            content = content or "항목 없음"
            lines.append(_markdown(content) if markdown else content)
        elif section == "transcript":
            paragraphs = _transcript_paragraphs(doc)
            lines.extend(_markdown(line) if markdown else line for line in paragraphs)
            if not paragraphs:
                lines.append("항목 없음")
        else:
            if doc.analysis_state != "available":
                lines.append("분석되지 않음")
            else:
                lines.extend(_item_lines(doc, section, markdown=markdown))
    return "\n".join(lines).rstrip() + "\n"


async def format_txt(document: MeetingDocumentV1, part: str) -> bytes:
    if part != "document":
        raise ValueError("document part required")
    return _render_document(document, markdown=False).encode("utf-8")


async def format_md(document: MeetingDocumentV1, part: str) -> bytes:
    if part != "document":
        raise ValueError("document part required")
    return _render_document(document, markdown=True).encode("utf-8")


async def format_json(document: MeetingDocumentV1, part: str) -> bytes:
    if part != "document":
        raise ValueError("document part required")
    return json.dumps(document.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        probe = value.lstrip()
        index = 0
        while index < len(probe) and (probe[index].isspace() or unicodedata.category(probe[index]) in ("Cc", "Cf")):
            index += 1
        if value[:1] in ("\t", "\r", "\n") or (index < len(probe) and probe[index] in "=+-@"):
            return "'" + value
    return value


def _csv_rows(document: MeetingDocumentV1, part: str):
    if part == "utterances":
        transcript = document.transcript
        if transcript is None or not transcript.timed:
            raise ValueError("timed transcript required")
        job_id = str(transcript.adopted_stt_job_id) if transcript.adopted_stt_job_id else None
        return [{"adopted_stt_job_id": job_id, "utterance_id": item.utterance_id, "start_seconds": item.start,
            "end_seconds": item.end, "speaker_id": item.speaker_id, "speaker_label": item.speaker_label,
            "speaker_name": item.speaker_name, "text": item.text,
            "verification_status": item.verification_status, "overlap": item.overlap}
            for item in transcript.utterances]
    if part not in ("decisions", "actions", "discussions", "open_issues"):
        raise ValueError("unsupported CSV part")
    if part not in document.included_sections or document.analysis_state != "available":
        raise ValueError("generated selected analysis part required")
    return [item.model_dump(mode="json") for item in getattr(document, part)]


async def format_csv(document: MeetingDocumentV1, part: str) -> bytes:
    columns = CSV_COLUMNS.get(part)
    if columns is None:
        raise ValueError("unsupported CSV part")
    rows = _csv_rows(document, part)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_value(row.get(column)) for column in columns])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


FORMATTERS: dict[str, Callable] = {"txt": format_txt, "md": format_md, "json": format_json, "csv": format_csv}
