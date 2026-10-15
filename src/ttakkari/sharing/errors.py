from fastapi import HTTPException


def error(status: int, code: str, message: str = '요청을 처리할 수 없습니다.', *, retry: bool = False):
    return HTTPException(status, {'code': code, 'message': message}, headers={'Retry-After': '60'} if retry else None)


def unavailable(status: int = 404):
    return error(status, 'share_unavailable', '공유를 사용할 수 없습니다.')
