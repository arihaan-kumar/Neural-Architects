"""Deterministic sample data for UI development when external APIs are
unreachable. Used ONLY when USE_MOCK_DATA=true; every consumer labels
results as MOCK so they are never presented as live/official data.
"""
from __future__ import annotations

from datetime import date, timedelta

MOCK_RADAR_DAYS = 7


def mock_current(lat: float, lon: float) -> dict:
    return {
        "time": "mock", "temperature_2m": 29.5, "apparent_temperature": 33.0,
        "relative_humidity_2m": 68, "is_day": 1, "precipitation": 0.0,
        "weather_code": 2, "cloud_cover": 40, "wind_speed_10m": 14.2,
        "wind_direction_10m": 240, "wind_gusts_10m": 24.0, "surface_pressure": 1006.4,
    }


def mock_daily(days: int = 10) -> dict:
    today = date.today()
    wc = [0, 2, 61, 80, 95, 3, 2, 61, 1, 3][:days]
    return {
        "time": [(today + timedelta(days=i)).isoformat() for i in range(days)],
        "weather_code": wc,
        "temperature_2m_max": [33, 34, 31, 30, 29, 32, 33, 30, 34, 33][:days],
        "temperature_2m_min": [24, 25, 24, 23, 23, 24, 25, 24, 25, 24][:days],
        "precipitation_sum": [0.0, 0.0, 12.0, 4.0, 18.0, 0.0, 0.0, 8.0, 0.0, 0.0][:days],
        "precipitation_probability_max": [10, 15, 75, 60, 90, 10, 12, 70, 10, 15][:days],
        "wind_speed_10m_max": [18, 20, 26, 22, 34, 19, 18, 24, 17, 19][:days],
        "wind_gusts_10m_max": [30, 32, 44, 36, 58, 30, 31, 40, 28, 32][:days],
        "uv_index_max": [7, 8, 6, 5, 4, 7, 8, 5, 8, 7][:days],
        "sunrise": [(today + timedelta(days=i)).isoformat() + "T06:10" for i in range(days)],
        "sunset": [(today + timedelta(days=i)).isoformat() + "T18:20" for i in range(days)],
    }


def mock_hourly() -> dict:
    today = date.today()
    return {
        "time": [(today + timedelta(hours=i)).isoformat() + "T%02d:00" % ((10 + i) % 24) for i in range(24)],
        "temperature_2m": [26 + (i % 10) for i in range(24)],
        "precipitation_probability": [max(0, 60 - i * 3) for i in range(24)],
        "weather_code": [61 if i % 6 == 3 else 2 for i in range(24)],
        "precipitation": [2.0 if i % 6 == 3 else 0.0 for i in range(24)],
    }


def mock_alerts() -> list:
    return [{
        "event": "Thunderstorm (MOCK)", "severity": "orange", "source": "IMD (mock)",
        "description": "Sample: thunderstorms with lightning likely in the evening. "
                       "MOCK DATA - replace with live IMD warnings.",
    }]


def mock_radar() -> dict:
    """Safe sample frame list (no real tiles, clearly labelled)."""
    now = date.today().isoformat() + "T00:00"
    return {
        "source": "mock", "version": "mock",
        "host": "https://tilecache.rainviewer.com",
        "radar": {"past": [{"time": now, "path": "/v2/radar/mock/000"}]},
        "nowcast": [],
        "note": "Mock radar (sample frames) - RainViewer unavailable",
    }


def mock_grid(lat: float, lon: float, size: int = 9) -> dict:
    import math
    step = 0.35
    rows = []
    for i in range(size):
        for j in range(size):
            la = lat - step * ((size - 1) / 2 - i)
            lo = lon - step * ((size - 1) / 2 - j)
            rows.append({
                "latitude": round(la, 3), "longitude": round(lo, 3),
                "temperature_2m": round(26 + 6 * math.sin(i / 2.0), 1),
                "wind_speed_10m": round(8 + (i * j) % 14, 1),
                "wind_direction_10m": 0,
                "cloud_cover": round(((i + j) * 9) % 101, 0),
                "cape": 400 + (i * 30 % 900),
            })
    return {"points": rows, "source": "mock"}


def mock_history(years: int = 5) -> dict:
    rows = []
    start = date.today().year - years + 1
    for y in range(start, date.today().year + 1):
        rows.append({"year": y, "avg_tmax": 30.5 + (y - start) * 0.12,
                     "avg_tmin": 20.1 + (y - start) * 0.08,
                     "annual_precip_mm": 900 + (y - start) * 18})
    return {"years": rows, "trend_c_per_year": 0.12, "n_years": len(rows), "source": "mock"}
