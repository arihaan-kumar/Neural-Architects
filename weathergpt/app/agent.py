"""Unified agent dispatch — GEMINI ONLY (with rule-engine fallback).

Backends:
- gemini      : Google Gemini API function calling (default; key secret from .env)
- rule engine : built-in multilingual NLU (always available, zero cost, no keys)

The pipeline, tools, dashboard actions and fallbacks are identical no matter the
backend; only the model provider differs. (Groq / Ollama / OpenAI-compatible were
removed by design — WeatherGPT stays Gemini-only.)
"""
from __future__ import annotations

from typing import Any

from . import gemini_client
from .config import settings


def backend_name() -> str:
    return "gemini" if settings.llm_backend.lower() == "gemini" else "rule-engine"


def ready() -> bool:
    return gemini_client.gemini_ready() if backend_name() == "gemini" else False


def model_label() -> str:
    return settings.gemini_model if ready() else None


def chat(message: str, history: list[dict[str, Any]] | None = None,
         context: dict[str, Any] | None = None, language: str = "en",
         backend: str | None = None) -> dict[str, Any]:
    """Gemini turns => answer (identical shape for every backend)."""
    if ready():
        return gemini_client.chat(message, history, context, language)
    return {
        "ready": True, "reply": "Gemini API key is not configured.",
        "turns": [], "action": None, "payload": {}, "model": None,
        "latency_ms": 0.0, "context": context, "rule_engine": True,
    }


def secondary_backend() -> str | None:
    return None  # Gemini-only: failure falls straight to the built-in rule engine


def status() -> dict[str, Any]:
    return {
        "backend": backend_name(),
        "configured": ready(),
        "model": model_label(),
        "gemini": gemini_client.status_info(),
    }
