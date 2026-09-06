"""Optional answer polishing (Gemini-only project; kept minimal).

The Gemini brain already produces the final answer, so polishing is a no-op
unless LLM_BACKEND=ollama is set explicitly (legacy local option). Never blocks
the pipeline; failures return the original text.
"""
from __future__ import annotations

import logging

from .config import settings

log = logging.getLogger("weathergpt.llm")


def polish(analysis: str, payload: dict, language: str) -> str:
    """Return an enhanced version of `analysis` or the original (never None)."""
    if settings.llm_backend.lower() != "ollama":
        return analysis  # Gemini brain (default) already handled the answer
    return analysis
