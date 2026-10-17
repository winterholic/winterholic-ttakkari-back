"""Native /usage compatibility contract. Fixtures are not live quota evidence."""
from datetime import UTC, datetime

import pytest

from ttakkari.runs.muse_usage import parse_usage_card


def test_reads_native_card_and_handles_current_reset_tomorrow():
    now = datetime(2026, 10, 17, 20, 43, 25, tzinfo=UTC)
    body = parse_usage_card('''
│ Subscription · Muse Code Power Usage                 │
│   Current       0% used · Resets at 1:43 AM            │
│   Weekly       17% used · Resets Oct 19 at 9:00 AM     │
│   as of 8:43 PM                                      │
''', now)
    assert body['usage']['usage']['window']['usedPercent'] == 0
    assert body['usage']['usage']['weekly']['usedPercent'] == 17
    assert body['usage']['usage']['window']['resetsAtMs'] == int(datetime(2026, 10, 18, 1, 43, tzinfo=UTC).timestamp()*1000)
    assert body['usage']['usage']['observedAtMs'] == int(now.replace(second=0).timestamp()*1000)


def test_idle_subscription_does_not_become_zero_usage():
    body = parse_usage_card('Subscription · Muse Code Power Usage\nCurrently unavailable', datetime.now(UTC))
    assert body['usage'] == {}
    assert body['plan'] == 'Muse Code Power Usage'
    assert body['quota_status'] == 'unavailable'


@pytest.mark.parametrize('text', [
    'Input 1000\nOutput 300\nTotal 1300',
    'Subscription · power\nCurrent 15% used\nWeekly 20% used',
    'Subscription · power\nCurrent unknown used\nWeekly 20% used',
])
def test_unknown_or_changed_ui_is_rejected(text):
    assert parse_usage_card(text, datetime.now(UTC)) is None


def test_year_rollover_and_old_observation_are_preserved():
    body = parse_usage_card('''Subscription · Power
Current 120% used · Resets at 4:30 AM
Weekly 75% used · Resets Jan 2 at 9:00 AM
as of 11:59 PM''', datetime(2027, 1, 1, 0, 1, tzinfo=UTC))
    data = body['usage']['usage']
    assert data['window']['usedPercent'] == 120
    assert data['observedAtMs'] == int(datetime(2026, 12, 31, 23, 59, tzinfo=UTC).timestamp()*1000)
    assert data['weekly']['resetsAtMs'] == int(datetime(2027, 1, 2, 9, tzinfo=UTC).timestamp()*1000)


def test_leap_day_reset():
    body = parse_usage_card('''Subscription · Power
Current 1% used · Resets at 4:30 AM
Weekly 2% used · Resets Feb 29 at 9:00 AM
as of 11:59 PM''', datetime(2028, 2, 28, 23, 59, tzinfo=UTC))
    assert body['usage']['usage']['weekly']['resetsAtMs'] == int(datetime(2028, 2, 29, 9, tzinfo=UTC).timestamp()*1000)


def test_auth_prompt_is_never_answered(tmp_path):
    import json
    import subprocess
    import sys

    from ttakkari.runs import muse_usage

    commands = tmp_path / 'commands'
    fake = tmp_path / 'waiting-cli'
    fake.write_text(f'#!{sys.executable}\nimport sys\nprint("Sign in: enter code",flush=True)\n'
                    f'open({str(commands)!r},"w").write(sys.stdin.readline())\n')
    fake.chmod(0o700)
    proc = subprocess.run([sys.executable, muse_usage.__file__, str(fake), '--timeout', '.3'],
                          capture_output=True, text=True, timeout=5, check=True)
    result = json.loads(proc.stdout)
    assert result['builtin_usage_sent'] is False
    assert result['child_reaped'] is True
    if commands.exists():
        assert not commands.read_text()
