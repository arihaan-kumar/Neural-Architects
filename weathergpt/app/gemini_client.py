"""Gemini function-calling client (backend-only, key from environment).

Flow (per requirement):
    Frontend -> FastAPI -> Gemini -> Tool/Function Calling -> Weather APIs
              -> Structured result -> Dashboard action + chatbot response

KEY RULES
- GEMINI_API_KEY is read EXCLUSIVELY from the process environment / .env.
  It is never emitted to the frontend or logged.
- Gemini is the REASONER: it selects tools and explains results. Every number
  in an answer comes from the tool (Open-Meteo / RainViewer / IMD).
- If the key is missing -> `gemini_ready=False`; the app falls back to the
  built-in rule engine so nothing breaks. Message: "Gemini API key is not configured."
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import requests

from .config import settings
from . import tools

log = logging.getLogger("weathergpt.gemini")

_session = requests.Session()
_session.headers.update({"User-Agent": "WeatherGPT/1.0"})

_SYSTEM = (
    "You are WeatherGPT - a professional conversational weather assistant for India "
    "('Ask. Understand. Prepare.').\n"
    "RULES: 1) NEVER invent weather numbers - always use the tools and explain their output. "
    "2) Respect/continue the CONTEXT place, date and follow-ups ('the day after' = next day). "
    "3) Choose the minimal tools needed. 4) Reply in the user's language (English, Hindi, Tamil, "
    "Bengali, Telugu, Marathi, Gujarati, Kannada, Malayalam, Punjabi, Odia, Assamese, Urdu, Nepali), "
    "2-4 sentences, with units (°C, km/h, mm) and a short ⚠ alert/risk note when relevant. "
    "Name the data source honestly (Open-Meteo, IMD, RainViewer); if a warning's source is "
    "'IMD (mock)', say 'sample IMD advisory (mock)'. "
    "5) No markdown tables.\nCONTEXT: {context}\n"
)


def gemini_ready() -> bool:
    return settings.gemini_configured


def status_info() -> dict[str, Any]:
    return {
        "gemini_configured": settings.gemini_configured,
        "model": settings.gemini_model if settings.gemini_configured else None,
        "mock_mode": settings.use_mock_data,
        "auth": validate_auth() if settings.gemini_configured else {"valid": False, "reason": "no_key"},
        "key_visible_to_frontend": False,  # never sent to the browser
    }


def _url(model: str) -> str:
    return f"{settings.gemini_base}/models/{model}:generateContent"


def _content_block(role: str, parts: list[dict]) -> dict[str, Any]:
    return {"role": role, "parts": parts}


def _headers() -> dict[str, str]:
    """AQ.-format keys (new AI Studio format) are accepted via the
    x-goog-api-key header; legacy AIza keys via ?key=. Send BOTH variants."""
    return {"x-goog-api-key": settings.gemini_key}


def _call_model(model: str, contents: list[dict], tools_decl: list[dict], system: str,
                max_rounds: int) -> tuple[dict | None, str | None]:
    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system}]},
        "tools": [{"functionDeclarations": tools_decl}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024},
    }
    last_resp = None
    backoff = 0
    for attempt in range(4):
        try:
            resp = _session.post(_url(model), params={"key": settings.gemini_key}, headers=_headers(),
                                 json=body, timeout=max(settings.timeout_s * 3, 60))
            last_resp = resp
            if resp.status_code == 200:
                return resp.json(), None
            if resp.status_code in (429, 500, 502, 503, 504):
                # free tier: 20 requests/min - honor the API's own retry hint
                delay = 3.0 * (attempt + 1)
                import re as _re
                m = _re.search(r"retry in ([\d.]+)s", resp.text or "")
                if m and resp.status_code == 429:
                    delay = round(min(float(m.group(1)) + 0.5, 20), 1)
                log.info("gemini %s backoff %ss (status %s)", model, delay, resp.status_code)
                time.sleep(delay)
                backoff += delay
                if backoff > 30:
                    return {"status": resp.status_code, "text": f"quota wait too long: {resp.text[:120]}"}, None
                continue
            if resp.status_code == 404 or (resp.status_code >= 400 and "models/" not in (resp.text or "") and
                                           "404" in (resp.text or "")):
                return {"model_error": True}, resp.text[:300]
            # other 4xx: try header-only / query-only variants once
            for variant in ("header", "query"):
                try:
                    if variant == "header":
                        r2 = _session.post(_url(model), headers=_headers(), json=body,
                                           timeout=max(settings.timeout_s * 3, 60))
                    else:
                        r2 = _session.post(_url(model), params={"key": settings.gemini_key}, json=body,
                                           timeout=max(settings.timeout_s * 3, 60))
                    if r2.status_code == 200:
                        return r2.json(), None
                except Exception:
                    continue
            return {"status": resp.status_code, "text": resp.text[:400]}, None
        except Exception as exc:
            if attempt == 3:
                return {"error": str(exc)}, None
            time.sleep(2 * (attempt + 1))
    return {"status": getattr(last_resp, "status_code", 0), "text": "rate limited / retries exhausted"}, None


# --- lightweight auth check (cached 5 min) ---------------------------------
_auth_check: dict[str, Any] = {"ts": 0.0, "result": None}
_auth_lock = threading.Lock()


def validate_auth(force: bool = False) -> dict[str, Any]:
    """Cheap probe: does Google accept this key? Result cached 5 minutes."""
    if not gemini_ready():
        return {"valid": False, "reason": "no_key"}
    import time as _t
    with _auth_lock:
        if not force and _auth_check["result"] and _t.time() - _auth_check["ts"] < 300:
            return _auth_check["result"]
        result = {"valid": False, "reason": "unknown"}
        try:
            r = _session.get(settings.gemini_base + "/models?key=" + settings.gemini_key,
                             headers=_headers(), timeout=settings.timeout_s)
            if r.status_code == 200:
                result = {"valid": True, "reason": "ok"}
            elif r.status_code == 401:
                result = {"valid": False, "reason": "google_rejected_key"}
            else:
                result = {"valid": False, "reason": f"http_{r.status_code}"}
        except Exception as exc:
            result = {"valid": False, "reason": f"network:{exc.__class__.__name__}"}
        _auth_check["ts"] = _t.time()
        _auth_check["result"] = result
        return result


def _call_raw(model: str, contents: list[dict], tools_decl: list[dict], system: str,
              max_rounds: int) -> tuple[list[dict[str, Any]], str, int | None]:
    """Run the function-calling loop. Returns (turn_data, final_text, model_used)."""
    rounds = 0
    text = ""
    events: list[dict[str, Any]] = []
    model_used = model
    body_contents: list[dict] = [dict(c) for c in contents]
    while rounds < max_rounds:
        data, err = _call_model(model_used, body_contents, tools_decl, system, max_rounds)
        if not data:
            return events, "", None
        if "error" in data and "candidates" not in data:
            # real failure (401 auth, 404 model, network) -> signal caller to retry/fallback
            return events, str((data.get("error") or {}).get("message") or data.get("text") or "")[:160], None
        candidates = data.get("candidates") or []
        if not candidates:
            return events, "", None
        parts = candidates[0].get("content", {}).get("parts") or []
        function_calls: list[dict] = []
        text_parts: list[str] = []
        for p in parts:
            if "functionCall" in p:
                function_calls.append(p["functionCall"])
            elif "text" in p:
                text_parts.append(p["text"])
        body_contents.append({"role": "model", "parts": parts})
        if text_parts:
            text = "".join(text_parts).strip()
        if not function_calls:
            return events, text, model_used
        # execute tools in order and feed results back to Gemini
        new_parts = []
        for call in function_calls:
            name = call.get("name", "")
            args = call.get("args") or {}
            t0 = time.perf_counter()
            result = tools.execute_tool(name, args)
            events.append({"round": rounds, "tool": name, "args": args,
                           "result": result.get("result"), "ms": int((time.perf_counter() - t0) * 1000)})
            new_parts.append({"functionResponse": {"name": name, "response": {"result": result.get("result")}}})
        body_contents.append({"role": "user", "parts": new_parts})
        rounds += 1
    return events, text or "(no text)", model_used


def _models_to_try() -> list[str]:
    models = [settings.gemini_model]
    for m in settings.gemini_models_fallback.split(","):
        m = m.strip()
        if m and m not in models:
            models.append(m)
    return models


def chat(message: str, history: list[dict[str, Any]] | None = None,
         context: dict[str, Any] | None = None, language: str = "en") -> dict[str, Any]:
    """Main entry: Gemini turns => answers. Deterministic actions derived from tools.
    Results are cached (5 min) so repeated/similar asks never re-burn the free
    daily quota (20 req/day on the free tier)."""
    t0 = time.perf_counter()
    if not gemini_ready():
        return {
            "ready": False, "reply": "Gemini API key is not configured.",
            "turns": [], "action": None, "model": None, "latency_ms": 0.0,
            "context": context,
        }

    cache_key = _cache_key(message, history, context, language)
    cached = _response_cache.get(cache_key)
    if cached and time.time() - cached[0] < 300:
        out = dict(cached[1])
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        out["cached"] = True
        return out
    out = _chat_uncached(message, history, context, language)
    _response_cache[cache_key] = (time.time(), out)
    return out


_response_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_key(message: str, history: list[dict] | None, context: dict | None, language: str) -> str:
    import hashlib
    raw = f"{message}|{language}|{json.dumps(context or {}, sort_keys=True, ensure_ascii=False)}|" \
          f"{[(h.get('role'), h.get('text')) for h in (history or [])][-2:]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _chat_uncached(message: str, history: list[dict[str, Any]] | None = None,
                   context: dict[str, Any] | None = None, language: str = "en") -> dict[str, Any]:
    t0 = time.perf_counter()

    contents: list[dict] = []
    for h in (history or [])[-12:]:
        role = "user" if h.get("role") == "user" else "model"
        txt = (h.get("text") or "").strip()
        if txt:
            contents.append({"role": role, "parts": [{"text": txt}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    system = _SYSTEM.format(context=json.dumps(context or {}, ensure_ascii=False))
    decls = tools.tool_declarations()

    last_error = None
    for model in _models_to_try():
        events, text, used = _call_raw(model, contents, decls, system, settings.gemini_max_calls)
        if used is None:
            last_error = text or "model unavailable"
            log.warning("gemini model %s failed: %s", model, last_error)
            continue
        # success
        action = None
        payload = {}
        for ev in events:
            a = tools.dashboard_action({"tool": ev["tool"], "result": ev["result"]}, context or {}, message)
            if a:
                action = a
                payload = a.get("payload", {})
        if not text:
            text = "I've updated the dashboard with the latest observations."
        return {
            "ready": True, "reply": text, "turns": events, "action": action,
            "payload": payload, "model": model,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "context": _updated_context(context, events, message),
            "sources": _sources(events),
        }
    # all models failed -> graceful
    return {
        "ready": True, "reply": f"Gemini is not reachable right now ({last_error or 'network error'}). "
                                f"Using the built-in assistant instead.",
        "turns": [], "action": None, "payload": {},
        "model": settings.gemini_model, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "context": context, "degrades_to_rule_engine": True,
    }


def _sources(events: list[dict]) -> list[str]:
    src = []
    for ev in events:
        res = ev.get("result") or {}
        s = res.get("source") if isinstance(res, dict) else None
        if s:
            src.append(str(s))
    return sorted(set(src))


def _updated_context(context: dict | None, events: list[dict], message: str) -> dict:
    ctx = dict(context or {
        "current_location": "Delhi", "current_page": "weather",
        "selected_date": "today", "active_layer": "temperature", "active_view": "daily_forecast",
    })
    for ev in events:
        res = ev.get("result") or {}
        if isinstance(res, dict):
            loc = res.get("location") or {}
            if res.get("place") and not loc.get("name"):
                ctx["current_location"] = res["place"]
            if loc.get("name"):
                ctx["current_location"] = loc["name"]
            if ev["tool"] == "get_daily_forecast":
                ctx["active_view"] = "daily_forecast"
            elif ev["tool"] == "get_hourly_forecast":
                ctx["active_view"] = "hourly_forecast"
            elif ev["tool"] == "get_weather_alerts":
                ctx["active_view"] = "alerts"
            elif ev["tool"] == "get_current_weather":
                ctx["selected_date"] = "today"
    return ctx


def reframe(text: str, language: str = "en") -> str:
    """Purely conversational rewrite of a deterministic, fact-correct piece of
    weather advice. No tools, no invention - Gemini only rewords it. FAST &
    safe: skips the network call unless auth is valid, single attempt, and
    returns the original text on any failure (never worse than the input)."""
    if not gemini_ready():
        return text
    try:
        if not validate_auth().get("valid"):
            return text
    except Exception:
        return text
    prompt = (
        "Rewrite this weather advice in a warm, friendly, practical way (2-3 sentences). "
        "Keep ALL numbers and facts exactly as given. Never invent conditions or times. "
        "Avoid technical/API language. Reply in " + (language or "English") + ".\n\nAdvice:\n" + text
    )
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": "You are WeatherGPT, a calm, friendly, practical weather companion."}]},
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300}}
    try:
        resp = _session.post(_url(settings.gemini_model), params={"key": settings.gemini_key},
                             headers=_headers(), json=body, timeout=12)
        if resp.status_code != 200:
            return text
        cands = resp.json().get("candidates") or []
        if not cands:
            return text
        parts = cands[0].get("content", {}).get("parts") or []
        txt = "".join(p.get("text", "") for p in parts if "text" in p).strip()
        return txt or text
    except Exception:
        return text
