"""Backend TOOLS / function-calling registry for Gemini.

Every tool returns REAL data from free meteorological sources
(Open-Meteo / RainViewer / IMD) - Gemini NEVER invents numbers; it only
explains what these tools produce. This module is the single source of
authoritative weather data for the chatbot -> dashboard pipeline.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Callable

from . import imd, meteo, radar
from .config import settings
from .mockdata import mock_daily, mock_hourly, mock_current

log = logging.getLogger("weathergpt.tools")

# --- simple geocode cache (name -> (ts, result)) -----------------------------
_geo_cache: dict[str, tuple[float, Any]] = {}


def _time() -> float:
    import time as _t
    return _t.time()

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def resolve_location(location: str | None, lat: float | None = None, lon: float | None = None) -> dict[str, Any] | None:
    """City name -> coordinates (Nominatim preference, Open-Meteo fallback).
    Results are cached 24h to keep latency low on repeated asks."""
    global _geo_cache
    if location and str(location).strip():
        q = str(location).strip()
        key = q.lower()
        now = _time()
        hit = _geo_cache.get(key)
        if hit and now - hit[0] < 86400:
            return hit[1]
        loc = None
        # 1) Nominatim (free/open geocoder per requirements) - short timeout, cached
        try:
            import requests
            resp = requests.get(settings.nominatim_url, params={
                "q": q, "format": "jsonv2", "limit": 1, "accept-language": "en",
            }, headers={"User-Agent": "WeatherGPT/1.0 (free)"}, timeout=6)
            if resp.status_code == 200 and resp.json():
                r = resp.json()[0]
                loc = {"name": r.get("display_name", q).split(",")[0].strip(),
                       "latitude": float(r["lat"]), "longitude": float(r["lon"]),
                       "country": (r.get("address") or {}).get("country", ""),
                       "admin1": (r.get("address") or {}).get("state", ""),
                       "source": "nominatim"}
        except Exception as exc:
            log.debug("nominatim fallback used (%s)", exc)
        # 2) Open-Meteo geocoding (also free, orientation to Indian cities)
        if not loc:
            locs = meteo.geocode(q)
            if locs:
                loc = {**locs[0], "source": "open-meteo-geocoder"}
        if loc:
            _geo_cache[key] = (now, loc)
            return loc
    if lat is not None and lon is not None:
        name = location or f"{lat:.2f},{lon:.2f}"
        return {"name": name, "latitude": float(lat), "longitude": float(lon),
                "country": "", "source": "coordinates"}
    return None


def _deg_to_dir(deg: float | None) -> str:
    if deg is None:
        return "unknown"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def _fmt_data(daily: dict[str, Any], n: int = 10) -> list[dict[str, Any]]:
    times = daily.get("time") or []
    out = []
    wc = daily.get("weather_code") or []
    for i in range(min(n, len(times))):
        def g(key):
            lst = daily.get(key)
            return lst[i] if isinstance(lst, list) and i < len(lst) else None
        out.append({
            "date": times[i],
            "weather_code": g("weather_code"),
            "temperature_max_c": g("temperature_2m_max"),
            "temperature_min_c": g("temperature_2m_min"),
            "precipitation_mm": g("precipitation_sum"),
            "rain_probability_pct": g("precipitation_probability_max"),
            "wind_gusts_kmh": g("wind_gusts_10m_max"),
            "uv_index": g("uv_index_max"),
        })
    return out


def _mock_guard(result, mocked: bool) -> dict[str, Any]:
    if mocked:
        result.setdefault("source", "mock")
    return result


# ---------------------------------------------------------------------------
# TOOL IMPLEMENTATIONS  (name, description, params, handler)
# ---------------------------------------------------------------------------

def _fmt_current(place: str, lat: float, lon: float) -> dict[str, Any]:
    if settings.use_mock_data:
        c = mock_current(lat, lon)
    else:
        data = meteo.forecast(lat, lon, days=1)
        c = (data.get("current") or {})
    return {
        "place": place, "latitude": round(lat, 4), "longitude": round(lon, 4),
        "observed_at": c.get("time"),
        "temperature_c": c.get("temperature_2m"),
        "feels_like_c": c.get("apparent_temperature"),
        "humidity_pct": c.get("relative_humidity_2m"),
        "weather_code": c.get("weather_code"),
        "cloud_cover_pct": c.get("cloud_cover"),
        "wind_speed_kmh": c.get("wind_speed_10m"),
        "wind_gusts_kmh": c.get("wind_gusts_10m"),
        "wind_direction": _deg_to_dir(c.get("wind_direction_10m")),
        "precipitation_mm": c.get("precipitation"),
        "pressure_hpa": c.get("surface_pressure"),
        "is_day": c.get("is_day"),
        "source": "open-meteo (live)" if not settings.use_mock_data else "mock",
    }


def tool_current_weather(location: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    loc = resolve_location(location, lat, lon)
    if not loc:
        return {"error": f"could not resolve location '{location}'"}
    base = _fmt_current(loc["name"], loc["latitude"], loc["longitude"])
    extra = {"location": loc}
    if not settings.use_mock_data:
        station = imd.live_observation(loc["latitude"], loc["longitude"])
        if station.get("ok"):
            extra["imd_aws_observation"] = station
    return {**base, **extra}


def tool_hourly_forecast(location: str | None = None, date: str | None = None,
                         lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    loc = resolve_location(location, lat, lon)
    if not loc:
        return {"error": f"could not resolve location '{location}'"}
    if settings.use_mock_data:
        hourly = mock_hourly()
    else:
        data = meteo.forecast(loc["latitude"], loc["longitude"], days=2)
        hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    rows = []
    target = date or None
    for i, t in enumerate(times):
        if target and target != "today" and not t.startswith(target):
            continue
        def g(key):
            lst = hourly.get(key)
            return lst[i] if isinstance(lst, list) and i < len(lst) else None
        rows.append({
            "time": t, "temperature_c": g("temperature_2m"),
            "feels_like_c": g("apparent_temperature"),
            "rain_probability_pct": g("precipitation_probability"),
            "precipitation_mm": g("precipitation"),
            "weather_code": g("weather_code"),
            "wind_kmh": g("wind_speed_10m"),
            "humidity_pct": g("relative_humidity_2m"),
        })
        if len(rows) >= 24:
            break
    return {"place": loc["name"], "date": date or "today", "hours": rows,
            "source": "open-meteo (live)" if not settings.use_mock_data else "mock"}


def tool_daily_forecast(location: str | None = None, days: int = 7,
                        lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    loc = resolve_location(location, lat, lon)
    if not loc:
        return {"error": f"could not resolve location '{location}'"}
    days = max(1, min(int(days or 7), 10))
    if settings.use_mock_data:
        daily = mock_daily(days)
    else:
        data = meteo.forecast(loc["latitude"], loc["longitude"], days=days)
        daily = data.get("daily") or {}
    return {"place": loc["name"], "days": days, "forecast": _fmt_data(daily, days),
            "source": "open-meteo (live)" if not settings.use_mock_data else "mock"}


def tool_weather_alerts(location: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    loc = resolve_location(location, lat, lon)
    if not loc:
        loc = {"name": "current location", "latitude": lat or 28.61, "longitude": lon or 77.20}
    imd_warns = imd.imd_warnings(loc["latitude"], loc["longitude"], loc["name"])
    if settings.use_mock_data:
        from .mockdata import mock_alerts
        imd_warns = mock_alerts()
    else:
        nwp = meteo.all_warnings(loc["latitude"], loc["longitude"])
        imd_warns = imd_warns + [w for w in nwp if w.get("event", "").lower().startswith("imd") is False]
    return {
        "place": loc["name"],
        "alerts": imd_warns,
        "source_notes": "IMD (live if IMD_API_KEY set, else labelled mock) + NWP-derived warnings (live)",
    }


def tool_radar_data(location: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    cat = radar.radar_catalog()
    past = (cat.get("radar") or {}).get("past") or []
    nowcast = cat.get("nowcast") or []
    frames = [{"time": f.get("time"), "path": f.get("path")} for f in past + nowcast]
    frames.sort(key=lambda f: f.get("time") or "")
    return {
        "source": "RainViewer radar (live)" if cat.get("source") == "rainviewer" else "mock",
        "note": cat.get("note"),
        "frame_count": len(frames),
        "frames": frames,
        "host": cat.get("host"),
        "latest_time": frames[-1]["time"] if frames else None,
        "tile_schemes": {"2": "original", "4": "universal-blue", "5": "universal"},
    }


def tool_temperature_map(location: str | None = None, hour_offset: int | None = None,
                         lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    """Real Open-Meteo grid map (temperature/wind/cloud + composite intensity).
    hour_offset (0-47) returns the forecast frame that many hours ahead -
    used to animate the thermal 'video prediction' with real NWP data."""
    loc = resolve_location(location, lat, lon)
    if not loc:
        loc = {"name": "current location", "latitude": lat or 28.61, "longitude": lon or 77.20}
    grid = meteo.make_grid(loc["latitude"], loc["longitude"], hour_offset=hour_offset)
    return {
        "place": loc["name"],
        "source": "open-meteo live grid" if not settings.use_mock_data else "mock",
        "hour_offset": hour_offset,
        "points": grid,
        "interpolation": "bilinear (rendered client-side)",
        "intensity": "composite severity 0-100 (heat+wind+lightning, real data)",
        "units": "°C",
    }


def tool_historical_weather(location: str | None = None, start_date: str | None = None,
                            end_date: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    loc = resolve_location(location, lat, lon)
    if not loc:
        return {"error": f"could not resolve location '{location}'"}
    try:
        start = start_date or (date.today() - timedelta(days=365)).isoformat()
        end = end_date or date.today().isoformat()
        tr = meteo.monthly_trend(loc["latitude"], loc["longitude"], years=5)
        rows = tr.get("years", [])
        return {
            "place": loc["name"], "start": start, "end": end,
            "yearly_averages": rows,
            "trend_c_per_year": tr.get("trend_c_per_year"),
            "source": "open-meteo archive (ERA5-backed, live)" if not settings.use_mock_data else "mock",
        }
    except Exception as exc:
        return {"error": f"history unavailable: {exc}"}


def tool_weather_risk(weather_data: dict | None = None, location: str | None = None,
                      lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    """Compute disaster-management risk indicators from REAL weather data."""
    loc = resolve_location(location, lat, lon)
    if not loc:
        loc = {"name": "current location", "latitude": lat or 28.61, "longitude": lon or 77.20}
    fc = tool_daily_forecast(location=loc["name"], days=7)
    days = fc.get("forecast", [])[:7]
    risks: list[dict[str, Any]] = []
    tmax = [d["temperature_max_c"] for d in days if d.get("temperature_max_c") is not None]
    rain = [d["precipitation_mm"] for d in days if d.get("precipitation_mm") is not None]
    gusts = [d["wind_gusts_kmh"] for d in days if d.get("wind_gusts_kmh") is not None]
    if tmax and max(tmax) >= 40:
        risks.append({"type": "heat", "level": "red" if max(tmax) >= 45 else "orange",
                      "value": max(tmax), "unit": "°C",
                      "advice": "Heat stress: hydrate, avoid 12-4pm outdoor work, protect livestock/crops."})
    if rain and max(rain) >= 50:
        risks.append({"type": "flood", "level": "red" if max(rain) >= 100 else "orange",
                      "value": max(rain), "unit": "mm",
                      "advice": "Flash-flood risk: clear drains, avoid underpasses, alert low-lying areas."})
    elif rain and max(rain) >= 25:
        risks.append({"type": "rain", "level": "yellow", "value": max(rain), "unit": "mm",
                      "advice": "Moderate rain: carry umbrella, plan drainage maintenance."})
    if gusts and max(gusts) >= 60:
        risks.append({"type": "wind", "level": "orange", "value": max(gusts), "unit": "km/h",
                      "advice": "Strong gusts: secure hoardings/tarps, delay outdoor events."})
    stormy = any(d.get("weather_code") in (95, 96, 99) for d in days)
    if stormy:
        risks.append({"type": "lightning", "level": "orange", "value": "yes", "unit": "",
                      "advice": "Lightning risk: avoid open fields & tall trees, unplug sensitive electronics."})
    overall = "red" if any(r["level"] == "red" for r in risks) else \
              ("orange" if any(r["level"] == "orange" for r in risks) else
               ("yellow" if risks else "green"))
    return {"place": loc["name"], "overall_risk": overall, "risks": risks,
            "source": "derived from live NWP data" if not settings.use_mock_data else "mock"}


_SECTOR_HINTS = {
    "agriculture": ["sowing", "irrigation", "spray", "harvest window", "soil moisture proxy"],
    "aviation": ["visibility", "crosswind", "convective avoidance", "runway guidance"],
    "marine": ["wave heights", "safe fishing window", "port operations"],
    "urban": ["waterlogging", "transport", "drainage maintenance", "energy demand"],
}


def tool_sector_advisory(weather_data: dict | None = None, sector: str = "agriculture",
                         location: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    loc = resolve_location(location, lat, lon)
    if not loc:
        loc = {"name": "current location", "latitude": lat or 28.61, "longitude": lon or 77.20}
    sector = (sector or "agriculture").lower()
    fc = tool_daily_forecast(location=loc["name"], days=7)
    days = fc.get("forecast", [])[:7]
    rain_days = [d for d in days if (d.get("precipitation_mm") or 0) >= 1.0]
    tmax = [d["temperature_max_c"] for d in days if d.get("temperature_max_c") is not None]
    gust = max([d["wind_gusts_kmh"] for d in days if d.get("wind_gusts_kmh") is not None] or [0])
    advice_pool: dict[str, list[str]] = {
        "agriculture": [
            "Rain expected: window for sowing/irrigation ✓" if rain_days else "Dry spell: plan irrigation now.",
            "Heat spike: irrigate early morning/evening to reduce crop stress." if tmax and max(tmax) >= 35 else
            "Temperatures moderate for most field crops.",
            "Spray window: avoid pesticide application during rain/high wind days.",
        ],
        "aviation": [
            "Convective activity possible - monitor departure alternates." if any(d.get("weather_code") in (95, 96, 99) for d in days) else
            "Thunderstorm risk low - VFR conditions likely.",
            f"Peak gusts ~{gust:.0f} km/h - brief crews on crosswind limits." if gust >= 40 else
            "Wind within normal operational limits.",
            "Rain likely: expect ATIS QNH changes & runway wet ops." if rain_days else "Dry runway conditions expected.",
        ],
        "marine": [
            "Moderate seas - fishing possible with caution near shore." if 1.0 <= gust <= 3.0 else
            "Calm seas favourable for fishing and coastal trips." if gust < 1.0 else
            "Rough seas: advise small boats to stay near shore.",
            f"Peak wind ~{gust:.0f} km/h - port operations advisory.",
        ],
        "urban": [
            "Waterlogging risk in low-lying roads: clear storm drains." if any((d.get("precipitation_mm") or 0) >= 25 for d in days) else
            "No major drainage pressure in the next 7 days.",
            "Plan commutes early on rain days; expect minor delays." if rain_days else
            "Good conditions for public infrastructure maintenance.",
        ],
    }
    return {"place": loc["name"], "sector": sector,
            "headline": f"{sector.title()} advisory - {loc['name']}",
            "points": advice_pool.get(sector, advice_pool["agriculture"]),
            "data_summary": {"rain_days_7d": len(rain_days), "max_temp_c": max(tmax) if tmax else None,
                             "peak_gust_kmh": gust},
            "source": "derived from live NWP data" if not settings.use_mock_data else "mock"}


def tool_get_location_coordinates(location: str) -> dict[str, Any]:
    loc = resolve_location(location)
    if not loc:
        return {"error": "location not found"}
    return loc


def tool_weather_brief(location: str | None = None, lat: float | None = None,
                       lon: float | None = None) -> dict[str, Any]:
    """Structured 'Today's Weather Brief': DATA -> INTERPRETATION -> RISK -> ACTION.
    Built deterministically from REAL data (never invented). Falls back to live
    IMD observations if Open-Meteo is unavailable; never fabricates."""
    from datetime import datetime
    loc = resolve_location(location, lat, lon) or {"name": "Delhi", "latitude": 28.6139, "longitude": 77.2090}
    name = loc["name"]
    cur = {}
    source = "open-meteo (live)"
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=2)
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    hourly = data.get("hourly") or {}
    if not cur.get("temperature_2m"):
        obs = imd.live_observation(loc["latitude"], loc["longitude"])
        if obs.get("ok"):
            cur = {"temperature_2m": obs.get("temperature_c"), "apparent_temperature": obs.get("temperature_c"),
                   "relative_humidity_2m": obs.get("relative_humidity"), "wind_speed_10m": obs.get("wind_speed"),
                   "wind_direction_10m": obs.get("wind_direction"), "precipitation": obs.get("rain")}
            source = f"IMD AWS ({obs.get('station')})"
    if not cur.get("temperature_2m"):
        return {"place": name, "updated_at": datetime.now().strftime("%I:%M %p"), "source": "temporarily unavailable",
                "summary": "Weather data is temporarily unavailable for this location.", "risk_level": "unknown",
                "key_factors": [], "recommendation": "Try again in a moment.", "confidence": "n/a"}
    prob = hourly.get("precipitation_probability") or []
    precip = hourly.get("precipitation") or []
    daily_tmax = daily.get("temperature_2m_max") or []
    daily_tmin = daily.get("temperature_2m_min") or []
    def _idx(vals, i): return vals[i] if isinstance(vals, list) and i < len(vals) else None
    rainy = [i for i in range(min(len(prob), 24)) if ((_idx(prob, i) or 0) >= 60) or ((_idx(precip, i) or 0) >= 1.0)]
    desc = meteo.weather_code_name(cur.get("weather_code")) if cur.get("weather_code") is not None else "current conditions"
    temp = cur.get("temperature_2m")
    names = "rain" if rainy else "conditions"
    summary = f"Right now in {name}: {desc}, around {temp}°C." if temp is not None else f"Conditions in {name}: {desc}."
    if rainy:
        summary += f" Rain likely in the coming hours (peak probability ~{_idx(prob, rainy[0]) or 0}%)."
    if daily_tmax and daily_tmax[0] is not None:
        summary += f" Today's range around {daily_tmin[0] if daily_tmin and daily_tmin[0] is not None else '—'}°C to {daily_tmax[0]}°C."
    factors = []
    if rainy: factors.append({"label": "Precipitation", "value": f"{_idx(prob, rainy[0]) or 0}% chance", "level": "moderate"})
    if (cur.get("wind_speed_10m") or 0) >= 30: factors.append({"label": "Wind", "value": f"{int(cur['wind_speed_10m'])} km/h", "level": "moderate"})
    if (cur.get("relative_humidity_2m") or 0) >= 85: factors.append({"label": "Humidity", "value": f"{int(cur['relative_humidity_2m'])}%", "level": "moderate"})
    risk_level = (tool_weather_risk(location=name).get("overall_risk") or "low")
    hi = daily_tmax[0] if daily_tmax and daily_tmax[0] is not None else None
    if risk_level == "red":
        rec = "Take precautions: avoid unnecessary travel in severe conditions, stay indoors during storms, keep an emergency kit ready."
    elif risk_level == "orange":
        rec = "Moderate risk today — stay alert for changing conditions" + ("; carry rain protection and plan around the heaviest hours." if rainy else ".")
    elif risk_level == "yellow":
        rec = "Some risk today — check for updates if you have outdoor plans in the afternoon."
    elif rainy and hi is not None and hi >= 33:
        rec = "Light rain expected — carry an umbrella; moderate heat means stay hydrated if you're out."
    else:
        rec = "Weather looks stable — a good day for outdoor plans."
    confidence = "high" if source == "open-meteo (live)" else ("medium" if source.startswith("IMD") else "low")
    return {"place": name, "summary": summary, "risk_level": risk_level, "key_factors": factors,
            "recommendation": rec, "confidence": confidence, "source": source,
            "updated_at": datetime.now().strftime("%I:%M %p"),
            "disclaimer": "WeatherGPT interprets meteorological data; values come from live sources."}


def tool_cyclone_watch(location: str | None = None, south: float | None = None, west: float | None = None,
                       north: float | None = None, east: float | None = None) -> dict[str, Any]:
    """Cyclone / severe-storm watch: real NWP wind centers (peak point always
    returned) + official IMD tracks when IMD_API_KEY is configured (free)."""
    if None not in (south, west, north, east):
        grid = meteo.make_grid_bbox(south, west, north, east)
    else:
        loc = resolve_location(location) or {"name": "Delhi", "latitude": 28.61, "longitude": 77.2}
        grid = meteo.make_grid(loc["latitude"], loc["longitude"])
    storm_pts = []
    max_wind = 0.0
    for p in grid:
        wind = float(p.get("wind_speed_10m") or 0)
        if wind > max_wind: max_wind = wind
        if wind >= 40:
            storm_pts.append({"lat": p["latitude"], "lon": p["longitude"], "wind_kmh": round(wind, 0), "type": "strong-wind-center"})
    if not storm_pts and max_wind > 0:
        best = max(grid, key=lambda p: float(p.get("wind_speed_10m") or 0))
        storm_pts.append({"lat": best["latitude"], "lon": best["longitude"], "wind_kmh": round(max_wind, 0), "type": "max-wind-center"})
    if max_wind >= 62: watch_note = "cyclone-grade winds (>=62 km/h) detected - monitor IMD bulletins."
    elif max_wind >= 40: watch_note = "strong wind centers (>=40 km/h) - watch for cyclonic development."
    else: watch_note = f"calm - peak wind {round(max_wind, 0)} km/h in view (real NWP)."
    tracks = []
    if settings.imd_api_key:
        try:
            import requests as _req
            r = _req.get("https://api.imd.gov.in/api/v1/cyclone_track", params={"key": settings.imd_api_key},
                         timeout=settings.timeout_s, headers={"key": settings.imd_api_key})
            if r.status_code == 200:
                data = r.json(); recs = data.get("features") or data.get("data") or []
                for rec in (recs if isinstance(recs, list) else []):
                    geom = (rec.get("geometry") or {}).get("coordinates") or []
                    props = rec.get("properties") or {}
                    tracks.append({"lat": geom[1], "lon": geom[0], "name": props.get("name") or "Cyclone",
                                   "intensity": props.get("intensity") or props.get("category")})
        except Exception as exc:
            log.debug("IMD cyclone track unavailable: %s", exc)
    return {"source": "live NWP wind field" + (" + IMD track" if tracks else " (IMD track: set IMD_API_KEY)"),
            "storms": storm_pts[:12], "tracks": tracks[:12], "max_wind_kmh": round(max_wind, 0), "watch": watch_note,
            "note": "Watch points are real wind centers from the current NWP field."}


# ---------------------------------------------------------------------------
# Registry (declaration shared with Gemini API functionDeclarations)
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict[str, Any]] = {
    "get_current_weather": {
        "description": "Get real-time current weather (temperature, feels-like, humidity, wind, pressure, precipitation, cloud cover) for a place.",
        "parameters": {"location": {"type": "string", "description": "City / place name"},
                       "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_current_weather,
    },
    "get_hourly_forecast": {
        "description": "Get hourly forecast for a place, optionally filtered by date (YYYY-MM-DD).",
        "parameters": {"location": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD or 'today'"},
                       "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_hourly_forecast,
    },
    "get_daily_forecast": {
        "description": "Get daily forecast (high/low, rain probability, precipitation, wind, UV) for N days (1-10).",
        "parameters": {"location": {"type": "string"}, "days": {"type": "integer"},
                       "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_daily_forecast,
    },
    "get_weather_alerts": {
        "description": "Get official IMD district warnings + NWP-derived severe weather alerts (heatwave, heavy rain, thunderstorm, fog, gale).",
        "parameters": {"location": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_weather_alerts,
    },
    "get_radar_data": {
        "description": "Get real RainViewer precipitation radar frame catalog (timestamps, tile paths) for animation.",
        "parameters": {"location": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_radar_data,
    },
    "get_temperature_map": {
        "description": "Get real Open-Meteo grid around a place: temperature, wind, cloud cover plus a 0-100 weather intensity index. Optional hour_offset (0-47) returns the forecast frame that many hours ahead (for thermal prediction video).",
        "parameters": {"location": {"type": "string"}, "hour_offset": {"type": "integer", "description": "hours ahead, 0-47"},
                       "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_temperature_map,
    },
    "get_historical_weather": {
        "description": "Get historical/climate data (yearly averages, trends) for a place.",
        "parameters": {"location": {"type": "string"}, "start_date": {"type": "string"},
                       "end_date": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_historical_weather,
    },
    "calculate_weather_risk": {
        "description": "Compute disaster-management risk indicators (heat, flood, wind, lightning) from real weather data.",
        "parameters": {"location": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_weather_risk,
    },
    "generate_sector_advisory": {
        "description": "Generate a decision-support advisory for a sector (agriculture, aviation, marine, urban).",
        "parameters": {"sector": {"type": "string"}, "location": {"type": "string"},
                       "lat": {"type": "number"}, "lon": {"type": "number"}},
        "handler": tool_sector_advisory,
    },
    "get_location_coordinates": {
        "description": "Resolve a place name into coordinates + country/state.",
        "parameters": {"location": {"type": "string"}},
        "handler": tool_get_location_coordinates,
    },
}


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if not spec:
        return {"error": f"unknown tool {name}"}
    handler: Callable[..., dict[str, Any]] = spec["handler"]
    try:
        result = handler(**{k: v for k, v in (args or {}).items() if k in spec["parameters"]})
    except TypeError:
        result = handler(**(args or {}))
    except Exception as exc:  # a broken tool must never break the conversation
        log.exception("tool %s failed", name)
        result = {"error": f"{name} failed: {exc}"}
    return {"tool": name, "result": result}


def tool_declarations() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "parameters": {
                "type": "OBJECT",
                "properties": spec["parameters"],
                "required": [k for k, v in spec["parameters"].items() if k == "location"],
            },
        }
        for name, spec in TOOLS.items()
    ]


def dashboard_action(result: dict[str, Any], context: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Translate tool results into structured dashboard commands for the UI."""
    tool = result.get("tool")
    data = result.get("result") or {}
    q = query.lower()
    if tool == "get_current_weather":
        return {"type": "UPDATE_DASHBOARD", "view": "current", "payload": {"current": data, "location": data.get("location", data)}}
    if tool == "get_daily_forecast":
        return {"type": "UPDATE_DASHBOARD", "view": "daily_forecast", "payload": {"forecast": data}}
    if tool == "get_hourly_forecast":
        return {"type": "UPDATE_DASHBOARD", "view": "hourly_forecast", "payload": {"hourly": data}}
    if tool == "get_weather_alerts":
        return {"type": "UPDATE_DASHBOARD", "view": "alerts", "payload": {"alerts": data}}
    if tool == "get_temperature_map":
        return {"type": "UPDATE_LAYER", "layer": "temperature", "payload": data}
    if tool == "get_radar_data":
        return {"type": "UPDATE_LAYER", "layer": "precipitation", "payload": data}
    if tool == "get_historical_weather":
        return {"type": "UPDATE_DASHBOARD", "view": "climate", "payload": {"climate": data}}
    if tool == "calculate_weather_risk":
        return {"type": "UPDATE_DASHBOARD", "view": "risk", "payload": {"risk": data}}
    if tool == "generate_sector_advisory":
        return {"type": "UPDATE_DASHBOARD", "view": "advisory", "payload": {"advisory": data}}
    if tool == "get_location_coordinates":
        return {"type": "UPDATE_LOCATION", "payload": data}
    return None


