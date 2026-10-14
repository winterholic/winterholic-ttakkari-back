"""파일 본문 전송 진단. 인증 값·파일 내용은 기록하지 않고 수신/저장 단계를 구분한다."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("uvicorn.error.transfer")
RAW_PATH = "/api/transfers/raw"


def fields(request: Request) -> dict:
    return dict(getattr(request.state, "raw_transfer", {}))


def emit(request: Request, event: str, *, exception: bool = False, **extra) -> None:
    entry = {"at": datetime.now(UTC).isoformat(), "event": event, **fields(request), **extra}
    log.log(logging.ERROR if exception else logging.INFO, json.dumps(entry, ensure_ascii=False), exc_info=exception)


class TransferDiagnosticsMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != RAW_PATH:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        state = scope.setdefault("state", {})["raw_transfer"] = {
            "requestId": uuid.uuid4().hex, "filename": request.query_params.get("filename"),
            "contentLength": request.headers.get("content-length"), "receivedBytes": None,
            "authPassed": False, "savedPath": None, "success": False,
        }
        # None은 아직 본문을 읽지 않았다는 뜻이다. 인증/라우팅 실패를 0바이트 업로드로 오인하지 않는다.
        emit(request, "request_received", method=request.method,
             authorizationPresent=bool(request.headers.get("authorization")),
             contentType=request.headers.get("content-type"))
        started = False
        status = 500

        async def counted_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                state["receivedBytes"] = (state["receivedBytes"] or 0) + len(message.get("body", b""))
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                started, status = True, message["status"]
                if status >= 400:
                    state["success"] = False
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except Exception:
            state["success"] = False
            emit(request, "request_failed", exception=True, stage=getattr(request.state, "transfer_stage", "routing_or_auth"))
            if started:
                raise
            response = JSONResponse({"code": "upload_failed", "message": "파일 전송 중 오류가 발생했습니다. requestId로 서버 로그를 확인해 주세요.",
                                     **fields(request)}, status_code=500)
            await response(scope, counted_receive, tracked_send)
        finally:
            emit(request, "request_finished", status=status)
