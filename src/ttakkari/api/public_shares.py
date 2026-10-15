from fastapi import APIRouter, Request

from ttakkari.sharing import delivery

router = APIRouter(prefix='/api/public/shares', tags=['public shares'])


@router.api_route('/{sid}', methods=['GET', 'HEAD'])
async def get(sid: str, request: Request):
    grant = await delivery.authenticate(request, sid)
    return await delivery.view(request, grant)


@router.api_route('/{sid}/download', methods=['GET', 'HEAD'])
async def download(sid: str, request: Request):
    grant = await delivery.authenticate(request, sid)
    return await delivery.download(request, grant)
