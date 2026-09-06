"""WeatherGPT API - FastAPI application.

Endpoints (all free, no API keys required):
  GET  /                     mobile-first chat UI
  GET  /api/health           health & capabilities
  GET  /api/languages        supported languages / voice locales
  GET  /api/geocode?q=...    place search
  GET  /api/weather/now|forecast|history?q=...
  GET  /api/alerts?q=...     active alerts + global feed
  GET  /api/marine?q=..., /api/air?q=..., /api/climate?q=...&years=N
  GET  /api/status           system/dashboard metrics
  POST /api/chat             conversational endpoint (multilingual, voice-ready)
  WS   /ws                   live chat + /ws/alerts live alerts
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, gemini_client, imd, llm, meteo, nlu, radar, response, store, tools
from .alerts import alert_loop
from .config import settings
from .translate import translate
from .ws import hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("weathergpt")

DEFAULT_LOCATION = {"name": "Delhi", "latitude": 28.6139, "longitude": 77.2090, "country": "India"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    task = asyncio.create_task(alert_loop(hub))
    log.info("%s v%s started | storage: %s | llm: %s",
             settings.app_name, settings.version,
             settings.database_url.split("://")[0], settings.llm_backend)
    yield
    task.cancel()


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Never serve stale frontend assets (earlier browsers cached old app.js
    causing '$("#placeholderInput")' errors)."""
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(..., description="User utterance (any of 14 languages, text or ASR transcript)")
    language: str = Field(default="en", description="Response language ISO code")
    latitude: float | None = None
    longitude: float | None = None
    units: str = Field(default="metric", pattern="^(metric|imperial)$")
    session_id: str | None = None
    voice: bool = False
    context: dict[str, Any] = Field(default_factory=lambda: {
        "current_location": "", "current_page": "weather",
        "selected_date": "today", "active_layer": "temperature", "active_view": "daily_forecast",
    })
    history: list[dict[str, Any]] | None = None


class Location(BaseModel):
    name: str | None = None
    latitude: float
    longitude: float
    country: str | None = None
    admin1: str | None = None


# --------------------------------------------------------------------------
# Location resolution
# --------------------------------------------------------------------------
def resolve_location(q: str | None, lat: float | None, lon: float | None) -> dict[str, Any] | None:
    """q > coordinates > default. Coordinates always win if both given? No:
    explicit q is more descriptive; coordinates used when present without q."""
    if q and q.strip():
        locs = meteo.geocode(q.strip())
        if locs:
            return locs[0]
    if lat is not None and lon is not None:
        return {"name": q or f"{lat:.2f},{lon:.2f}", "latitude": lat, "longitude": lon, "country": ""}
    return None


def ensure_location(parsed, lat, lon, q) -> dict[str, Any]:
    city = parsed.entities.get("city")
    loc = resolve_location(q, lat, lon)
    if not loc and city:
        loc = (meteo.geocode(city) or [None])[0]
    if not loc and lat is not None and lon is not None:
        loc = {"name": f"{lat:.2f},{lon:.2f}", "latitude": lat, "longitude": lon, "country": ""}
    if not loc:
        loc = dict(DEFAULT_LOCATION)
    return loc


# --------------------------------------------------------------------------
# Core chat pipeline (Gemini function-calling first; rule engine fallback)
# --------------------------------------------------------------------------
def chat_pipeline(req: ChatRequest) -> dict[str, Any]:
    t0 = time.perf_counter()

    # ADVICE / MAP-COMMAND path: deterministic, real-data, always grounded.
    # (The core WeatherGPT behavior: UNDERSTAND -> REAL DATA -> ANALYZE -> INTERPRET -> RECOMMEND.)
    advice = tools.detect_advice(req.message)
    if advice:
        return _advice_pipeline(req, advice, t0)

    if agent.ready():
        result = _agent_pipeline(req)
        if not result.get("need_rule_fallback"):
            return result
        # Secondary brain: local Qwen3:8b (Ollama) if the primary is quota-limited/down
        secondary = agent.secondary_backend()
        if secondary:
            fallback_result = _agent_pipeline(req, backend=secondary)
            if not fallback_result.get("need_rule_fallback"):
                fallback_result["note"] = "Primary AI engine busy (quota) — answered by the local backup (Qwen3:8b)."
                return fallback_result
        out = _rule_pipeline(req, t0)
        out["gemini_status"] = "error"
        out["note"] = "AI engine call failed (auth, quota or model issue) — answered by the built-in rule engine."
        return out
    return _rule_pipeline(req, t0)


