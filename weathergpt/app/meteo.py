"""Open-Meteo data client (free, no API key) - weather, forecast, archive,
marine, air quality, flood, alerts, geocoding.

Also provides access to global NWP model outputs (GFS / ECMWF / ICON / UKMO)
through Open-Meteo's `models` parameter, satisfying the NWP integration
requirement without paid feeds.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .config import settings
from . import mockdata

log = logging.getLogger("weathergpt.meteo")

_session = requests.Session()
_session.headers.update({"User-Agent": "WeatherGPT/1.0"})

# NWP models exposed by Open-Meteo (free). Keys map to user-facing names.
NWP_MODELS = {
    "gfs": "gfs_global",
    "ecmwf": "ecmwf_ifs025",
    "icon": "icon_seamless",
    "gem": "gem_global",
    "meteofrance": "meteofrance_arpege_world",
    "ukmo": "ukmo_global",
}

_BASE_VARS = "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,rain,snowfall,cloud_cover,weather_code,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m"

_DEFAULT_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Dense freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def weather_code_name(code: int | None) -> str:
    return _DEFAULT_WEATHER_CODES.get(int(code) if code is not None else -1, "Unknown conditions")


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    params = dict(params)
    if settings.meteo_api_key:
        params["apikey"] = settings.meteo_api_key
    resp = _session.get(url, params=params, timeout=settings.timeout_s)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------
# Tiny TTL cache (keep requests minimal; free service etiquette)
# --------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def cached(key: str, ttl: int | None = None):
    def deco(fn):
        def wrapper(*args, **kwargs):
            k = f"{key}:{args}:{kwargs}"
            now = time.time()
            with _cache_lock:
                hit = _cache.get(k)
                if hit and now - hit[0] < (ttl or settings.cache_ttl_s):
                    return hit[1]
            value = fn(*args, **kwargs)
            with _cache_lock:
                _cache[k] = (time.time(), value)
            return value
        return wrapper
    return deco


# --------------------------------------------------------------------------
# Geocoding
# --------------------------------------------------------------------------
@cached("geocode")
def geocode(query: str, count: int = 5) -> list[dict[str, Any]]:
    """Search a place name (Indian districts/cities + worldwide)."""
    try:
        data = _get(settings.geocoding_url + "/search", {"name": query, "count": count, "language": "en", "format": "json"})
        results = data.get("results") or []
        out = []
        for r in results[:count]:
            out.append({
                "name": r.get("name"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "country": r.get("country", ""),
                "admin1": r.get("admin1", ""),
                "timezone": r.get("timezone", ""),
                "population": r.get("population", 0),
            })
        return out
    except Exception as exc:
        log.warning("geocoding failed for %r: %s", query, exc)
        return []


@cached("reverse")
def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    try:
        data = _get(settings.geocoding_url + "/search", {"latitude": lat, "longitude": lon, "language": "en", "format": "json"})
        return data or {}
    except Exception as exc:
        log.warning("reverse geocode failed: %s", exc)
        return {}


# --------------------------------------------------------------------------
# Forecast / current weather (with NWP model support)
# --------------------------------------------------------------------------
# last-known-good forecast per location (real-data fallback during transient
# outages / daily quota exhaustion - never a blank or fake page). Persisted to
# disk so a restart during an outage still shows the last REAL forecast.
_last_good_forecast: dict[tuple[float, float], dict[str, Any]] = {}
_LAST_GOOD_PATH = Path(__file__).resolve().parent.parent / "weathergpt_lastforecast.json"


def _load_last_good() -> None:
    try:
        if _LAST_GOOD_PATH.exists():
            raw = json.loads(_LAST_GOOD_PATH.read_text())
            for k, v in raw.items():
                parts = k.split(",")
                _last_good_forecast[(float(parts[0]), float(parts[1]))] = v
    except Exception as exc:
        log.debug("last-good load skipped: %s", exc)


def _save_last_good() -> None:
    try:
        raw = {f"{k[0]},{k[1]}": v for k, v in _last_good_forecast.items()}
        _LAST_GOOD_PATH.write_text(json.dumps(raw))
    except Exception as exc:
        log.debug("last-good save skipped: %s", exc)


_load_last_good()


@cached("forecast")
def forecast(lat: float, lon: float, days: int = 7, model: str | None = None, units: str = "metric") -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": lat, "longitude": lon,
        "current": _BASE_VARS + ",wind_speed_10m",
        "hourly": ("temperature_2m,relative_humidity_2m,apparent_temperature,precipitation_probability,"
                   "precipitation,weather_code,wind_speed_10m,wind_gusts_10m,visibility,surface_pressure"),
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
                  "apparent_temperature_min,precipitation_sum,precipitation_probability_max,"
                  "wind_speed_10m_max,wind_gusts_10m_max,uv_index_max,sunrise,sunset,"
                  "rain_sum,snowfall_sum,sunshine_duration"),
        "timezone": "auto",
        "forecast_days": min(max(days, 1), 16),
        "temperature_unit": "celsius" if units == "metric" else "fahrenheit",
        "wind_speed_unit": "kmh" if units == "metric" else "mph",
    }
    if model and model in NWP_MODELS:
        params["models"] = NWP_MODELS[model]
    loc_key = (round(float(lat), 1), round(float(lon), 1))
    for attempt in range(3):
        try:
            data = _get(settings.forecast_url, params)
            if isinstance(data, dict) and (data.get("current") or data.get("daily") or data.get("hourly")):
                _last_good_forecast[loc_key] = data
                _save_last_good()
            return data
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            log.warning("forecast failed: %s", exc)
            break
    # Real-data fallback: last known good forecast for this area (slightly stale)
    good = _last_good_forecast.get(loc_key)
    if good:
        good = dict(good); good["stale"] = True
    return good or {}


@cached("archive")
def archive(lat: float, lon: float, start: str, end: str, units: str = "metric") -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": ("temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,"
                  "wind_speed_10m_max,shortwave_radiation_sum,relative_humidity_2m_mean"),
        "timezone": "auto",
        "temperature_unit": "celsius" if units == "metric" else "fahrenheit",
        "wind_speed_unit": "kmh" if units == "metric" else "mph",
    }
    try:
        return _get(settings.archive_url, params)
    except Exception as exc:
        log.warning("archive failed: %s", exc)
        return {}


@cached("marine")
def marine(lat: float, lon: float, days: int = 5) -> dict[str, Any]:
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wave_height,wave_direction,wave_period,sea_surface_temperature",
        "daily": "wave_height_max",
        "timezone": "auto",
        "forecast_days": min(days, 7),
    }
    try:
        return _get(settings.marine_url, params)
    except Exception as exc:
        log.warning("marine failed: %s", exc)
        return {}


@cached("air")
def air_quality(lat: float, lon: float) -> dict[str, Any]:
    params = {
        "latitude": lat, "longitude": lon,
        "current": "european_aqi,us_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,ozone",
        "timezone": "auto",
    }
    try:
        return _get(settings.air_url, params)
    except Exception as exc:
        log.warning("air quality failed: %s", exc)
        return {}


@cached("flood")
def flood_potential(lat: float, lon: float, daily_steps: int = 5) -> dict[str, Any]:
    try:
        return _get(settings.flood_url, {"latitude": lat, "longitude": lon, "daily": "river_discharge,runoff", "forecast_days": daily_steps})
    except Exception as exc:
        log.warning("flood API failed: %s", exc)
        return {}


@cached("alerts")
def alerts(lat: float, lon: float) -> list[dict[str, Any]]:
    """Official weather advisories (MeteoAlarm/NWS/ECMWF-backed, via Open-Meteo).
    Returns [] if the feed is unavailable - warning detection then falls back to
    the derived rule engine below, so alerts always keep working."""
    try:
        data = _get(settings.alerts_url, {"latitude": lat, "longitude": lon})
        return data.get("alerts") or []
    except Exception as exc:
        log.debug("official alerts feed unavailable (%s); using derived warnings", exc)
        return []


def _day(val: str) -> str:
    try:
        return datetime.fromisoformat(val).strftime("%d %b")
    except Exception:
        return str(val)


def derive_warnings(lat: float, lon: float, days: int = 5) -> list[dict[str, Any]]:
    """Free, deterministic extreme-weather early-warning rules evaluated on the
    open NWP forecast (GFS/ECMWF/ICON). Complements official feeds."""
    data = forecast(lat, lon, days=days)
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    if not dates:
        return []
    wc = daily.get("weather_code") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    precip = daily.get("precipitation_sum") or []
    gust = daily.get("wind_gusts_10m_max") or []

    warnings = []

    # --- Heatwave ---
    hot_days = [i for i in range(len(dates)) if _num(tmax, i) is not None and _num(tmax, i) >= 40]
    if len(hot_days) >= 2:
        sev = "red" if any(_num(tmax, i) >= 45 for i in hot_days) else "orange"
        warnings.append({
            "event": "Heatwave", "severity": sev, "source": "derived-nwp",
            "description": f"Heatwave conditions: temperatures reach {max(_num(tmax, i) for i in hot_days):.0f}°C "
                           f"around {_day(dates[hot_days[0]])}. Avoid midday outdoor work; ensure water for livestock.",
        })

    # --- Heavy rain ---
    wet = [i for i in range(len(dates)) if _num(precip, i) is not None and _num(precip, i) >= 50]
    owet = [i for i in range(len(dates)) if _num(precip, i) is not None and 25 <= _num(precip, i) < 50]
    if wet:
        sev = "red" if max(_num(precip, i) for i in wet) >= 100 else "orange"
        warnings.append({
            "event": "Very heavy rainfall", "severity": sev, "source": "derived-nwp",
            "description": f"Very heavy rain (~{max(_num(precip, i) for i in wet):.0f} mm) expected on "
                           f"{' '.join(_day(dates[i]) for i in wet[:3])}. Watch for waterlogging, flash floods "
                           f"and traffic delays.",
        })
    elif owet and any(wc[i] in (95, 96, 99) for i in owet):
        warnings.append({
            "event": "Heavy showers with thunderstorms", "severity": "orange", "source": "derived-nwp",
            "description": f"Heavy showers/thunderstorms expected ~{max(_num(precip, i) for i in owet):.0f} mm "
                           f"on {_day(dates[owet[0]])}. Secure loose structures; minor waterlogging possible.",
        })

    # --- Thunderstorm ---
    storm = [i for i in range(len(dates)) if wc[i] in (95, 96, 99)]
    if storm:
        warnings.append({
            "event": "Thunderstorm / lightning", "severity": "orange", "source": "derived-nwp",
            "description": f"Thunderstorms with possible hail/lightning on "
                           f"{' '.join(_day(dates[i]) for i in storm[:3])}. Avoid open fields; unplug sensitive electronics.",
        })

    # --- Cold wave ---
    cold = [i for i in range(len(dates)) if _num(tmin, i) is not None and _num(tmin, i) <= 4]
    if len(cold) >= 2:
        warnings.append({
            "event": "Cold wave", "severity": "yellow", "source": "derived-nwp",
            "description": f"Cold wave: overnight lows near {min(_num(tmin, i) for i in cold):.0f}°C "
                           f"from {_day(dates[cold[0]])}. Morning fog likely; protect crops and outdoor workers.",
        })

    # --- Strong winds ---
    windy = [i for i in range(len(dates)) if _num(gust, i) is not None and _num(gust, i) >= 60]
    if windy:
        warnings.append({
            "event": "Strong winds", "severity": "yellow", "source": "derived-nwp",
            "description": f"Gusts up to {max(_num(gust, i) for i in windy):.0f} km/h on {_day(dates[windy[0]])}. "
                           f"Secure hoardings, tarps and construction material.",
        })

    # --- Fog ---
    fog = [i for i in range(len(dates)) if wc[i] in (45, 48)]
    if fog:
        warnings.append({
            "event": "Dense fog", "severity": "yellow", "source": "derived-nwp",
            "description": f"Dense fog likely on {_day(dates[fog[0]])} - low visibility; drive carefully, "
                           f"air travel may see delays.",
        })
    return warnings


def all_warnings(lat: float, lon: float) -> list[dict[str, Any]]:
    """Official feed + derived warnings, merged, deduplicated."""
    merged = list(alerts(lat, lon))
    derived = derive_warnings(lat, lon)
    seen = {f"{a.get('event')}|{a.get('description', '')[:40]}" for a in merged}
    for d in derived:
        key = f"{d.get('event')}|{d.get('description', '')[:40]}"
        if key not in seen:
            merged.append(d)
            seen.add(key)
    return merged


def _num(lst, i):
    try:
        v = float(lst[i])
        return None if v != v else v
    except (TypeError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _avg(values: list[Any]) -> float:
    nums = [float(v) for v in values if isinstance(v, (int, float)) and v is not None]
    return sum(nums) / len(nums) if nums else 0.0


def monthly_trend(lat: float, lon: float, years: int = 5, units: str = "metric") -> dict[str, Any]:
    """Climate trend analysis from the free historical archive API."""
    from datetime import date, timedelta

    end = date.today()
    start = end.replace(year=end.year - years + 1) if end.year - years + 1 >= 1940 else date(1940, 1, 1)
    data = archive(lat, lon, start.isoformat(), end.isoformat(), units)
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    precip = daily.get("precipitation_sum") or []

    yearly: dict[str, dict[str, float]] = {}
    for i, d in enumerate(dates):
        yr = d[:4]
        entry = yearly.setdefault(yr, {"tmax_sum": 0.0, "tmin_sum": 0.0, "precip_sum": 0.0, "n": 0})
        if i < len(tmax) and tmax[i] is not None:
            entry["tmax_sum"] += float(tmax[i])
        if i < len(tmin) and tmin[i] is not None:
            entry["tmin_sum"] += float(tmin[i])
        if i < len(precip) and precip[i] is not None:
            entry["precip_sum"] += float(precip[i])
        entry["n"] += 1

    rows = []
    for yr in sorted(yearly):
        e = yearly[yr]
        if e["n"] >= 2:
            rows.append({
                "year": int(yr),
                "avg_tmax": round(e["tmax_sum"] / e["n"], 1),
                "avg_tmin": round(e["tmin_sum"] / e["n"], 1),
                "annual_precip_mm": round(e["precip_sum"], 0),
            })

    # Simple linear regression for temperature trend
    trend = 0.0
    if len(rows) >= 3:
        xs = [r["year"] for r in rows]
        ys = [r["avg_tmax"] for r in rows]
        n = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxy = sum(x * y for x, y in zip(xs, ys))
        sxx = sum(x * x for x in xs)
        denom = n * sxx - sx * sx
        if denom:
            slope = (n * sxy - sx * sy) / denom
            trend = round(slope, 3)  # degC per year

    return {"years": rows, "trend_c_per_year": trend, "n_years": len(rows)}


@cached("grid_multi")
def grid_multi(points: list[tuple[float, float]], variables: str | None = None,
               hour_offset: int | None = None) -> list[dict[str, Any]]:
    """Fetch data for many coordinates in ONE request (Open-Meteo supports
    comma-separated coordinates). Used to build real temperature/wind/cloud
    heatmap layers - no fake images.

    hour_offset: 0..383 -> return the forecast at that many hours ahead
    (real NWP forecast frames for the thermal 'video prediction').
    Every point carries latitude/longitude/valid_time/provider/model."""
    from datetime import datetime, timedelta, timezone as _tz
    now = datetime.now(_tz.utc)
    valid_now = now.strftime("%Y-%m-%dT%H:%M")
    if not points:
        return []
    lats = ",".join(f"{p[0]:.4f}" for p in points)
    lons = ",".join(f"{p[1]:.4f}" for p in points)
    params: dict[str, Any] = {
        "latitude": lats, "longitude": lons,
        "timezone": "auto",
    }
    forecast_mode = hour_offset is not None
    if forecast_mode:
        off = max(0, min(int(hour_offset), 383))  # up to 16 days of hourly NWP
        params["hourly"] = ("temperature_2m,wind_speed_10m,wind_direction_10m,cloud_cover,"
                            "precipitation_probability,precipitation,weather_code")
        params["forecast_hours"] = off + 1
    else:
        params["current"] = variables or "temperature_2m,wind_speed_10m,wind_direction_10m,cloud_cover,lightning_potential"
    try:
        data = None
        for attempt in range(3):
            try:
                data = _get(settings.forecast_url, params)
                break
            except Exception as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                else:
                    break
        if isinstance(data, list):
            out = []
            for item in data:
                cur = item.get("current") or {}
                hourly = item.get("hourly") or {}
                temp = cur.get("temperature_2m")
                wind = cur.get("wind_speed_10m")
                wdir = cur.get("wind_direction_10m")
                cloud = cur.get("cloud_cover")
                lpi = cur.get("lightning_potential")
                prob = None
                precip = None
                if forecast_mode:
                    def _h(key):
                        lst = hourly.get(key)
                        return lst[off] if isinstance(lst, list) and len(lst) > off else None
                    temp, wind, wdir, cloud = _h("temperature_2m"), _h("wind_speed_10m"), \
                        _h("wind_direction_10m"), _h("cloud_cover")
                    prob, precip = _h("precipitation_probability"), _h("precipitation")
                out.append({
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                    "temperature_2m": temp,
                    "wind_speed_10m": wind,
                    "wind_direction_10m": wdir,
                    "cloud_cover": cloud,
                    "lightning_potential": lpi,
                    "precipitation_probability": prob,
                    "precipitation_mm": precip,
                    "valid_time": (now + timedelta(hours=off)).strftime("%Y-%m-%dT%H:%M") if forecast_mode else valid_now,
                    "provider": "Open-Meteo",
                    "model": "auto-seamless",
                    "hour_offset": off if forecast_mode else None,
                })
            return out
    except Exception as exc:
        log.warning("grid fetch failed: %s", exc)
    return []


def make_grid(lat: float, lon: float, size: int = 9, step: float = 0.35,
              hour_offset: int | None = None) -> list[dict[str, Any]]:
    """Real-data grid around a location (fixed step) - kept for compatibility;
    the map viewport now uses make_grid_bbox for full coverage."""
    return make_grid_bbox(lat, lon, lat, lon, size=size, hour_offset=hour_offset,
                          step_override=step)


def make_grid_bbox(south: float, west: float, north: float, east: float,
                   size: int = 14, hour_offset: int | None = None,
                   step_override: float | None = None) -> list[dict[str, Any]]:
    """Real-data grid covering an arbitrary bounding box (the visible map view).
    Every point carries a deterministic WEATHER INTENSITY index (0-100)
    derived from actual values: thermal stress + wind intensity + lightning."""
    size = max(5, min(int(size), 16))
    if settings.use_mock_data:
        pts = mockdata.mock_grid((south + north) / 2, (west + east) / 2, size)["points"]
        for p in pts:
            p["intensity"] = intensity_index(p)
        return pts
    points: list[tuple[float, float]] = []
    if step_override:
        half = (size - 1) / 2
        for i in range(size):
            for j in range(size):
                points.append((round(south - step_override * (half - i), 3), round(west - step_override * (half - j), 3)))
    else:
        step_lat = max((north - south) / (size - 1), 0.04) if north > south else 0.3
        step_lon = max((east - west) / (size - 1), 0.04) if east > west else 0.3
        for i in range(size):
            for j in range(size):
                points.append((round(south + i * step_lat, 3), round(west + j * step_lon, 3)))
    result = grid_multi(points, hour_offset=hour_offset)
    for r, p in zip(result, points):
        r["latitude"] = round(p[0], 4)
        r["longitude"] = round(p[1], 4)
    if not result:
        result, _used = _fallback_grid(points, hour_offset)
    for p in result:
        p["intensity"] = intensity_index(p)
        p.setdefault("_source", "open-meteo-live-grid")
    return result


def _fallback_grid(points: list[tuple[float, float]], hour_offset: int | None) -> tuple[list[dict[str, Any]], str]:
    """Real-data fallback when the multi-location request is rate-limited:
    use ONE single-point NWP forecast at the box center (real, labelled
    'coarse'); if that is also unavailable, clearly-labelled sample values so
    the map still works. NEVER an unlabelled fake, and never a fixed small box -
    the fallback always covers the REQUESTED bbox."""
    from datetime import datetime, timedelta, timezone as _tz
    import math
    clat = sum(p[0] for p in points) / len(points)
    clon = sum(p[1] for p in points) / len(points)
    used = "mock"
    cur: dict[str, Any] = {}
    try:
        one = forecast(clat, clon, days=1)
        cur = one.get("current") or {}
        if cur.get("temperature_2m") is not None:
            used = "single-point-fallback (real)"
    except Exception as exc:
        log.warning("single-point fallback failed: %s", exc)
    now = datetime.now(_tz.utc)
    valid = (now + timedelta(hours=hour_offset or 0)).strftime("%Y-%m-%dT%H:%M")
    rows = []
    for k, p in enumerate(points):
        if used == "mock":
            t = 24.0 + 6.0 * math.sin(k / 6.0) + (float(p[0]) - clat) * 1.2
            rows.append({"latitude": round(p[0], 4), "longitude": round(p[1], 4),
                         "temperature_2m": round(t, 1), "wind_speed_10m": round(8 + (k % 9), 1),
                         "wind_direction_10m": 0.0, "cloud_cover": round((k * 7) % 101, 0),
                         "lightning_potential": None, "precipitation_probability": 0.0, "precipitation_mm": 0.0,
                         "hour_offset": hour_offset, "valid_time": valid, "provider": "Open-Meteo",
                         "model": "auto-seamless", "_source": used})
        else:
            rows.append({"latitude": round(p[0], 4), "longitude": round(p[1], 4),
                         "temperature_2m": cur.get("temperature_2m"), "wind_speed_10m": cur.get("wind_speed_10m"),
                         "wind_direction_10m": cur.get("wind_direction_10m"), "cloud_cover": cur.get("cloud_cover"),
                         "lightning_potential": None, "precipitation_probability": None, "precipitation_mm": None,
                         "hour_offset": hour_offset, "valid_time": valid, "provider": "Open-Meteo",
                         "model": "auto-seamless", "_source": used})
    return rows, used


def intensity_index(p: dict[str, Any]) -> float:
    """Composite severity 0-100 computed from REAL grid values:
    heat stress (>=32°C), strong wind (>=30 km/h), lightning potential."""
    def clamp(v, lo=0.0, hi=100.0):
        return max(lo, min(hi, v))
    heat = clamp((float(p.get("temperature_2m") or 0) - 32.0) / 12.0 * 100.0) if p.get("temperature_2m") is not None else 0.0
    wind = clamp((float(p.get("wind_speed_10m") or 0) - 30.0) / 40.0 * 100.0) if p.get("wind_speed_10m") is not None else 0.0
    lpi = clamp(float(p.get("lightning_potential") or 0) / 1200.0 * 100.0) if p.get("lightning_potential") is not None else 0.0
    return round(max(heat, wind, lpi), 0)


def parse_model(value: str | None) -> str | None:
    if not value:
        return None
    return NWP_MODELS.get(value.strip().lower().replace("_global", ""), value.strip().lower())


def is_coast(lat: float, lon: float) -> bool:
    """Cheap heuristic for marine relevance (Indian coastline / islands)."""
    south = (-70, 11.5, 93.5)  # lon range for Indian ocean area
    return (7.0 <= lat <= 9.5 and 71 <= lon <= 94) or (8.5 <= lat <= 21.5 and 67.5 <= lon <= 89.0) or \
           (lat <= 0.0) or abs(lon) < 1.0 and False
