"""백엔드와 같은 코드 경로(runs/desk_qwen_service)로 영구 경로의 실제 llama-server 를 자식 프로세스로 띄워 확인한다.

OS 서비스 등록 없음. 사용: PYTHONPATH=src python real_managed_check.py   (공용 벤치 lock 을 잡고 끝나면 놓는다)
확인: preflight(메모리) → spawn(loopback 빈 포트) → /health → 실제 추론(desk_qwen.decide) → stop 후 자식 소멸·pid 파일 정리.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import psutil

LOCK = Path("/private/tmp/ttakkari-apple-inference-bench.lock")


async def main() -> int:
    from ttakkari.config import get_settings
    from ttakkari.runs import desk_qwen
    from ttakkari.runs import desk_qwen_service as svc

    s = get_settings()
    s.home = Path(tempfile.mkdtemp(prefix="ttk-real-managed-"))
    s.desk_qwen_enabled, s.desk_qwen_base_url, s.desk_qwen_manage = True, "", True
    q = svc.service  # 백엔드가 쓰는 싱글턴(desk_qwen.endpoint() 가 이것을 본다)
    t0 = time.monotonic()
    await q.start()
    while q.state not in ("ready", "failed") and time.monotonic() - t0 < 150:
        await asyncio.sleep(0.5)
    out = {"endpoint_available": desk_qwen.available(), "state": q.state, "reason": q.reason, "ready_seconds": round(time.monotonic() - t0, 1), "status": q.status()}
    if q.state != "ready":
        print(json.dumps(out, ensure_ascii=False))
        await q.stop()
        return 1
    pid = q._child.pid
    proc = psutil.Process(pid)
    cmd = proc.cmdline()
    out["child"] = {"pid": pid, "server": cmd[0], "model": cmd[cmd.index("--model") + 1], "host": cmd[cmd.index("--host") + 1],
                    "threads": cmd[cmd.index("--threads") + 1], "ctx": cmd[cmd.index("--ctx-size") + 1], "rss_mb": round(proc.memory_info().rss / 2**20)}
    out["listen"] = sorted({f"{c.laddr.ip}:{c.laddr.port}" for c in proc.net_connections(kind="inet") if c.status == psutil.CONN_LISTEN})
    out["pidfiles"] = [p.name for p in (s.home / "qwen").glob("*.json")]
    desk_qwen.reset_cooldown()
    d1 = await desk_qwen.decide([], [], "고마워 덕분에 살았어요", [])
    d2 = await desk_qwen.decide([], [], "지금 일하는 에이전트가 몇 개나 돼?", [])
    d3 = await desk_qwen.decide([], [], "README 를 영어로 번역해줘", [])
    out["decisions"] = [{"kind": d.kind, "action": d.action, "abstained": d.abstained, "ms": d.ms, "calls": d.calls, "version": d.prompt_version} for d in (d1, d2, d3)]
    await q.stop()
    await asyncio.sleep(0.3)
    out["child_gone_after_stop"] = not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    out["pidfiles_after_stop"] = [p.name for p in (s.home / "qwen").glob("*.json")]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["child_gone_after_stop"] and not out["pidfiles_after_stop"] else 2


if __name__ == "__main__":
    try:
        LOCK.mkdir()
    except FileExistsError:
        print("LOCK_BUSY", flush=True)
        sys.exit(75)
    try:
        (LOCK / "owner.json").write_text(json.dumps({"owner_id": "real-managed-check", "pid": os.getpid()}))
        rc = asyncio.run(main())
    finally:
        with __import__("contextlib").suppress(OSError):
            (LOCK / "owner.json").unlink()
            LOCK.rmdir()
    sys.exit(rc)
