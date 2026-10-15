from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

Section = Literal['summary', 'decisions', 'actions', 'discussions', 'open_issues', 'transcript']
Format = Literal['txt', 'md', 'json', 'csv', 'srt', 'vtt', 'docx', 'pdf']
SECTIONS = ('summary', 'decisions', 'actions', 'discussions', 'open_issues', 'transcript')
Revision = Annotated[str, Field(pattern=r'^sha256:[0-9a-f]{64}$')]


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Resource(StrictDTO):
    type: Literal['meeting', 'pms_project', 'wbs', 'artifact']
    id: uuid.UUID


class Selection(StrictDTO):
    sections: list[Section] = Field(min_length=1, max_length=6)
    include_people: StrictBool = False
    include_evidence: StrictBool = False

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.sections)) != len(self.sections):
            raise ValueError('duplicate sections')
        if self.include_evidence and 'transcript' not in self.sections:
            raise ValueError('evidence_requires_transcript')
        return self


class PreviewIn(StrictDTO):
    resource: Resource
    selection: Selection
    download_formats: list[Format] = Field(default_factory=list, max_length=8)
    confirm_work_scope: StrictBool = False

    @field_validator('download_formats')
    @classmethod
    def unique_formats(cls, v):
        if len(set(v)) != len(v):
            raise ValueError('duplicate formats')
        return v


class PublishIn(PreviewIn):
    expected_source_revision: Revision
    expires_at: datetime = Field(default_factory=lambda: _default_expiry())

    @field_validator('expires_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('timezone required')
        return value


def _default_expiry():
    from datetime import UTC, timedelta
    return datetime.now(UTC) + timedelta(days=7)


class RepublishIn(PublishIn):
    expected_publication_revision: Annotated[StrictInt, Field(ge=1)]


class ExportIn(StrictDTO):
    resource: Resource
    selection: Selection
    format: Format
    part: Literal['document', 'utterances', 'decisions', 'actions', 'discussions', 'open_issues', 'captions'] = 'document'
    expected_source_revision: Revision


class EvidenceItem(StrictDTO):
    item_id: uuid.UUID
    position: StrictInt
    text: str
    detail: str | None
    status: Literal['open', 'done', 'cancelled']
    evidence_quote: str | None
    evidence_start: float | None
    evidence_utterance_id: str | None


class ActionItem(EvidenceItem):
    owner: str | None
    due_text: str | None


class Utterance(StrictDTO):
    utterance_id: str
    start: Annotated[float, Field(ge=0)]
    end: Annotated[float, Field(ge=0)]
    speaker_id: str | None
    speaker_label: str | None
    speaker_name: str | None
    text: str
    verification_status: Literal['unverified', 'needs_review']
    overlap: StrictBool | None

    @model_validator(mode='after')
    def ordered(self):
        if self.end < self.start:
            raise ValueError('invalid timing')
        return self


class Review(StrictDTO):
    status: Literal['unverified', 'needs_review']
    review_utterance_count: StrictInt
    unknown_speaker_count: StrictInt


class Transcript(StrictDTO):
    adopted_stt_job_id: uuid.UUID | None
    source: Literal['local_whisper', 'paste', 'file']
    timed: StrictBool
    duration_seconds: Annotated[float, Field(ge=0)] | None
    text: str
    utterances: list[Utterance] = Field(max_length=10000)
    review: Review


class MeetingDocumentV1(StrictDTO):
    schema_version: Literal[1] = 1
    kind: Literal['meeting'] = 'meeting'
    title: str
    held_at: datetime | None
    included_sections: list[Section]
    section_states: dict[Section, Literal['available', 'empty', 'not_generated', 'not_selected']]
    analysis_state: Literal['available', 'not_generated']
    participants: list[str]
    summary: str | None
    decisions: list[EvidenceItem] = Field(max_length=5000)
    actions: list[ActionItem] = Field(max_length=5000)
    discussions: list[EvidenceItem] = Field(max_length=5000)
    open_issues: list[EvidenceItem] = Field(max_length=5000)
    transcript: Transcript | None

    @model_validator(mode='after')
    def all_states(self):
        if set(self.section_states) != set(SECTIONS):
            raise ValueError('section states incomplete')
        return self


class DownloadOption(StrictDTO):
    format: Format
    parts: list[str]


class PublicShareDocument(StrictDTO):
    schema_version: Literal[1] = 1
    share_id: uuid.UUID
    resource_type: Literal['meeting'] = 'meeting'
    publication_revision: StrictInt
    published_at: datetime
    expires_at: datetime
    included_sections: list[Section]
    document: MeetingDocumentV1
    downloads: list[DownloadOption]
