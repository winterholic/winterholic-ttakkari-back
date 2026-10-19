"""Bounded Muse 1.4.x /usage compatibility reader; no prompts or secrets.

MSP usage/read only returns a host-local observation. The native /usage command
refreshes the subscription. Keep CLI auth in the CLI; never extract credentials,
call inference for quota, or fall back to a metered API. UI text is not a stable
API: reject changed cards and preserve truthful absence. Reset precision is one
minute, as displayed by the native command. TZ/locale are fixed for parsing.
"""
from __future__ import annotations

import re
import tempfile
import time
from datetime import UTC, datetime, timedelta

import pexpect
import pyte


def _clock(text: str, day: datetime) -> datetime:
    value = datetime.strptime(text, '%I:%M %p').replace(tzinfo=UTC)
    return day.replace(hour=value.hour, minute=value.minute, second=0, microsecond=0)


def parse_usage_card(text: str, now: datetime) -> dict | None:
    plans = list(re.finditer(r'Subscription\s*·\s*([^│\n]+)', text))
    if not plans:
        return None
    # Repeated /usage cards remain in the terminal. Never combine an earlier
    # unavailable card (or earlier numbers) with the latest refresh.
    plan = plans[-1]
    text = text[plan.start():]
    base = {'account': {'state': 'accountLogin'}, 'plan': plan[1].strip(),
            'usage': {}, 'quota_status': 'unavailable'}
    if 'Currently unavailable' in text or 'Usage currently unavailable' in text:
        return base
    clock = r'(\d{1,2}:\d{2}\s+[AP]M)'
    current = re.search(r'\bCurrent\s+(\d+)%\s+used\s*·\s*Resets at\s+' + clock, text)
    weekly = re.search(r'\bWeekly\s+(\d+)%\s+used\s*·\s*Resets\s+([A-Z][a-z]{2}\s+\d{1,2})\s+at\s+' + clock, text)
    observed = re.search(r'\bas of\s+' + clock, text)
    if not current or not weekly or not observed:
        return None
    try:
        stamp = _clock(observed[1], now)
        if stamp > now:
            stamp -= timedelta(days=1)
        reset = _clock(current[2], stamp)
        # Only an imminent reset can belong to tomorrow. A card painted at or
        # just after reset still refers to the expired window, not a 24h cap.
        if reset < stamp and reset + timedelta(days=1) <= stamp + timedelta(hours=5):
            reset += timedelta(days=1)
        if reset > stamp + timedelta(hours=5):
            return None
        day = datetime.strptime(weekly[2] + ' 2000', '%b %d %Y').replace(tzinfo=UTC)
        candidates = []
        for year in (stamp.year-1, stamp.year, stamp.year+1):
            try:
                candidates.append(_clock(weekly[3], stamp.replace(year=year, month=day.month, day=day.day)))
            except ValueError:  # Feb 29 in an adjacent non-leap year
                continue
        week = next((d for d in candidates if stamp < d <= stamp + timedelta(days=8)), None)
        if week is None:
            return None
    except (ValueError, OverflowError):
        return None
    return {**base, 'quota_status': 'observed', 'usage': {'usage': {
        'tier': base['plan'], 'observedAtMs': int(stamp.timestamp()*1000),
        'window': {'usedPercent': int(current[1]), 'resetsAtMs': int(reset.timestamp()*1000)},
        'weekly': {'usedPercent': int(weekly[1]), 'resetsAtMs': int(week.timestamp()*1000)},
    }}}


