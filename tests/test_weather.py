import json
from datetime import UTC, date, datetime

import httpx
import pytest

from ttakkari.weather import service
from ttakkari.weather.open_meteo import normalize_forecast
from ttakkari.weather.service import fallback_content, parse_model_result


def test_normalize_forecast_keeps_nulls_and_source_timestamps():
    raw = {
        "latitude": 37.26,
        "longitude": 127.03,
        "timezone": "Asia/Seoul",
        "current": {
            "time": "2026-10-16T06:00",
            "temperature_2m": 11.2,
            "relative_humidity_2m": None,
            "apparent_temperature": 9.8,
            "wind_speed_10m": 3.4,
            "weather_code": 3,
        },
        "hourly": {
            "time": ["2026-10-16T06:00", "2026-10-16T07:00"],
            "temperature_2m": [11.2, None],
            "precipitation_probability": [None, 40],
            "precipitation": [0.0, None],
            "weather_code": [3, 61],
        },
        "daily": {
            "time": ["2026-10-16", "2026-10-17"],
            "temperature_2m_min": [8.0, 7.0],
            "temperature_2m_max": [17.0, None],
            "precipitation_probability_max": [20, None],
            "precipitation_sum": [0.0, None],
            "weather_code": [3, 61],
        },
    }
    observed = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)

    result = normalize_forecast(raw, location="수원", fetched_at=observed)

    assert result["location"] == {"name": "수원", "latitude": 37.26, "longitude": 127.03}
    assert result["timezone"] == "Asia/Seoul"
    assert result["fetched_at"] == observed.isoformat()
    assert result["current"]["humidity_percent"] is None
    assert result["hourly"][1]["temperature_c"] is None
    assert result["hourly"][0]["precipitation_mm"] == 0.0
    assert result["hourly"][0]["precipitation_probability_percent"] is None
    assert len(result["daily"]) == 2
    assert result["daily"][1]["temperature_max_c"] is None


def test_met_no_compact_normalizes_to_weather_shape_and_maps_symbols():
    from ttakkari.weather.met_no import normalize_met_no

    raw = {
        "geometry": {"coordinates": [127.03, 37.26]},
        "properties": {"meta": {"updated_at": "2026-11-18T00:00:00Z"}, "timeseries": [
            {"time": "2026-11-18T00:00:00Z", "data": {
                "instant": {"details": {"air_temperature": 12.5, "relative_humidity": 60,
                                          "wind_speed": 2.5, "cloud_area_fraction": 35}},
                "next_1_hours": {"summary": {"symbol_code": "partlycloudy_day"},
                                  "details": {"precipitation_amount": 0.2}},
            }},
            {"time": "2026-11-18T01:00:00Z", "data": {
                "instant": {"details": {"air_temperature": 10, "relative_humidity": 65,
                                          "wind_speed": 3, "cloud_area_fraction": 90}},
                "next_1_hours": {"summary": {"symbol_code": "rain"},
                                  "details": {"precipitation_amount": 1.2}},
            }},
        ]},
    }

    result = normalize_met_no(raw, location="수원", latitude=37.26, longitude=127.03,
                              fetched_at=datetime(2026, 11, 18, tzinfo=UTC))

    assert result["provider"] == "met.no"
    assert result["current"]["temperature_c"] == 12.5
    assert result["current"]["weather_code"] == 2
    assert result["hourly"][1]["weather_code"] == 63
    assert result["hourly"][1]["precipitation_mm"] == 1.2
    assert result["hourly"][1]["precipitation_probability_percent"] is None
    assert result["daily"][0]["temperature_min_c"] == 10
    assert result["daily"][0]["temperature_max_c"] == 12.5


@pytest.mark.asyncio
async def test_fetch_one_retries_open_meteo_then_uses_met_no(monkeypatch):
    attempts = []
    async def open_meteo(_location):
        attempts.append("open-meteo")
        raise httpx.ConnectTimeout("timeout")

    async def met_no(_location):
        attempts.append("met.no")
        return {"provider": "met.no", "current": {"time": "now"}}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(service, "_fetch_open_meteo", open_meteo)
    monkeypatch.setattr(service, "_fetch_met_no", met_no)
    monkeypatch.setattr(service.asyncio, "sleep", no_sleep)

    result = await service._fetch_one(("수원", 37.26, 127.03))

    assert attempts == ["open-meteo"] * 3 + ["met.no"]
    assert result["provider"] == "met.no"


