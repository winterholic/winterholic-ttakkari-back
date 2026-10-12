from __future__ import annotations

import os

from ttakkari import sysinfo

URL = "/api/system/resources"


async def test_resources_requires_auth(client):
    assert (await client.get(URL)).status_code == 401


async def test_resources_shape(client, auth):
    r = await client.get(URL, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    cpu, mem = body["cpu"], body["memory"]
    assert 0 <= cpu["percent"] <= 100 and cpu["cores_logical"] >= 1
    assert len(cpu["per_core"]) == cpu["cores_logical"] and len(cpu["load_avg"]) == 3
    assert mem["total"] > 0 and 0 <= mem["used"] <= mem["total"] and mem["available"] > 0
    assert {"total", "used", "percent"} <= mem["swap"].keys()
    assert body["host"]["uptime_seconds"] > 0 and body["host"]["hostname"]
    assert body["network"]["rx_bytes_per_sec"] >= 0 and body["network"]["tx_bytes_per_sec"] >= 0
    # APFS 컨테이너 하나는 한 줄로 묶이고, 시스템 보조 볼륨은 나오지 않는다.
    mounts = [m for d in body["disks"] for m in d["mounts"]]
    assert "/" in mounts and len(mounts) == len(set(mounts))
    assert not any(m.startswith("/System/Volumes/") and m != "/System/Volumes/Data" for m in mounts)
    for d in body["disks"]:
        assert d["used"] + d["free"] == d["total"]
    procs = body["processes"]
    assert len(procs["top_cpu"]) <= sysinfo.TOP_N and len(procs["top_memory"]) <= sysinfo.TOP_N
    backend = [p for p in procs["ttakkari"] if p["kind"] == "backend"]
    assert [p["pid"] for p in backend] == [os.getpid()]


async def test_resources_never_expose_cmdline(client, auth):
    body = (await client.get(URL, headers=auth)).json()
    p = body["processes"]
    for row in p["top_cpu"] + p["top_memory"] + p["ttakkari"]:
        assert set(row) <= {"pid", "name", "cpu_percent", "memory_bytes", "kind"}


async def test_resources_cached(client, auth, monkeypatch):
    first = (await client.get(URL, headers=auth)).json()
    calls = []
    monkeypatch.setattr(sysinfo, "_collect", lambda: calls.append(1) or {})
    second = (await client.get(URL, headers=auth)).json()
    assert second["collected_at"] == first["collected_at"] and calls == []
