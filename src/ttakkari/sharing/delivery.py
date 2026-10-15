"""Formatter integration boundary. Only verified F1 meeting formatters are registered.

A formatter receives only the approved strict DTO and selected part, never a DB,
resource ID, file path or capability. All bytes pass final_gate before response.start.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import replace

from fastapi import HTTPException, Request
from starlette.responses import Response

from ttakkari.config import get_settings
from ttakkari.sharing import service
from ttakkari.sharing.errors import error, unavailable
from ttakkari.sharing.exports import FORMATTERS
from ttakkari.sharing.schemas import MeetingDocumentV1

# Each formatter accepts only the approved DTO and the already validated part.
Formatter = Callable[[MeetingDocumentV1, str], Awaitable[bytes]]
MEDIA = {'json': 'application/json', 'txt': 'text/plain; charset=utf-8',
         'md': 'text/markdown; charset=utf-8', 'csv': 'text/csv; charset=utf-8'}
MAX_BYTES = 8 * 1024 * 1024


class WindowLimiter:
    """Bounded process-local windows. Eviction never removes an unexpired bucket."""
    def __init__(self):
        self.buckets = OrderedDict()

    def reset(self):
        self.buckets.clear()

    def consume(self, key, limit):
        now = time.monotonic()
        while self.buckets and next(iter(self.buckets.values()))[0] <= now - 60:
            self.buckets.popitem(last=False)
        start, count = self.buckets.get(key, (now, 0))
        if count >= limit or (key not in self.buckets and len(self.buckets) >= 2048):
            raise error(429, 'rate_limited', retry=True)
        self.buckets[key] = (start, count + 1)


failures, pages, downloads = WindowLimiter(), WindowLimiter(), WindowLimiter()
_generating = 0


async def authenticate(request: Request, sid: str):
    try:
        grant = await service.access(sid, request.headers.get('authorization'))
    except HTTPException as exc:
        if exc.status_code == 404 and get_settings().public_sharing_enabled:
            # Use configured proxy policy; never bucket by a caller-supplied share id.
            from ttakkari.security.auth import client_ip
            failures.consume(client_ip(request) or 'unknown', 30)
        raise
    return grant


def parts_for(doc, fmt):
    if fmt in ('txt', 'md', 'json'):
        return ['document']
    if fmt == 'csv':
        return [p for p in ('utterances', 'decisions', 'actions', 'discussions', 'open_issues')
                if ('transcript' if p == 'utterances' else p) in doc.included_sections
                and (doc.transcript is not None and doc.transcript.timed if p == 'utterances'
                     else doc.analysis_state == 'available')]
    return []


def download_options(doc, permitted):
    return [{'format': f, 'parts': parts_for(doc, f)} for f in permitted if f in FORMATTERS and parts_for(doc, f)]


def format_options(doc):
    options = []
    for fmt in ('txt', 'md', 'json', 'csv', 'srt', 'vtt', 'docx', 'pdf'):
        parts = parts_for(doc, fmt)
        enabled = fmt in FORMATTERS
        analysis_pending = (fmt == 'csv' and doc.analysis_state == 'not_generated'
                            and any(section in doc.included_sections for section in
                                    ('decisions', 'actions', 'discussions', 'open_issues')))
        reason = None if parts and enabled else (
            'format_not_enabled' if not enabled else 'analysis_not_generated' if analysis_pending
            else 'format_not_applicable')
        applicable = bool(parts) if fmt == 'csv' else fmt in ('txt', 'md', 'json')
        options.append({'format': fmt, 'enabled': enabled, 'applicable': applicable,
                        'parts': parts if enabled else [], 'reason': reason})
    return options


class AuthorizedResponse(Response):
    def __init__(self, grant, body=b'', **kwargs):
        super().__init__(body, **kwargs)
        self.grant = grant

    async def __call__(self, scope, receive, send):
        await service.final_gate(self.grant)
        await super().__call__(scope, receive, send)


async def view(request: Request, grant):
    pages.consume(str(grant.share_id), 120)
    if request.headers.get('range'):
        raise error(416, 'range_not_supported')
    body = grant.envelope.model_dump_json().encode()
    return AuthorizedResponse(grant, b'' if request.method == 'HEAD' else body, media_type='application/json')


async def download(request: Request, grant):
    global _generating
    downloads.consume(str(grant.share_id), 10)
    q = request.query_params
    if set(q) - {'format', 'part', 'revision'} or any(len(q.getlist(k)) != 1 for k in q):
        raise error(422, 'validation')
    fmt, part, revision = q.get('format'), q.get('part', 'document'), q.get('revision', '')
    if not revision.isascii() or not revision.isdecimal() or len(revision) > 10 or int(revision) < 1:
        raise error(422, 'validation')
    if part not in ('document', 'utterances', 'decisions', 'actions', 'discussions', 'open_issues', 'captions'):
        raise error(422, 'validation')
    if fmt not in FORMATTERS or fmt not in MEDIA:
        raise error(422, 'format_not_enabled', '이 형식은 아직 지원하지 않습니다.')
    if int(revision) != grant.publication_revision:
        raise error(409, 'revision_conflict')
    if fmt not in grant.download_formats:
        raise error(403, 'download_not_allowed')
    selected = 'transcript' if part == 'utterances' else part
    if part != 'document' and selected not in grant.envelope.included_sections:
        raise error(403, 'download_not_allowed')
    if part not in parts_for(grant.envelope.document, fmt):
        if (fmt == 'csv' and selected in ('decisions', 'actions', 'discussions', 'open_issues')
                and grant.envelope.document.analysis_state == 'not_generated'):
            raise error(409, 'analysis_not_generated')
        raise error(409, 'format_not_applicable')
    if request.headers.get('range'):
        raise error(416, 'range_not_supported')
    grant = replace(grant, delivery_format=fmt)
    if request.method == 'HEAD':
        return AuthorizedResponse(grant, media_type=MEDIA[fmt])
    if _generating >= 2:
        raise error(503, 'export_busy', retry=True)
    _generating += 1
    try:
        body = await FORMATTERS[fmt](grant.envelope.document.model_copy(deep=True), part)
        if not isinstance(body, bytes):
            raise unavailable(503)
        if len(body) > MAX_BYTES:
            raise error(413, 'export_too_large')
    finally:
        _generating -= 1
    filename = f'meeting-{grant.share_id}-r{grant.publication_revision}.{fmt}'
    return AuthorizedResponse(grant, body, media_type=MEDIA[fmt], headers={
        'Content-Disposition': f"attachment; filename=\"{filename}\"; filename*=UTF-8''{filename}", 'Accept-Ranges': 'none'})