def _advice_pipeline(req: ChatRequest, adv: dict, t0: float) -> dict[str, Any]:
    """Answer advice/map questions with deterministic, data-grounded logic.
    Gemini only REFRAMES the validated text (never invents conditions/times)."""
    from . import nlu as _nlu

    # --- location context (avoid stale/duplicate resolution) ---
    parsed = _nlu.parse_query(req.message)
    city = parsed.entities.get("city")
    loc = None
    if city:
        loc = tools.resolve_location(city, req.latitude, req.longitude)
    if not loc and req.context.get("current_location") and req.context["current_location"] != "":
        if req.context["current_location"] not in ("My location",):
            loc = tools.resolve_location(req.context["current_location"], req.latitude, req.longitude)
    if not loc:
        loc = tools.resolve_location(None, req.latitude, req.longitude) or \
            {"name": DEFAULT_LOCATION["name"], "latitude": DEFAULT_LOCATION["latitude"], "longitude": DEFAULT_LOCATION["longitude"]}
    name = loc.get("name", "your area")

    # --- MAP command ---
    if adv["kind"] == "map":
        layer = adv["layer"]; span = adv.get("span", "live")
        label = {"precipitation": "rain radar", "temperature": "temperature", "wind": "wind", "cyclone": "cyclones"}.get(layer, layer)
        extra = " I've set the timeframe to look ahead as well." if span != "live" else ""
        reply = f"Done — I've opened the {label} layer for {name}.{extra}"
        action = {"type": "UPDATE_LAYER", "layer": layer, "span": span}
        intent = "map"
        payload = {}
        engine = "advice-engine"
    else:
        # --- ADVICE ---
        a = tools.activity_advice(loc["latitude"], loc["longitude"], name, adv["activity"])
        text = f"{a['verdict']} {a['recommendation']}"
        final = text
        target = req.language if req.language in settings.languages else "en"
        if agent.ready():
            final = gemini_client.reframe(text, target)
        if target != "en":
            final = translate(final, target, "auto")
        reply = final
        action = None
        intent = "advice"
        payload = {"advice": a}
        engine = "gemini-advice" if agent.ready() else "advice-engine"

    latency_ms = (time.perf_counter() - t0) * 1000.0
    store.save_chat(req.message, reply, req.language, intent, latency_ms, source_type="voice" if req.voice else "chat")
    return {
        "reply": reply, "intent": intent, "confidence": 1.0, "language": req.language,
        "detected_language": req.language, "location": {"name": name, "latitude": loc["latitude"], "longitude": loc["longitude"]},
        "payload": payload, "latency_ms": round(latency_ms, 1), "voice": req.voice,
        "engine": engine, "gemini_status": ("configured" if agent.ready() else "not-configured"),
        "model": agent.model_label(), "turns": [], "sources": ["Open-Meteo (live)"],
        "action": action,
        "context": {**dict(req.context or {}), "current_location": name, "active_view": "advice"},
    }


def _rule_pipeline(req: ChatRequest, t0: float) -> dict[str, Any]:
    # ---- Built-in rule engine (works without any key) ----
    parsed = nlu.parse_query(req.message, hint_language=req.language if req.language != "en" else None)
    loc = ensure_location(parsed, req.latitude, req.longitude, None)
    # Follow-up context: if no place is mentioned, keep the SELECTED location
    # (never silently fall back to Delhi).
    if not parsed.entities.get("city") and (req.context or {}).get("current_location"):
        ctx_loc = tools.resolve_location(req.context["current_location"], req.latitude, req.longitude)
        if ctx_loc:
            loc = ctx_loc

    answer = response.build_answer(parsed, loc)
    reply = answer.get("reply", "")
    polished = llm.polish(reply, answer.get("payload", {}), parsed.language)
    if polished:
        reply = polished

    target = req.language if req.language in settings.languages else parsed.language
    if target != "en":
        reply = translate(reply, target, "auto")

    latency_ms = (time.perf_counter() - t0) * 1000.0
    store.save_chat(req.message, reply, target, parsed.intent, latency_ms,
                    source_type="voice" if req.voice else "chat")
    return {
        "reply": reply,
        "intent": parsed.intent,
        "confidence": parsed.confidence,
        "language": target,
        "detected_language": parsed.language,
        "location": loc,
        "payload": answer.get("payload", {}),
        "latency_ms": round(latency_ms, 1),
        "voice": req.voice,
        "engine": "rule-engine",
        "gemini_status": "not-configured",
        "action": _rule_action(parsed.intent, answer.get("payload", {})),
        "context": _rule_context(parsed, loc),
    }