def read_usage_card(muse_bin: str, env: dict[str, str], *, timeout: float = 18, diagnostic=None) -> dict:
    """Only send /usage and /exit after an idle composer is observed.

    A PTY is needed by the official interactive CLI, not by the user. The
    emulator consumes terminal control sequences; prompts, trust, auth, billing
    and permissions are never answered. No raw terminal output is returned.
    """
    child_env = {**env, 'TERM': 'xterm-256color', 'TZ': 'UTC', 'LC_ALL': 'C', 'LANG': 'C',
                 'MUSE_NO_AUTO_UPDATE': '1', 'MUSE_LOGIN': '0'}
    for key in ('META_API_KEY', 'MUSE_AUTH_PATH', 'MUSE_TRANSPORT_TRACE'):
        child_env.pop(key, None)
    args = ['--contributor-mode', 'off', '--no-session-log', '--preset', 'empty',
            '--disable-shell', '--disable-write', '--disable-request-user-input',
            '--approval-judge', 'off', '--trust-workspace']
    screen = pyte.Screen(160, 60)
    stream = pyte.Stream(screen)
    started = time.monotonic()
    deadline = started + timeout
    sent = False
    attempts = 0
    last_sent = 0.0
    result = {'usage': {}, 'quota_status': 'timeout'}
    # Dedicated empty workspace: no project config, tools, session logs or code.
    with tempfile.TemporaryDirectory(prefix='ttakkari-muse-usage-') as cwd:
        try:
            child = pexpect.spawn(muse_bin, args, env=child_env, cwd=cwd, encoding='utf-8',
                                  codec_errors='replace', dimensions=(60, 160), echo=False)
        except (OSError, pexpect.ExceptionPexpect):
            return {'usage': {}, 'quota_status': 'spawn_failed', 'builtin_usage_sent': False}
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = child.read_nonblocking(65536, timeout=.2)
                except pexpect.TIMEOUT:
                    chunk = ''
                except pexpect.EOF:
                    if result['quota_status'] == 'timeout':
                        result = {'usage': {}, 'quota_status': 'exited'}
                    break
                stream.feed(chunk)
                # Harmless terminal protocol answers; never approval answers.
                if '\x1b[6n' in chunk:
                    child.send('\x1b[1;1R')
                if '\x1b[c' in chunk:
                    child.send('\x1b[?1;2c')
                text = '\n'.join(screen.display)
                if re.search(r'permission denied|operation not permitted', text, re.IGNORECASE):
                    result = {'usage': {}, 'quota_status': 'permission_denied'}
                    break
                if sent:
                    parsed = parse_usage_card(text, datetime.now(UTC))
                    if parsed is not None:
                        result = parsed
                        # The first paint may precede the asynchronous quota
                        # refresh. Keep the bounded read alive for its result.
                        if parsed['quota_status'] == 'observed':
                            break
                # Muse paints the composer before loading its account. A card
                # containing session tokens alone is not subscription data.
                # Retry the read-only builtin at most three times while this
                # startup race settles; never submit a prompt.
                if (attempts < 3 and time.monotonic() >= max(started+3, last_sent+3)
                        and re.search(r'^\s*❯\s*$', text, re.MULTILINE) and 'Muse Code' in text):
                    child.send('/usage')
                    # Separate typing and Enter, so a slash completion is never
                    # mistaken for a submitted model prompt.
                    time.sleep(.1)
                    child.send('\r')
                    sent = True
                    attempts += 1
                    last_sent = time.monotonic()
        finally:
            if diagnostic is not None:
                diagnostic('\n'.join(screen.display))
            if child.isalive() and re.search(r'^\s*❯\s*$', '\n'.join(screen.display), re.MULTILINE):
                child.send('/exit\r')
                try:
                    child.expect(pexpect.EOF, timeout=1)
                except (pexpect.TIMEOUT, pexpect.EOF):
                    pass
            child.close(force=True)
        result['builtin_usage_sent'] = sent
        result['child_reaped'] = not child.isalive()
    return result


if __name__ == '__main__':
    # forkpty runs in a single-threaded helper, not a backend executor thread.
    import argparse
    import json
    import os
    import signal

    def _terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    parser = argparse.ArgumentParser()
    parser.add_argument('muse_bin')
    parser.add_argument('--timeout', type=float, default=18)
    options = parser.parse_args()
    print(json.dumps(read_usage_card(options.muse_bin, dict(os.environ), timeout=options.timeout)))
