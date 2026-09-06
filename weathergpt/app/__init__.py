"""WeatherGPT - Conversational AI for Weather Forecasting, Alerts, and Climate.

Free / open-source stack:
- Data: Open-Meteo APIs (forecast, archive, marine, air quality, flood, alerts, geocoding) - no API key.
- NLU: rule-based multilingual intent engine (fast, offline-safe) + optional free LLM (Ollama local / Gemini free tier).
- Translation: Google Translate free endpoint (translate.googleapis.com gtx) with graceful offline fallback.
- Storage: SQLite by default (PostgreSQL via DATABASE_URL).
- Frontend: mobile-first web app, Leaflet maps, Chart.js, Web Speech API for voice.
"""