def _agent_pipeline(req: ChatRequest, backend: str | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    ctx = dict(req.context or {})
    if not ctx.get("current_location") and req.latitude and req.longitude:
        ctx["current_location"] = f"{req.latitude:.2f},{req.longitude:.2f}"
    result = agent.chat(
        message=req.message,
        history=req.history,
        context=ctx,
        language=req.language,
        backend=backend or agent.backend_name(),
    )
    reply = result.get("reply", "I've updated the dashboard.")
    action = result.get("action")
    payload = result.get("payload", {})
    intent = _intent_from_turns(result.get("turns", []))
    if not intent:
        intent = (action or {}).get("view", "weather-intelligence")
    loc = payload.get("location") if isinstance(payload, dict) else None
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    store.save_chat(req.message, reply, req.language, intent, latency_ms,
                    source_type="voice" if req.voice else "chat")
    need_fallback = bool(result.get("degrades_to_rule_engine") or result.get("ready") is False)
    return {
        "reply": reply,
        "intent": intent,
        "confidence": 1.0,
        "language": req.language,
        "detected_language": req.language,
        "location": loc or (payload.get("place") and {"name": payload.get("place")}) or None,
        "payload": payload,
        "latency_ms": latency_ms,
        "voice": req.voice,
        "engine": "gemini-tools" if not need_fallback else "rule-engine",
        "gemini_status": "configured" if (result.get("ready") and not need_fallback) else "error",
        "model": result.get("model"),
        "turns": result.get("turns", []),
        "sources": result.get("sources", []),
        "action": action,
        "context": result.get("context", ctx),
        "need_rule_fallback": need_fallback,
        "agent_backend": agent.backend_name(),
    }


def _intent_from_turns(turns: list[dict]) -> str:
    """Map the executed tool set to a stable intent label."""
    if not turns:
        return "weather-intelligence"
    tools_used = [t["tool"] for t in turns]
    last = tools_used[-1]
    return {
        "get_current_weather": "current_weather",
        "get_daily_forecast": "daily_forecast",
        "get_hourly_forecast": "hourly_forecast",
        "get_weather_alerts": "alerts",
        "get_radar_data": "radar",
        "get_temperature_map": "temperature_map",
        "get_historical_weather": "climate",
        "calculate_weather_risk": "risk",
        "generate_sector_advisory": "advisory",
        "get_location_coordinates": "location",
    }.get(last, last)


def _rule_action(intent: str, payload: dict) -> dict | None:
    view_map = {
        "current_weather": ("current", payload.get("current")),
        "forecast": ("daily_forecast", payload.get("forecast")),
        "climate": ("climate", payload.get("climate")),
        "alerts": ("alerts", payload.get("alerts")),
        "marine": ("advisory", payload.get("marine")),
        "aviation": ("advisory", payload.get("aviation")),
        "agriculture": ("advisory", payload.get("agriculture")),
        "urban": ("advisory", payload.get("urban")),
        "air_quality": ("air", payload.get("air")),
        "sun": ("sun", payload.get("sun")),
        "rain": ("rain", payload.get("rain")),
    }
    view, data = view_map.get(intent, (None, None))
    if view is None:
        return None
    return {"type": "UPDATE_DASHBOARD", "view": view, "payload": {intent: data}}


def _rule_context(parsed, loc) -> dict:
    return {
        "current_location": (loc or {}).get("name", ""), "current_page": "weather",
        "selected_date": parsed.entities.get("date", "today"),
        "active_layer": "temperature", "active_view": parsed.intent,
    }


# --------------------------------------------------------------------------
# REST endpoints
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name, "version": settings.version,
        "providers": ["Open-Meteo (forecast/archive/marine/air/flood/geocoding)",
                      "RainViewer radar (free)",
                      "IMD (AWS observations + optional free IMD_API_KEY)",
                      "Nominatim + Open-Meteo geocoding",
                      "MapLibre + OSM-based tiles"],
        "nwp_models": "GFS, ECMWF, ICON, GEM, UKMO, MeteoFrance",
        "llm": gemini_client.status_info(),
        "agent": agent.status(),
        "gemini_key_exposed": "no (backend-only, env var)",
        "mock_mode": settings.use_mock_data,
        "languages": len(settings.languages),
        "watch_cities": len(settings.watch_city_list),
    }


@app.get("/api/languages")
def languages():
    return {"languages": settings.languages,
            "voice_locales": {code: code for code in settings.languages}}


@app.get("/api/geocode")
def geocode(q: str = Query(..., min_length=1)):
    return {"results": meteo.geocode(q)}


