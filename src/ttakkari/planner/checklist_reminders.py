"""KST 07:30 정리 알림, 12·16·20·22·22:30 미완료 알림. 시간대별 영속 차지로 최대 1회 전송."""
import logging
from datetime import datetime, time, timedelta

from sqlalchemy.dialects.postgresql import insert

from ttakkari.db import sessionmaker
from ttakkari.models import ChecklistReminder, utcnow
from ttakkari.planner import checklists as svc
from ttakkari.planner.service import KST
from ttakkari.push.service import PushKind, notify

log = logging.getLogger(__name__)
SLOTS = ("07:30", "12:00", "16:00", "20:00", "22:00", "22:30")


async def send_due(now: datetime | None = None) -> int:
    local = (now or utcnow()).astimezone(KST)
    day = local.date()
    due = [slot for slot in SLOTS if datetime.combine(day, time.fromisoformat(slot), KST) <= local]
    async with sessionmaker()() as db:
        # 자정 이후 오늘을 실체화한다. 화면을 열지 않아도 날짜별 고정 항목 기록을 남긴다.
        data = await svc.daily(db, day)
        if not due:
            return 0
        slot = due[-1]
        # 지난 시간대를 먼저 차지해서 재시작 뒤 여러 알림이 몰리지 않게 한다.
        claimed = list(await db.scalars(insert(ChecklistReminder).values([
            {"day": day, "slot": s, "claimed_at": local} for s in due
        ]).on_conflict_do_nothing(index_elements=["day", "slot"]).returning(ChecklistReminder.slot)))
        await db.commit()
    scheduled = datetime.combine(day, time.fromisoformat(slot), KST)
    if slot not in claimed or local - scheduled >= timedelta(hours=1):
        return 0
    remaining = data["total"] - data["completed"]
    if slot != "07:30" and remaining == 0:
        return 0
    title = "오늘 체크리스트를 정리해 주세요" if slot == "07:30" else "아직 체크하지 않은 항목이 있어요"
    body = f"{data['total']}개 중 {data['completed']}개 완료 · {remaining}개 남음" if data["total"] else "오늘 할 일을 추가하고 하루를 시작해 보세요."
    try:
        await notify(PushKind.checklist_reminder, title, body, url=f"/checklists?date={day}",
                     tag=f"checklist-{day}-{slot}")
    except Exception:
        # 최대 한 번 정책: 전송 실패는 로그에 남기고 다음 정시 스캔은 계속한다.
        log.exception("checklist reminder delivery failed for %s %s", day, slot)
    return 1
