"""SEC2-AUTHZ-03 범위 매트릭스 공용 canary fixture.

1/3 분할(agent.py 기억·메모·일정)에서 만들고, 2/3(host_env·deploy·notebooklm·
stocks·reverse·oplogs·design) · 3/3(orchestrator) 분할이 그대로 재사용한다.

- 개인 canary: 고유 토큰 ``AUTHZ03_P`` 를 포함한 기억·엔터티·메모·일정·파일.
  work 세션 토큰으로 읽히거나 바뀌면 안 된다.
- work 대조 자료: 고유 토큰 ``AUTHZ03_W``. work 세션의 정상 동작 대조군이다.
- 운영 코드는 건드리지 않는다. 이 모듈은 테스트 데이터 생성만 한다.

사용법(각 분할 테스트 파일)::

    from authz03_canary import ensure_authz03

    @pytest.fixture
    async def env(make_dir):
        async with sessionmaker()() as db:
            yield await ensure_authz03(db, make_dir)
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ttakkari.models import (
    Artifact,
    CalendarEvent,
    ChatSession,
    CtxEntity,
    CtxMemory,
    Memo,
    Workspace,
)

#: 개인 canary 식별 토큰. work 토큰 응답 본문에 이 문자열이 있으면 누출이다.
AUTHZ03_P = "authz03canaryp"
#: work 대조 자료 식별 토큰. work 토큰 응답에 있어야 정상이다.
AUTHZ03_W = "authz03canaryw"

PERSONAL_ENTITY_PROJECT = f"{AUTHZ03_P}프로젝트"
PERSONAL_ENTITY_TOPIC = f"{AUTHZ03_P}거래처"
WORK_ENTITY_TOPIC = f"{AUTHZ03_W}거래처"

PERSONAL_TAG = f"{AUTHZ03_P}tag"
WORK_TAG = f"{AUTHZ03_W}tag"

#: 일정 조회 구간(시드 일정이 모두 들어간다).
EVENT_FROM = "2026-11-01"
EVENT_TO = "2026-11-07"


@dataclass
class Authz03Env:
    """시드된 세션·자료. id 는 문자열이 아니라 모델 id 그대로 둔다."""

    workspace_id: uuid.UUID
    personal_session_id: uuid.UUID
    work_session_id: uuid.UUID
    personal_memory_id: uuid.UUID
    work_memory_id: uuid.UUID
    personal_project_id: uuid.UUID
    personal_topic_id: uuid.UUID
    work_topic_id: uuid.UUID
    personal_agent_memo_id: uuid.UUID
    personal_user_memo_id: uuid.UUID
    work_memo_id: uuid.UUID
    personal_agent_event_id: uuid.UUID
    personal_user_event_id: uuid.UUID
    work_event_id: uuid.UUID
    personal_artifact_id: uuid.UUID
    work_artifact_id: uuid.UUID
    #: 응답 본문 스캔용 개인 canary 토큰 목록.
    personal_tokens: tuple[str, ...] = field(default=(AUTHZ03_P, PERSONAL_TAG))


def leaks_personal(payload: object) -> str | None:
    """응답 payload(JSON 직렬화)에 개인 canary가 있으면 그 토큰을 돌려준다."""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    for token in (AUTHZ03_P, PERSONAL_TAG):
        if token in text:
            return token
    return None


def _session(workspace_id: uuid.UUID, title: str, pool: str) -> ChatSession:
    now = datetime.now(UTC)
    return ChatSession(
        id=uuid.uuid4(), workspace_id=workspace_id, title=title, engine="codex",
        pool=pool, created_at=now, updated_at=now,
    )


async def ensure_authz03(db, make_dir) -> Authz03Env:
    """워크스페이스·personal/work 세션·canary 자료를 만들고 커밋한다.

    같은 DB에 여러 번 시드해도 되도록(세션 루프당 한 번 TRUNCATE) 토큰은
    고정 문자열을 쓴다. absence 단언은 누적 행에 영향을 받지 않는다.
    """
    root = make_dir()
    ws = Workspace(id=uuid.uuid4(), name=root.name, root_path=str(root))
    db.add(ws)
    await db.flush()
    personal = _session(ws.id, "AUTHZ03 Personal", "personal")
    work = _session(ws.id, "AUTHZ03 Work", "work")
    db.add_all([personal, work])
    await db.flush()

    personal_memory = CtxMemory(content=f"{AUTHZ03_P} 개인 기억 문장이다", scope="personal", source="agent")
    work_memory = CtxMemory(content=f"{AUTHZ03_W} 회사 기억 문장이다", scope="work", source="agent")
    personal_project = CtxEntity(type="project", name=PERSONAL_ENTITY_PROJECT, scope="personal",
                                 aliases=[], attributes={}, summary=f"{AUTHZ03_P} 개인 프로젝트 요약")
    personal_topic = CtxEntity(type="topic", name=PERSONAL_ENTITY_TOPIC, scope="personal",
                               aliases=[], attributes={}, summary=f"{AUTHZ03_P} 개인 거래처 요약")
    work_topic = CtxEntity(type="topic", name=WORK_ENTITY_TOPIC, scope="work",
                           aliases=[], attributes={}, summary=f"{AUTHZ03_W} 회사 거래처 요약")
    db.add_all([personal_memory, work_memory, personal_project, work_topic, personal_topic])
    await db.flush()

    personal_agent_memo = Memo(title=f"{AUTHZ03_P} 개인 메모", body=f"{AUTHZ03_P} 개인 메모 본문",
                               tags=[PERSONAL_TAG], source="agent")
    personal_user_memo = Memo(title=f"{AUTHZ03_P} 사용자 메모", body=f"{AUTHZ03_P} 사용자 메모 본문",
                              tags=[], source="user")
    work_memo = Memo(title=f"{AUTHZ03_W} 회사 메모", body=f"{AUTHZ03_W} 회사 메모 본문",
                     tags=[WORK_TAG], source="agent")
    db.add_all([personal_agent_memo, personal_user_memo, work_memo])
    await db.flush()

    def _event(title: str, source: str) -> CalendarEvent:
        return CalendarEvent(
            title=title, description=f"{title} 설명", location="",
            all_day=False, start_at=datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
            end_at=datetime(2026, 11, 3, 11, 0, tzinfo=UTC), source=source,
        )

    personal_agent_event = _event(f"{AUTHZ03_P} 개인 일정", "agent")
    personal_user_event = _event(f"{AUTHZ03_P} 사용자 일정", "user")
    work_event = _event(f"{AUTHZ03_W} 회사 일정", "agent")
    db.add_all([personal_agent_event, personal_user_event, work_event])
    await db.flush()

    def _artifact(name: str, session_id: uuid.UUID) -> Artifact:
        return Artifact(filename=name, mime_type="text/plain", kind="text", source="agent_output",
                        workspace_id=ws.id, session_id=session_id, original_path=f"/{name}",
                        rel_path=name, storage_mode="copy", stored_path=f"/{name}", size_bytes=1,
                        sha256="0" * 64, retention_until=None)

    personal_artifact = _artifact(f"{AUTHZ03_P}.txt", personal.id)
    work_artifact = _artifact(f"{AUTHZ03_W}.txt", work.id)
    db.add_all([personal_artifact, work_artifact])
    await db.commit()

    return Authz03Env(
        workspace_id=ws.id, personal_session_id=personal.id, work_session_id=work.id,
        personal_memory_id=personal_memory.id, work_memory_id=work_memory.id,
        personal_project_id=personal_project.id, personal_topic_id=personal_topic.id,
        work_topic_id=work_topic.id, personal_agent_memo_id=personal_agent_memo.id,
        personal_user_memo_id=personal_user_memo.id, work_memo_id=work_memo.id,
        personal_agent_event_id=personal_agent_event.id, personal_user_event_id=personal_user_event.id,
        work_event_id=work_event.id, personal_artifact_id=personal_artifact.id,
        work_artifact_id=work_artifact.id,
    )
