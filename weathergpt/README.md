# ☁️ WeatherGPT — Ask. Understand. Prepare.

**Conversational AI for Weather Forecasting, Alerts and Climate Information (SIH 26068)**
**100% free · fully autonomous · Gemini tool-calling backend (key stays server-side)**

WeatherGPT is an AI Weather Assistant + Meteorological Dashboard + Disaster-Management Command Center.
**The floating chatbot is the CONTROL interface — the dashboard is the WEATHER INTELLIGENCE display.**
Chat in the panel, get real weather data on the dashboard.

- 🧠 **Gemini (backend-only, key from `.env`)** — natural language understanding, intent detection,
  tool selection, structured dashboard commands, multilingual explanations. Gemini NEVER invents numbers:
  it explains what the backend weather tools return.
- 🌦 **Open-Meteo (free, no key)** — current/hourly/daily/historical weather, temperature, rainfall,
  wind, humidity, pressure, UV, GFS/ECMWF/ICON NWP models.
- 📡 **RainViewer (free)** — real precipitation radar frames, animated on the map (no fake movement).
- 🏛 **IMD (official)** — real AWS/synoptic station observations (no key) via station index bundled from
  IMD's site + optional free `IMD_API_KEY` for official district warnings (register at
  api.imd.gov.in/public/register.php). Unreachable ⇒ clearly labelled **MOCK**, never passed as live.
- 🗺 **MapLibre + OSM-derived basemap** (no Google Maps/Places/Geocoding anywhere) + **Nominatim/Open-Meteo**
  free geocoding.
- ⚠ **Severe-weather rules** (heatwave, heavy rain, thunderstorm, cold wave, fog, gale) derived from live
  NWP data + WebSocket alert push to all clients.
- 🗣 **Voice** via Web Speech API (🎤 input + 🔊 TTS) in 14 Indian languages + English.
- 🧪 **`USE_MOCK_DATA=true`** — labelled sample data for offline UI development.

## 🏗 Architecture

```
Frontend (dashboard + floating chatbot, MapLibre, Chart.js, Web Speech)
      │  (no API keys ever leave the backend)
      ▼
FastAPI  ──►  /api/chat ──►  Gemini (function calling, GEMINI_API_KEY from env/.env ONLY)
      │                              │  tool selection
      │                              ▼
      │                     [Tool registry — REAL data]
      │   get_current_weather        → Open-Meteo + IMD AWS
      │   get_hourly_forecast        → Open-Meteo
      │   get_daily_forecast         → Open-Meteo (GFS/ECMWF/ICON)
      │   get_weather_alerts         → IMD (+ NWP-derived rules)
      │   get_radar_data             → RainViewer (real frames)
      │   get_temperature_map        → Open-Meteo live grid (interpolated heatmap)
      │   get_historical_weather     → Open-Meteo archive (ERA5)
      │   calculate_weather_risk     → derived from real NWP data
      │   generate_sector_advisory   → agriculture/aviation/marine/urban
      │   get_location_coordinates   → Nominatim / Open-Meteo geocoder
      │                                    │
      ▼                                    ▼
  Structured result → { action: UPDATE_DASHBOARD | UPDATE_LAYER | UPDATE_LOCATION }
      → dashboard renders REAL data; chatbot shows only the conversational reply
  ⚠ If Gemini fails/absent → built-in rule engine answers; app NEVER crashes.
```

## 🚀 Quick Start (free, no keys needed for weather)

```bash
cp .env.example .env            # contains your GEMINI_API_KEY (secret, git-ignored)
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
# → http://localhost:8000
```

Docker: `docker compose up --build` (`.env` is mounted automatically).

## 🔐 Gemini configuration (secret discipline)

- `GEMINI_API_KEY` is read **exclusively** in `app/config.py` from the process environment / `.env`.
- The key is **never** in frontend JS/HTML, not in localStorage/sessionStorage, not in API responses
  (only `gemini_configured` / `model` / `auth.valid` booleans are exposed), never logged, never committed
  (`.env` is git-ignored; `.env.example` carries the template value).
- Missing key ⇒ UI shows **"Gemini API key is not configured."** and the built-in rule engine runs.
- Google rejects the key (401 `ACCESS_TOKEN_TYPE_UNSUPPORTED` — known AI Studio AQ-key rollout issue) ⇒
  the UI shows **"Gemini key rejected by Google → rule engine"**; `POST /api/status` reports
  `gemini.auth = {valid:false, reason:"google_rejected_key"}`. Regenerate the key at
  aistudio.google.com/apikey (free) and update `.env` — no code changes needed.
