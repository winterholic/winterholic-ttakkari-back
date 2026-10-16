from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    Run,
    RunStatus,
    SessionKind,
    WeatherBriefing,
    utcnow,
)
from ttakkari.push.service import PushKind, notify
from ttakkari.weather.open_meteo import normalize_forecast

log = logging.getLogger("ttakkari.weather")
KST = ZoneInfo("Asia/Seoul")
MODEL = "gpt-6-luna"
EFFORT = "low"
LOCATIONS = {
    "suwon": ("수원", 37.2636, 127.0286),
    "seoul": ("서울", 37.5665, 126.9780),
}
API_URL = "https://api.open-meteo.com/v1/forecast"


class BriefingContent(BaseModel):
    summary: list[str] = Field(min_length=2, max_length=4)
    umbrella: str = Field(min_length=1, max_length=300)
    outfit: str = Field(min_length=1, max_length=300)
    commute: str = Field(min_length=1, max_length=500)
    daypart: str = Field(min_length=1, max_length=500)
    week: str = Field(min_length=1, max_length=500)


def wmo_label(code: int | None) -> str | None:
    if code is None:
        return None
    if code == 0:
        return "맑음"
    if code in (1, 2):
        return "대체로 맑음"
    if code == 3:
        return "흐림"
    if code in (45, 48):
        return "안개"
    if code in (51, 53, 55, 56, 57):
        return "이슬비"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "비"
    if code in (71, 73, 75, 77, 85, 86):
        return "눈"
    if code in (95, 96, 99):
        return "뇌우"
    return f"날씨 코드 {code}"


def fallback_content(forecast: dict[str, Any], day: date, source_errors: dict[str, str] | None = None) -> dict[str, Any]:
    """Data-only result: wording is deterministic and missing values stay explicitly unknown."""
    suwon = forecast.get("suwon", {})
    seoul = forecast.get("seoul", {})
    today = next((r for r in suwon.get("daily", []) if r.get("date") == day.isoformat()), {})
    hourly = suwon.get("hourly", [])
    commute = [r for r in hourly if r.get("time", "")[11:13] in {"08", "09", "18", "19"}]
    probs = [r.get("precipitation_probability_percent") for r in commute]
    max_prob = max((p for p in probs if p is not None), default=None)
    rain_sum = today.get("precipitation_sum_mm")
    if max_prob is None:
        umbrella = "강수확률 자료가 없어 우산 필요 여부를 판단할 수 없습니다."
    elif max_prob >= 40 or (rain_sum is not None and rain_sum > 0):
        umbrella = f"출퇴근 시간대 강수확률 최고 {max_prob}%로 우산을 챙기는 편이 좋습니다."
    else:
        umbrella = f"출퇴근 시간대 강수확률은 최고 {max_prob}%입니다."
    current = suwon.get("current", {})
    temp = current.get("temperature_c")
    feels = current.get("apparent_temperature_c")
    state = wmo_label(current.get("weather_code")) or "상태 자료 없음"
    summary = [
        f"수원 현재 {temp}°C{f' (체감 {feels}°C)' if feels is not None else ''}, {state}입니다."
        if temp is not None else f"수원 현재 기온 자료가 없습니다. 날씨 상태는 {state}입니다.",
        umbrella,
    ]
    low, high = today.get("temperature_min_c"), today.get("temperature_max_c")
    outfit = (f"오늘 수원 기온은 {low}~{high}°C로 예보됩니다. 체감온도와 현재 기온을 함께 보고 겉옷을 조절하세요."
              if low is not None and high is not None else "기온 자료가 부족해 복장을 구체적으로 판단하기 어렵습니다.")
    sday = next((r for r in seoul.get("daily", []) if r.get("date") == day.isoformat()), {})
    slo, shi = sday.get("temperature_min_c"), sday.get("temperature_max_c")
    commute_text = f"출퇴근은 08~09시·18~19시를 가정했습니다. 수원 {low}~{high}°C, 서울 {slo}~{shi}°C입니다."
    if source_errors:
        failed = "·".join(LOCATIONS[key][0] for key in source_errors if key in LOCATIONS)
        commute_text += f" {failed} 예보 수집이 실패해 해당 지역은 비교할 수 없습니다."
    daypart = "오전·오후·저녁 시간별 기온과 강수 변화를 확인하세요. 결측값은 표시하지 않고 자료 없음으로 둡니다."
    week_rows = suwon.get("daily", [])[:7]
    rainy = [r["date"] for r in week_rows
             if r.get("precipitation_probability_max_percent") is not None
             and r["precipitation_probability_max_percent"] >= 40]
    unknown_rain = sum(r.get("precipitation_probability_max_percent") is None for r in week_rows)
    if unknown_rain:
        rain_text = "강수확률 40% 이상 확인된 날짜: " + (", ".join(rainy) if rainy else "확인된 날짜 없음")
        rain_text += f" · 강수확률 자료 없음 {unknown_rain}일"
    else:
        rain_text = ", ".join(rainy) if rainy else "강수확률 40% 이상 날짜 없음"
    week = "7일 수원 최저·최고 기온과 강수 가능 날짜: " + rain_text
    return BriefingContent(summary=summary, umbrella=umbrella, outfit=outfit, commute=commute_text,
                           daypart=daypart, week=week).model_dump()


