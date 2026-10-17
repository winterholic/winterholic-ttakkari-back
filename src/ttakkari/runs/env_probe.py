"""실행 환경 능력 프로브(ADR 0037). 표준 라이브러리만 쓰므로 어느 엔진의 작업 세션 셸에서도 그대로 돌릴 수 있다.

`python3 env_probe.py --json` 은 이 프로세스가 실제로 겪은 결과만 낸다. 항목마다 available / blocked / unknown 중 하나이고,
blocked 는 "이 실행 환경에서 막혔다"는 뜻이지 호스트가 못 한다는 뜻이 아니다. 시도하지 못한 항목은 unknown 이다.
이 파일은 백엔드(호스트 기준선)와 엔진 샌드박스 안의 세션이 같은 코드로 측정하도록 단일 원천으로 둔다.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

PROBE_VERSION = 2
ITEMS = ("gpu_metal", "llama_list_devices", "process_list", "sysctl_read", "spawn_process",
         "network_loopback", "network_outbound", "writable_paths")
DEFAULT_LLAMA_SERVER = "~/private-resources/service-data/ttakkari-qwen/llama/llama-b11655/llama-server"
_DENIED = {errno.EPERM, errno.EACCES}
_UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ECONNREFUSED}


def _item(status: str, detail: str = "", **extra) -> dict:
    # 항목마다 측정 시각을 둔다. 시도한 시점이 곧 그 항목의 신선도다.
    return {"status": status, "detail": detail[:200], "measured_epoch": time.time(), **extra}


def attest(payload: dict, challenge: str) -> str:
    """백엔드가 이 Run 에 발급한 일회용 challenge 로 보고 전체에 서명한다(digest 필드는 제외).
    값을 손으로 쓰거나 고치면 서명이 어긋나므로 실제로 이 프로브를 challenge 와 함께 돌린 결과만 통과한다."""
    body = {k: v for k, v in payload.items() if k != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(challenge.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _run(argv: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL)


def _failed(done: subprocess.CompletedProcess[str]) -> str:
    return ((done.stderr or done.stdout).strip().splitlines() or [f"exit {done.returncode}"])[0]


def probe_gpu_metal() -> dict:
    """MTLCreateSystemDefaultDevice 가 장치를 주는지. nil 이면 이 환경의 GPU(IOKit)·Metal 접근이 막힌 것이다."""
    if sys.platform != "darwin":
        return _item("unknown", f"not macOS ({sys.platform})")
    try:
        metal = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        create = metal.MTLCreateSystemDefaultDevice
        create.restype = ctypes.c_void_p
        create.argtypes = []
        device = create()
    except (OSError, AttributeError) as exc:
        return _item("unknown", f"Metal framework not callable: {exc}")
    if device:
        return _item("available", "MTLCreateSystemDefaultDevice returned a device")
    return _item("blocked", "MTLCreateSystemDefaultDevice returned nil")


def probe_llama_list_devices(server: str | None) -> dict:
    path = Path(server or DEFAULT_LLAMA_SERVER).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return _item("unknown", "llama-server binary not found")
    try:
        done = _run([str(path), "--list-devices"], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _item("unknown", f"llama-server --list-devices did not run: {exc}")
    devices = re.findall(r"^\s*((?:MTL|Metal)\w*):\s*(.+)$", done.stdout + done.stderr, re.MULTILINE)
    if devices:
        name, rest = devices[0]
        return _item("available", f"{name}: {rest}")
    if done.returncode != 0:
        return _item("unknown", f"exit {done.returncode}: {_failed(done)}")
    return _item("blocked", "llama-server --list-devices listed no Metal device")


def probe_process_list() -> dict:
    try:
        done = _run(["ps", "-axo", "pid="])
    except subprocess.TimeoutExpired as exc:
        return _item("unknown", f"ps did not run: {exc}")
    except OSError as exc:
        # 샌드박스가 ps 실행 자체를 거부하는 경우(EPERM/EACCES)는 막힌 것이다. 파일 없음 등은 시도 못 한 것이다.
        status = "blocked" if exc.errno in _DENIED else "unknown"
        return _item(status, f"ps did not run: {exc}")
    if done.returncode != 0:
        return _item("blocked", _failed(done))
    return _item("available", f"{len(done.stdout.split())} processes visible")


def probe_sysctl_read() -> dict:
    keys = ("hw.memsize", "vm.swapusage", "kern.memorystatus_vm_pressure_level")
    results: dict[str, str] = {}
    for key in keys:
        try:
            done = _run(["sysctl", "-n", key])
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _item("unknown", f"sysctl did not run: {exc}")
        results[key] = "ok" if done.returncode == 0 else _failed(done)
    ok = [k for k, v in results.items() if v == "ok"]
    if len(ok) == len(keys):
        return _item("available", "hw.memsize, vm.swapusage, pressure level readable", keys=results)
    if not ok:
        return _item("blocked", next(iter(results.values())), keys=results)
    denied = [f"{k}: {v}" for k, v in results.items() if v != "ok"]
    return _item("available", "partial; unreadable: " + "; ".join(denied), keys=results)


def probe_spawn_process() -> dict:
    try:
        done = _run([sys.executable, "-c", "print(1)"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _item("blocked", str(exc))
    return _item("available" if done.stdout.strip() == "1" else "blocked", "child interpreter " + (
        "ran" if done.stdout.strip() == "1" else _failed(done)))


def probe_network_loopback() -> dict:
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.create_connection(listener.getsockname(), timeout=2):
                pass
    except OSError as exc:
        return _item("blocked" if exc.errno in _DENIED else "unknown", f"{type(exc).__name__}: {exc}")
    return _item("available", "bound and connected on 127.0.0.1")


def probe_network_outbound() -> dict:
    """공용 IP 로 TCP 연결 한 번만 시도한다(데이터는 보내지 않는다)."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    except OSError as exc:
        status = "blocked" if exc.errno in _DENIED | _UNREACHABLE else "unknown"
        return _item(status, f"{type(exc).__name__}: {exc}")
    return _item("available", "TCP connect to 1.1.1.1:443 succeeded")


