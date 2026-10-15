"""Pure ASGI headers also cover validation, auth, CORS and final gate errors."""
import logging
import uuid

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

HEADERS = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff', 'X-Robots-Tag': 'noindex, nofollow, noarchive'}


class PublicAccessFilter(logging.Filter):
    """Uvicorn logs raw query strings. Replace public access with our safe event."""
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            path = str(record.args[2]).split('?', 1)[0]
            if path == '/api/public/shares' or path.startswith('/api/public/shares/'):
                return False
        return True


class ShareSecurityMiddleware:
    def __init__(self, app):
        self.app = app
        access_log = logging.getLogger('uvicorn.access')
        if not any(isinstance(f, PublicAccessFilter) for f in access_log.filters):
            access_log.addFilter(PublicAccessFilter())

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        public = path == '/api/public/shares' or path.startswith('/api/public/shares/')
        owner_share = path == '/api/shares' or path.startswith('/api/shares/')
        owner_export = path == '/api/exports' or path.startswith('/api/exports/')
        if scope['type'] != 'http' or not (public or owner_share or owner_export):
            return await self.app(scope, receive, send)
        started = False
        request_id = uuid.uuid4().hex

        async def secure_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                headers = MutableHeaders(scope=message)
                for k, v in HEADERS.items():
                    headers[k] = v
                if public:
                    headers.add_vary_header('Origin, Authorization')
                    template = '/api/public/shares/{sid}' + ('/download' if path.endswith('/download') else '')
                    logging.getLogger('ttakkari.public_access').info(
                        'path=%s status=%s request_id=%s', template, message['status'], request_id)
                for k in ('etag', 'last-modified'):
                    if k in headers:
                        del headers[k]
            await send(message)
        try:
            await self.app(scope, receive, secure_send)
        except Exception:
            if started or not public:
                raise
            # Don't log exception/SQL params, the header, URL/query or snapshot.
            response = JSONResponse({'code': 'share_unavailable', 'message': '공유를 사용할 수 없습니다.'}, status_code=503)
            await response(scope, receive, secure_send)
