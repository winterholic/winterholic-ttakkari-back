"""이 맥(Mac Studio)의 자원 현황. 사용자 메뉴 > 서버 리소스 화면이 3~5초마다 부른다.

psutil 이 주는 값을 쓰고, psutil 이 모르는 것(칩 이름, P/E 코어 수, 메모리 압력)만 sysctl 을
고정 인자로 부른다. 요청 값은 어떤 명령에도 넘기지 않는다.

프로세스는 이름·pid·CPU·메모리만 낸다. 명령줄 인자에는 토큰이 섞일 수 있어 cmdline 은 읽지 않는다.
다른 사용자(root) 프로세스는 macOS 가 메모리 조회를 막아(AccessDenied) 상위 목록에서 빠진다.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import socket
import subprocess
import time
from functools import lru_cache
from typing import Any

import psutil

CACHE_SECONDS = 2.0
# CPU·네트워크·프로세스 CPU 는 두 시점의 차이로 구한다. 길면 응답이 늦고, 짧으면 값이 튄다.
SAMPLE_SECONDS = 0.5
TOP_N = 5
SYSCTL = "/usr/sbin/sysctl"
# kern.memorystatus_vm_pressure_level: 1 정상, 2 경고, 4 위험(xnu 의 kVMPressure* 값).
PRESSURE = {1: "normal", 2: "warn", 4: "critical"}
# /dev/disk3s1s1(봉인된 시스템 스냅숏)·disk3s5(Data) → 컨테이너 disk3
APFS_CONTAINER = re.compile(r"^(disk\d+)s\d+")

_lock = asyncio.Lock()
_cached: tuple[float, dict[str, Any]] | None = None


def _sysctl(*names: str) -> list[str | None]:
    try:
        out = subprocess.run(
            [SYSCTL, "-n", *names], capture_output=True, text=True, timeout=2, check=False, stdin=subprocess.DEVNULL,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return [None] * len(names)
    # 없는 이름은 줄이 빠지므로 개수가 다르면 하나씩 다시 묻는다.
    if len(out) == len(names):
        return [x.strip() or None for x in out]
    return [_sysctl(n)[0] for n in names] if len(names) > 1 else [None]


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _static() -> dict[str, Any]:
    chip, model, perf, eff = _sysctl("machdep.cpu.brand_string", "hw.model", "hw.perflevel0.physicalcpu", "hw.perflevel1.physicalcpu")
    mac = platform.mac_ver()[0]
    return {
        "chip": chip,
        "model": model,
        "performance_cores": _int(perf),
        "efficiency_cores": _int(eff),
        "os": f"macOS {mac}" if mac else f"{platform.system()} {platform.release()}",
    }


def _net_bytes() -> tuple[int, int]:
    rx = tx = 0
    for nic, c in psutil.net_io_counters(pernic=True).items():
        # 루프백은 백엔드↔터널 같은 내부 통신이라 바깥 송수신에서 뺀다.
        if nic.startswith("lo"):
            continue
        rx += c.bytes_recv
        tx += c.bytes_sent
    return rx, tx


def _disks() -> list[dict[str, Any]]:
    """APFS 는 한 컨테이너의 볼륨들이 빈 공간을 나눠 쓴다. 그래서 볼륨마다 따로 세면 같은 디스크가
    여러 번 나온다. 컨테이너(diskN) 하나를 한 줄로 묶고, 그 안의 / 와 Data 볼륨 사용량은 내역으로 둔다."""
    groups: dict[str, dict[str, Any]] = {}
    for p in psutil.disk_partitions(all=False):
        mp = p.mountpoint
        opts = set(p.opts.split(","))
        system = mp in ("/", "/System/Volumes/Data")
        # 시스템 보조 볼륨(VM·Preboot·Update…), 복구 볼륨, 읽기 전용 디스크 이미지(.dmg)는 뺀다.
        if not system and (mp.startswith(("/System/", "/private/")) or "dontbrowse" in opts or "ro" in opts):
            continue
        try:
            u = psutil.disk_usage(mp)
        except OSError:
            continue
        if u.total < 1 << 30:
            continue
        m = APFS_CONTAINER.match(os.path.basename(p.device)) if p.fstype == "apfs" else None
        container = m.group(1) if m else p.device
        g = groups.setdefault(container, {
            "name": "내장 디스크" if system else os.path.basename(mp) or mp,
            "fstype": p.fstype, "total": u.total, "free": u.free, "volumes": [],
        })
        if system:
            g["name"] = "내장 디스크"
        g["free"] = min(g["free"], u.free)
        if not any(v["mount"] == mp for v in g["volumes"]):
            g["volumes"].append({"mount": mp, "used": u.used})
    out = []
    for g in groups.values():
        used = g["total"] - g["free"]
        g["volumes"].sort(key=lambda v: (v["mount"] != "/", v["mount"]))
        out.append({
            "name": g["name"], "fstype": g["fstype"], "mounts": [v["mount"] for v in g["volumes"]],
            "total": g["total"], "used": used, "free": g["free"],
            "percent": round(used / g["total"] * 100, 1) if g["total"] else 0.0, "volumes": g["volumes"],
        })
    out.sort(key=lambda d: (d["name"] != "내장 디스크", d["name"]))
    return out


def _kind(name: str, pid: int, me: int) -> str:
    n = name.lower()
    if pid == me:
        return "backend"
    if "claude" in n:
        return "claude"
    if "codex" in n:
        return "codex"
    return "other"


def _proc_row(p: psutil.Process, cpu: float) -> dict[str, Any] | None:
    try:
        with p.oneshot():
            return {"pid": p.pid, "name": p.name(), "cpu_percent": round(cpu, 1), "memory_bytes": p.memory_info().rss}
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _collect() -> dict[str, Any]:
    me = os.getpid()
    try:
        ours = {me, *(c.pid for c in psutil.Process(me).children(recursive=True))}
    except (psutil.Error, OSError):
        ours = {me}

    try:
        procs = list(psutil.process_iter())
    except (psutil.Error, OSError):
        # macOS may deny the process table (for example, in a sandbox). Keep the backend row useful.
        procs = [psutil.Process(me)]
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    psutil.cpu_percent(None, percpu=True)
    rx0, tx0 = _net_bytes()
    t0 = time.monotonic()
    time.sleep(SAMPLE_SECONDS)
    per_core = psutil.cpu_percent(None, percpu=True)
    rx1, tx1 = _net_bytes()
    dt = max(time.monotonic() - t0, 1e-3)

    rows: list[dict[str, Any]] = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
        except psutil.Error:
            continue
        r = _proc_row(p, cpu)
        if r:
            rows.append(r)
    top_cpu = sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:TOP_N]
    top_mem = sorted(rows, key=lambda r: r["memory_bytes"], reverse=True)[:TOP_N]
    ttk = [{**r, "kind": _kind(r["name"], r["pid"], me)} for r in rows if r["pid"] in ours]
    ttk.sort(key=lambda r: (r["kind"] != "backend", -r["memory_bytes"]))

    vm = psutil.virtual_memory()
    try:
        sw = psutil.swap_memory()
        swap = {"total": sw.total, "used": sw.used, "percent": sw.percent}
    except (psutil.Error, OSError):
        # Some restricted macOS contexts deny vm.swapusage; retain a usable resource snapshot.
        swap = {"total": 0, "used": 0, "percent": 0.0}
    level, free_pct = _sysctl("kern.memorystatus_vm_pressure_level", "kern.memorystatus_level")
    st = _static()
    try:
        boot = psutil.boot_time()
        uptime = int(time.time() - boot)
    except (psutil.Error, OSError):
        boot = None
        uptime = None
    return {
        "collected_at": time.time(),
        "host": {
            "hostname": socket.gethostname(), "os": st["os"], "model": st["model"],
            "boot_time": boot, "uptime_seconds": uptime,
        },
        "cpu": {
            "percent": round(sum(per_core) / len(per_core), 1) if per_core else 0.0,
            "per_core": [round(x, 1) for x in per_core],
            "load_avg": [round(x, 2) for x in psutil.getloadavg()],
            "cores_logical": psutil.cpu_count(logical=True), "cores_physical": psutil.cpu_count(logical=False),
            "performance_cores": st["performance_cores"], "efficiency_cores": st["efficiency_cores"], "chip": st["chip"],
        },
        "memory": {
            "total": vm.total, "used": vm.total - vm.available, "available": vm.available, "percent": vm.percent,
            "wired": getattr(vm, "wired", None),
            "pressure": PRESSURE.get(_int(level) or 0), "pressure_free_percent": _int(free_pct),
            "swap": swap,
        },
        "disks": _disks(),
        "network": {
            "rx_bytes_per_sec": max(0, round((rx1 - rx0) / dt)),
            "tx_bytes_per_sec": max(0, round((tx1 - tx0) / dt)),
        },
        "processes": {
            "top_cpu": top_cpu,
            "top_memory": top_mem,
            "ttakkari": ttk,
            "ttakkari_total": {
                "cpu_percent": round(sum(r["cpu_percent"] for r in ttk), 1),
                "memory_bytes": sum(r["memory_bytes"] for r in ttk),
            },
        },
    }


async def snapshot() -> dict[str, Any]:
    """CACHE_SECONDS 안의 요청은 같은 값을 돌려준다. 동시에 들어와도 수집은 한 번만 돈다."""
    global _cached
    async with _lock:
        if _cached and time.monotonic() - _cached[0] < CACHE_SECONDS:
            return _cached[1]
        data = await asyncio.to_thread(_collect)
        _cached = (time.monotonic(), data)
        return data
