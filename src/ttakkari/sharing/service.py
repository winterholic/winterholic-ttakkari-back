"""Short PostgreSQL authorization transactions; no model or filesystem access."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.meetings.document import build_meeting_document, canonical
from ttakkari.models import LocalSttJob, Meeting, MeetingItem, PublicShare
from ttakkari.sharing.errors import error, unavailable
from ttakkari.sharing.schemas import (
    ExportIn,
    MeetingDocumentV1,
    PreviewIn,
    PublicShareDocument,
    PublishIn,
    RepublishIn,
)

KEY = re.compile(r'^ps1_[A-Za-z0-9_-]{43}$')


@asynccontextmanager
async def transaction(*, repeatable=False, readonly=False):
    try:
        async with sessionmaker()() as db, db.begin():
            if repeatable:
                await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ' + (' READ ONLY' if readonly else '')))
            # Bound locks and queries even when another request holds a row.
            await db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            yield db
    except IntegrityError as exc:
        raise error(409, 'already_shared') from exc
    except DBAPIError as exc:
        # Never include driver exception text/parameters in responses or logs.
        if exc.orig and getattr(exc.orig, 'sqlstate', None) in ('40001', '40P01'):
            raise error(409, 'revision_conflict') from exc
        raise unavailable(503) from exc


def enabled():
    if not get_settings().public_sharing_enabled:
        raise error(409, 'sharing_disabled')


async def candidate(db, body: PreviewIn, *, lock=False):
    from ttakkari.sharing.delivery import FORMATTERS
    if body.resource.type != 'meeting':
        raise error(422, 'resource_not_enabled')
    if any(f not in FORMATTERS for f in body.download_formats):
        raise error(422, 'format_not_enabled', '이 형식은 아직 지원하지 않습니다.')
    try:
        c = await build_meeting_document(db, body.resource.id, body.selection, lock=lock)
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise error(409, 'source_not_ready') from exc
    if c.scope == 'work' and not body.confirm_work_scope:
        raise error(403, 'work_scope_confirmation_required')
    return c


async def preview(body: PreviewIn):
    async with transaction(repeatable=True, readonly=True) as db:
        c = await candidate(db, body)
    from ttakkari.sharing.delivery import format_options
    sharing_ready = get_settings().public_sharing_enabled
    return {'document': c.document.model_dump(mode='json'), 'source_revision': c.source_revision,
            'format_options': format_options(c.document), 'public_sharing_enabled': sharing_ready,
            'warnings': ['content_requires_owner_review'] + ([] if sharing_ready else ['sharing_disabled'])}


async def owner_export(body: ExportIn) -> tuple[bytes, str]:
    """Build a current owner projection and export only after its revision matches."""
    from ttakkari.sharing.delivery import FORMATTERS, MAX_BYTES, MEDIA, parts_for
    if body.resource.type != 'meeting':
        raise error(422, 'resource_not_enabled')
    formatter = FORMATTERS.get(body.format)
    if formatter is None:
        raise error(422, 'format_not_enabled', '이 형식은 아직 지원하지 않습니다.')
    async with transaction(repeatable=True, readonly=True) as db:
        try:
            c = await build_meeting_document(db, body.resource.id, body.selection)
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            raise error(409, 'source_not_ready') from exc
    if c.source_revision != body.expected_source_revision:
        raise error(409, 'source_changed')
    available_parts = parts_for(c.document, body.format)
    if body.part not in available_parts:
        if (body.format == 'csv' and body.part in ('decisions', 'actions', 'discussions', 'open_issues')
                and body.part in body.selection.sections and c.document.analysis_state == 'not_generated'):
            raise error(409, 'analysis_not_generated')
        raise error(409, 'format_not_applicable')
    try:
        result = await formatter(c.document.model_copy(deep=True), body.part)
    except (ValueError, TypeError) as exc:
        raise error(409, 'format_not_applicable') from exc
    if not isinstance(result, bytes):
        raise unavailable(503)
    if len(result) > MAX_BYTES:
        raise error(413, 'export_too_large')
    return result, MEDIA[body.format]


def revoke_row(row, now, reason='manual'):
    row.state, row.revoked_at, row.revoked_reason = 'revoked', now, reason
    row.secret_hash, row.snapshot, row.source_bindings = None, None, []


async def live_bindings(db, row, *, lock=False):
    """Lock root -> selected bindings -> grant (caller), also used by final gate.

    Deletes/scope updates revoke through DB triggers. Taking the grant last avoids
    a grant/item lock inversion with deletion triggers. No source text is read.
    """
    if row.resource_type != 'meeting' or row.snapshot_schema_version != 1 or not row.snapshot:
        return False
    try:
        doc = MeetingDocumentV1.model_validate(row.snapshot)
        expected = [{'type': 'meeting', 'id': str(row.resource_id), 'parent_id': None, 'role': 'root'}]
        if doc.transcript and doc.transcript.adopted_stt_job_id:
            expected.append({'type': 'job', 'id': str(doc.transcript.adopted_stt_job_id), 'parent_id': str(row.resource_id), 'role': 'transcript'})
        for section in ('actions', 'decisions', 'discussions', 'open_issues'):
            expected += [{'type': 'item', 'id': str(it.item_id), 'parent_id': str(row.resource_id), 'role': section}
                         for it in getattr(doc, section)]
        if sorted(canonical(b) for b in expected) != sorted(canonical(b) for b in row.source_bindings):
            return False
        root = select(Meeting.scope).where(Meeting.id == row.resource_id)
        if lock:
            root = root.with_for_update(read=True)
        if await db.scalar(root) != row.source_scope:
            return False
        jobs = [uuid.UUID(b['id']) for b in expected if b['type'] == 'job']
        if jobs:
            stmt = select(LocalSttJob.id).where(LocalSttJob.id.in_(jobs), LocalSttJob.meeting_id == row.resource_id,
                LocalSttJob.status == 'succeeded', LocalSttJob.parent_job_id.is_(None)).order_by(LocalSttJob.id)
            if lock:
                stmt = stmt.with_for_update(read=True)
            if set(await db.scalars(stmt)) != set(jobs):
                return False
        kinds = {'actions': 'action', 'decisions': 'decision', 'discussions': 'discussion', 'open_issues': 'open_issue'}
        items = {uuid.UUID(b['id']): kinds[b['role']] for b in expected if b['type'] == 'item'}
        if items:
            stmt = select(MeetingItem.id, MeetingItem.kind).where(MeetingItem.id.in_(items),
                MeetingItem.meeting_id == row.resource_id).order_by(MeetingItem.id)
            if lock:
                stmt = stmt.with_for_update(read=True)
            if dict((await db.execute(stmt)).all()) != items:
                return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


async def effective_state(db, row):
    if row.state != 'active':
        return row.state
    if row.expires_at <= await db.scalar(select(func.clock_timestamp())):
        return 'expired'
    return 'active' if await live_bindings(db, row) else 'unavailable'


async def owner_out(db, row):
    return {'id': str(row.id), 'resource': {'type': row.resource_type, 'id': str(row.resource_id)},
        'state': row.state, 'effective_state': await effective_state(db, row),
        'publication_revision': row.publication_revision, 'source_revision': row.source_revision,
        'selection': row.selection, 'download_formats': row.download_formats,
        'published_at': row.published_at.isoformat(), 'expires_at': row.expires_at.isoformat(),
        'revoked_at': row.revoked_at.isoformat() if row.revoked_at else None,
        'revoked_reason': row.revoked_reason, 'can_recover_link': False}


async def publish(body: PublishIn, sid: uuid.UUID | None = None):
    enabled()
    raw = None
    async with transaction(repeatable=True) as db:
        c = await candidate(db, body, lock=True)
        now = await db.scalar(select(func.clock_timestamp()))
        if not now < body.expires_at <= now + timedelta(days=90):
            raise error(422, 'invalid_expiry')
        if c.source_revision != body.expected_source_revision:
            raise error(409, 'source_changed')
        if sid is None:
            previous = await db.scalar(select(PublicShare).where(PublicShare.resource_type == 'meeting',
                PublicShare.resource_id == body.resource.id, PublicShare.state == 'active').with_for_update())
            if previous:
                if previous.expires_at <= now or not await live_bindings(db, previous):
                    revoke_row(previous, now, 'expired' if previous.expires_at <= now else 'source_unavailable')
                    await db.flush()
                else:
                    raise error(409, 'already_shared')
            raw = 'ps1_' + secrets.token_urlsafe(32)
            row = PublicShare(id=uuid.uuid4(), resource_type='meeting', resource_id=body.resource.id,
                created_by='owner', secret_hash=hashlib.sha256(raw.encode()).hexdigest(), state='active',
                publication_revision=1, created_at=now)
            db.add(row)
        else:
            row = await db.scalar(select(PublicShare).where(PublicShare.id == sid).with_for_update())
            if row is None:
                raise error(404, 'not_found')
            if row.resource_id != body.resource.id or row.resource_type != body.resource.type:
                raise error(409, 'revision_conflict')
            if await effective_state(db, row) != 'active':
                raise error(409, 'share_unavailable')
            assert isinstance(body, RepublishIn)
            if row.publication_revision != body.expected_publication_revision:
                raise error(409, 'revision_conflict')
            row.publication_revision += 1
        row.source_scope, row.selection = c.scope, body.selection.model_dump()
        row.download_formats = body.download_formats
        row.source_revision = c.source_revision
        row.snapshot_schema_version, row.snapshot = 1, c.document.model_dump(mode='json')
        row.source_bindings, row.published_at, row.expires_at = c.bindings, now, body.expires_at
        await db.flush()
        record(db, actor='owner', action='share.create' if sid is None else 'share.republish',
               target_type='public_share', target_id=row.id)
        result = await owner_out(db, row)
    if raw is not None:
        origin = get_settings().public_share_origin.rstrip('/')
        result['capability_url'] = f'{origin}/share/{row.id}#key={raw}'
    return result


async def revoke(sid):
    async with transaction() as db:
        row = await db.scalar(select(PublicShare).where(PublicShare.id == sid).with_for_update())
        if row is None:
            raise error(404, 'not_found')
        if row.state != 'revoked':
            revoke_row(row, await db.scalar(select(func.clock_timestamp())))
            record(db, actor='owner', action='share.revoke', target_type='public_share', target_id=row.id)


async def owner_get(sid):
    async with transaction() as db:
        row = await db.get(PublicShare, sid)
        if row is None:
            raise error(404, 'not_found')
        return await owner_out(db, row)


async def owner_list(resource_type, resource_id, offset=0):
    if resource_type != 'meeting':
        raise error(422, 'resource_not_enabled')
    async with transaction() as db:
        rows = await db.scalars(select(PublicShare).where(PublicShare.resource_type == resource_type,
            PublicShare.resource_id == resource_id).order_by(PublicShare.created_at.desc(), PublicShare.id).offset(offset).limit(100))
        return [await owner_out(db, row) for row in rows]


@dataclass(frozen=True)
class ShareAccess:
    share_id: uuid.UUID
    secret_hash: str
    publication_revision: int
    envelope: PublicShareDocument
    download_formats: tuple[str, ...] = ()
    delivery_format: str | None = None


async def _check(db, row, digest):
    if row is None or not row.secret_hash or not hmac.compare_digest(row.secret_hash, digest):
        raise unavailable()
    if row.state != 'active' or row.expires_at <= await db.scalar(select(func.clock_timestamp())):
        raise unavailable()
    if row.snapshot_schema_version != 1:
        raise unavailable(503)
    try:
        MeetingDocumentV1.model_validate(row.snapshot)
    except ValidationError as exc:
        raise unavailable(503) from exc
    if not await live_bindings(db, row):
        raise unavailable()


async def access(sid: str, authorization: str | None):
    if not get_settings().public_sharing_enabled:
        raise unavailable()
    try:
        if len(sid) > 36:
            raise ValueError()
        ident = uuid.UUID(sid)
        if not authorization or len(authorization) > 64 or not authorization.startswith('Bearer '):
            raise ValueError()
        raw = authorization[7:]
        if not KEY.fullmatch(raw):
            raise ValueError()
    except ValueError as exc:
        raise unavailable() from exc
    digest = hashlib.sha256(raw.encode()).hexdigest()
    async with transaction() as db:
        row = await db.get(PublicShare, ident)
        await _check(db, row, digest)
        try:
            doc = MeetingDocumentV1.model_validate(row.snapshot)
        except ValidationError as exc:
            raise unavailable(503) from exc
        from ttakkari.sharing.delivery import download_options
        envelope = PublicShareDocument(share_id=row.id, publication_revision=row.publication_revision,
            published_at=row.published_at, expires_at=row.expires_at,
            included_sections=doc.included_sections, document=doc, downloads=download_options(doc, row.download_formats))
        return ShareAccess(ident, digest, row.publication_revision, envelope, tuple(row.download_formats))


async def final_gate(grant: ShareAccess):
    """Linearization point immediately before ASGI response.start, never during generation."""
    if not get_settings().public_sharing_enabled:
        raise unavailable()
    async with transaction() as db:
        initial = await db.get(PublicShare, grant.share_id)
        if initial is None:
            raise unavailable()
        # Lock resources first. Delete/reparent triggers take the grant last too.
        if not await live_bindings(db, initial, lock=True):
            raise unavailable()
        row = await db.scalar(select(PublicShare).where(PublicShare.id == grant.share_id)
                              .execution_options(populate_existing=True).with_for_update(read=True))
        await _check(db, row, grant.secret_hash)
        if row.publication_revision != grant.publication_revision:
            raise error(409, 'revision_conflict', '공개 내용이 갱신되었습니다. 다시 조회해 주세요.')
        # DB wall clock, after all lock waits; transaction now() would be stale.
        if row.expires_at <= await db.scalar(select(func.clock_timestamp())):
            raise unavailable()
        record(db, actor='anonymous', action='share.download' if grant.delivery_format else 'share.read',
               target_type='public_share', target_id=row.id,
               detail={'format': grant.delivery_format} if grant.delivery_format else {})


async def cleanup():
    count = 0
    async with transaction() as db:
        ids = list(await db.scalars(select(PublicShare.id).where(PublicShare.state == 'active').limit(1000)))
    for sid in ids:
        async with transaction() as db:
            row = await db.get(PublicShare, sid)
            revision = row.publication_revision
            valid = await live_bindings(db, row, lock=True)
            row = await db.scalar(select(PublicShare).where(PublicShare.id == sid)
                .execution_options(populate_existing=True).with_for_update())
            now = await db.scalar(select(func.clock_timestamp()))
            if row.state == 'active' and row.publication_revision == revision and (row.expires_at <= now or not valid):
                revoke_row(row, now, 'expired' if row.expires_at <= now else 'source_unavailable')
                count += 1
    return count
