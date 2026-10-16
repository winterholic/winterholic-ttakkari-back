from __future__ import annotations

from datetime import datetime
from typing import Any


def _at(values: dict[str, Any], key: str, index: int) -> Any:
    items = values.get(key)
    return items[index] if isinstance(items, list) and index < len(items) else None


def normalize_forecast(raw: dict[str, Any], *, location: str, fetched_at: datetime) -> dict[str, Any]:
    """Keep the source timestamp, timezone and missing values while mapping provider names."""
    current = raw.get("current") if isinstance(raw.get("current"), dict) else {}
    hourly = raw.get("hourly") if isinstance(raw.get("hourly"), dict) else {}
    daily = raw.get("daily") if isinstance(raw.get("daily"), dict) else {}
    hourly_rows = []
    for i, time in enumerate(hourly.get("time") or []):
        hourly_rows.append({
            "time": time,
            "temperature_c": _at(hourly, "temperature_2m", i),
            "apparent_temperature_c": _at(hourly, "apparent_temperature", i),
            "precipitation_probability_percent": _at(hourly, "precipitation_probability", i),
            "precipitation_mm": _at(hourly, "precipitation", i),
            "weather_code": _at(hourly, "weather_code", i),
            "wind_speed_kmh": _at(hourly, "wind_speed_10m", i),
            "visibility_m": _at(hourly, "visibility", i),
        })
    daily_rows = []
    for i, time in enumerate(daily.get("time") or []):
        daily_rows.append({
            "date": time,
            "temperature_min_c": _at(daily, "temperature_2m_min", i),
            "temperature_max_c": _at(daily, "temperature_2m_max", i),
            "precipitation_probability_max_percent": _at(daily, "precipitation_probability_max", i),
            "precipitation_sum_mm": _at(daily, "precipitation_sum", i),
            "weather_code": _at(daily, "weather_code", i),
        })
    return {
        "provider": "Open-Meteo",
        "timezone": raw.get("timezone") or "Asia/Seoul",
        "fetched_at": fetched_at.isoformat(),
        "forecast_time": current.get("time"),
        "location": {"name": location, "latitude": raw.get("latitude"), "longitude": raw.get("longitude")},
        "units": {"temperature": "°C", "precipitation": "mm", "wind_speed": "km/h", "visibility": "m"},
        "current": {
            "time": current.get("time"), "temperature_c": current.get("temperature_2m"),
            "apparent_temperature_c": current.get("apparent_temperature"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "wind_speed_kmh": current.get("wind_speed_10m"), "weather_code": current.get("weather_code"),
        },
        "hourly": hourly_rows,
        "daily": daily_rows,
        "missing_fields": [
            key for key, value in {
                "current.temperature_2m": current.get("temperature_2m"),
                "current.apparent_temperature": current.get("apparent_temperature"),
                "current.humidity": current.get("relative_humidity_2m"),
                "current.wind_speed": current.get("wind_speed_10m"),
                "hourly.temperature": hourly.get("temperature_2m"),
                "hourly.precipitation_probability": hourly.get("precipitation_probability"),
                "hourly.precipitation": hourly.get("precipitation"),
                "daily.temperature_min": daily.get("temperature_2m_min"),
                "daily.temperature_max": daily.get("temperature_2m_max"),
            }.items() if value is None or (isinstance(value, list) and not any(v is not None for v in value))
        ],
    }
