"""Official IMD (India Meteorological Department) adapter - real data first.

Sources (all free):
1. IMD AWS/GIS observations: `mausam.imd.gov.in/responsive/curWxMap/fetchWxMapGIS.php`
   (JSON, no key required; station index bundled from IMD's own site).
2. IMD official API (api.imd.gov.in, FREE registration ->
   https://api.imd.gov.in/public/register.php) for district warnings/nowcasts.
   Pass the key via the IMD_API_KEY env var (optional, backend-only secret).
3. If neither is reachable, clearly-labelled MOCK warnings are returned
   (`source='IMD (mock)'`), never presented as live/official.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from .config import settings

log = logging.getLogger("weathergpt.imd")

_session = requests.Session()
_session.headers.update({"User-Agent": "WeatherGPT/1.0 (education)"})

_STATIONS_PATH = Path(__file__).resolve().parent / "data" / "imd_stations.json"
_STATIONS: list[dict[str, Any]] = []
try:
    _STATIONS = json.loads(_STATIONS_PATH.read_text())
except Exception as exc:
    log.warning("imd station index not loaded: %s", exc)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    a = sin(radians(lat2 - lat1) / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * asin(sqrt(a)) * 6371.0


def nearest_station(lat: float, lon: float) -> dict[str, Any] | None:
    if not _STATIONS:
        return None
    best, best_d = None, 1e9
    for s in _STATIONS:
        d = _haversine(lat, lon, s["lat"], s["lon"])
        if d < best_d:
            best, best_d = s, d
    return {**best, "distance_km": round(best_d, 1)}


def live_observation(lat: float, lon: float) -> dict[str, Any]:
    """Real IMD AWS/synoptic station observation (JSON, no API key).
    Numeric ids (e.g. 42182) are synoptic stations (station=S); hex ids are
    AWS stations (station=A). We try the likely type first, then the other."""
    station = nearest_station(lat, lon)
    if not station:
        return {"source": "IMD AWS", "ok": False, "note": "no station index"}
    sid = station["id"]
    primary = "S" if str(sid).isdigit() else "A"
    fallback = "A" if primary == "S" else "S"
    for station_type in (primary, fallback):
        try:
            resp = _session.post(
                "https://mausam.imd.gov.in/responsive/curWxMap/fetchWxMapGIS.php",
                data={"station": station_type, "id": sid},
                timeout=settings.timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data") or []
            if rows:
                row = rows[0]
                return {
                    "source": "IMD AWS", "ok": True,
                    "station": row.get("STATION"), "district": row.get("DISTRICT"),
                    "state": row.get("STATE"), "observed_at": f"{row.get('DATE')} {row.get('TIME')}",
                    "temperature_c": _num(row.get("CURR_TEMP")),
                    "dew_point_c": _num(row.get("DEW_POINT_TEMP")),
                    "relative_humidity": _num(row.get("REL_HUMIDITY")),
                    "wind_speed": _num(row.get("WIND_SPEED")),
                    "wind_direction": _num(row.get("WIND_DIR")),
                    "rain": _num(row.get("RAINFALL")),
                    "distance_km": station["distance_km"],
                }
        except Exception as exc:
            log.debug("IMD AWS unavailable: %s", exc)
    return {"source": "IMD AWS", "ok": False, "note": "no station rows",
            "station": station["name"], "distance_km": station["distance_km"]}


def _num(v):
    if v in (None, "", "-", "NA"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def imd_warnings(lat: float, lon: float, place: str) -> list[dict[str, Any]]:
    """District warnings:
    - IMD official API when IMD_API_KEY is set (free registration).
    - Otherwise clearly-labelled MOCK (UI dev always works)."""
    station = nearest_station(lat, lon)
    district = (station or {}).get("name", place)

    if settings.imd_api_key:
        try:
            params = {"id": station["id"]} if station else {}
            params["key"] = settings.imd_api_key
            resp = _session.get("https://api.imd.gov.in/api/v1/districtwarning",
                                params=params, timeout=settings.timeout_s,
                                headers={"key": settings.imd_api_key})
            if resp.status_code == 200:
                data = resp.json()
                out = _parse_district_warning(data, district)
                if out:
                    return out
                return [{
                    "event": "No IMD warning", "severity": "green", "source": "IMD",
                    "description": f"No active IMD district warning for {district}.",
                    "mock": False,
                }]
            log.debug("IMD api status %s", resp.status_code)
        except Exception as exc:
            log.debug("IMD API unavailable: %s", exc)

    # Clearly-labelled mock (never presented as live/official)
    return [{
        "event": "IMD-sample warning", "severity": "yellow", "source": "IMD (mock)",
        "description": f"MOCK: sample IMD-style advisory for {district} - connect a free IMD_API_KEY "
                       f"(api.imd.gov.in/public/register.php) for live district warnings.",
        "mock": True,
    }]


def _parse_district_warning(data: Any, district: str) -> list[dict[str, Any]]:
    """Parse the api.imd.gov.in response defensively (multiple shapes)."""
    out: list[dict[str, Any]] = []
    recs = data
    if isinstance(data, dict):
        recs = data.get("data") or data.get("features") or data.get("warnings") or []
    if isinstance(recs, dict):
        recs = [recs]
    if not isinstance(recs, list):
        return out
    for r in recs:
        if isinstance(r, dict):
            event = str(r.get("title") or r.get("event") or r.get("warning") or "Weather advisory")
            sev = str(r.get("severity") or "orange").lower()
            desc = str(r.get("description") or r.get("message") or r.get("text") or event)
            out.append({"event": event, "severity": sev, "source": "IMD",
                        "description": desc[:300], "mock": False})
    return out


def status() -> dict[str, Any]:
    return {
        "imd_api_key_configured": bool(settings.imd_api_key),
        "stations_indexed": len(_STATIONS),
        "station_sample": (_STATIONS[0]["name"] if _STATIONS else None),
    }
