from app.main import chat_pipeline
from app.nlu import parse_query
from app import meteo


def test_chat_current(mock_forecast, mock_geocode):
    from app.main import ChatRequest
    res = chat_pipeline(ChatRequest(message="weather in Delhi"))
    assert res["intent"] == "current_weather"
    assert "31" in res["reply"]
    assert res["location"]["latitude"] == 28.6139
    assert "current" in res["payload"]


def test_chat_forecast(mock_forecast, mock_geocode):
    from app.main import ChatRequest
    res = chat_pipeline(ChatRequest(message="7-day forecast for Delhi"))
    assert res["intent"] == "forecast"
    assert "Forecast for Delhi" in res["reply"]


def test_chat_alerts(mock_forecast, mock_alerts, mock_geocode):
    from app.main import ChatRequest
    res = chat_pipeline(ChatRequest(message="alerts near Delhi"))
    assert res["intent"] == "alerts"
    assert "Very heavy rain" in res["reply"]


def test_chat_climate(mock_archive, mock_geocode):
    from app.main import ChatRequest
    res = chat_pipeline(ChatRequest(message="climate trend for Delhi last 5 years"))
    assert res["intent"] == "climate"
    assert res["payload"]["climate"]


def test_chat_hindi_fallback_safe():
    # offline mode: no translation (translate returns original), must not crash
    from app.main import ChatRequest
    res = chat_pipeline(ChatRequest(message="मौसम", language="hi"))
    assert res["language"] == "hi"
    assert res["reply"]


def test_chat_voice_flag(mock_forecast, mock_geocode):
    from app.main import ChatRequest
    res = chat_pipeline(ChatRequest(message="weather now in Delhi", voice=True))
    assert res["voice"] is True
