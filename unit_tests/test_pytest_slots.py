from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLOTS = ROOT / "scripts" / "pytest_slots.py"


def test_slot_lock_remains_held_by_surviving_child_after_wrapper_is_killed(tmp_path):
    slot_dir = tmp_path / "slots"
    child_pid_file = tmp_path / "child.pid"
    child_code = "import pathlib,os,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(1.5)"
    wrapper = subprocess.Popen([
        sys.executable, str(SLOTS), "--slots", "1", "--slot-dir", str(slot_dir), "--",
        sys.executable, "-c", child_code, str(child_pid_file),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_file.exists(), "실행 자식이 시작되지 않았다"
        child_pid = int(child_pid_file.read_text())
        wrapper.send_signal(signal.SIGKILL)
        wrapper.wait(timeout=5)

        with open(slot_dir / "slot-0.lock", "r+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                blocked = True
            else:
                blocked = False
            assert blocked, "wrapper 종료 직후 살아 있는 자식이 슬롯 잠금을 보존해야 한다"
        os.kill(child_pid, signal.SIGTERM)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)

