def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "GFS" in body["nwp_models"]
    assert body["gemini_key_exposed"].startswith("no")
    assert "gemini_configured" in body["llm"]


def test_radar_endpoint(client, monkeypatch):
    from app import radar as radar_mod
    monkeypatch.setattr(radar_mod, "radar_catalog", lambda: {
        "source": "rainviewer", "host": "https://tilecache.rainviewer.com",
        "radar": {"past": [{"time": "2026-09-04T12:00", "path": "/v2/radar/123"}]},
        "nowcast": [], "note": None,
    })
    r = client.get("/api/radar")
    assert r.status_code == 200
    body = r.json()
    assert body["frame_count"] == 1
    assert body["frames"][0]["path"] == "/v2/radar/123"


def test_imd_endpoint(client, monkeypatch):
    from app import imd as imd_mod
    monkeypatch.setattr(imd_mod, "live_observation", lambda lat, lon: {"source": "IMD AWS", "ok": True,
                                                                       "station": "LODI ROAD", "temperature_c": 26.9})
    monkeypatch.setattr(imd_mod, "imd_warnings", lambda lat, lon, place: [{
        "event": "IMD-sample warning", "severity": "yellow", "source": "IMD (mock)", "description": "MOCK advisory", "mock": True}])
    r = client.get("/api/imd?location=Delhi")
    assert r.status_code == 200
    body = r.json()
    assert body["observation"]["ok"] is True
    assert body["warnings"][0]["mock"] is True


def test_temperature_map_endpoint(client, monkeypatch):
    from app import meteo as meteo_mod
    def fake_grid(lat, lon, size=9, step=0.35, hour_offset=None):
        return [
            {"latitude": 28.6, "longitude": 77.2, "temperature_2m": 31.4, "wind_speed_10m": 12,
             "wind_direction_10m": 180, "cloud_cover": 40, "lightning_potential": 300, "intensity": 20},
        ]
    monkeypatch.setattr(meteo_mod, "make_grid", fake_grid)
    r = client.get("/api/temperature-map?location=Delhi")
    assert r.status_code == 200
    assert r.json()["points"][0]["temperature_2m"] == 31.4
    r2 = client.get("/api/temperature-map?location=Delhi&hour_offset=6")
    assert r2.status_code == 200 and r2.json()["hour_offset"] == 6


def test_temperature_map_bbox(client, monkeypatch):
    from app import meteo as meteo_mod
    calls = {}
    def fake_bbox(south, west, north, east, size=14, hour_offset=None, step_override=None):
        calls["bbox"] = (south, west, north, east)
        return [{"latitude": south, "longitude": west, "temperature_2m": 30.0, "intensity": 10}]
    monkeypatch.setattr(meteo_mod, "make_grid_bbox", fake_bbox)
    r = client.get("/api/temperature-map?south=27.0&west=75.0&north=29.0&east=78.0&hour_offset=72")
    assert r.status_code == 200
    assert calls.get("bbox") == (27.0, 75.0, 29.0, 78.0)
    assert r.json()["hour_offset"] == 72
    assert r.json()["bounds"]["south"] == 27.0


def test_tools_catalog(client):
    r = client.get("/api/tools")
    assert r.status_code == 200
    names = r.json()["tools"]
    assert {"get_current_weather", "get_daily_forecast", "get_weather_alerts",
            "get_radar_data", "get_temperature_map", "get_historical_weather",
            "calculate_weather_risk", "generate_sector_advisory",
            "get_location_coordinates", "get_hourly_forecast"} <= set(names)


def test_languages(client):
    r = client.get("/api/languages")
    assert r.status_code == 200
    langs = r.json()["languages"]
    assert {"en", "hi", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa"} <= set(langs)


def test_weather_now(client, mock_forecast, mock_geocode):
    r = client.get("/api/weather/now", params={"q": "Delhi"})
    assert r.status_code == 200
    assert r.json()["current"]["temperature_2m"] == 31.2


def test_weather_forecast(client, mock_forecast, mock_geocode):
    r = client.get("/api/weather/forecast", params={"q": "Delhi", "days": 5, "model": "gfs"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["daily"]["time"]) == 5
    assert body["model"] == "gfs"


def test_history(client, mock_archive, mock_geocode):
    r = client.get("/api/weather/history", params={"q": "Delhi", "start": "2023-01-01", "end": "2023-12-31"})
    assert r.status_code == 200
    assert r.json()["daily"]["time"]


def test_climate(client, mock_archive, mock_geocode):
    r = client.get("/api/climate/trends", params={"q": "Delhi", "years": 5})
    assert r.status_code == 200
    assert "years" in r.json()


def test_alerts(client, mock_forecast, mock_alerts, mock_geocode):
    r = client.get("/api/alerts", params={"q": "Delhi"})
    assert r.status_code == 200
    assert r.json()["active"]


def test_derive_warnings(mock_forecast):
    from app import meteo
    ws = meteo.derive_warnings(13.08, 80.27, days=5)
    assert any(w["event"] == "Thunderstorm / lightning" for w in ws)
    assert ws[0]["source"] == "derived-nwp"


def test_geocode(client, mock_geocode):
    r = client.get("/api/geocode", params={"q": "Delhi"})
    assert r.status_code == 200
    assert r.json()["results"][0]["name"] == "Delhi"


def test_chat_endpoint(client, mock_forecast, mock_geocode):
    r = client.post("/api/chat", json={"message": "weather in Delhi", "language": "en"})
    assert r.status_code == 200
    body = r.json()
    assert "reply" in body and body["reply"]
    assert body["intent"] == "current_weather"


def test_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["storage"] == "sqlite"


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "WeatherGPT" in r.text
