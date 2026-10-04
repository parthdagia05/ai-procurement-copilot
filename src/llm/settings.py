"""LLM provider settings resolved from environment variables.

Any OpenAI-compatible endpoint works. LLM_PROVIDER picks defaults; LLM_BASE_URL
and MODEL_NAME override them. No key means the copilot runs deterministic-only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import src  # noqa: F401  (loads .env)

PRESETS: dict[str, tuple[str, str]] = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}
# Provider-specific key names are accepted as a convenience.
KEY_FALLBACKS = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    base_url: str
    model: str
    api_key: str | None
    rpm: int

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def load_settings() -> LLMSettings:
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower() or "gemini"
    base_url, model = PRESETS.get(provider, PRESETS["gemini"])
    api_key = os.getenv("LLM_API_KEY", "").strip() or None
    if api_key is None:
        for name in KEY_FALLBACKS.get(provider, ()):
            api_key = os.getenv(name, "").strip() or None
            if api_key:
                break
    return LLMSettings(
        provider=provider,
        base_url=os.getenv("LLM_BASE_URL", "").strip() or base_url,
        model=os.getenv("MODEL_NAME", "").strip() or model,
        api_key=api_key,
        rpm=int(os.getenv("LLM_RPM", "8") or 8),
    )