def parse_model_result(text: str, forecast: dict[str, Any]) -> dict[str, Any]:
    """Reject unsafe claims and malformed JSON instead of silently substituting another model."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Codex 결과에 JSON 객체가 없습니다.")
    data = BriefingContent.model_validate_json(text[start:end + 1]).model_dump()
    all_text = " ".join(str(v) for v in data.values()).lower()
    if any(word in all_text for word in ("자전거", "노면", "미끄러", "시야", "안개")):
        raise ValueError("데이터로 뒷받침되지 않는 위험 또는 제외된 조언이 포함됐습니다.")
    return data


def model_prompt(forecast: dict[str, Any], day: date, source_errors: dict[str, str] | None = None) -> str:
    payload = json.dumps(forecast, ensure_ascii=False, separators=(",", ":"))
    return f"""날씨 데이터를 근거로 {day.isoformat()} 한국어 브리핑을 작성하세요. 입력 JSON 외의 사실을 추측하지 마세요.
응답은 설명 없이 다음 JSON 객체만 출력하세요. summary는 2~4문장 배열, 나머지는 짧은 문자열입니다.
{{"summary":["...","..."],"umbrella":"...","outfit":"...","commute":"...","daypart":"...","week":"..."}}
수원 중심이며 서울과 비교하세요. 출퇴근은 08~09시와 18~19시로 가정하고, 가정임을 commute에 표시하세요.
비 조언은 시간별 강수확률·강수량 데이터만 사용하고, 옷차림은 기온·체감 자료만 사용하세요.
자료 없는 안개·노면·시야·강풍 위험을 만들지 말고 자전거 조언도 하지 마세요. 결측은 추측하지 말고 부족하다고 말하세요.
주간 요약에 7일 최저/최고, 비 예상일, 주말을 포함하세요. 출처는 Open-Meteo이며 갱신 정보는 시스템이 따로 표시합니다.
지역별 수집 오류(이 지역 값은 추정하지 말 것): {json.dumps(source_errors or {}, ensure_ascii=False)}
입력:
{payload}"""


async def _fetch_one(location: tuple[str, float, float]) -> dict[str, Any]:
    name, lat, lon = location
    params = {
        "latitude": lat, "longitude": lon, "timezone": "Asia/Seoul", "forecast_days": 7,
        "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm",
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m,visibility",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get(API_URL, params=params)
                response.raise_for_status()
                raw = response.json()
            break
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
            status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if status_code is not None and status_code < 500 and status_code not in (408, 429):
                raise
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)
    else:
        assert last_error is not None
        raise last_error
    if not isinstance(raw, dict) or not isinstance(raw.get("current"), dict):
        raise TypeError(f"{name} 예보 응답에 현재 날씨가 없습니다.")
    return raw


async def _collect() -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    outcomes = await asyncio.gather(*(_fetch_one(loc) for loc in LOCATIONS.values()), return_exceptions=True)
    fetched = utcnow()
    raw_result: dict[str, Any] = {}
    normalized: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for i, (key, loc) in enumerate(LOCATIONS.items()):
        value = outcomes[i]
        if isinstance(value, BaseException):
            status_code = value.response.status_code if isinstance(value, httpx.HTTPStatusError) else None
            errors[key] = f"http_{status_code}" if status_code else type(value).__name__
            continue
        raw_result[key] = value
        normalized[key] = normalize_forecast(value, location=loc[0], fetched_at=fetched)
    if not normalized:
        raise RuntimeError("두 지역 모두 예보를 가져오지 못했습니다.")
    return raw_result, normalized, errors


async def _dispatch_model(row_id, day: date) -> None:
    """Create a normal user-owned Codex Run, preserving the engine account/profile routing."""
    from ttakkari.api.sessions import queue_run
    from ttakkari.api.workspaces import ensure_default_workspace
    from ttakkari.models import RunOrigin

    async with sessionmaker()() as db:
        row = await db.get(WeatherBriefing, row_id)
        if row is None or row.forecast is None:
            return
        session = await db.get(ChatSession, row.session_id) if row.session_id else None
        if session is None:
            workspace = await ensure_default_workspace(db)
            session = ChatSession(workspace_id=workspace.id, title=f"날씨 브리핑 · {day.isoformat()}",
                                  engine="codex", model=MODEL, effort=EFFORT, kind=SessionKind.chat, pool="personal")
            db.add(session)
            await db.flush()
            row.session_id = session.id
            row.status = "starting"
            await db.commit()
        prompt = model_prompt(row.forecast, day, row.source_errors)
        run = await queue_run(db, session, prompt, origin=RunOrigin.system, model=MODEL, effort=EFFORT,
                              request_key=f"weather:{day.isoformat()}")
        row = await db.get(WeatherBriefing, row_id)
        row.run_id = run.id
        row.status = "generating"
        row.lease_until = None
        row.updated_at = utcnow()
        await db.commit()


async def _publish(row_id) -> None:
    async with sessionmaker()() as db:
        row = await db.get(WeatherBriefing, row_id)
        if row is None or row.status not in {"ready", "degraded"} or row.push_sent_at is not None:
            return
        title = "오늘의 날씨 브리핑" if row.status == "ready" else "오늘의 날씨 브리핑 · 데이터 기반"
        body = " ".join((row.briefing or {}).get("summary", []))[:180]
        key = f"weather:{row.day.isoformat()}"
        try:
            delivery = await notify(PushKind.weather, title, body, url=f"/weather?date={row.day.isoformat()}",
                                    tag=key, target_id=row.id, dedupe_key=key)
        except Exception as exc:  # noqa: BLE001
            row.push_error = type(exc).__name__
            row.updated_at = utcnow()
            await db.commit()
            log.warning("weather push failed: %s", type(exc).__name__)
            return
        if delivery["sent"] == 0 or delivery["failed"] > 0:
            row.push_error = "no_subscriptions" if delivery["sent"] == 0 else "partial_delivery"
            row.push_sent_at = None
        else:
            row.push_sent_at = utcnow()
            row.push_error = None
        row.updated_at = utcnow()
        await db.commit()


async def advance(row_id) -> None:
    """Advance one durable state. A short DB lease makes old/new generations and manual/scheduled work race-safe."""
    now = utcnow()
    async with sessionmaker()() as db:
        row = await db.scalar(select(WeatherBriefing).where(WeatherBriefing.id == row_id).with_for_update(skip_locked=True))
        if row is None or (row.lease_until and row.lease_until > now):
            return
        if row.status in {"ready", "degraded"}:
            should_publish = row.push_sent_at is None
            row.lease_until = now + timedelta(minutes=2) if should_publish else None
            await db.commit()
            if should_publish:
                await _publish(row_id)
            return
        if row.status == "generating":
            run = await db.get(Run, row.run_id) if row.run_id else None
            if run is None or run.status not in {RunStatus.succeeded, RunStatus.failed, RunStatus.interrupted, RunStatus.cancelled}:
                return
            if run.status == RunStatus.succeeded and run.result_text:
                try:
                    content = parse_model_result(run.result_text, row.forecast or {})
                    row.analysis_status = "partial_source" if row.source_errors else "succeeded"
                    row.status = "degraded" if row.source_errors else "ready"
                    row.failure = None
                except (ValueError, ValidationError) as exc:
                    content = fallback_content(row.forecast or {}, row.day, row.source_errors)
                    row.analysis_status = "invalid_output"
                    row.status = "degraded"
                    row.failure = str(exc)[:500]
            else:
                content = fallback_content(row.forecast or {}, row.day, row.source_errors)
                row.analysis_status = "failed"
                row.status = "degraded"
                row.failure = (run.error or run.status.value)[:500]
            row.briefing = content
            row.generated_at = utcnow()
            row.lease_until = now + timedelta(minutes=2)
            await db.commit()
            await _publish(row_id)
            return
        row.lease_until = now + timedelta(minutes=3)
        row.updated_at = now
        state, day, rid = row.status, row.day, row.id
        await db.commit()

    try:
        if state in {"collecting", "failed"}:
            raws, forecast, errors = await _collect()
            async with sessionmaker()() as db:
                row = await db.get(WeatherBriefing, rid)
                row.raw_forecasts, row.forecast = raws, forecast
                row.source_errors = errors
                row.failure = None
                row.status = "starting"
                row.updated_at = utcnow()
                await db.commit()
            state = "starting"
        if state == "starting":
            await _dispatch_model(rid, day)
    except Exception as exc:  # noqa: BLE001 - state is persisted for UI/retry and next scan
        async with sessionmaker()() as db:
            row = await db.get(WeatherBriefing, rid)
            if row:
                if row.forecast:
                    row.status = "failed"
                else:
                    row.status = "failed"
                row.failure = type(exc).__name__
                row.lease_until = None
                row.updated_at = utcnow()
                await db.commit()
        log.warning("weather pipeline failed: %s", type(exc).__name__)
        return
    async with sessionmaker()() as db:
        row = await db.get(WeatherBriefing, rid)
        if row:
            row.lease_until = None
            row.updated_at = utcnow()
            await db.commit()


async def start_for_day(day: date, source: str) -> WeatherBriefing:
    async with sessionmaker()() as db:
        row = await db.scalar(select(WeatherBriefing).where(WeatherBriefing.day == day).with_for_update())
        if row is None:
            row = WeatherBriefing(day=day, status="collecting", request_source=source,
                                  scheduled_at=datetime.combine(day, time(6, 30), KST).astimezone(UTC)
                                  if source == "scheduled" else None)
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                row = await db.scalar(select(WeatherBriefing).where(WeatherBriefing.day == day))
        elif row.status == "failed":
            row.status = "collecting"
            row.failure = None
            row.request_source = source
            row.lease_until = None
            row.updated_at = utcnow()
            await db.commit()
        elif source == "manual" and row.status in {"ready", "degraded"} and row.push_error:
            row.push_error = None
            row.lease_until = None
            row.updated_at = utcnow()
            await db.commit()
        row_id = row.id
    await advance(row_id)
    async with sessionmaker()() as db:
        return await db.get(WeatherBriefing, row_id)


async def scan() -> None:
    now = datetime.now(KST)
    if now.time() >= time(6, 30):
        async with sessionmaker()() as db:
            row = await db.scalar(select(WeatherBriefing.id).where(WeatherBriefing.day == now.date()))
        if row is None:
            await start_for_day(now.date(), "scheduled")
    async with sessionmaker()() as db:
        pending = list(await db.scalars(select(WeatherBriefing.id).where(
            WeatherBriefing.status.in_(["collecting", "starting", "generating", "ready", "degraded"]),
            ((WeatherBriefing.lease_until.is_(None)) | (WeatherBriefing.lease_until <= utcnow())),
            ((WeatherBriefing.status.not_in(["ready", "degraded"])) | WeatherBriefing.push_error.is_(None)),
        ).order_by(WeatherBriefing.day.desc()).limit(10)))
    for row_id in pending:
        await advance(row_id)