@pytest.mark.asyncio
async def test_collection_keeps_met_no_provider_on_normalized_region(monkeypatch):
    raw = {"properties": {"timeseries": [{
        "time": "2026-11-18T00:00:00Z",
        "data": {"instant": {"details": {"air_temperature": 8}},
                 "next_1_hours": {"summary": {"symbol_code": "cloudy"},
                                  "details": {"precipitation_amount": 0}}},
    }]}}

    async def fetch(_location):
        return {**raw, "_provider": "met.no"}

    monkeypatch.setattr(service, "_fetch_one", fetch)

    raw_results, forecasts, errors = await service._collect()

    assert not errors
    assert all(raw_results[key]["_provider"] == "met.no" for key in service.LOCATIONS)
    assert all(forecasts[key]["provider"] == "met.no" for key in service.LOCATIONS)


def test_fallback_reports_missing_data_without_inventing_risk_advice():
    result = fallback_content({
        "suwon": {
            "current": {"temperature_c": 12, "apparent_temperature_c": None, "weather_code": 3},
            "hourly": [{"time": "2026-10-16T08:00", "precipitation_probability_percent": None}],
            "daily": [{"date": "2026-10-16", "temperature_min_c": 8, "temperature_max_c": 17,
                       "precipitation_sum_mm": None, "precipitation_probability_max_percent": None}],
        },
        "seoul": {"daily": []},
    }, date.fromisoformat("2026-10-16"))

    assert "자료가 없어" in result["umbrella"]
    assert "8~17°C" in result["outfit"]
    assert "가정" in result["commute"]
    assert len(result["summary"]) in (2, 3, 4)
    assert all(term not in json.dumps(result, ensure_ascii=False) for term in ("안개 주의", "노면", "자전거"))


def test_fallback_does_not_treat_missing_weekly_rain_probability_as_zero():
    result = fallback_content({
        "suwon": {
            "current": {"temperature_c": 12, "weather_code": 3},
            "hourly": [],
            "daily": [
                {"date": f"2026-10-{16 + i}", "temperature_min_c": 8, "temperature_max_c": 17,
                 "precipitation_probability_max_percent": None, "precipitation_sum_mm": None}
                for i in range(7)
            ],
        },
        "seoul": {"daily": []},
    }, date.fromisoformat("2026-10-16"))

    assert "강수확률 자료 없음" in result["week"]
    assert "강수확률 40% 이상 날짜 없음" not in result["week"]


def test_model_result_requires_schema_and_rejects_unsupported_safety_claims():
    valid = {"summary": ["수원은 흐립니다.", "오후 기온이 오릅니다."], "umbrella": "강수확률은 낮습니다.",
             "outfit": "겉옷을 챙기세요.", "commute": "08~09시와 18~19시를 가정합니다.",
             "daypart": "오전부터 저녁까지 흐립니다.", "week": "주말 기온을 확인하세요."}
    assert parse_model_result(json.dumps(valid, ensure_ascii=False), {}) == valid
    valid["commute"] = "안개가 짙으니 운전 조심하세요."
    with pytest.raises(ValueError, match="위험"):
        parse_model_result(json.dumps(valid, ensure_ascii=False), {})


@pytest.mark.asyncio
async def test_collection_keeps_successful_region_when_the_other_api_call_fails(monkeypatch):
    raw = {
        "latitude": 37.5, "longitude": 127.0, "timezone": "Asia/Seoul",
        "current": {"time": "2026-10-16T06:00", "temperature_2m": 9, "weather_code": 0},
        "hourly": {"time": [], "temperature_2m": [], "precipitation_probability": [], "precipitation": []},
        "daily": {"time": ["2026-10-16"], "temperature_2m_min": [4], "temperature_2m_max": [14],
                  "precipitation_probability_max": [0], "precipitation_sum": [0]},
    }

    async def fetch(location):
        if location[0] == "수원":
            raise httpx.ConnectTimeout("upstream timeout")
        return raw

    monkeypatch.setattr(service, "_fetch_one", fetch)
    raws, normalized, errors = await service._collect()

    assert set(raws) == {"seoul"}
    assert set(normalized) == {"seoul"}
    assert errors == {"suwon": "ConnectTimeout"}
