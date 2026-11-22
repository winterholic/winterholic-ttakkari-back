import asyncio
import logging
import threading

from ttakkari import loop_stall


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_stall_dumps_stacks_once_per_repeat_and_logs_resume(caplog):
    clock = Clock()
    monitor = loop_stall.LoopStallMonitor(30, repeat=600, clock=clock)
    caplog.set_level(logging.WARNING, logger="ttakkari.loop_stall")
    clock.now += 29
    monitor.check()
    assert monitor.dumps == 0
    clock.now += 1
    monitor.check()
    clock.now += 100
    monitor.check()
    assert monitor.dumps == 1
    assert "unresponsive for 30s" in caplog.text and "--- thread MainThread" in caplog.text
    clock.now += 500
    monitor.check()
    assert monitor.dumps == 2
    monitor.last_beat = clock.now
    monitor.check()
    assert "resumed after 630s stall" in caplog.text
    assert monitor.stalled_since is None


def test_watch_thread_sees_a_blocked_loop(caplog):
    caplog.set_level(logging.ERROR, logger="ttakkari.loop_stall")
    release = threading.Event()

    async def main():
        monitor = loop_stall.LoopStallMonitor(0.3, repeat=60)
        monitor.start()
        await asyncio.sleep(0.05)
        # 동기 호출이 루프를 묶는 상황. 감시 스레드가 그동안 스택을 찍어야 한다.
        release.wait(2.5)
        monitor.stop()
        return monitor

    monitor = asyncio.run(main())
    assert monitor.dumps >= 1
    assert "release.wait" in caplog.text or "test_watch_thread_sees_a_blocked_loop" in caplog.text


def test_install_disabled_returns_none():
    assert loop_stall.install(0) is None
