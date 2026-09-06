"""Free machine translation (Google translate_public endpoints) with fallback.

Tries `clients5.google.com/translate_a/t?client=dict-chrome-ex` first (free,
no API key, reliable), then the legacy `translate.googleapis.com` gtx endpoint,
then returns the source text unchanged (offline-safe). 14 Indian languages +
English are supported.
"""
from __future__ import annotations

import json
import logging
import re

import requests

from .config import settings

log = logging.getLogger("weathergpt.translate")

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 WeatherGPT/1.0"})


def _extract_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\[\[.*\]\]", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _parse_payload(data) -> str | None:
    """Handle both formats: [[translated, original], ...] or [[[t,o,...],...], ...]."""
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    parts = []
    # dict-chrome-ex / new format
    for seg in first if isinstance(first, list) else data:
        if isinstance(seg, list) and seg and isinstance(seg[0], str):
            parts.append(seg[0])
        elif isinstance(seg, str):
            parts.append(seg)
    # drop trailing detection tokens like ["...","en"] -> keep translation only
    if len(parts) > 1:
        parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", p.strip())]
    return "".join(parts).strip() or None


def _call(url: str, params: dict, timeout: int) -> str | None:
    resp = _session.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    data = _extract_json(resp.text)
    return _parse_payload(data)


def _gtx(text: str, target: str, source: str = "auto") -> str | None:
    """Translate `text` into `target`; None on any failure."""
    text = text[:4500]
    q = [("sl", source), ("tl", target), ("q", text)]
    # Strategy 1: clients5 dict-chrome-ex (free, no key; client param required)
    try:
        params = dict(q)
        params["client"] = "dict-chrome-ex"
        out = _call(settings.translate_url, params, settings.timeout_s)
        if out and out.strip():
            return out
    except Exception as exc:
        log.debug("translate primary failed: %s", exc)
    # Strategy 2: legacy gtx endpoint
    try:
        params = dict(q)
        params["client"] = "gtx"
        params["dt"] = "t"
        out = _call(settings.translate_fallback_url, params, settings.timeout_s)
        if out and out.strip():
            return out
    except Exception as exc:
        log.debug("translate fallback failed: %s", exc)
    return None


def translate(text: str, target: str, source: str = "auto") -> str:
    """Translate `text` into `target` ISO code; return original on failure."""
    text = (text or "").strip()
    if not text:
        return text
    if target == "en" and source == "auto" and _is_ascii(text):
        return text
    if target == source:
        return text
    out = _gtx(text, target, source)
    return out if out else text


def _is_ascii(text: str) -> bool:
    return bool(re.fullmatch(r"[\x00-\x7f\s.,!?;:'\"()\-%0-9/°]+", text or ""))


def detect_language_script(text: str) -> str:
    """Best-effort language detection using Unicode script blocks."""
    blocks = {
        "hi": (0x0900, 0x097F),   # Devanagari (hi/mr/ne shared -> refined by keywords)
        "bn": (0x0980, 0x09FF),
        "pa": (0x0A00, 0x0A7F),
        "gu": (0x0A80, 0x0AFF),
        "or": (0x0B00, 0x0B7F),
        "ta": (0x0B80, 0x0BFF),
        "te": (0x0C00, 0x0C7F),
        "kn": (0x0C80, 0x0CFF),
        "ml": (0x0D00, 0x0D7F),
        "ur": (0x0600, 0x06FF),
    }
    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        for lang, (lo, hi) in blocks.items():
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return "en"
    return max(counts, key=counts.get)