@app.get("/api/weather/now")
def weather_now(q: str | None = None, lat: float | None = None, lon: float | None = None,
                units: str = "metric"):
    loc = resolve_location(q, lat, lon) or dict(DEFAULT_LOCATION)
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=1, units=units)
    cur = data.get("current") or {}
    if not cur.get("temperature_2m"):
        # real-data fallback: live IMD AWS observation (no key) - never a blank card
        obs = imd.live_observation(loc["latitude"], loc["longitude"])
        if obs.get("ok"):
            cur = {"time": obs.get("observed_at"), "temperature_2m": obs.get("temperature_c"),
                   "apparent_temperature": obs.get("temperature_c"),
                   "relative_humidity_2m": obs.get("relative_humidity"),
                   "wind_speed_10m": obs.get("wind_speed"), "wind_direction_10m": obs.get("wind_direction"),
                   "wind_gusts_10m": obs.get("wind_speed"), "cloud_cover": None, "surface_pressure": None,
                   "precipitation": obs.get("rain"), "is_day": 1, "_imd": True}
    return {"location": loc, "current": cur, "units": units,
            "source": "IMD AWS (live)" if cur.get("_imd") else "open-meteo (live)",
            "timezone": data.get("timezone"), "utc_offset_seconds": data.get("utc_offset_seconds"),
            "weather_code_text": meteo.weather_code_name(cur.get("weather_code"))}


@app.get("/api/weather/forecast")
def weather_forecast(q: str | None = None, lat: float | None = None, lon: float | None = None,
                     days: int = Query(7, ge=1, le=16), model: str | None = None, units: str = "metric"):
    loc = resolve_location(q, lat, lon) or dict(DEFAULT_LOCATION)
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=days,
                          model=meteo.parse_model(model), units=units)
    return {"location": loc, "daily": data.get("daily", {}), "hourly": data.get("hourly", {}),
            "timezone": data.get("timezone"), "utc_offset_seconds": data.get("utc_offset_seconds"),
            "model": model or "default", "units": units}


@app.get("/api/weather/history")
def weather_history(q: str | None = None, lat: float | None = None, lon: float | None = None,
                    start: str | None = None, end: str | None = None, units: str = "metric"):
    from datetime import date, timedelta
    loc = resolve_location(q, lat, lon) or dict(DEFAULT_LOCATION)
    end = end or date.today().isoformat()
    start = start or (date.today() - timedelta(days=365)).isoformat()
    data = meteo.archive(loc["latitude"], loc["longitude"], start, end, units)
    return {"location": loc, "start": start, "end": end, "daily": data.get("daily", {})}


@app.get("/api/climate/trends")
def climate_trends(q: str | None = None, lat: float | None = None, lon: float | None = None,
                   years: int = Query(5, ge=2, le=30), units: str = "metric"):
    loc = resolve_location(q, lat, lon) or dict(DEFAULT_LOCATION)
    trend = meteo.monthly_trend(loc["latitude"], loc["longitude"], years=years, units=units)
    return {"location": loc, **trend}


@app.get("/api/alerts")
def get_alerts(q: str | None = None, lat: float | None = None, lon: float | None = None):
    local = []
    loc = resolve_location(q, lat, lon)
    if loc:
        local = meteo.all_warnings(loc["latitude"], loc["longitude"])
    return {
        "location": loc,
        "active": local,
        "recent_feed": store.recent_alerts(15),
        "watch_city_count": len(settings.watch_city_list),
    }


@app.get("/api/marine")
def marine(q: str | None = None, lat: float | None = None, lon: float | None = None):
    loc = resolve_location(q, lat, lon) or dict(DEFAULT_LOCATION)
    return {"location": loc, "marine": meteo.marine(loc["latitude"], loc["longitude"])}


@app.get("/api/air")
def air(q: str | None = None, lat: float | None = None, lon: float | None = None):
    loc = resolve_location(q, lat, lon) or dict(DEFAULT_LOCATION)
    return {"location": loc, "air": meteo.air_quality(loc["latitude"], loc["longitude"])}


@app.get("/api/status")
def status():
    return {
        "storage": settings.database_url.split("://")[0],
        "alert_watcher": {"poll_seconds": settings.alert_poll_seconds,
                          "cities": len(settings.watch_city_list)},
        "llm_backend": settings.llm_backend,
        "gemini": gemini_client.status_info(),
        "agent": agent.status(),
        "imd": imd.status(),
        "mock_mode": settings.use_mock_data,
        "radar_source": "RainViewer (free)",
        "connected_clients": len(hub.connections),
        "db": store.stats(),
        "cache": {k: len(meteo._cache) for k in [""]},
    }


@app.get("/api/radar")
def radar_frames():
    """Real RainViewer radar frame catalog + tile URL template."""
    return radar.frames_summary()


