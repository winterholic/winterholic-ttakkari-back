from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def fresh_dir(path: Path) -> Path:
    """공유 games_root 위에 테스트용 디렉터리를 깨끗이 준비한다.

    게임 테스트는 한 pytest 프로세스가 공유하는 games_root 아래에 고정된
    이름으로 디렉터리를 만든다. bare mkdir()은 다른 파일이 먼저 같은 이름을
    만들었거나 같은 테스트가 반복 실행되면 FileExistsError를 낸다. 남은
    디렉터리·파일·심링크를 먼저 치우고 빈 디렉터리를 만든다.
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


async def wait_run(client, auth, run_id: str, timeout: float = 20.0) -> dict:
    """Run 이 종료 상태가 될 때까지 폴링한다."""
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/runs/{run_id}", headers=auth)
        run = r.json()
        if run["status"] in TERMINAL:
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} 가 {timeout}초 안에 끝나지 않음: {run['status']}")
        await asyncio.sleep(0.1)


async def wait_status(client, auth, run_id: str, status: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        run = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
        if run["status"] == status:
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"run 상태가 {status} 가 되지 않음: {run['status']}")
        await asyncio.sleep(0.05)


async def run_events(client, auth, run_id: str) -> list[dict]:
    r = await client.get(f"/api/runs/{run_id}/events", headers=auth, params={"limit": 2000})
    assert r.status_code == 200
    return r.json()


async def start_run(client, auth, session_id: str, prompt: str) -> dict:
    r = await client.post(f"/api/sessions/{session_id}/runs", json={"prompt": prompt}, headers=auth)
    assert r.status_code == 202, r.text
    return r.json()


async def run_to_end(client, auth, session_id: str, prompt: str, timeout: float = 20.0) -> dict:
    run = await start_run(client, auth, session_id, prompt)
    return await wait_run(client, auth, run["id"], timeout)


def parse_sse(text: str) -> list[dict]:
    out, cur = [], {}
    for line in text.splitlines():
        if not line:
            if cur:
                out.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        k, _, v = line.partition(":")
        cur[k] = v.lstrip(" ")
    if cur:
        out.append(cur)
    return out


def sse_events(text: str) -> list[dict]:
    return [{"id": int(e["id"]), **json.loads(e["data"])} for e in parse_sse(text) if e.get("event") == "run_event"]


async def wait_event(client, auth, run_id: str, type_: str, timeout: float = 20.0) -> dict:
    """Run 이 해당 type 의 이벤트를 남길 때까지 기다려 그 이벤트를 돌려준다."""
    deadline = time.monotonic() + timeout
    while True:
        found = next((e for e in await run_events(client, auth, run_id) if e["type"] == type_), None)
        if found:
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} 에 {type_} 이벤트가 {timeout}초 안에 없음")
        await asyncio.sleep(0.1)
