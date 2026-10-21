#!/usr/bin/env python3
"""Mac 공용 pytest 입장 제어: 무거운 실행(전체·Docker·브라우저)의 동시 수를 제한한다(ADR 0066 D3a).

사용법: scripts/pytest_slots.py [--slots N] [--timeout S] -- <명령...>
예:     scripts/pytest_slots.py -- uv run pytest -q tests/test_game_build_isolation.py

- 호스트 전역 슬롯 디렉터리(기본 /tmp/ttk-pytest-slots)의 슬롯 파일마다 flock 을 건다.
  프로세스가 죽으면 flock 이 자동으로 풀리므로 오래된 잠금이 남지 않는다.
- 슬롯이 다 차면 --timeout(기본 1800초)까지 기다린다. 1초 이상 기다리면 waits.log 에
  시각·대기초·pid·명령을 남긴다(입장 제어 대기 지표).
- 빠른 계약 검사(pytest -m contract)는 제한 밖이다. 배포 게이트도 슬롯을 잡지 않는다(우선권):
  게이트는 기다리지 않고, 작업 세션의 무거운 실행끼리만 슬롯 안에서 돈다.
  게이트 실행 중 작업 실행을 멈추는 선점은 게이트 코드 변경이 필요해서 W2로 미룬다.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_SLOTS = 2
DEFAULT_TIMEOUT = 1800
POLL_INTERVAL = 5.0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="무거운 pytest 동시 실행 수 제한 래퍼")
    p.add_argument(
        "--slots",
        type=int,
        default=int(os.environ.get("TTK_PYTEST_SLOTS", DEFAULT_SLOTS)),
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("TTK_PYTEST_SLOT_TIMEOUT", DEFAULT_TIMEOUT)),
    )
    p.add_argument(
        "--slot-dir",
        default=os.environ.get("TTK_PYTEST_SLOT_DIR", "/tmp/ttk-pytest-slots"),
    )
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        p.error("실행할 명령이 없습니다. -- 뒤에 명령을 붙이세요.")
    if args.slots < 1:
        p.error("--slots 는 1 이상이어야 합니다.")
    return args


def try_acquire(slot_dir: Path, slots: int) -> tuple[int, object] | None:
    for i in range(slots):
        # 잠금 파일은 슬롯을 쥐고 있는 동안 열린 채로 둔다(닫으면 flock 이 풀린다).
        fh = open(slot_dir / f"slot-{i}.lock", "w")  # noqa: SIM115
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
        else:
            return i, fh
    return None


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    slot_dir = Path(args.slot_dir)
    slot_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.timeout
    waited = 0.0
    held: tuple[int, object] | None = None
    while time.monotonic() < deadline:
        held = try_acquire(slot_dir, args.slots)
        if held is not None:
            break
        if waited == 0.0:
            print(
                f"[slots] {args.slots}개 슬롯이 다 찼습니다. 비기를 기다립니다(최대 {args.timeout:.0f}초)...",
                flush=True,
            )
        time.sleep(min(POLL_INTERVAL, max(0.1, deadline - time.monotonic())))
        waited = time.monotonic() - (deadline - args.timeout)
    if held is None:
        print(
            f"[slots] {args.timeout:.0f}초 대기 후에도 슬롯이 비지 않았습니다.",
            file=sys.stderr,
        )
        return 3
    slot, fh = held
    try:
        if waited >= 1.0:
            ts = datetime.now(UTC).isoformat(timespec="seconds")
            with open(slot_dir / "waits.log", "a", encoding="utf-8") as log:
                log.write(
                    f"{ts} waited={waited:.0f}s slot={slot} pid={os.getpid()} cmd={' '.join(args.command)}\n"
                )
            print(f"[slots] 슬롯 {slot} 획득(대기 {waited:.0f}초).", flush=True)
        proc = subprocess.run(
            args.command, check=False, pass_fds=(fh.fileno(),)
        )  # 호출자가 준 명령을 그대로 실행한다
        return proc.returncode
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
        fh.close()  # type: ignore[union-attr]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