@app.get("/api/imd")
def imd_info(location: str | None = None, lat: float | None = None, lon: float | None = None):
    """IMD observations + warnings (real when reachable; mock clearly labelled)."""
    loc = tools.resolve_location(location, lat, lon) or {"name": "Delhi", "latitude": 28.6139, "longitude": 77.2090}
    obs = imd.live_observation(loc["latitude"], loc["longitude"])
    warns = imd.imd_warnings(loc["latitude"], loc["longitude"], loc["name"])
    return {"location": loc, "observation": obs, "warnings": warns}


@app.get("/api/temperature-map")
def temperature_map(location: str | None = None, lat: float | None = None, lon: float | None = None,
                    hour_offset: int | None = None, size: int = 14,
                    south: float | None = None, west: float | None = None,
                    north: float | None = None, east: float | None = None):
    """Real Open-Meteo grid. With a bbox (south/west/north/east) the grid covers
    exactly the visible map viewport; without it, a default area around the
    resolved location is used. hour_offset animates real NWP frames."""
    if None not in (south, west, north, east):
        grid = meteo.make_grid_bbox(south, west, north, east, size=size, hour_offset=hour_offset)
        src = (grid[0].get("_source") if grid else "") or "open-meteo-live-grid"
        return {"location": {"name": location or "map view", "latitude": (south + north) / 2,
                             "longitude": (west + east) / 2},
                "source": src,
                "hour_offset": hour_offset, "bounds": {"south": south, "west": west, "north": north, "east": east},
                "points": grid}
    loc = tools.resolve_location(location, lat, lon) or {"name": "Delhi", "latitude": 28.6139, "longitude": 77.2090}
    grid = meteo.make_grid(loc["latitude"], loc["longitude"], hour_offset=hour_offset)
    src = (grid[0].get("_source") if grid else "") or "open-meteo-live-grid"
    return {"location": loc, "source": src, "hour_offset": hour_offset, "points": grid}


@app.get("/api/brief")
def brief(location: str | None = None, lat: float | None = None, lon: float | None = None):
    """'Today's Weather Brief' - DATA -> INTERPRETATION -> RISK -> ACTION."""
    return tools.tool_weather_brief(location=location, lat=lat, lon=lon)


@app.get("/api/advisory")
def advisory(location: str | None = None, sector: str = "agriculture",
             lat: float | None = None, lon: float | None = None):
    """Sector decision-support advisory generated from REAL NWP data (no AI needed)."""
    loc = tools.resolve_location(location, lat, lon) or {"name": "Delhi", "latitude": 28.6139, "longitude": 77.2090}
    return tools.tool_sector_advisory(sector=sector, location=loc["name"])


@app.get("/api/cyclones")
def cyclones(location: str | None = None, south: float | None = None, west: float | None = None,
             north: float | None = None, east: float | None = None):
    """Cyclone / severe-storm watch (real NWP-derived + optional IMD track)."""
    return tools.tool_cyclone_watch(location=location, south=south, west=west, north=north, east=east)


@app.get("/api/risk")
def risk(location: str | None = None, lat: float | None = None, lon: float | None = None):
    """Risk indicators computed directly from real NWP data (no Gemini call)."""
    loc = tools.resolve_location(location, lat, lon) or {"name": "Delhi", "latitude": 28.6139, "longitude": 77.2090}
    return tools.tool_weather_risk(location=loc["name"])


@app.get("/api/tools")
def tool_catalog():
    return {"tools": list(tools.TOOLS.keys()), "count": len(tools.TOOLS)}


# --------------------------------------------------------------------------
# Chat REST
# --------------------------------------------------------------------------
@app.post("/api/chat")
def chat(req: ChatRequest):
    try:
        return chat_pipeline(req)
    except Exception as exc:
        log.exception("chat pipeline failed")
        return JSONResponse(status_code=200, content={
            "reply": "Sorry, I hit an internal error. Please retry. ({}: {})".format(type(exc).__name__, exc),
            "intent": "error", "confidence": 0.0, "language": req.language,
            "location": DEFAULT_LOCATION, "payload": {}, "latency_ms": 0.0,
        })


# --------------------------------------------------------------------------
# WebSockets
# --------------------------------------------------------------------------
@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive / ping
    except WebSocketDisconnect:
        hub.disconnect(ws)


@app.websocket("/ws")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                import json
                req = ChatRequest(**json.loads(raw))
            except Exception:
                req = ChatRequest(message=raw)
            result = await asyncio.get_running_loop().run_in_executor(None, chat_pipeline, req)
            await ws.send_json(result)
    except WebSocketDisconnect:
        pass
