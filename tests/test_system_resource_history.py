from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ttakkari import resource_history
from ttakkari.db import sessionmaker
from ttakkari.models import SystemResourceSample


@pytest.mark.asyncio
async def test_history_api_exposes_auth_required_history_endpoint(client, auth):
    assert (await client.get("/api/system/resources/history?range=1h")).status_code == 401
    response = await client.get("/api/system/resources/history?range=1h", headers=auth)
    assert response.status_code == 200
    assert response.json()["range"] == "1h"
    assert response.json()["bucket_seconds"] == 30
    daily = await client.get("/api/system/resources/history?range=24h", headers=auth)
    assert daily.status_code == 200
    assert daily.json()["bucket_seconds"] == 60


@pytest.mark.asyncio
async def test_collector_saves_gpu_and_cpu_snapshot(monkeypatch):
    snapshot = {
        "collected_at": datetime.now(UTC).timestamp(),
        "gpu": {"status": "available", "percent": 0.0, "memory_used_bytes": 1234},
        "cpu": {"percent": 12.5},
        "memory": {"used": 9876},
    }

    async def read_snapshot():
        return snapshot

    monkeypatch.setattr(resource_history.sysinfo, "snapshot", read_snapshot)
    await resource_history.collect_once()

    async with sessionmaker()() as db:
        row = await db.scalar(select(SystemResourceSample).order_by(SystemResourceSample.sampled_at.desc()))
    assert row is not None
    assert row.gpu_percent == 0.0
    assert row.gpu_memory_used_bytes == 1234
    assert row.cpu_percent == 12.5
    assert row.memory_used_bytes == 9876
    assert row.collection_status == "success"


@pytest.mark.asyncio
async def test_collector_saves_failure_as_null_not_zero(monkeypatch):
    async def fail_snapshot():
        raise RuntimeError("collector failed")

    monkeypatch.setattr(resource_history.sysinfo, "snapshot", fail_snapshot)
    await resource_history.collect_once()

    async with sessionmaker()() as db:
        row = await db.scalar(select(SystemResourceSample).order_by(SystemResourceSample.sampled_at.desc()))
    assert row is not None
    assert row.collection_status == "failed"
    assert row.gpu_status == "unavailable"
    assert row.gpu_percent is None
    assert row.gpu_memory_used_bytes is None


@pytest.mark.asyncio
async def test_real_local_snapshot_is_persisted_to_test_database():
    await resource_history.collect_once()
    async with sessionmaker()() as db:
        row = await db.scalar(select(SystemResourceSample).order_by(SystemResourceSample.sampled_at.desc()))
    assert row is not None
    assert row.collection_status == "success"
    assert row.cpu_percent is not None
    assert row.memory_used_bytes is not None
    assert row.sampled_at.tzinfo is not None


@pytest.mark.asyncio
async def test_retention_removes_rows_older_than_90_days(monkeypatch):
    old = datetime.now(UTC) - timedelta(days=91)
    async with sessionmaker()() as db:
        db.add(SystemResourceSample(sampled_at=old, gpu_percent=10, gpu_memory_used_bytes=1,
                                    cpu_percent=1, memory_used_bytes=1, gpu_status="available",
                                    collection_status="success"))
        await db.commit()

    monkeypatch.setattr(resource_history, "_last_retention_sweep", 0.0)

    async def read_snapshot():
        return {"gpu": {"status": "available", "percent": 0.0}, "cpu": {"percent": 1}, "memory": {"used": 1}}

    monkeypatch.setattr(resource_history.sysinfo, "snapshot", read_snapshot)
    await resource_history.collect_once()
    async with sessionmaker()() as db:
        expired = await db.scalar(select(SystemResourceSample).where(SystemResourceSample.sampled_at == old))
    assert expired is None


@pytest.mark.asyncio
async def test_history_api_aggregates_7d_and_keeps_failed_buckets_null(client, auth):
    now = datetime.now(UTC)
    bucket_start = now - timedelta(seconds=int(now.timestamp()) % 600) - timedelta(minutes=10)
    async with sessionmaker()() as db:
        db.add_all([
            SystemResourceSample(sampled_at=bucket_start + timedelta(minutes=1), gpu_percent=25.0,
                                 gpu_memory_used_bytes=100, cpu_percent=10.0, memory_used_bytes=1000,
                                 collection_status="success", gpu_status="available"),
            SystemResourceSample(sampled_at=bucket_start + timedelta(minutes=2), gpu_percent=75.0,
                                 gpu_memory_used_bytes=200, cpu_percent=20.0, memory_used_bytes=2000,
                                 collection_status="success", gpu_status="available"),
            SystemResourceSample(sampled_at=bucket_start - timedelta(minutes=10), gpu_percent=None,
                                 gpu_memory_used_bytes=None, cpu_percent=None, memory_used_bytes=None,
                                 collection_status="failed", gpu_status="unavailable"),
        ])
        await db.commit()

    response = await client.get("/api/system/resources/history?range=7d", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["range"] == "7d"
    assert body["bucket_seconds"] == 600
    assert any(p["gpu_percent"] == 50.0 and p["sample_count"] == 2 for p in body["points"])
    assert any(p["gpu_percent"] is None and p["failed_count"] == 1 for p in body["points"])


@pytest.mark.asyncio
async def test_history_api_rejects_unknown_range(client, auth):
    from ttakkari.security.auth import issue_access_token

    token, _ = issue_access_token()
    response = await client.get("/api/system/resources/history?range=month", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 422
