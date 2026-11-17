from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SYMBOL_CODES = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65, "lightsleet": 66,
    "sleet": 66, "heavysleet": 67, "lightsnow": 71, "snow": 73,
    "heavysnow": 75, "rainshowers": 80, "heavyrainshowers": 82,
    "snowshowers": 85, "heavysnowshowers": 86, "thunderstorm": 95,
}


def _symbol_code(data: dict[str, Any]) -> int | None:
    for period in ("next_1_hours", "next_6_hours", "next_12_hours"):
        summary = data.get(period, {}).get("summary", {})
        symbol = summary.get("symbol_code")
        if symbol:
            stem = symbol.removesuffix("_day").removesuffix("_night").removesuffix("_polartwilight")
            return SYMBOL_CODES.get(stem)
    return None


def normalize_met_no(raw: dict[str, Any], *, location: str, latitude: float, longitude: float,
                     fetched_at: datetime) -> dict[str, Any]:
    """Map Locationforecast compact values to the weather briefing's normalized schema."""
    timeseries = raw.get("properties", {}).get("timeseries", [])
    hourly: list[dict[str, Any]] = []
    days: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in timeseries:
        timestamp = datetime.fromisoformat(point["time"])
        local_time = timestamp.astimezone(KST).isoformat(timespec="minutes")
        data = point.get("data", {})
        details = data.get("instant", {}).get("details", {})
        period = next((data[key].get("details", {}) for key in ("next_1_hours", "next_6_hours", "next_12_hours")
                       if key in data), {})
        row = {
            "time": local_time,
            "temperature_c": details.get("air_temperature"),
            "apparent_temperature_c": details.get("apparent_air_temperature"),
            "precipitation_probability_percent": None,
            "precipitation_mm": period.get("precipitation_amount"),
            "weather_code": _symbol_code(data),
            "wind_speed_kmh": round(details["wind_speed"] * 3.6, 1) if details.get("wind_speed") is not None else None,
            "visibility_m": None,
        }
        hourly.append(row)
        days[timestamp.astimezone(KST).date().isoformat()].append(row)

    daily = []
    for day, rows in sorted(days.items()):
        temperatures = [r["temperature_c"] for r in rows if r["temperature_c"] is not None]
        precipitation = [r["precipitation_mm"] for r in rows if r["precipitation_mm"] is not None]
        daily.append({
            "date": day,
            "temperature_min_c": min(temperatures) if temperatures else None,
            "temperature_max_c": max(temperatures) if temperatures else None,
            "precipitation_probability_max_percent": None,
            "precipitation_sum_mm": round(sum(precipitation), 1) if precipitation else None,
            "weather_code": next((r["weather_code"] for r in rows if r["weather_code"] is not None), None),
        })

    first = timeseries[0] if timeseries else {}
    current = first.get("data", {}).get("instant", {}).get("details", {})
    return {
        "provider": "met.no",
        "timezone": "Asia/Seoul",
        "fetched_at": fetched_at.isoformat(),
        "forecast_time": hourly[0]["time"] if hourly else None,
        "location": {"name": location, "latitude": latitude, "longitude": longitude},
        "units": {"temperature": "°C", "precipitation": "mm", "wind_speed": "km/h", "visibility": "m"},
        "current": {
            "time": hourly[0]["time"] if hourly else None,
            "temperature_c": current.get("air_temperature"),
            "apparent_temperature_c": current.get("apparent_air_temperature"),
            "humidity_percent": current.get("relative_humidity"),
            "wind_speed_kmh": round(current["wind_speed"] * 3.6, 1) if current.get("wind_speed") is not None else None,
            "weather_code": _symbol_code(first.get("data", {})),
        },
        "hourly": hourly,
        "daily": daily[:9],
        "missing_fields": [name for name, value in {
            "current.temperature": current.get("air_temperature"),
            "hourly.precipitation_probability": None,
            "daily.temperature": [r["temperature_c"] for r in hourly],
        }.items() if value is None or (isinstance(value, list) and not any(v is not None for v in value))],
    }
