"""화면·프롬프트에 보이는 시각은 UTC 원시 값이 아니라 한국 시간이어야 한다(2026-11-03: 한도 대기 문구가 9시간 이르게 보였다)."""
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

from ttakkari.api.desk_feedback import quota_wait_reason
from ttakkari.models import RunStatus
from ttakkari.timefmt import kst_label, to_kst


def test_kst_label_converts_utc_and_treats_naive_as_utc():
    assert kst_label(datetime(2026, 11, 4, 8, 40, tzinfo=UTC)) == "11/04 17:40"
    assert kst_label(datetime(2026, 11, 4, 8, 40)) == "11/04 17:40"  # noqa: DTZ001 - naive 입력이 시험 대상이다. naive 는 UTC 로 본다
    assert kst_label(datetime(2026, 11, 4, 17, 40, tzinfo=timezone(timedelta(hours=9)))) == "11/04 17:40"
    assert kst_label(datetime(2026, 12, 31, 20, 0, tzinfo=UTC)) == "01/01 05:00"  # 날짜 경계를 넘는다
    assert to_kst(datetime(2026, 11, 4, 8, 40, tzinfo=UTC)).utcoffset() == timedelta(hours=9)


def test_quota_wait_reason_shows_kst_not_raw_utc():
    run = SimpleNamespace(status=RunStatus.queued, resume_after=datetime(2026, 11, 4, 8, 40, 54, tzinfo=UTC))
    event = SimpleNamespace(type="run.waiting")
    assert quota_wait_reason(run, event) == "한도 대기 (11/04 17:40까지)"
    assert quota_wait_reason(SimpleNamespace(status=RunStatus.running, resume_after=run.resume_after), event) is None
