"""Free, offline-capable multilingual NLU engine for WeatherGPT.

Design (no paid LLM required):
- Language detection via Unicode script + keyword evidence (14 Indian languages).
- If the query is not English, it is machine-translated to English (free gtx endpoint)
  for maximum intent coverage; a native keyword lexicon per language still scores
  intents so the system degrades gracefully offline.
- Intent classification = weighted keyword scoring + regex patterns (fast, deterministic,
  no latency cost). Entity extraction: location (city list + geocoder in handler),
  time/date, units, NWP model, crops.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .translate import detect_language_script, translate

# --------------------------------------------------------------------------
# Lexicons: intent -> {lang: [keywords]}
# --------------------------------------------------------------------------

LEXICONS: dict[str, dict[str, list[str]]] = {
    "current_weather": {
        "en": ["current weather", "weather now", "weather right now", "what is the weather", "what's the weather",
               "how is the weather", "current temp", "temperature now", "is it raining now", "weather today",
               "today's weather", "current conditions", "weather", "current"],
        "hi": ["मौसम", "आज का मौसम", "अभी मौसम", "तापमान", "क्या मौसम", "मौसम कैसा", "वर्तमान", "आज"],
        "ta": ["வானிலை", "இப்போது வானிலை", "வெப்பநிலை", "இன்று வானிலை", "இப்போது"],
        "te": ["వాతావరణం", "ఇప్పుడు వాతావరణం", "ఉష్ణోగ్రత", "నేటి"],
        "bn": ["আবহাওয়া", "এখন আবহাওয়া", "তাপমাত্রা", "আজকের"],
        "mr": ["हवामान", "आत्ताचे हवामान", "तापमान", "आज"],
        "gu": ["હવામાન", "હવે હવામાન", "તાપમાન", "આજે"],
        "kn": ["ಹವಾಮಾನ", "ಈಗ ಹವಾಮಾನ", "ತಾಪಮಾನ", "ಇಂದು"],
        "ml": ["കാലാവസ്ഥ", "ഇപ്പോഴത്തെ", "താപനില", "ഇന്ന്"],
        "pa": ["ਮੌਸਮ", "ਹੁਣ ਮੌਸਮ", "ਤਾਪਮਾਨ", "ਅੱਜ"],
        "ur": ["موسم", "اب موسم", "درجہ حرارت", "آج"],
    },
    "forecast": {
        "en": ["forecast", "weather later", "this week", "next week", "next few days", "week ahead",
               "western disturbance", "rain tomorrow", "will it rain", "chance of rain", "tomorrow",
               "next 3 days", "weekly forecast", "predict"],
        "hi": ["पूर्वानुमान", "कल मौसम", "कल बारिश", "अगले", "हफ्ते", "सप्ताह", "मौसम पूर्वानुमान"],
        "ta": ["முன்னறிவிப்பு", "நாளை", "இந்த வாரம்", "அடுத்த"],
        "te": ["అంచనా", "రేపు", "ఈ వారం", "తదుపరి"],
        "bn": ["পূর্বাভাস", "আগামীকাল", "এই সপ্তাহে", "আগামী"],
        "mr": ["अंदाज", "उद्या", "आठवडा", "पुढील"],
        "pa": ["ਪੂਰਵ-ਅਨੁਮਾਨ", "ਕੱਲ੍ਹ", "ਹਫ਼ਤਾ"],
    },
    "alerts": {
        "en": ["alert", "warning", "advisory", "cyclone", "heatwave", "heat wave", "cold wave", "storm warning",
               "hail", "heavy rain warning", "imd", "red alert", "orange alert", "warning issued"],
        "hi": ["चेतावनी", "अलर्ट", "चक्रवात", "गर्मी", "आंधी", "बारिश की चेतावनी"],
        "ta": ["எச்சரிக்கை", "எச்சரிக்கை", "புயல்", "அலர்ட்"],
        "te": ["హెచ్చరిక", "అలర్ట్", "తుఫాను", "వర్ష హెచ్చరిక"],
        "bn": ["সতর্কতা", "সতর্কবার্তা", "ঝড়", "ঘূর্ণিঝড়"],
        "mr": ["सतर्कता", "इशारा", "वादळ", "चक्रीवादळ"],
        "gu": ["ચેતવણી", "ચક્રવાત", "એલર્ટ"],
        "pa": ["ਚੇਤਾਵਨੀ", "ਅਲਰਟ", "ਚੱਕਰਵਾਤ"],
    },
    "climate": {
        "en": ["climate", "trend", "historical", "past year", "last year", "average temperature", "climate change",
               "warming", "long term", "season analysis", "precipitation trend", "anomaly", "last 5 years"],
        "hi": ["जलवायु", "इतिहास", "पिछले", "औसत तापमान", "जलवायु परिवर्तन"],
        "ta": ["காலநிலை", "வரலாறு", "சராசரி", "கடந்த"],
        "te": ["వాతావరణ శాస్త్రం", "చరిత్ర", "గడిచిన", "సగటు"],
        "bn": ["জলবায়ু", "ইতিহাস", "গড়", "বিগত"],
        "mr": ["हवामानशास्त्र", "इतिहास", "सरासरी", "मागील"],
    },
    "marine": {
        "en": ["marine", "sea", "ocean", "wave", "wave height", "coastal", "fishermen", "fishing", "tide",
               "harbour", "port", "boat"],
        "hi": ["समुद्र", "लहर", "मछुआरे", "मत्स्य", "बंदरगाह", "तट"],
        "ta": ["கடல்", "அலை", "மீனவர்", "துறைமுகம்"],
        "te": ["సముద్రం", "అల", "మత్స్య", "ఓడరేవు"],
        "bn": ["সমুদ্র", "ঢেউ", "মৎস্যজীবী", "বন্দর"],
    },
    "aviation": {
        "en": ["flight", "aviation", "airport", "visibility", "fog", "crosswind", "wind shear", "takeoff",
               "landing", "pilot", "metar", "taf", "ceiling", "delay"],
        "hi": ["उड़ान", "विमान", "हवाई अड्डा", "कोहरा", "दृश्यता"],
        "ta": ["விமானம்", "மூடுபனி", "பார்வை", "விமான நிலையம்"],
    },
    "agriculture": {
        "en": ["crop", "farmer", "agriculture", "sowing", "irrigation", "harvest", "paddy", "wheat", "sugarcane",
               "cotton", "maize", "onion", "vegetable", "spray", "fertilizer"],
        "hi": ["फसल", "किसान", "खेती", "बुआई", "सिंचाई", "कटाई", "गेहूं", "धान", "गन्ना", "कपास", "मक्का", "प्याज"],
        "ta": ["பயிர்", "விவசாயி", "வேளாண்மை", "நெல்", "கோதுமை"],
        "te": ["పంట", "రైతు", "వ్యవసాయం", "వరి", "గోధుమ"],
        "bn": ["ফসল", "কৃষক", "কৃষি", "ধান", "গম"],
        "mr": ["पीक", "शेतकरी", "शेती", "भात", "गहू"],
        "gu": ["પાક", "ખેડૂત", "ખેતી", "ઘઉં"],
        "pa": ["ਫ਼ਸਲ", "ਕਿਸਾਨ", "ਖੇਤੀ", "ਕਣਕ"],
        "kn": ["ಬೆಳೆ", "ರೈತ", "ಕೃಷಿ", "ಗೋಧಿ"],
        "ml": ["വിള", "കർഷക", "കൃഷി", "നെല്ല്"],
    },
    "urban": {
        "en": ["flood", "waterlogging", "water logging", "smart city", "drainage", "urban", "city planning",
               "stagnant water", "traffic", "infrastructure"],
        "hi": ["बाढ़", "जलभराव", "नाला", "स्मार्ट सिटी", "शहर"],
        "ta": ["வெள்ளம்", "நகர", "சாலை"],
        "bn": ["বন্যা", "শহর", "জলাবদ্ধতা"],
    },
    "air_quality": {
        "en": ["air quality", "aqi", "pollution", "pm2.5", "pm10", "smog", "dust"],
        "hi": ["वायु गुणवत्ता", "प्रदूषण", "एक्यूआई", "धूल"],
        "ta": ["காற்று தரம்", "மாசு"],
        "bn": ["বায়ুর মান", "দূষণ"],
    },
    "sun": {
        "en": ["sunrise", "sunset", "sun rise", "sun set", "daylight", "dusk", "dawn"],
        "hi": ["सूर्योदय", "सूर्यास्त"],
        "ta": ["சூரிய உதயம்", "சூரிய அஸ்தமனம்"],
    },
    "rain": {
        "en": ["rain", "rainfall", "shower", "drizzle", "precipitation", "umbrella", "monsoon"],
        "hi": ["बारिश", "वर्षा", "बूंदाबांदी", "मानसून"],
        "ta": ["மழை", "மழைப்பொழிவு", "பருவமழை"],
        "te": ["వర్షం", "వర్షపాతం"],
        "bn": ["বৃষ্টি", "বর্ষা", "বৃষ্টিপাত"],
        "mr": ["पाऊस", "पर्जन्य", "मान्सून"],
        "gu": ["વરસાદ", "ચોમાસું"],
    },
    "wind": {
        "en": ["wind", "windy", "breeze", "storm", "cyclone radial", "speed of wind"],
        "hi": ["हवा", "तेज़ हवा", "आंधी", "हवा की गति"],
        "ta": ["காற்று", "புயல்", "காற்றின் வேகம்"],
    },
    "greeting": {
        "en": ["hello", "hi ", "hey", "namaste", "good morning", "good evening", "good afternoon", "namaskar"],
        "hi": ["नमस्ते", "नमस्कार", "प्रणाम", "हेलो"],
        "ta": ["வணக்கம்", "ஹலோ"],
        "te": ["నమస్కారం", "హలో"],
        "bn": ["নমস্কার", "হ্যালো"],
        "mr": ["नमस्कार", "हॅलो"],
        "gu": ["નમસ્તે", "હેલો"],
        "pa": ["ਸਤ ਸ੍ਰੀ ਅਕਾਲ", "ਹੈਲੋ"],
    },
    "thanks": {
        "en": ["thank", "thanks", "thx", "dhanyavad"],
        "hi": ["धन्यवाद", "शुक्रिया"],
        "ta": ["நன்றி"],
        "te": ["ధన్యవాదాలు"],
        "bn": ["ধন্যবাদ"],
        "mr": ["धन्यवाद"],
        "gu": ["આભાર"],
        "pa": ["ਧੰਨਵਾਦ"],
    },
    "help": {
        "en": ["help", "what can you do", "options", "menu", "features", "how to use", "guide"],
        "hi": ["मदद", "क्या कर सकते", "विकल्प", "मेनू"],
        "ta": ["உதவி", "என்ன செய்யலாம்"],
        "bn": ["সাহায্য", "কী করতে পারেন"],
    },
}

LANGUAGE_CODES = {"en", "hi", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa", "or", "as", "ur", "ne"}

# Native language hints to refine script-based detection (Devanagari is shared)
_LANG_HINTS = {
    "hi": ["मौसम", "बारिश", "मोसम", "आज", "कल"],
    "mr": ["महाराष्ट्र", "पुणे", "हवामान", "उद्या", "मुंबई"],
    "ne": ["मौसम", "काठमाडौं"],
}

INDIA_CITIES = [
    "delhi", "new delhi", "mumbai", "kolkata", "chennai", "bengaluru", "bangalore", "hyderabad", "pune",
    "ahmedabad", "jaipur", "lucknow", "kanpur", "nagpur", "indore", "thane", "bhopal", "patna", "vadodara",
    "ludhiana", "surat", "visakhapatnam", "coimbatore", "bhubaneswar", "amaravati", "guwahati", "ranchi",
    "raipur", "kochi", "chandigarh", "dehradun", "shimla", "srinagar", "aizawl", "agartala", "imphal",
    "itanagar", "gangtok", "panaji", "kohima", "mysuru", "vijayawada", "tiruchirappalli", "jammu",
    "jodhpur", "amritsar", "nashik", "varanasi", "allahabad", "kota", "madurai", "salem", "srinagar",
    "puducherry", "silchar", "darjeeling", "shillong", "tirupati", "kakinada", "mangaluru", "hubli",
    "jamshedpur", "dhanbad", "gwalior", "agra", "meerut", "bareilly", "aligarh", "gorakhpur", "saharanpur",
    "bhilai", "jalandhar", "warangal", "nellore", "rajkot", "guntur", "jabalpur", "asansol",
    "world", "delhi ncr", "noida", "gurgaon", "gurugram", "faridabad", "ghaziabad", "navi mumbai",
    "opc", "nalanda", "kullu", "manali", "rishikesh", "haridwar", "ujjain", "mathura", "ayodhya", "dwarka",
]

# Non-Indian world airports/cities commonly asked; India-first dataset keeps it free.
WORLD_CITIES = ["london", "paris", "new york", "tokyo", "sydney", "dubai", "singapore", "berlin", "moscow", "cairo",
                "nairobi", "seoul", "beijing", "mexico city", "são paulo", "zurich", "amsterdam", "istanbul",
                "toronto", "chicago", "los angeles", "san francisco", "miami", "boston", "seattle"]

_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass
class ParsedQuery:
    text: str
    language: str = "en"
    intent: str = "fallback"
    confidence: float = 0.0
    entities: dict[str, Any] = field(default_factory=dict)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "entities": self.entities,
            "query_en": self.raw or self.text,
        }


def detect_language(text: str) -> str:
    script = detect_language_script(text)
    if script == "en":
        lowered = text.lower()
        if lowered in ("namaste", "namaskar", "dhanyavad") or "mausam" in lowered:
            return "hi"
        return "en"
    if script in ("hi", "mr", "ne"):
        lowered = text.strip()
        score: dict[str, int] = {}
        for lang, words in _LANG_HINTS.items():
            score[lang] = sum(1 for w in words if w in lowered)
        # Default Devanagari to Hindi
        return max(score, key=score.get) if any(score.values()) else "hi"
    return script


def _score_intent(text: str, lang: str) -> list[tuple[str, float]]:
    lowered = text.lower()
    scores: list[tuple[str, float]] = []
    for intent, lexicons in LEXICONS.items():
        score = 0.0
        for lexicon_lang in (lang, "en"):
            for kw in lexicons.get(lexicon_lang, []):
                kw_l = kw.lower()
                if kw_l in lowered:
                    score += 1.2 if kw.startswith(" ") or kw_l in ("hi",) else 1.0
                elif re.search(rf"\b{re.escape(kw_l.strip())}\b", lowered):
                    score += 0.8
        if score > 0:
            scores.append((intent, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


def _extract_entities(text: str) -> dict[str, Any]:
    lowered = text.lower()
    entities: dict[str, Any] = {}

    # --- location ---
    for city in INDIA_CITIES + WORLD_CITIES:
        if re.search(rf"\bin\s+{re.escape(city)}\b|\b{re.escape(city)}\b", lowered):
            entities["city"] = city.title()
            break

    # --- time/date ---
    date = None
    if re.search(r"\btomorrow\b|\bnext day\b", lowered):
        date = "tomorrow"
    elif re.search(r"\btoday\b|\btonight\b", lowered):
        date = "today"
    elif re.search(r"\bweekend\b", lowered):
        date = "weekend"
    elif re.search(r"\bweek\b|\b7 days\b", lowered):
        date = "week"
    elif re.search(r"\bmonth\b", lowered):
        date = "month"
    elif re.search(r"\bday after tomorrow\b", lowered):
        date = "day_after_tomorrow"
    if date:
        entities["date"] = date

    m = re.search(r"\bnext\s+(\d+)\s+(day|days|weeks|week|hours)\b", lowered)
    if m:
        entities["horizon_days"] = int(m.group(1)) * (7 if "week" in m.group(2) else 1)
    elif date:
        entities.setdefault("horizon_days", {"today": 1, "tomorrow": 2, "day_after_tomorrow": 3,
                                             "weekend": 3, "week": 7, "month": 30}.get(date, 3))

    m = re.search(r"\b(\d+)\s*(day|days)\b", lowered)
    if m and "horizon_days" not in entities:
        entities["horizon_days"] = min(int(m.group(1)), 16)

    # --- units ---
    if re.search(r"\bfahrenheit\b|\bf\b|\b°f\b", lowered):
        entities["units"] = "imperial"
    elif re.search(r"\bcelsius\b|\bc\b|\b°c\b", lowered):
        entities["units"] = "metric"

    # --- NWP model ---
    for model in ("gfs", "icon", "ecmwf", "gem", "meteofrance", "ukmo"):
        if re.search(rf"\b{model}\b", lowered):
            entities["model"] = model

    # --- crops ---
    for crop in ("paddy", "rice", "wheat", "sugarcane", "cotton", "maize", "corn", "onion", "millet", "soybean",
                 "mustard", "potato", "chilli", "turmeric", "tea", "coffee", "groundnut", "ragi", "bajra"):
        if re.search(rf"\b{crop}\b", lowered):
            entities["crop"] = crop

    # --- aviation ---
    if re.search(r"\bflight\b|\bairport\b|\bpilot\b", lowered):
        entities["aviation"] = True

    # --- seasonal keyword ---
    if re.search(r"\bwinter\b|\bsummer\b|\bmonsoon\b|\bspring\b|\bautumn\b", lowered):
        entities["season"] = re.search(r"\b(winter|summer|monsoon|spring|autumn)\b", lowered).group(1)

    # --- year (climate) ---
    ym = re.search(r"\b(19\d\d|20\d\d)\b", lowered)
    if ym:
        entities["year"] = int(ym.group(1))

    m = re.search(r"\blast\s+(\d+)\s+years?\b", lowered)
    if m:
        entities["years"] = min(int(m.group(1)), 30)

    return entities


def parse_query(message: str, hint_language: str | None = None) -> ParsedQuery:
    """Main entrypoint: understand a user message."""
    text = (message or "").strip()
    lang = hint_language if hint_language in LANGUAGE_CODES else detect_language(text)

    native_scores = _score_intent(text, lang)
    work_text = text
    if lang != "en":
        work_text = translate(text, "en", lang)

    scores = _score_intent(work_text, "en")
    # Native lexicon scores should dominate when present
    merged: dict[str, float] = {}
    for intent, sc in native_scores:
        merged[intent] = merged.get(intent, 0) + sc
    for intent, sc in scores[:8]:
        merged[intent] = merged.get(intent, 0) + sc * (0.8 if intent not in native_scores or native_scores[0][0] == intent else 0.5)

    entities = _extract_entities(work_text)
    entities.update(_extract_entities_native(text))

    if not merged:
        intent, conf = ("fallback", 0.3)
    else:
        intent = max(merged, key=merged.get)
        conf = merged[intent]
        total = sum(merged.values())
        conf = max(0.35, min(0.97, conf / max(total, 1.0) * 1.9 + 0.45))

    # greeting only if very short
    if intent == "greeting" and len(text.split()) > 3 and scores and scores[0][0] != "greeting":
        intent, conf = scores[0]

    return ParsedQuery(text=work_text, language=lang, intent=intent, confidence=float(conf),
                       entities=entities, raw=text)


def _extract_entities_native(text: str) -> dict[str, Any]:
    """Very small native entity extraction (dates)."""
    out: dict[str, Any] = {}
    if re.search(r"कल|नालं|मुन्न", text):
        pass
    for pattern, val in [
        (r"आज", "today"), (r"कल", "tomorrow"), (r"अभी", "today"),
        (r"இன்று", "today"), (r"நாளை", "tomorrow"), (r"ఈరోజు", "today"), (r"రేపు", "tomorrow"),
        (r"আজ", "today"), (r"আগামীকাল", "tomorrow"), (r"आज", "today"), (r"उद्या", "tomorrow"),
        (r"હવે", "today"), (r"இப்போது", "today"), (r"ਅੱਜ", "today"), (r"ਕੱਲ੍ਹ", "tomorrow"),
    ]:
        if re.search(pattern, text):
            out["date"] = val
            break
    return out
