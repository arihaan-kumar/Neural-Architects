import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./test_weathergpt.db"
os.environ["ALERT_POLL_SECONDS"] = "3600"
# Deterministic tests: force rule-engine path; Gemini path is covered by
# test_gemini_tools (mocked) and test_live (real key, network).
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(_env_path)
    except Exception:
        vals = {}
    if vals.get("GEMINI_API_KEY"):
        os.environ["_GEMINI_REAL_KEY"] = vals["GEMINI_API_KEY"]
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app import meteo


@pytest.fixture(autouse=True)
def clean_db():
    # recreate schema on a fresh file per test session
    from app.store import _engine
    yield
    # keep stats only; fine to keep file


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_forecast(monkeypatch):
    """Deterministic forecast payload with no network."""
    data = {
        "timezone": "Asia/Kolkata",
        "current": {"time": "2026-09-04T12:00", "temperature_2m": 31.2, "apparent_temperature": 34.0,
                    "relative_humidity_2m": 62, "is_day": 1, "precipitation": 0.4, "rain": 0.4,
                    "weather_code": 3, "cloud_cover": 80, "wind_speed_10m": 12.5, "wind_direction_10m": 180,
                    "wind_gusts_10m": 22.0},
        "hourly": {"time": ["2026-09-04T12:00"], "visibility": [9000]},
        "daily": {
            "time": ["2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08"],
            "weather_code": [3, 61, 80, 2, 95],
            "temperature_2m_max": [33.0, 31.0, 30.0, 33.5, 32.0],
            "temperature_2m_min": [24.0, 23.0, 22.5, 24.0, 23.5],
            "precipitation_sum": [0.2, 8.4, 3.1, 0.0, 12.0],
            "precipitation_probability_max": [20, 80, 65, 10, 90],
            "wind_gusts_10m_max": [25, 30, 28, 26, 42],
            "sunrise": ["2026-09-04T06:12", "2026-09-05T06:11", "2026-09-06T06:10", "2026-09-07T06:09", "2026-09-08T06:08"],
            "sunset": ["2026-09-04T18:41", "2026-09-05T18:40", "2026-09-06T18:39", "2026-09-07T18:38", "2026-09-08T18:37"],
        },
    }
    monkeypatch.setattr(meteo, "forecast", lambda lat, lon, days=7, model=None, units="metric": data)
    return data


@pytest.fixture
def mock_archive(monkeypatch):
    data = {
        "timezone": "Asia/Kolkata",
        "daily": {
            "time": ["2022-01-01", "2022-07-01", "2023-01-01", "2023-07-01", "2024-01-01", "2024-07-01"],
            "temperature_2m_max": [22, 34, 23, 35, 24, 36],
            "temperature_2m_min": [10, 25, 11, 26, 12, 27],
            "precipitation_sum": [5, 200, 6, 210, 7, 220],
        },
    }
    monkeypatch.setattr(meteo, "archive", lambda lat, lon, start, end, units="metric": data)
    return data


@pytest.fixture
def mock_geocode(monkeypatch):
    monkeypatch.setattr(meteo, "geocode", lambda q, count=5: [{
        "name": "Delhi", "latitude": 28.6139, "longitude": 77.2090, "country": "India",
        "admin1": "Delhi", "timezone": "Asia/Kolkata", "population": 16700000,
    }])
    return None


@pytest.fixture
def mock_alerts(monkeypatch):
    monkeypatch.setattr(meteo, "alerts", lambda lat, lon: [{
        "event": "Very heavy rain", "severity": "orange", "description": "Very heavy rain in Chennai",
        "start": "2026-09-05T00:00", "end": "2026-09-06T00:00",
    }])
    return None
