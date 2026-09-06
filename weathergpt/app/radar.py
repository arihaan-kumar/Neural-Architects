"""RainViewer radar client (free public API - real/recent radar frames only).

API: https://api.rainviewer.com/public/weather-maps.json
Returns frame metadata (timestamps, tile paths) + tile URL builder. The
frontend animates REAL frames; nothing here is synthetic.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from .config import settings
from . import mockdata

log = logging.getLogger("weathergpt.radar")

_session = requests.Session()
_session.headers.update({"User-Agent": "WeatherGPT/1.0"})

COLOR_SCHEMES = ["2", "4", "5"]  # 2=original, 4=universal-blue, 5=universal


def radar_catalog() -> dict[str, Any]:
    """Radar frame catalog: past + nowcast frames. Falls back to clearly
    labelled mock (single frame) only when the API is unreachable."""
    if settings.use_mock_data:
        return mockdata.mock_radar()
    try:
        resp = _session.get(settings.radar_url, timeout=settings.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        return {
            "source": "rainviewer", "version": data.get("version"),
            "host": data.get("host"),
            "radar": data.get("radar") or {},
            "nowcast": data.get("nowcast") or [],
            "satellite": data.get("satellite") or [],
            "tile_url": tile_url(data.get("host"), "{path}", "{z}", "{x}", "{y}"),
        }
    except Exception as exc:
        log.warning("RainViewer unavailable (%s); returning labelled mock radar", exc)
        data = mockdata.mock_radar()
        data["note"] = "RainViewer unavailable - showing labelled MOCK frames"
        return data


def tile_url(host: str, path: str, z: str, x: str, y: str, scheme: str = "2") -> str:
    host = host or "https://tilecache.rainviewer.com"
    return f"{host}{path}/256/{z}/{x}/{y}/{int(scheme)}/1_1.png"


def frames_summary() -> dict[str, Any]:
    cat = radar_catalog()
    past = (cat.get("radar") or {}).get("past") or []
    nowcast = cat.get("nowcast") or []
    frames = [
        {"time": _fmt_time(f.get("time")), "raw_time": f.get("time"), "path": f.get("path"), "type": "past"} for f in past
    ] + [
        {"time": _fmt_time(f.get("time")), "raw_time": f.get("time"), "path": f.get("path"), "type": "nowcast"} for f in nowcast
    ]
    frames.sort(key=lambda f: f.get("raw_time") or 0)
    # strip raw_time duplicates for the wire format
    for f in frames:
        f.pop("raw_time", None)
    return {
        "source": cat.get("source"), "host": cat.get("host"),
        "note": cat.get("note"),
        "frames": frames,
        "frame_count": len(frames),
        "latest_time": frames[-1]["time"] if frames else None,
    }


def _fmt_time(raw) -> str | None:
    """RainViewer timestamps are Unix seconds; render ISO-like text for the UI."""
    if raw is None:
        return None
    try:
        import datetime as dt
        return dt.datetime.fromtimestamp(int(raw), tz=dt.timezone.utc).astimezone().isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return str(raw)