def probe_writable_paths(paths: list[str]) -> dict:
    """실제로 파일을 만들었다 지워 본다. os.access 는 샌드박스 정책을 반영하지 않는다."""
    candidates = [os.getcwd(), tempfile.gettempdir(), "/private/tmp", *paths]
    seen: dict[str, bool] = {}
    for raw in candidates:
        path = os.path.realpath(raw)
        if path in seen:
            continue
        try:
            fd, name = tempfile.mkstemp(prefix=".ttakkari-probe-", dir=path)
            os.close(fd)
            os.unlink(name)
            seen[path] = True
        except OSError:
            seen[path] = False
    writable = [p for p, ok in seen.items() if ok]
    status = "available" if writable else "blocked"
    return _item(status, f"{len(writable)}/{len(seen)} candidate directories writable", paths=seen)


def run_probe(paths: list[str] | None = None, llama_server: str | None = None, challenge: str | None = None) -> dict:
    items = {
        "gpu_metal": probe_gpu_metal(),
        "llama_list_devices": probe_llama_list_devices(llama_server),
        "process_list": probe_process_list(),
        "sysctl_read": probe_sysctl_read(),
        "spawn_process": probe_spawn_process(),
        "network_loopback": probe_network_loopback(),
        "network_outbound": probe_network_outbound(),
        "writable_paths": probe_writable_paths(paths or []),
    }
    report = {"probe_version": PROBE_VERSION, "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "measured_epoch": time.time(), "platform": sys.platform, "items": items}
    if challenge:
        report["challenge"] = challenge
        report["digest"] = attest(report, challenge)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure what this execution environment allows.")
    parser.add_argument("--json", action="store_true", help="print one JSON object (default)")
    parser.add_argument("--path", action="append", default=[], help="extra directory to test for writability")
    parser.add_argument("--llama-server", default=None)
    parser.add_argument("--challenge", default=None, help="one-time value from begin_environment_probe; needed to report")
    args = parser.parse_args(argv)
    print(json.dumps(run_probe(args.path, args.llama_server, args.challenge), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