- `GEMINI_MODEL` defaults to `gemini-3.6-flash`; `GEMINI_MODELS_FALLBACK` provides a chain.
- **Free-tier quota:** the AI Studio standard free tier allows **20 requests/day** per project/model
  (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). WeatherGPT mitigates this: 5-minute response
  cache, no background Gemini calls (risks/alerts/dashboard refresh use Open-Meteo directly), retry
  honoring the API's own "retry in Xs" hint, and automatic rule-engine fallback when quota is exhausted
  (the UI explains the situation; weather data stays real and free).
  💡 Easiest fix (no code): AI Studio → Projects → enable **Usage-based billing** → the project
  auto-upgrades Free→Tier 1 (instant) → limits jump from 20/day to tiers of ~500+ RPM / millions RPD
  for flash-class models; usage is pay-per-token (a demo costs pennies, and new accounts often have
  free credits). The built-in rule engine remains the always-available zero-cost fallback.

## 🧠 AI engine: Gemini only (by design)

| Backend | Cost | Quality | Setup |
|---|---|---|---|
| `gemini` (default, only one) | free 20 req/day; lift via usage-based billing | ★★★★★ multilingual + tool-calling | `GEMINI_API_KEY` in `.env` |
| rule engine | zero cost | ★★★ deterministic, ~0 ms, 14 languages | always-on automatic fallback |

(Groq / Ollama / OpenAI-compatible backends were removed — WeatherGPT stays Gemini-only, keeping the
footprint, docs and config clean. `agent.py` is the single dispatch point so a future backend can be
added without touching the pipeline.)

## 🖥 Dashboard (chatbot only speaks; dashboard shows data)

Location search · current weather (feels-like, high/low, humidity, wind, pressure, visibility, UV,
sunrise/sunset) · 24h hourly + rain probability · 7–10 day forecast · weather intelligence panel ·
risk indicators (heat/flood/wind/lightning) · official alerts (IMD + derived) · sector advisories ·
climate charts (ERA5 trend) · **large MapLibre map** with layers:
- 🌧 **Radar** — RainViewer REAL frames (past + nowcast), play/pause/timeline/latest/timestamp/legend.
- 🌡 **Thermal** — temperature heatmap interpolated from a LIVE Open-Meteo grid, with **▶ video
  prediction**: animates real NWP forecast frames now → +3h → +6h → +9h → +12h (no fake imagery).
- 🔥 **Intensity** — weather severity index 0–100 composite (heat + wind + lightning from real grid data).
- ☁ clouds · 💨 wind vectors (real) · ⚡ lightning potential (where available) ·
  ⚠ severe-risk points · 🏛 IMD alert points (mock clearly labelled) + zoom/locate/fullscreen.

## 🧪 Tests

```bash
uv run pytest                          # offline suite (mocked APIs) — 38 tests
uv run pytest -m live                  # real-API checks (network + optional key)
```

## ⚙️ Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | — | **secret**, backend-only, from `.env` |
| `GEMINI_MODEL` | `gemini-3.6-flash` | model for tool calling |
| `USE_MOCK_DATA` | `false` | labelled sample data (UI dev offline) |
| `IMD_API_KEY` | — | optional free key for live IMD district warnings |
| `ALERT_POLL_SECONDS` | `600` | watcher cadence (40 cities) |
| `DATABASE_URL` | `sqlite:///./weathergpt.db` | or `postgresql+psycopg://…` |
| `GEMINI_MAX_CALLS` / `CACHE_TTL` / `HTTP_TIMEOUT` | 5 / 600 / 15 | tuning |

## ✅ Verified (this build)

1. Backend-only Gemini config (key read from `.env`; auth status surface in `/api/status`; graceful fallback).
2. Open-Meteo real data (current/forecast/history/UV/grid) ✓ live.
3. RainViewer real frames (13 live frames, timestamps) ✓; radar animates real tiles.
4. MapLibre frontend with radar/thermal/wind/cloud/risk/IMD layers (no Google services).
5. Chatbot → dashboard actions (`UPDATE_DASHBOARD`, `UPDATE_LAYER`, `UPDATE_LOCATION`).
6. Key never leaves the backend (verified by absence of key in any `api/…` payload).
7. No paid APIs; mock mode clearly labelled on every panel.
8. IMD: real AWS observations (live, no key); warnings via mock until a free `IMD_API_KEY` is set.
