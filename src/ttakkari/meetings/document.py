"""Pure reads, explicit public allowlist. Never call the owner detail/reconcile handler."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ttakkari.meetings.evidence import locate_evidence
from ttakkari.meetings.local_stt.read_model import adopted_job, job_utterances
from ttakkari.models import Meeting, MeetingItem, MeetingStatus
from ttakkari.sharing.errors import error
from ttakkari.sharing.schemas import (
    SECTIONS,
    MeetingDocumentV1,
    Selection,
    Transcript,
    Utterance,
)

KINDS = {'decisions': 'decision', 'actions': 'action', 'discussions': 'discussion', 'open_issues': 'open_issue'}
PRIVATE_LINK = re.compile(r'/api/(?:dl|view)/|/share/[^\s#]+#key=|ps1_[A-Za-z0-9_-]{43}')


@dataclass(frozen=True)
class Candidate:
    document: MeetingDocumentV1
    scope: str
    bindings: list[dict]
    source_revision: str


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


async def build_meeting_document(db: AsyncSession, mid: uuid.UUID, selection: Selection, *, lock=False) -> Candidate:
    columns = [Meeting.id, Meeting.scope, Meeting.status, Meeting.title, Meeting.held_at]
    if selection.include_people:
        columns.append(Meeting.participants)
    if 'summary' in selection.sections:
        columns.append(Meeting.summary)
    if 'transcript' in selection.sections:
        columns += [Meeting.transcript, Meeting.transcript_source, Meeting.provider_job_id]
    stmt = select(Meeting).options(load_only(*columns)).where(Meeting.id == mid)
    if lock:
        stmt = stmt.with_for_update()
    m = await db.scalar(stmt)
    if m is None:
        raise error(404, 'not_found')
    if m.status not in (MeetingStatus.transcribed, MeetingStatus.analyzed, MeetingStatus.review):
        raise error(409, 'source_not_ready')
    if m.scope not in ('personal', 'work', 'shared'):
        raise error(409, 'source_not_ready')
    bindings = [{'type': 'meeting', 'id': str(m.id), 'parent_id': None, 'role': 'root'}]
    # Review means analysis finished with pending proposals, not unfinished content.
    analyzed = m.status in (MeetingStatus.analyzed, MeetingStatus.review)
    transcript = None
    utterances = []
    if 'transcript' in selection.sections:
        job = await adopted_job(db, m)
        if m.transcript_source == 'provider:local_whisper' and job is None:
            raise error(409, 'stt_source_unresolved')
        if not m.transcript:
            raise error(409, 'source_not_ready')
        if job:
            meta = job.result_metadata
            if (meta.get('quality', {}).get('auto_analysis_allowed') is False
                    or meta.get('quality_gate', {}).get('blocked') or meta.get('quality_status') == 'blocked'):
                raise error(409, 'quality_review_required')
            utterances = await job_utterances(db, job)
            if len(utterances) > 10000:
                raise error(413, 'export_too_large')
            names = meta.get('speaker_names', {}) if selection.include_people else {}
            rows = [Utterance(utterance_id=str(u['id']), start=u['start'], end=u['end'],
                speaker_id=u.get('speaker_id'), speaker_label=u.get('speaker_label'),
                speaker_name=names.get(u.get('speaker_id')), text=u['text'],
                verification_status='unverified' if u.get('verification_status') == 'unverified' else 'needs_review',
                overlap=u.get('overlap')) for u in utterances]
            rows.sort(key=lambda u: (u.start, u.end))
            duration = meta.get('duration')
            if duration is not None and any(u.end > duration for u in rows):
                raise error(409, 'source_not_ready')
            review = sum(u.verification_status == 'needs_review' for u in rows)
            transcript = Transcript(adopted_stt_job_id=job.id, source='local_whisper', timed=bool(rows),
                duration_seconds=duration,
                text='\n'.join(f'[{u.start:.2f}–{u.end:.2f}] {u.speaker_name or u.speaker_label or "화자 미상"} '
                    f'[{"검증 필요" if u.verification_status == "needs_review" else "미검수"}]: {u.text}' for u in rows),
                utterances=rows, review={'status': 'needs_review' if review else 'unverified',
                    'review_utterance_count': review, 'unknown_speaker_count': sum(u.speaker_id is None for u in rows)})
            bindings.append({'type': 'job', 'id': str(job.id), 'parent_id': str(m.id), 'role': 'transcript'})
        else:
            transcript = Transcript(adopted_stt_job_id=None, source='file' if m.transcript_source == 'file' else 'paste',
                timed=False, duration_seconds=None, text=m.transcript, utterances=[],
                review={'status': 'unverified', 'review_utterance_count': 0, 'unknown_speaker_count': 0})
    groups = {section: [] for section in KINDS}
    selected_kinds = [kind for section, kind in KINDS.items() if section in selection.sections]
    if analyzed and selected_kinds:
        fields = [MeetingItem.id, MeetingItem.meeting_id, MeetingItem.kind, MeetingItem.position, MeetingItem.text,
                  MeetingItem.detail, MeetingItem.status]
        if selection.include_evidence:
            fields.append(MeetingItem.evidence)
        if 'action' in selected_kinds:
            fields.append(MeetingItem.due)
            if selection.include_people:
                fields.append(MeetingItem.owner)
        items = list(await db.scalars(select(MeetingItem).options(load_only(*fields))
            .where(MeetingItem.meeting_id == mid, MeetingItem.kind.in_(selected_kinds))
            .order_by(MeetingItem.kind, MeetingItem.position, MeetingItem.id).limit(5001)))
        if len(items) > 5000:
            raise error(413, 'export_too_large')
        reverse = {kind: section for section, kind in KINDS.items()}
        for it in items:
            start, uid = locate_evidence(it.evidence, utterances) if selection.include_evidence else (None, None)
            value = {'item_id': it.id, 'position': it.position, 'text': it.text, 'detail': it.detail, 'status': it.status,
                'evidence_quote': it.evidence if selection.include_evidence else None,
                'evidence_start': start, 'evidence_utterance_id': uid}
            if it.kind == 'action':
                value.update(owner=it.owner if selection.include_people else None, due_text=it.due)
            groups[reverse[it.kind]].append(value)
            bindings.append({'type': 'item', 'id': str(it.id), 'parent_id': str(mid), 'role': reverse[it.kind]})
    summary = m.summary if analyzed and 'summary' in selection.sections else None
    states = {}
    for section in SECTIONS:
        if section not in selection.sections:
            states[section] = 'not_selected'
        elif section != 'transcript' and not analyzed:
            states[section] = 'not_generated'
        else:
            states[section] = 'available' if (transcript if section == 'transcript' else summary if section == 'summary' else groups[section]) else 'empty'
    doc = MeetingDocumentV1(title=m.title, held_at=m.held_at, included_sections=selection.sections,
        section_states=states, analysis_state='available' if analyzed else 'not_generated',
        participants=m.participants if selection.include_people else [], summary=summary, transcript=transcript, **groups)
    payload = canonical(doc.model_dump(mode='json'))
    if len(payload) > 4 * 1024 * 1024:
        raise error(413, 'export_too_large')
    if PRIVATE_LINK.search(payload.decode()):
        raise error(422, 'private_link_in_content')
    revision = hashlib.sha256(canonical({'document': doc.model_dump(mode='json'), 'bindings': bindings,
        'selection': selection.model_dump(), 'scope': m.scope})).hexdigest()
    return Candidate(doc, m.scope, bindings, 'sha256:' + revision)
