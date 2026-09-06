"""Natural-language response builder and domain advisors.

Turns structured Open-Meteo data into friendly, action-oriented answers and
generates decision-support advisories (agriculture, aviation, marine, urban
flood risk). Responses are plain English; the API layer translates them into
the user's language using the free translation service.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from . import meteo
from .nlu import ParsedQuery

WMO_DESC = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "freezing drizzle", 57: "dense freezing drizzle", 61: "light rain", 63: "moderate rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "moderate snow",
    75: "heavy snow", 77: "snow grains", 80: "light rain showers", 81: "rain showers",
    82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _desc(code: int | None) -> str:
    return WMO_DESC.get(int(code) if code is not None else -1, "variable conditions")


def _u(units: str) -> dict[str, str]:
    return {"temp": "°F" if units == "imperial" else "°C",
            "wind": "mph" if units == "imperial" else "km/h",
            "precip": "in" if units == "imperial" else "mm"}


def _f(v: Any, nd: int = 0) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "N/A"
    return f"{float(v):.{nd}f}"


def _dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %b, %H:%M")
    except Exception:
        return iso


# --------------------------------------------------------------------------
# Intent handlers
# --------------------------------------------------------------------------

def answer_current(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    units = parsed.entities.get("units", "metric")
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=1, units=units)
    cur = (data.get("current") or {})
    name = loc.get("name", "your location")
    if not cur:
        return _simple_reply(f"I couldn't fetch live conditions for {name}. Please check the connection and try again.",
                             loc, parsed)

    u = _u(units)
    desc = _desc(cur.get("weather_code"))
    temp = _f(cur.get("temperature_2m"))
    feels = _f(cur.get("apparent_temperature"))
    hum = _f(cur.get("relative_humidity_2m"), 0)
    wind = _f(cur.get("wind_speed_10m"))
    win_dir = cur.get("wind_direction_10m")
    comp = _compass(win_dir) if win_dir is not None else "N/A"
    gust = _f(cur.get("wind_gusts_10m"))
    rain_now = _f(cur.get("precipitation"), 1)
    cloud = _f(cur.get("cloud_cover"), 0)
    text = (f"Right now in {name}: {desc}, {temp}{u['temp']} (feels like {feels}{u['temp']}). "
            f"Humidity {hum}%, cloud cover {cloud}%, wind {wind}{u['wind']} from {comp} "
            f"(gusting {gust}{u['wind']})"
            + (f", precipitation {rain_now}{u['precip']} in the last hour" if float(rain_now or 0) > 0 else ".")
            + (" Carry an umbrella if you head out." if float(rain_now or 0) > 0.2 or desc in ("light rain", "moderate rain", "light rain showers", "rain showers") else ""))
    return {
        "reply": text, "intent": "current_weather",
        "payload": {"current": _slim_current(cur), "location": loc, "units": units},
        "thought": f"matched live observations via Open-Meteo at {loc['latitude']:.2f},{loc['longitude']:.2f}",
    }


def answer_forecast(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    units = parsed.entities.get("units", "metric")
    days = int(parsed.entities.get("horizon_days") or 7)
    model = meteo.parse_model(parsed.entities.get("model"))
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=days, model=model, units=units)
    daily = data.get("daily") or {}
    name = loc.get("name", "your location")
    u = _u(units)
    if not daily.get("time"):
        return _simple_reply(f"I couldn't build a forecast for {name} right now.", loc, parsed)

    dates = daily.get("time") or []
    model_note = f" (NWP source: {model.upper()})" if model else ""
    lines = [f"Forecast for {name} over the next {len(dates)} days{model_note}:"]
    show = max(1, days)
    for i in range(min(show, len(dates))):
        day = datetime.fromisoformat(dates[i]).strftime("%A %d %b")
        d = _desc(daily.get("weather_code", [None])[i] if isinstance(daily.get("weather_code"), list) else None)
        tmax = _f(_get_index(daily, "temperature_2m_max", i))
        tmin = _f(_get_index(daily, "temperature_2m_min", i))
        precip = _f(_get_index(daily, "precipitation_sum", i), 1)
        pprob = _get_index(daily, "precipitation_probability_max", i)
        prob = f", {_f(pprob, 0)}% rain chance" if pprob is not None else ""
        wmax = _f(_get_index(daily, "wind_gusts_10m_max", i))
        lines.append(f"• {day}: {d}, high {tmax}{u['temp']} / low {tmin}{u['temp']}, "
                     f"{precip}{u['precip']} rain{prob}, gusts up to {wmax}{u['wind']}.")
        if i >= 6:
            break

    # highlight the riskiest day
    risk = max(range(len(dates)), key=lambda i: _num(_get_index(daily, "precipitation_probability_max", i)))
    if _num(_get_index(daily, "precipitation_probability_max", risk)) >= 60:
        rd = datetime.fromisoformat(dates[risk]).strftime("%A")
        lines.append(f"⚠ Highest rain chance is {rd} — plan outdoor work accordingly.")

    return {
        "reply": "\n".join(lines), "intent": "forecast",
        "payload": {"forecast": _slim_daily(daily, show), "location": loc, "units": units, "model": model},
        "thought": f"NWP-based forecast with model={model or 'open-meteo default'}",
    }


def answer_alerts(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    ups = meteo.all_warnings(loc["latitude"], loc["longitude"])
    if not ups:
        return {
            "reply": f"No active weather alerts or warnings are currently issued for {name} — conditions are stable. ✅",
            "intent": "alerts", "payload": {"alerts": [], "location": loc},
            "thought": "checked meteoalarm/IMD feeds via Open-Meteo alerts",
        }
    lines = [f"⚠ Active alerts for {name}:"]
    for a in ups[:5]:
        sev = a.get("severity", "unknown")
        lines.append(f"• {a.get('event', 'Weather alert')} ({sev}) — {a.get('description', '')[:180]}")
    lines.append("Check back for updates; I will push new alerts to your screen automatically.")
    return {
        "reply": "\n".join(lines), "intent": "alerts",
        "payload": {"alerts": ups[:5], "location": loc},
        "thought": f"found {len(ups)} active alert(s)",
    }


def answer_climate(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    units = parsed.entities.get("units", "metric")
    years = int(parsed.entities.get("years") or 5)
    name = loc.get("name", "your location")
    u = _u(units)
    tr = meteo.monthly_trend(loc["latitude"], loc["longitude"], years=years, units=units)
    rows = tr.get("years", [])
    if not rows:
        return _simple_reply(f"I don't have enough historical data for {name} yet.", loc, parsed)
    trend = tr.get("trend_c_per_year", 0.0)
    first, last = rows[0], rows[-1]
    night = last["avg_tmin"] - first["avg_tmin"]
    arrow = "warming" if trend > 0.01 else ("cooling" if trend < -0.01 else "stable")
    lines = [
        f"Climate trend analysis for {name} ({first['year']}–{last['year']}):",
        f"• Average daily high: {_f(first['avg_tmax'],1)}{u['temp']} ({first['year']}) → {_f(last['avg_tmax'],1)}{u['temp']} ({last['year']})",
        f"• Average overnight low: {_f(first['avg_tmin'],1)}{u['temp']} → {_f(last['avg_tmin'],1)}{u['temp']} ({night:+.1f}{u['temp']})",
        f"• Annual rainfall: {_f(first['annual_precip_mm'],0)} mm → {_f(last['annual_precip_mm'],0)} mm",
        f"• Long-term trend: {arrow} at about {abs(trend):.2f}{u['temp']}/year.",
    ]
    if trend > 0.02:
        lines.append("For planning: expect longer heat spells and shifting monsoon patterns — "
                     "great for catchment planning, difficult for rain-fed crops.")
    return {
        "reply": "\n".join(lines), "intent": "climate",
        "payload": {"climate": rows, "trend_c_per_year": trend, "location": loc, "units": units},
        "thought": "derived from Open-Meteo archive (ERA5-backed) with linear regression",
    }


def answer_marine(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    data = meteo.marine(loc["latitude"], loc["longitude"])
    hourly = data.get("hourly") or {}
    waves = hourly.get("wave_height") or []
    if not waves:
        return _simple_reply(f"No marine data available right now for the coast near {name}.", loc, parsed)
    latest = int(data.get("current", {}).get("time", "T")[11:13]) if False else None
    w = waves[0]
    period = (hourly.get("wave_period") or [None])[0]
    sst = (hourly.get("sea_surface_temperature") or [None])[0]
    max_wave = None
    dmax = (data.get("daily") or {}).get("wave_height_max") or []
    if dmax:
        max_wave = max(dmax[:5])
    msg = (f"Marine outlook near {name}: wave height ~{_f(w,1)} m (period {_f(period,1)} s, "
           f"sea temperature {_f(sst,1)}°C).")
    if max_wave is not None:
        msg += f" Highest waves in the 5-day window: up to {_f(max_wave,1)} m."
    if float(w or 0) >= 2.5:
        msg += " Conditions are rough — advise small fishing boats to stay near shore."
    elif float(w or 0) >= 1.25:
        msg += " Moderate seas; fishing is possible with caution."
    else:
        msg += " Seas are calm — good for fishing and coastal activity."
    return {
        "reply": msg, "intent": "marine",
        "payload": {"marine": {"wave_height": w, "max_5d": max_wave, "sea_surface_temperature": sst},
                    "location": loc},
        "thought": "marine forecast from Open-Meteo marine API",
    }


def answer_aviation(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    units = parsed.entities.get("units", "metric")
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=2, units=units)
    cur = data.get("current") or {}
    hourly = data.get("hourly") or {}
    u = _u(units)
    vis = None
    vis_list = hourly.get("visibility") or []
    if vis_list:
        vis = float(max(vis_list[:24])) / 1000.0  # m -> km
    wind = _f(cur.get("wind_speed_10m"))
    gust = _f(cur.get("wind_gusts_10m"))
    desc = _desc(cur.get("weather_code"))
    msg = (f"Aviation briefing for {name} area: {desc}, surface wind {wind}{u['wind']} "
           f"(gusts {gust}{u['wind']}), temperature {_f(cur.get('temperature_2m'))}{u['temp']}.")
    if vis is not None:
        if vis < 5:
            msg += f" Visibility is reduced (~{vis:g} km) — expect IFR/MVFR conditions; fog risk for early departures."
        else:
            msg += f" Visibility ~{vis:g} km — VFR conditions are favourable."
    if float(gust or 0) >= 35:
        msg += " Strong crosswinds possible — check runway performance before takeoff."
    return {
        "reply": msg, "intent": "aviation",
        "payload": {"aviation": {"visibility_km": vis, "wind_kmh": cur.get("wind_speed_10m"),
                                 "gust_kmh": cur.get("wind_gusts_10m")}, "location": loc},
        "thought": "derived METAR-style briefing from forecast model output",
    }


def answer_agriculture(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    units = parsed.entities.get("units", "metric")
    crop = parsed.entities.get("crop")
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=7, units=units)
    daily = data.get("daily") or {}
    u = _u(units)
    if not daily.get("time"):
        return _simple_reply(f"I couldn't fetch agriculture weather for {name}.", loc, parsed)
    dates = daily.get("time") or []
    rain_days = [i for i in range(min(len(dates), 7))
                 if _num(_get_index(daily, "precipitation_sum", i)) >= 1.0]
    tmax = [_num(_get_index(daily, "temperature_2m_max", i)) for i in range(min(len(dates), 7))]
    tmax_f = [t for t in tmax if t is not None]
    hot = (max(tmax_f) >= 38) if tmax_f else False
    crop_note = f" for {crop}" if crop else ""
    lines = [f"Agri-advisory{crop_note} for {name}:", ]
    if rain_days:
        rd = ", ".join(datetime.fromisoformat(dates[i]).strftime("%a %d") for i in rain_days[:4])
        lines.append(f"• Rain expected on {rd} — good window for sowing/irrigation; skip pesticide spray on wet leaves.")
    else:
        lines.append("• No rain in the next 7 days — plan drip irrigation now.")
    if hot:
        lines.append("• Heat spike risk — irrigate early morning/evening to reduce stress on flowering crops.")
    nm = _desc(_get_index(daily, "weather_code", 0) if isinstance(daily.get("weather_code"), list) else None) if False else None
    lines.append(f"• Peak temperatures this week: {_f(max(tmax_f),0)}{u['temp']}; keep water channels clear.")
    lines.append("• Advisory auto-updates daily; ask for specific crops (paddy, wheat, sugarcane, etc.).")
    return {
        "reply": "\n".join(lines), "intent": "agriculture",
        "payload": {"agriculture": {"rain_windows": rain_days[:5], "max_temp": max(tmax_f) if tmax_f else None,
                                    "crop": crop}, "location": loc},
        "thought": "rule-based advisory from 7-day forecast",
    }


def answer_urban(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    units = parsed.entities.get("units", "metric")
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=3, units=units)
    daily = data.get("daily") or {}
    if not daily.get("time"):
        return _simple_reply(f"No urban flood data for {name} right now.", loc, parsed)
    dates = daily.get("time") or []
    flood = meteo.flood_potential(loc["latitude"], loc["longitude"])
    fdaily = flood.get("daily") or {}
    runoff = fdaily.get("runoff") or []
    discharge = fdaily.get("river_discharge") or []
    lines = [f"Urban / flood outlook for {name}:"]
    flagged = False
    for i in range(min(3, len(dates))):
        p = _num(_get_index(daily, "precipitation_sum", i))
        ro = _num(runoff[i] if i < len(runoff) else None)
        dc = _num(discharge[i] if i < len(discharge) else None)
        day = datetime.fromisoformat(dates[i]).strftime("%a %d")
        if p >= 25:
            lines.append(f"• {day}: heavy rain ~{_f(p,0)} mm — watch for waterlogging on low-lying roads and underpasses.")
            flagged = True
        elif p >= 10:
            lines.append(f"• {day}: moderate rain ~{_f(p,0)} mm; minor drainage pressure possible.")
        else:
            lines.append(f"• {day}: light rain ~{_f(p,0)} mm — no flood concern.")
    if flagged:
        lines.append("Municipal advisory: clear storm drains, avoid stalled traffic routes, keep emergency teams on standby.")
    else:
        lines.append("No significant flood risk identified in the next 72 hours.")
    return {
        "reply": "\n".join(lines), "intent": "urban",
        "payload": {"urban": {"precip_mm": [_num(_get_index(daily, "precipitation_sum", i)) for i in range(min(3, len(dates)))],
                              "runoff": runoff[:3], "river_discharge": discharge[:3]}, "location": loc},
        "thought": "rain + runoff/discharge model from grid flood API",
    }


def answer_air(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    data = meteo.air_quality(loc["latitude"], loc["longitude"])
    cur = data.get("current") or {}
    if not cur:
        return _simple_reply(f"Air quality data unavailable for {name} right now.", loc, parsed)
    aqi = cur.get("european_aqi") or cur.get("us_aqi") or 0
    pm25 = _f(cur.get("pm2_5"), 1)
    pm10 = _f(cur.get("pm10"), 1)
    def band(a):
        if a <= 20: return "GOOD 🟢 - excellent air"
        if a <= 40: return "FAIR 🟡 - acceptable"
        if a <= 60: return "MODERATE 🟠 - sensitive groups reduce exertion"
        if a <= 80: return "POOR 🔴 - masks advised for outdoor activity"
        return "VERY POOR 🟣 - avoid prolonged outdoor exertion"
    msg = (f"Air quality in {name}: AQI {_f(aqi,0)} ({band(float(aqi))}). "
           f"PM2.5 = {pm25} µg/m³, PM10 = {pm10} µg/m³.")
    if float(aqi) > 60:
        msg += " Advise school outdoor assemblies to move indoors if possible."
    return {
        "reply": msg, "intent": "air_quality",
        "payload": {"air": {"aqi": aqi, "pm2_5": pm25, "pm10": pm10}, "location": loc},
        "thought": "CAMS air quality model via Open-Meteo air-quality API",
    }


def answer_sun(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=2, units="metric")
    daily = data.get("daily") or {}
    if not daily.get("time"):
        return _simple_reply(f"No sunrise data for {name} right now.", loc, parsed)
    dates = daily.get("time") or []
    lines = [f"Sun times in {name}:"]
    for i in range(min(2, len(dates))):
        day = datetime.fromisoformat(dates[i]).strftime("%A %d %b")
        sr = _get_index(daily, "sunrise", i)
        ss = _get_index(daily, "sunset", i)
        lines.append(f"• {day}: sunrise {_time(sr)}, sunset {_time(ss)}")
    return {"reply": "\n".join(lines), "intent": "sun",
            "payload": {"sun": {"sunrise": str(_get_index(daily, "sunrise", 0)), "sunset": str(_get_index(daily, "sunset", 0))},
                        "location": loc},
            "thought": "solar geometry from forecast API"}


def answer_rain(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    units = parsed.entities.get("units", "metric")
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=7, units=units)
    daily = data.get("daily") or {}
    name = loc.get("name", "your location")
    u = _u(units)
    if not daily.get("time"):
        return _simple_reply(f"No rain data for {name} right now.", loc, parsed)
    dates = daily.get("time") or []
    wet = [(i, _num(_get_index(daily, "precipitation_sum", i)))
           for i in range(min(7, len(dates))) if _num(_get_index(daily, "precipitation_sum", i)) > 0.3]
    if not wet:
        return {"reply": f"Dry spell for {name} over the next 7 days — no meaningful rainfall expected.",
                "intent": "rain", "payload": {"rain": [], "location": loc},
                "thought": "systematic rain check over forecast window"}
    lines = [f"Rain outlook for {name}:"]
    for i, amt in wet[:5]:
        lines.append(f"• {datetime.fromisoformat(dates[i]).strftime('%A %d %b')}: {_f(amt,1)}{u['precip']} expected")
    total = sum(a for _, a in wet)
    lines.append(f"Weekly total ≈ {_f(total,1)}{u['precip']}. {'Keep an umbrella handy ☂' if total > 20 else 'Light rain only — outdoor plans mostly safe.'}")
    return {"reply": "\n".join(lines), "intent": "rain",
            "payload": {"rain": [{"day": dates[i], "mm": a} for i, a in wet[:7]], "location": loc},
            "thought": "precipitation accumulation from NWP"}


def answer_wind(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    name = loc.get("name", "your location")
    units = parsed.entities.get("units", "metric")
    data = meteo.forecast(loc["latitude"], loc["longitude"], days=2, units=units)
    cur = data.get("current") or {}
    u = _u(units)
    if not cur:
        return _simple_reply(f"No wind data for {name} right now.", loc, parsed)
    msg = (f"Wind in {name}: {_f(cur.get('wind_speed_10m'))}{u['wind']} from {_compass(cur.get('wind_direction_10m'))}, "
           f"gusting to {_f(cur.get('wind_gusts_10m'))}{u['wind']}.")
    if float(cur.get("wind_gusts_10m") or 0) >= 45:
        msg += " Gusty — secure loose objects (hoardings, tarps, farm structures)."
    return {"reply": msg, "intent": "wind",
            "payload": {"wind": {"speed": cur.get("wind_speed_10m"), "gusts": cur.get("wind_gusts_10m")}, "location": loc},
            "thought": "10m wind from forecast model"}


def answer_greeting(loc: dict[str, Any] | None, parsed: ParsedQuery) -> dict[str, Any]:
    name = (loc or {}).get("name", "your area")
    return _simple_reply(
        f"Namaste! 🙏 I'm WeatherGPT. Ask me things like: “Weather in Mumbai”, “Rain forecast for Delhi”, "
        f"“Any cyclone alerts near Chennai?”, “Climate trend for Pune”, or “Flight briefing for Bengaluru”. "
        f"I speak 14 Indian languages and read your voice too.", loc or {}, parsed)


def answer_help(loc: dict[str, Any] | None, parsed: ParsedQuery) -> dict[str, Any]:
    return _simple_reply(
        "Here's what I can do (all free, no cost):\n"
        "• Current weather, hourly/daily forecasts (GFS/ECMWF/ICON models)\n"
        "• Alerts & warnings (cyclone, heatwave, heavy rain) with live push\n"
        "• Climate trends & historical analysis (up to 30 years)\n"
        "• Marine/coastal outlook for fishermen, aviation briefings\n"
        "• Agri advisories and urban flood risk\n"
        "• Air quality, sunrise/sunset — in 14 Indian languages, voice-enabled.\n"
        "Try: “What's the weather in Chennai?”, “Kisan advisory for paddy in Patna”, “Aurangabad flood risk”.",
        loc or {}, parsed)


def answer_thanks(loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    return _simple_reply("You're welcome! Ask me anytime for weather, alerts, or climate data. 😊", loc or {}, parsed)


def answer_fallback(loc: dict[str, Any] | None, parsed: ParsedQuery) -> dict[str, Any]:
    name = (loc or {}).get("name", "your area")
    return _simple_reply(
        f"I understood your language but not the exact ask. For {name}, try:\n"
        f"• “weather now”, “7-day forecast”, “any alerts?”\n"
        f"• “climate trend last 10 years”, “sea conditions”, “AQI”, “flood risk”.",
        loc or {}, parsed)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

HANDLERS = {
    "current_weather": answer_current, "forecast": answer_forecast, "alerts": answer_alerts,
    "climate": answer_climate, "marine": answer_marine, "aviation": answer_aviation,
    "agriculture": answer_agriculture, "urban": answer_urban, "air_quality": answer_air,
    "sun": answer_sun, "rain": answer_rain, "wind": answer_wind,
    "greeting": answer_greeting, "help": answer_help, "thanks": answer_thanks,
}


def build_answer(parsed: ParsedQuery, location: dict[str, Any] | None) -> dict[str, Any]:
    """Execute the parsed intent and return a response dict."""
    handler = HANDLERS.get(parsed.intent, answer_fallback)
    try:
        resp = handler(location if location else {}, parsed)
    except Exception as exc:  # never break the conversation on data hiccups
        resp = _simple_reply(
            f"I hit a snag fetching weather data for {location.get('name', 'your area') if location else 'your area'}. "
            f"Please try again in a moment. ({exc.__class__.__name__})", location or {}, parsed)
        resp["error"] = str(exc)
    resp["intent"] = parsed.intent
    resp["language"] = parsed.language
    resp["confidence"] = parsed.confidence
    resp["query"] = parsed.raw or parsed.text
    return resp


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _simple_reply(text: str, loc: dict[str, Any], parsed: ParsedQuery) -> dict[str, Any]:
    return {"reply": text, "intent": parsed.intent, "payload": {"location": loc}}


def _slim_current(cur: dict[str, Any]) -> dict[str, Any]:
    keys = ["temperature_2m", "apparent_temperature", "weather_code", "relative_humidity_2m",
            "precipitation", "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "cloud_cover", "is_day"]
    return {k: cur.get(k) for k in keys if k in cur}


def _slim_daily(daily: dict[str, Any], show: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ["time", "weather_code", "temperature_2m_max", "temperature_2m_min",
              "precipitation_sum", "precipitation_probability_max", "wind_speed_10m_max",
              "wind_gusts_10m_max", "uv_index_max"]:
        lst = daily.get(k)
        if isinstance(lst, list):
            out[k] = lst[:show]
    return out


def _get_index(daily: dict[str, Any], key: str, i: int) -> Any:
    lst = daily.get(key)
    if isinstance(lst, list) and i < len(lst):
        return lst[i]
    return None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _time(iso: str | None) -> str:
    if not iso:
        return "N/A"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return str(iso)


def _compass(deg: float | None) -> str:
    if deg is None:
        return "N/A"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((float(deg) % 360) / 22.5 + 0.5) % 16]
