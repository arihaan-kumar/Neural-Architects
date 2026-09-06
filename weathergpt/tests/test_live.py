"""Live-verification smoke checks that hit the real free APIs (network needed)."""
import os

import pytest


@pytest.mark.live
def test_real_geocode():
    from app import meteo
    assert meteo.geocode("Pune")[0]["country"] == "India"


@pytest.mark.live
def test_real_forecast():
    from app import meteo
    data = meteo.forecast(28.61, 77.20, days=3)
    assert data.get("current")
    assert data.get("daily", {}).get("time")


@pytest.mark.live
def test_real_alerts():
    from app import meteo
    feed = meteo.alerts(13.08, 80.27)
    assert isinstance(feed, list)


@pytest.mark.live
def test_real_translate():
    from app.translate import translate
    out = translate("What is the weather in Delhi?", "hi")
    assert out and out != "What is the weather in Delhi?"


@pytest.mark.live
def test_real_gemini_tool_calling():
    """Only when a real GEMINI_API_KEY is provided (env/.env)."""
    key = os.environ.get("_GEMINI_REAL_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        pytest.skip("no GEMINI_API_KEY set")
    from app import gemini_client
    from app.config import settings
    settings.gemini_key = key  # restore for this test only
    assert gemini_client.gemini_ready()
    auth = gemini_client.validate_auth(force=True)
    if not auth.get("valid"):
        pytest.skip(f"Google rejects key ({auth.get('reason')}) - graceful fallback path covers this")
    res = gemini_client.chat("What is the weather?", context={"current_location": "Mumbai"})
    if not res.get("turns"):
        pytest.skip(f"quota busy / model busy during test: {res.get('reply', '')[:80]}")
    assert res["ready"]
    assert res["reply"], "Gemini returned no text"
    tools_used = [t["tool"] for t in res["turns"]]
    assert "get_current_weather" in tools_used
    assert res["sources"], "no data source labelled"


@pytest.mark.live
def test_real_radar_frames():
    from app import radar
    cat = radar.radar_catalog()
    assert cat.get("source") == "rainviewer", f"radar unavailable: {cat.get('note')}"
    frames = (cat.get("radar") or {}).get("past") or []
    assert frames, "RainViewer returned no past frames"
