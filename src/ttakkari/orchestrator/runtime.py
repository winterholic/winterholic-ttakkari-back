"""오케스트레이터 Run 에 붙이는 것들: 세션 한정 토큰, MCP 서버 실행 명세, 역할 지침(ADR 0006)."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ttakkari.config import get_settings
from ttakkari.security.auth import ALGO, ISSUER, decode

AUDIENCE = "ttakkari-orch"
MCP_SERVER_NAME = "ttakkari"
_bearer = HTTPBearer(auto_error=False)

ORCHESTRATOR_NOTE = """\
You are the user's main orchestrator in Winterholic Ttakkari. The user talks only to you, often from a phone, \
and may send several messages in a row without waiting; they are queued and reach you one turn at a time.
- Delegate substantive work (coding, research, file work, long commands) to worker sessions with the \
`ttakkari` MCP tools: spawn_worker for a new task, message_worker to follow up on an existing one, \
list_workers/get_worker to check progress, stop_worker to cancel. Workers run in parallel and show up as tabs \
in the user's UI, where the user can also talk to them directly.
- Keep your own turns short: decide, delegate, tell the user in one or two sentences what you started. \
Do not wait or poll for workers to finish; you will receive a message starting with "[작업 보고]" when one ends.
- On a "[작업 보고]" message, summarize the outcome for the user, and delegate follow-up work if it is clearly \
needed. Do not repeat the whole worker output.
- Reuse an existing worker (message_worker) when the request continues its task; spawn a new one for unrelated work.
- Small questions you can answer directly without tools, answer directly."""


def issue_token(session_id: uuid.UUID) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    # Run 시간 제한보다 조금 길게. Run 이 끝나면 프로세스와 함께 버려진다.
    exp = now + timedelta(seconds=s.run_timeout_seconds + 600)
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "orchestrator", "sid": str(session_id), "iat": now, "exp": exp,
               "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGO)


async def require_orchestrator(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 필요합니다.")
    try:
        claims = decode(creds.credentials, AUDIENCE)
        return uuid.UUID(claims["sid"])
    except (jwt.PyJWTError, KeyError, ValueError) as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "유효하지 않은 오케스트레이터 토큰입니다.") from e


OrchestratorSession = Annotated[uuid.UUID, Depends(require_orchestrator)]


def mcp_server_spec(session_id: uuid.UUID) -> dict[str, Any]:
    """엔진에 넘길 stdio MCP 서버 명세. TTAKKARI_* 는 자식 env 에서 지워지므로 접두사를 달리 쓴다."""
    s = get_settings()
    return {
        "command": sys.executable,
        "args": ["-m", "ttakkari.orchestrator.mcp_server"],
        "env": {"TTK_ORCH_TOKEN": issue_token(session_id), "TTK_API_URL": s.internal_api_url},
    }
