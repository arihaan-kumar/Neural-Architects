from app.nlu import parse_query, detect_language, LEXICONS


def test_detect_language():
    assert detect_language("what is the weather today") == "en"
    assert detect_language("आज मौसम कैसा है") == "hi"
    assert detect_language("இன்று வானிலை எப்படி") == "ta"
    assert detect_language("আজ আবহাওয়া কেমন") == "bn"


def test_current_weather_english():
    p = parse_query("What is the weather in Delhi right now?")
    assert p.intent == "current_weather"
    assert p.entities.get("city", "").lower() == "delhi"


def test_forecast_english():
    p = parse_query("7 day forecast for Mumbai")
    assert p.intent in ("forecast", "current_weather")
    assert p.entities.get("horizon_days") in (7, 6, 8)


def test_alerts_question():
    p = parse_query("Any cyclone warnings near Chennai?")
    assert p.intent == "alerts"


def test_climate_question():
    p = parse_query("historical climate trend for Pune last 10 years")
    assert p.intent == "climate"
    assert p.entities.get("years") == 10


def test_marine_fishermen():
    p = parse_query("sea conditions for fishermen near Visakhapatnam")
    assert p.intent == "marine"


def test_agriculture_paddy():
    p = parse_query("paddy crop advisory for Patna")
    assert p.intent == "agriculture"
    assert p.entities.get("crop") == "paddy"


def test_hindi_current():
    p = parse_query("दिल्ली में मौसम कैसा है?")
    assert p.language == "hi"
    assert p.intent in ("current_weather", "forecast", "fallback")


def test_units_entity():
    p = parse_query("temperature in celsius")
    assert p.entities.get("units") == "metric"


def test_greeting():
    p = parse_query("hello")
    assert p.intent == "greeting"


def test_all_languages_have_lexicons():
    for intent, lexis in LEXICONS.items():
        assert "en" in lexis, intent
