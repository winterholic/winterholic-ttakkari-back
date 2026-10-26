"""One disposable worker's native-independent owner EOF fallback. Stdlib only.

No process discovery, DB, credentials, model imports, or stored PID recovery.
The only signal target is this monitor's still-current kernel parent.
"""
from __future__ import annotations

import os
import select
import signal
import sys


def watch(worker_pid: int, owner_fd: int, stop_fd: int, ready_fd: int) -> None:
    if worker_pid <= 1 or os.getppid() != worker_pid:
        return
    os.write(ready_fd, b"1")
    os.close(ready_fd)
    while True:
        ready, _, _ = select.select([owner_fd, stop_fd], [], [])
        if stop_fd in ready:
            return  # normal shutdown or worker already dead
        if owner_fd in ready and not os.read(owner_fd, 1):
            # Preserve the existing Python self-exit(74) when its thread can run.
            # A native GIL hold cannot prevent this separate process's fallback.
            stopped, _, _ = select.select([stop_fd], [], [], 1.5)
            if not stopped and os.getppid() == worker_pid:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # our parent exited between the relation check and signal
            return


if __name__ == "__main__":
    watch(*(int(arg) for arg in sys.argv[1:]))
