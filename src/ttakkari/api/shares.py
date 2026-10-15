from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from starlette.responses import Response

from ttakkari.security.auth import User
from ttakkari.sharing import service
from ttakkari.sharing.schemas import ExportIn, PreviewIn, PublishIn, RepublishIn

router = APIRouter(prefix='/api/shares', tags=['shares'])
exports_router = APIRouter(prefix='/api/exports', tags=['exports'])


@exports_router.post('')
async def export(body: ExportIn, _: User):
    content, media_type = await service.owner_export(body)
    return Response(content, media_type=media_type, headers={
        'Content-Disposition': f"attachment; filename=\"meeting-export.{body.format}\"; "
        f"filename*=UTF-8''meeting-export.{body.format}",
        'Accept-Ranges': 'none',
    })


@router.post('/preview')
async def preview(body: PreviewIn, _: User):
    return await service.preview(body)


@router.post('', status_code=201)
async def create(body: PublishIn, _: User):
    return await service.publish(body)


@router.get('')
async def listing(_: User, resource_type: Literal['meeting', 'pms_project', 'wbs', 'artifact'],
                  resource_id: uuid.UUID, offset: Annotated[int, Query(ge=0)] = 0):
    return await service.owner_list(resource_type, resource_id, offset)


@router.get('/{sid}')
async def get(sid: uuid.UUID, _: User):
    return await service.owner_get(sid)


@router.put('/{sid}/publication')
async def republish(sid: uuid.UUID, body: RepublishIn, _: User):
    return await service.publish(body, sid)


@router.delete('/{sid}', status_code=204)
async def revoke(sid: uuid.UUID, _: User):
    await service.revoke(sid)
