"""호스트 GPU 시험 서버의 신원 확인·정리와 경량 watchdog(ADR 0037).

감독자(`ttakkari.host_gpu supervise`)가 SIGKILL 등으로 갑자기 죽으면 finally 정리가 돌지 않아 llama-server 가 가드 없이 남는다.
macOS 에는 PR_SET_PDEATHSIG 가 없으므로, 감독자가 서버보다 먼저 이 watchdog 을 별도 세션으로 띄워 감독자 생사를 짧은 주기로
지켜보게 한다. 감독자가 사라지면 "우리 서버"로 확인된 프로세스에만 신호를 보낸다. psutil 외에는 가져오지 않아 가볍다.
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import signal
import sys
import time
from pathlib import Path

import psutil

ALIAS = "ttakkari-gpu-test"
POLL_SECONDS = 0.5
START_SKEW = 1.5  # psutil create_time 은 초 단위 오차가 있어 시작 시각 비교에 여유를 둔다


def proc_start(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def same_process(pid: int | None, start: float | None) -> bool:
    current = proc_start(pid)
    return current is not None and start is not None and abs(current - start) < START_SKEW


def cmdline(pid: int) -> list[str]:
    try:
        return psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return []


def zombie(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.Error:
        return True


def alive(pid: int | None, start: float | None) -> bool:
    return same_process(pid, start) and not zombie(int(pid))


def terminate_group(pid: int, grace: float = 15.0) -> None:
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 3.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid) or zombie(pid):
                return
            time.sleep(0.1)


def _is_our_server(cmd: list[str], server_path: str, port: int) -> bool:
    # 셔뱅 스크립트는 argv 앞에 인터프리터가 붙으므로 처음 두 칸까지 본다.
    if server_path not in cmd[:2] or ALIAS not in cmd:
        return False
    return any(token == "--port" and nxt == str(port) for token, nxt in itertools.pairwise(cmd))


def find_our_servers(server_path: str, port: int, since: float) -> list[int]:
    """시험 서버로 확인되는 프로세스 pid. 실행 파일 경로·별칭·포트가 모두 같고 감독자 시작 이후에 뜬 것만 고른다."""
    found: list[int] = []
    with contextlib.suppress(psutil.Error, OSError):
        for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
            info = proc.info
            if info.get("create_time") is None or info["create_time"] < since - START_SKEW:
                continue
            if _is_our_server(info.get("cmdline") or [], server_path, port):
                found.append(int(info["pid"]))
    return found


def kill_our_server(pid: int, start: float | None, port: object, grace: float = 15.0) -> bool:
    """상태 파일에 적힌 서버만: pid+시작 시각이 같고 argv 에 별칭과 포트가 있고 자기 프로세스 그룹 리더일 때 그룹에 신호를 보낸다."""
    if not same_process(pid, start):
        return False
    cmd = cmdline(pid)
    if ALIAS not in cmd or str(port) not in cmd:
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.getpgid(pid) != pid:
            return False
    terminate_group(pid, grace)
    return True


def watch(*, supervisor_pid: int, supervisor_start: float, server_path: str, port: int, ready_file: Path | None = None,
          poll: float = POLL_SECONDS, grace: float = 15.0) -> list[int]:
    """감독자가 사라질 때까지 지켜본 뒤, 남은 우리 서버를 정리하고 정리한 pid 를 돌려준다."""
    if ready_file is not None:
        ready_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    seen: dict[int, float | None] = {}
    while alive(supervisor_pid, supervisor_start):
        if not seen:
            seen = {pid: proc_start(pid) for pid in find_our_servers(server_path, port, supervisor_start)}
        elif not any(alive(pid, start) for pid, start in seen.items()):
            return []  # 서버가 이미 정상 종료했다. 지킬 것이 없다.
        time.sleep(poll)
    killed = []
    for pid in find_our_servers(server_path, port, supervisor_start):
        if kill_our_server(pid, proc_start(pid), port, grace):
            killed.append(pid)
    return killed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ttakkari.host_gpu_guard")
    parser.add_argument("--supervisor-pid", type=int, required=True)
    parser.add_argument("--supervisor-start", type=float, required=True)
    parser.add_argument("--server-path", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ready-file", type=Path, default=None)
    args = parser.parse_args(argv)
    watch(supervisor_pid=args.supervisor_pid, supervisor_start=args.supervisor_start, server_path=args.server_path,
          port=args.port, ready_file=args.ready_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