# ---------------------------------------------------------------------------
# ADVICE ENGINE (deterministic; real data only; Gemini only reframes it)
# ---------------------------------------------------------------------------
_ACTIVITY_KEYWORDS = {
    "cycling": ["cycl", "bike", "ride", "bicycle"],
    "running": ["run", "jog"],
    "walking": ["walk"],
    "picnic": ["picnic", "outdoor plan", "outside"],
    "sports": ["football", "cricket", "play", "sport", "game"],
    "commute": ["commute", "drive", "driving", "travel", "road"],
    "umbrella": ["umbrella", "rain gear", "carry umbrella"],
    "heat": ["too hot", "heat", "hot", "sunburn", "uv"],
}
_MAP_KEYWORDS = {
    "precipitation": ["rain radar", "radar", "precipitation", "rain map"],
    "temperature": ["temperature", "thermal", "heat map", "temp map"],
    "wind": ["wind", "windy"],
    "cyclone": ["cyclone", "storm", "typhoon"],
}


def detect_advice(message: str | None) -> dict | None:
    """Classify a message as an ADVICE question, a MAP command, or neither.
    Returns {kind:'advice', activity} | {kind:'map', layer, span} | None."""
    m = (message or "").lower()
    # MAP commands (explicit show/see + layer word, or "any <layer>?" natural asks)
    map_words = any(w in m for w in ("show", "see", "display", "open", "view", "switch", "activate"))
    if map_words or m.strip() in ("radar", "wind", "temperature", "cyclones", "cyclone") or "any" in m:
        for layer, kws in _MAP_KEYWORDS.items():
            if any(k in m for k in kws):
                span = "d3" if "tomorrow" in m else ("live" if layer == "precipitation" else state_span_default())
                return {"kind": "map", "layer": layer, "span": span}
    # ADVICE questions
    for activity, kws in _ACTIVITY_KEYWORDS.items():
        if any(k in m for k in kws):
            return {"kind": "advice", "activity": activity}
    return None


