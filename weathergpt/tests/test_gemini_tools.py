"""Gemini function-calling pipeline tests (Gemini call is mocked; tools are real/logic-free)."""
import json

from app import gemini_client, tools
from app.main import chat_pipeline, ChatRequest


class FakeGemini:
    def __init__(self, events, text):
        self.events, self.text = events, text
        self.calls = []

    def chat(self, message, history=None, context=None, language="en", backend=None):
        self.calls.append({"message": message, "context": context, "history": history})
        action = tools.dashboard_action(self.events[0], context or {}, message) if self.events else None
        return {
            "ready": True, "reply": self.text, "turns": self.events,
            "action": action,
            "payload": (action or {}).get("payload", {}),
            "model": "gemini-mock", "latency_ms": 12.3,
            "sources": ["open-meteo (live)"],
            "context": context, "degrades_to_rule_engine": False,
        }


def _fake_gemini_response(events, text):
    return FakeGemini(events, text)


def test_gemini_ready_without_key():
    from app.config import settings
    # Force no key for deterministic test
    orig = settings.gemini_key
    settings.gemini_key = ""
    try:
        assert gemini_client.gemini_ready() is False
        res = gemini_client.chat("hello")
        assert res["ready"] is False
        assert "Gemini API key is not configured" in res["reply"]
    finally:
        settings.gemini_key = orig


def test_gemini_pipeline_uses_tools(monkeypatch, mock_forecast):
    import app.main as main_mod
    from app import tools as tools_mod
    from app.config import settings
    monkeypatch.setattr(tools_mod, "resolve_location", lambda location=None, lat=None, lon=None:
                        {"name": "Delhi", "latitude": 28.61, "longitude": 77.2, "country": "India", "source": "test"})
    fake = _fake_gemini_response([
        {"round": 0, "tool": "get_current_weather", "args": {"location": "Delhi"},
         "result": {"place": "Delhi", "temperature_c": 31.2, "humidity_pct": 62,
                    "wind_speed_kmh": 12, "source": "open-meteo (live)",
                    "location": {"name": "Delhi", "latitude": 28.61, "longitude": 77.2}},
         "ms": 40},
    ], "Delhi is at 31.2°C right now, humid and breezy.")
    monkeypatch.setattr(main_mod.agent, "chat", fake.chat)
    orig = settings.gemini_key
    settings.gemini_key = "fake-key-for-test"
    try:
        res = chat_pipeline(ChatRequest(message="What's the weather in Delhi?"))
    finally:
        settings.gemini_key = orig
    assert res["engine"] == "gemini-tools"
    assert res["reply"].startswith("Delhi is at")
    assert res["action"]["type"] == "UPDATE_DASHBOARD"
    assert res["payload"]["current"]["temperature_c"] == 31.2


def test_dashboard_action_mapping():
    a = tools.dashboard_action({"tool": "get_daily_forecast", "result": {"place": "X"}}, {}, "forecast")
    assert a["view"] == "daily_forecast"
    a2 = tools.dashboard_action({"tool": "get_temperature_map", "result": {"points": []}}, {}, "map")
    assert a2["type"] == "UPDATE_LAYER" and a2["layer"] == "temperature"
    a3 = tools.dashboard_action({"tool": "get_location_coordinates", "result": {"name": "Pune"}}, {}, "pune")
    assert a3["type"] == "UPDATE_LOCATION"


def test_tools_require_location_key():
    for name, spec in tools.TOOLS.items():
        assert "location" in spec["parameters"], f"{name} should expose a location param"


def test_execute_tool_daily_forecast_mock(monkeypatch):
    from app.config import settings
    orig = settings.use_mock_data
    settings.use_mock_data = True
    try:
        res = tools.execute_tool("get_daily_forecast", {"location": "Delhi", "days": 1})
        assert res["result"]["forecast"]
        assert res["result"]["source"] == "mock"
    finally:
        settings.use_mock_data = orig
