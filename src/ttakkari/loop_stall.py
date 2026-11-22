"""이벤트 루프가 동기 호출에 묶였을 때 모든 스레드의 스택을 로그에 남긴다.

루프가 막히면 요청 로그도 예외도 남지 않아서, 사후에는 어느 호출이 막았는지 알 수 없다(2026-11-22 15:10~17:18).
감시 스레드는 루프 밖에서 돌기 때문에 루프가 막혀 있어도 스택을 찍을 수 있다. 판단·복구는 하지 않고 기록만 한다.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("ttakkari.loop_stall")
BEAT_SECONDS = 1.0
REPEAT_SECONDS = 600.0  # 계속 막혀 있으면 이 간격으로 다시 찍는다. 같은 자리에 묶였는지 옮겨 갔는지 구분하려고.


def format_stacks() -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    blocks = []
    for ident, frame in sys._current_frames().items():
        blocks.append(f"--- thread {names.get(ident, '?')} ({ident})\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(blocks)


class LoopStallMonitor:
    def __init__(self, threshold: float, *, repeat: float = REPEAT_SECONDS, clock=time.monotonic):
        self.threshold = threshold
        self.repeat = repeat
        self.clock = clock
        self.last_beat = clock()
        self.stalled_since: float | None = None
        self.last_dump: float | None = None
        self.dumps = 0
        self._stop = threading.Event()
        self._beat_task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None

    async def _beat(self) -> None:
        while True:
            self.last_beat = self.clock()
            await asyncio.sleep(BEAT_SECONDS)

    def check(self) -> None:
        """감시 스레드가 주기마다 부른다. 시험에서는 직접 부른다."""
        now = self.clock()
        silent = now - self.last_beat
        if silent < self.threshold:
            if self.stalled_since is not None:
                log.warning("event loop resumed after %.0fs stall", now - self.stalled_since)
                self.stalled_since = self.last_dump = None
            return
        if self.stalled_since is None:
            self.stalled_since = self.last_beat
        if self.last_dump is not None and now - self.last_dump < self.repeat:
            return
        self.last_dump = now
        self.dumps += 1
        log.error("event loop unresponsive for %.0fs; thread stacks follow\n%s", silent, format_stacks())

    def _watch(self) -> None:
        while not self._stop.wait(BEAT_SECONDS):
            try:
                self.check()
            except Exception:  # 감시가 죽으면 다음 정지를 놓친다
                log.exception("loop stall check failed")

    def start(self) -> None:
        self.last_beat = self.clock()
        self._beat_task = asyncio.get_running_loop().create_task(self._beat(), name="loop-stall-beat")
        self._thread = threading.Thread(target=self._watch, name="loop-stall-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._beat_task is not None:
            self._beat_task.cancel()


_current: LoopStallMonitor | None = None


def install(threshold: float) -> LoopStallMonitor | None:
    """lifespan 시작에서 부른다. 같은 프로세스에서 lifespan 이 다시 열리면 이전 감시를 멈추고 새 루프에 붙는다."""
    global _current
    if _current is not None:
        _current.stop()
        _current = None
    if threshold <= 0:
        return None
    _current = LoopStallMonitor(threshold)
    _current.start()
    return _current