def state_span_default() -> str:
    return "d3"


def activity_advice(lat: float, lon: float, name: str, activity: str,
                    day: str | None = None) -> dict:
    """Deterministic outdoor-advice analysis from REAL forecast data.
    Returns structured insight (verdict/evidence/recommendation/best_window);
    Gemini may reframe the text, but every number here is real."""
    from datetime import datetime
    data = meteo.forecast(lat, lon, days=2)
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    prob = hourly.get("precipitation_probability") or []
    precip = hourly.get("precipitation") or []
    temp = hourly.get("temperature_2m") or []
    feels = hourly.get("apparent_temperature") or []
    gust = hourly.get("wind_gusts_10m") or []
    codes = hourly.get("weather_code") or []
    tmax = (daily.get("temperature_2m_max") or [])
    tmax_today = tmax[0] if tmax else None

    def idx(vals, i): return vals[i] if isinstance(vals, list) and i < len(vals) else None
    def frac(v): return v == v and v is not None  # not nan

    # Scan the next 24h into 3-4h windows
    windows = []
    for start in range(0, min(len(times), 24), 4):
        seg = list(range(start, min(start + 4, min(len(times), 24))))
        if not seg:
            continue
        p = [idx(prob, i) for i in seg if idx(prob, i) is not None]
        t = [idx(temp, i) for i in seg if idx(temp, i) is not None]
        f = [idx(feels, i) for i in seg if idx(feels, i) is not None]
        g = [idx(gust, i) for i in seg if idx(gust, i) is not None]
        c = [idx(codes, i) for i in seg if idx(codes, i) is not None]
        w = {
            "label": _window_label(times[start]),
            "rain_prob": max(p) if p else None,
            "precip_mm": sum(idx(precip, i) or 0 for i in seg),
            "temp_max": max(t) if t else None,
            "feels_max": max(f) if f else None,
            "wind_max": max(g) if g else None,
            "storm": any(x in (95, 96, 99) for x in c) if c else False,
        }
        windows.append(w)

    def score_w(w):
        s = 0.0
        if w["rain_prob"] is not None:
            if w["rain_prob"] >= 60: s -= 3.0
            elif w["rain_prob"] >= 40: s -= 1.5
        if w["precip_mm"] and w["precip_mm"] >= 1: s -= 1.0
        if w["storm"]: s -= 3.5
        if w["feels_max"] is not None:
            if w["feels_max"] >= 36: s -= 3.0
            elif w["feels_max"] >= 33: s -= 1.5
            elif w["feels_max"] <= 5: s -= 1.5
            elif 15 <= w["feels_max"] <= 29: s += 2.0
        if w["wind_max"] is not None and w["wind_max"] >= 40: s -= 2.0
        return s

    for w in windows:
        w["score"] = round(score_w(w), 1)
    # Best window (prefer windows with a real chance; label now/local)
    best = max(windows, key=lambda w: w["score"]) if windows else None

    # Aggregate risk facts
    next_hours = windows[:3]
    rain_up = max([w["rain_prob"] or 0 for w in next_hours]) if next_hours else 0
    feels_now = cur.get("apparent_temperature")
    temp_now = cur.get("temperature_2m")
    wind_now = cur.get("wind_speed_10m")
    storm_likely = any(w["storm"] for w in windows[:6])

    verdict, recommendation, why = _make_advice(activity, rain_up, storm_likely,
                                                feels_now, temp_now, wind_now, tmax_today, best, windows[:6])
    return {
        "place": name, "activity": activity,
        "verdict": verdict, "why": why, "recommendation": recommendation,
        "best_window": (best["label"] if best and best["score"] >= 0 else None),
        "evidence": {
            "temp_now_c": temp_now, "feels_now_c": feels_now, "rain_risk_pct": rain_up,
            "wind_kmh": wind_now, "storm_likely": storm_likely, "max_today_c": tmax_today,
            "windows": windows[:8],
        },
        "source": "Open-Meteo (live)",
    }


