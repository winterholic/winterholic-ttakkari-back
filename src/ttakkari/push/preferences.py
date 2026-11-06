"""단일 사용자 앱의 유형별 수신 설정. 기기 구독과 독립이며 발송·알림함 저장 전에 확인한다."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import AppSetting, utcnow
from ttakkari.push.service import INBOX_CATEGORY, PushKind

# kind, 표시 이름, 설명, 기본 수신. 과거 NotebookLM 유형은 Google 로그인 설정을 따른다.
CATALOG = (
    (PushKind.weather, "날씨 브리핑", "오늘의 날씨와 우산·옷차림 정보를 알려드려요.", True),
    (PushKind.calendar_reminder, "일정 알림", "일정마다 지정한 시간에 알려드려요.", True),
    (PushKind.checklist_reminder, "할 일 알림", "체크리스트의 할 일을 챙겨드려요.", True),
    (PushKind.agent, "메인 에이전트의 직접 알림", "메인이 직접 보내는 최종 결과와 확인 요청을 받아요.", True),
    (PushKind.run_failed, "메인 작업 실패", "메인 대화의 작업이 오류로 멈추면 알려드려요.", True),
    (PushKind.run_waiting, "메인 작업 대기", "사용량 한도나 모델 혼잡으로 기다리는 상황을 알려드려요.", False),
    (PushKind.worker_agent, "하위 에이전트의 직접 알림", "각 하위 에이전트가 보내는 개별 보고를 받아요.", False),
    (PushKind.worker_failed, "하위 작업 실패", "각 하위 작업의 오류를 하나씩 알려드려요.", False),
    (PushKind.worker_waiting, "하위 작업 대기", "각 하위 작업의 사용량 한도·모델 혼잡 대기를 알려드려요.", False),
    (PushKind.meeting, "회의록 분석 결과", "회의록 분석 완료와 실패를 알려드려요.", True),
    (PushKind.google_account, "Google 계정 로그인", "연결한 계정에 다시 로그인해야 할 때 알려드려요.", True),
    (PushKind.deployment, "배포 결과", "따까리 서버의 배포 결과를 알려드려요.", False),
)
DEFAULTS = {kind: default for kind, _, _, default in CATALOG}
PREFIX = "notification:"


async def enabled(db: AsyncSession, kind: PushKind) -> bool:
    if kind == PushKind.test:
        return True  # 사용자가 직접 누른 기기 연결 시험은 수신 설정과 무관하다.
    if kind == PushKind.notebooklm:
        kind = PushKind.google_account
    setting = await db.get(AppSetting, PREFIX + kind.value)
    value = setting.value.get("enabled") if setting else None
    return value if isinstance(value, bool) else DEFAULTS[kind]


async def preferences(db: AsyncSession) -> dict:
    rows = await db.scalars(select(AppSetting).where(AppSetting.key.in_([PREFIX + k.value for k in DEFAULTS])))
    values = {row.key: row.value.get("enabled") for row in rows}
    return {"items": [
        {"kind": kind.value, "category": INBOX_CATEGORY[kind], "label": label, "description": description,
         "default_enabled": default,
         "enabled": values[PREFIX + kind.value] if isinstance(values.get(PREFIX + kind.value), bool) else default}
        for kind, label, description, default in CATALOG
    ]}


async def save(db: AsyncSession, kind: PushKind, receive: bool) -> None:
    # 유형마다 한 행을 원자적으로 갱신하므로 다른 기기의 별도 유형 변경을 덮어쓰지 않는다.
    await db.execute(insert(AppSetting).values(key=PREFIX + kind.value, value={"enabled": receive}).on_conflict_do_update(
        index_elements=[AppSetting.key], set_={"value": {"enabled": receive}, "updated_at": utcnow()}))
