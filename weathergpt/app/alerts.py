"""Background alert ingestion: polls official alert feeds (MeteoAlarm, NWS,
ECMWF-backed with IMD-adjacent sources via Open-Meteo alerts) for a list of
watch cities, persists new events, and pushes them live to connected clients
over WebSocket. Runs fully autonomously - zero human intervention.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from . import meteo, store
from .config import settings

log = logging.getLogger("weathergpt.alerts")


def scan_cities() -> list[dict]:
    """Poll warning sources (official feed + derived NWP rules) for every watch
    city, persist new events. Blocking; run in executor."""
    new_alerts: list[dict] = []
    for city in settings.watch_city_list:
        locs = meteo.geocode(city)
        if not locs:
            continue
        loc = locs[0]
        lat, lon = loc["latitude"], loc["longitude"]
        feed = meteo.all_warnings(lat, lon)
        for a in feed:
            fp = hashlib.sha256(
                f"{a.get('event')}|{a.get('description', '')[:60]}|{lat:.2f}|{lon:.2f}".encode()
            ).hexdigest()[:24]
            evt = a.get("event", "Weather alert")
            sev = a.get("severity", "unknown")
            desc = a.get("description") or a.get("event", "")
            is_new = store.upsert_alert(
                fingerprint=fp, city=city, event=evt, severity=sev,
                description=desc, lat=lat, lon=lon,
            )
            if is_new:
                rec = {"city": city, "event": evt, "severity": sev, "description": desc[:300],
                       "lat": lat, "lon": lon, "source": a.get("source", "official")}
                new_alerts.append(rec)
                log.info("NEW ALERT: %s -> %s (%s)", city, evt, sev)
    return new_alerts


async def alert_loop(hub) -> None:
    """Autonomous polling loop. Broadcasts each new alert to all connected clients."""
    log.info("alert watcher started (poll every %ss, %d cities)",
             settings.alert_poll_seconds, len(settings.watch_city_list))
    while True:
        try:
            new_alerts = await asyncio.get_running_loop().run_in_executor(None, scan_cities)
            for a in new_alerts:
                await hub.broadcast({"type": "new_alert", "alert": a})
        except Exception as exc:
            log.warning("alert scan failed: %s", exc)
        await asyncio.sleep(settings.alert_poll_seconds)
