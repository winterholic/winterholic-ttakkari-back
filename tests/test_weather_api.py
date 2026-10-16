from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from ttakkari.db import sessionmaker
from ttakkari.models import Run, WeatherBriefing
from ttakkari.weather import service


def sample_forecast():
    location = {
        "provider": "Open-Meteo", "timezone": "Asia/Seoul", "fetched_at": "2026-10-16T00:00:00+00:00",
        "forecast_time": "2026-10-16T06:00", "location": {"name": "수원", "latitude": 37.26, "longitude": 127.03},
        "units": {"temperature": "°C", "precipitation": "mm", "wind_speed": "km/h", "visibility": "m"},
        "current": {"time": "2026-10-16T06:00", "temperature_c": 10, "apparent_temperature_c": 8,
                    "humidity_percent": 50, "wind_speed_kmh": 2, "weather_code": 0},
        "hourly": [], "daily": [{"date": "2026-10-16", "temperature_min_c": 5, "temperature_max_c": 17,
                                  "precipitation_probability_max_percent": 10, "precipitation_sum_mm": 0, "weather_code": 0}],
        "missing_fields": [],
    }
    return {"suwon": location, "seoul": {**location, "location": {**location["location"], "name": "서울"}}}


async def test_weather_api_requires_auth_and_returns_empty_history(client):
    assert (await client.get("/api/weather/briefings")).status_code == 401
    assert (await client.post("/api/weather/generate")).status_code == 401
    assert (await client.get("/api/weather/briefings", headers={"Authorization": "Bearer invalid"})).status_code == 401


async def test_manual_and_scheduler_share_one_daily_auto_run(client, auth, monkeypatch):
    async def collect():
        return {"suwon": {"raw": True}, "seoul": {"raw": True}}, sample_forecast(), {}

    monkeypatch.setattr(service, "_collect", collect)
    first = await client.post("/api/weather/generate", headers=auth)
    assert first.status_code == 202, first.text
    row = first.json()
    assert row["status"] == "generating"
    assert row["forecast"]["suwon"]["location"]["name"] == "수원"
    async with sessionmaker()() as db:
        saved = await db.get(WeatherBriefing, row["id"])
        run = await db.get(Run, saved.run_id)
        assert run.model == "gpt-6-luna" and run.effort == "low"
        assert run.engine in {"auto", "claude", "codex"}  # 시작 전에는 auto, 시작하면 매니저가 고른 엔진
        assert saved.analysis_engine in {"auto", "claude", "codex"}
    again = await client.post("/api/weather/generate", headers=auth)
    assert again.status_code == 202, again.text
    assert again.json()["id"] == row["id"]
    async with sessionmaker()() as db:
        saved = await db.get(WeatherBriefing, row["id"])
        assert await db.scalar(select(func.count()).select_from(WeatherBriefing).where(
            WeatherBriefing.day == date.fromisoformat(row["day"]))) == 1
        assert await db.scalar(select(func.count()).select_from(Run).where(Run.id == saved.run_id)) == 1


async def test_open_meteo_failure_is_visible_and_retryable(client, auth, monkeypatch):
    async def fail():
        raise TimeoutError("upstream timeout detail")

    monkeypatch.setattr(service, "_collect", fail)
    response = await client.post("/api/weather/generate", headers=auth)
    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert response.json()["failure"] == "TimeoutError"
    history = await client.get("/api/weather/briefings?limit=10", headers=auth)
    assert history.json()[0]["day"] == response.json()["day"]