def _window_label(tiso: str) -> str:
    try:
        from datetime import datetime as dt
        h = int(dt.fromisoformat(tiso).hour)
        day = "Tom" if dt.fromisoformat(tiso).date() != dt.now().date() else "Today"
        part = ("morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17 else
                "evening" if 17 <= h < 22 else "night")
        return f"{day} {part}"
    except Exception:
        return "later"


def _make_advice(activity, rain_up, storm, feels_now, temp_now, wind_now,
                 tmax_today, best, windows):
    activity_label = {
        "cycling": "cycling", "running": "going for a run", "walking": "taking a walk",
        "picnic": "a picnic", "sports": "playing outdoor sports", "commute": "travelling",
        "umbrella": "heading outside", "heat": "spending time outside",
    }.get(activity, "being outside")
    # Umbrella / heat get dedicated, data-driven answers (never "postpone" generically)
    if activity == "umbrella":
        if storm:
            verdict = "Yes — definitely keep an umbrella handy (and stay indoors if a thunderstorm passes through)."
            recommendation = "Thunderstorms are expected, so rain could be heavy and sudden."
        elif rain_up >= 60:
            verdict = "I'd take an umbrella. Rain is quite likely in the next few hours."
            recommendation = "Keep it handy, especially if you'll be out later."
        elif rain_up >= 40:
            verdict = "You may want to keep an umbrella nearby; some rain chances are in the forecast."
            recommendation = "It probably won't be needed right away, but it would be useful later."
        else:
            verdict = "You're probably fine without an umbrella today."
            recommendation = "Rain chances look low for the next few hours."
    elif activity == "heat":
        if feels_now is not None and feels_now >= 35:
            verdict = "It's quite hot right now, so make sure to stay hydrated and take breaks."
            recommendation = "Plan outdoor time for early morning or after sunset."
        else:
            verdict = f"It's {temp_now}°C right now with a feels-like of {feels_now}°C."
            recommendation = "Heat is manageable — just stay hydrated and avoid the hottest window."
    elif storm:
        verdict = f"Conditions look unsafe for {activity_label} today — thunderstorms are expected."
        recommendation = "I'd postpone it and wait for conditions to improve."
    else:
        if rain_up >= 60:
            verdict = f"This isn't the best time for {activity_label} — rain looks likely in the next few hours."
            recommendation = ("If you can, shift it later; the roads could stay wet after the rain." if best and best["score"] >= -1
                              else "I'd wait until conditions improve.")
        elif rain_up >= 40:
            verdict = f"{activity_label[0].upper() + activity_label[1:]} is possible today, but there's a chance of rain."
            recommendation = "Best to start early and keep a backup plan for later."
        else:
            verdict = f"This looks like a reasonable window for {activity_label}."
            if feels_now is not None and feels_now >= 33:
                recommendation = "It may feel warm — go earlier in the day or in the evening to stay comfortable."
            else:
                recommendation = "Conditions are comfortable; enjoy it."
    # Best available window, if genuinely good
    if best and best["score"] >= 1 and best["label"]:
        recommendation += f" Looking at the forecast, {best['label'].lower()} looks like the best window."
    elif best and best["score"] < 0 and windows and any(w["score"] >= 0 for w in windows):
        rec_good = [w for w in windows if w["score"] >= 0]
        if rec_good:
            recommendation += f" A better window looks to be {rec_good[0]['label'].lower()}."
    return verdict, recommendation, "Based on the live forecast."
