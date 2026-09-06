"""Central configuration for WeatherGPT.

All settings are environment-overridable so the app runs unchanged
on a laptop, in Docker, or in Kubernetes (12-factor style).
Secrets (GEMINI_API_KEY) are read EXCLUSIVELY from the process environment /
.env file on the backend - never hardcoded, never shipped to the frontend.
Everything here is free / open-source by default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env once at import time (backend only). Missing file is fine.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, "").strip().lower() in ("1", "true", "yes", "on") if os.environ.get(key) else default


@dataclass
class Settings:
    app_name: str = "WeatherGPT"
    version: str = "1.0.0"

    # --- Data providers (all free, no API key) ---
    meteo_base: str = field(default_factory=lambda: _env("METEO_BASE", "https://api.open-meteo.com/v1"))
    forecast_url: str = field(default_factory=lambda: _env("FORECAST_URL", "https://api.open-meteo.com/v1/forecast"))
    archive_url: str = field(default_factory=lambda: _env("ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive"))
    marine_url: str = field(default_factory=lambda: _env("MARINE_URL", "https://marine-api.open-meteo.com/v1/marine"))
    air_url: str = field(default_factory=lambda: _env("AIR_URL", "https://air-quality-api.open-meteo.com/v1/air-quality"))
    flood_url: str = field(default_factory=lambda: _env("FLOOD_URL", "https://flood-api.open-meteo.com/v1/flood"))
    alerts_url: str = field(default_factory=lambda: _env("ALERTS_URL", "https://alerts.open-meteo.com/v1"))
    geocoding_url: str = field(default_factory=lambda: _env("GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1"))
    nominatim_url: str = field(default_factory=lambda: _env("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"))
    radar_url: str = field(default_factory=lambda: _env("RAINVIEWER_URL", "https://api.rainviewer.com/public/weather-maps.json"))
    translate_url: str = field(default_factory=lambda: _env("TRANSLATE_URL", "https://clients5.google.com/translate_a/t"))
    translate_fallback_url: str = field(default_factory=lambda: _env(
        "TRANSLATE_FALLBACK_URL", "https://translate.googleapis.com/translate_a/single"))

    timeout_s: int = field(default_factory=lambda: _env_int("HTTP_TIMEOUT", 15))
    cache_ttl_s: int = field(default_factory=lambda: _env_int("CACHE_TTL", 600))

    # --- Storage ---
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./weathergpt.db"))

    # --- Mock mode (UI development without external APIs; clearly labelled) ---
    use_mock_data: bool = field(default_factory=lambda: _env_bool("USE_MOCK_DATA", False))
    # Optional FREE Open-Meteo API key (open-meteo.com/signup) raises the daily
    # request limit from 10k to 100k. Sent as &apikey= when set.
    meteo_api_key: str = field(default_factory=lambda: _env("METEO_API_KEY", ""))

    # --- IMD (free registration; optional backend-only secret) ---
    imd_api_key: str = field(default_factory=lambda: _env("IMD_API_KEY", ""))

    # --- Alerts ingestion ---
    alert_poll_seconds: int = field(default_factory=lambda: _env_int("ALERT_POLL_SECONDS", 600))
    watch_cities: str = field(default_factory=lambda: _env(
        "WATCH_CITIES",
        "Delhi,Mumbai,Kolkata,Chennai,Bengaluru,Hyderabad,Pune,Ahmedabad,Jaipur,"
        "Lucknow,Kanpur,Nagpur,Indore,Thane,Bhopal,Patna,Vadodara,Ludhiana,"
        "Surat,Visakhapatnam,Coimbatore,Bhubaneswar,Amaravati,Guwahati,Ranchi,"
        "Raipur,Kochi,Chandigarh,Dehradun,Shimla,Srinagar,Aizawl,Agartala,"
        "Imphal,Itanagar,Gangtok,Panaji,Nagaland,Kohima,Bengaluru",
    ))

    # --- AI brain: Gemini ONLY (function calling). Rule engine is the no-key fallback ---
    llm_backend: str = field(default_factory=lambda: _env("LLM_BACKEND", "gemini"))
    # SECRET: read from process environment / .env ONLY (backend). Never in frontend.
    gemini_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.6-flash"))
    gemini_models_fallback: str = field(default_factory=lambda: _env(
        "GEMINI_MODELS_FALLBACK", "gemini-2.5-flash,gemini-2.0-flash"))
    gemini_base: str = field(default_factory=lambda: _env(
        "GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta"))
    gemini_max_calls: int = field(default_factory=lambda: _env_int("GEMINI_MAX_CALLS", 5))

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_key) and self.gemini_key.strip() != "none"

    units_default: str = field(default_factory=lambda: _env("UNITS_DEFAULT", "metric"))
    language_default: str = field(default_factory=lambda: _env("LANGUAGE_DEFAULT", "en"))

    # --- Frontend ---
    static_dir: str = field(default_factory=lambda: _env("STATIC_DIR", "static"))

    # Supported languages (ISO code -> native label + Google translate code)
    languages: dict = field(default_factory=lambda: {
        "en": "English",
        "hi": "हिन्दी (Hindi)",
        "bn": "বাংলা (Bengali)",
        "ta": "தமிழ் (Tamil)",
        "te": "తెలుగు (Telugu)",
        "mr": "मराठी (Marathi)",
        "gu": "ગુજરાતી (Gujarati)",
        "kn": "ಕನ್ನಡ (Kannada)",
        "ml": "മലയാളം (Malayalam)",
        "pa": "ਪੰਜਾਬੀ (Punjabi)",
        "or": "ଓଡ଼ିଆ (Odia)",
        "as": "অসমীয়া (Assamese)",
        "ur": "اردو (Urdu)",
        "ne": "नेपाली (Nepali)",
    })

    watch_city_list: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.watch_city_list = [c.strip() for c in self.watch_cities.split(",") if c.strip()]


settings = Settings()
