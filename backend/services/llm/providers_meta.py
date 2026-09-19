"""Default model names + display labels per provider, and per-role model
preferences.  Used when a role is switched to an API provider and no explicit
model identifier has been chosen yet.

PRICING is intentionally approximate and only surfaced as a UI indicator
(Free / Low / Medium / High), never as an authoritative bill.
"""

from __future__ import annotations

DEFAULT_MODELS: dict[str, str] = {
    "local": "qwen3:8b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "gemini": "gemini-1.5-flash",
    "openai_compatible": "gpt-4o-mini",
}

# Human labels for the Settings/AI-Models dropdowns.
LABELS: dict[str, str] = {
    "local": "Local (Ollama)",
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "openai_compatible": "OpenAI-compatible endpoint",
}

# Cost bucket per provider for the UI tier indicator.
COST_TIER: dict[str, str] = {
    "local": "free",
    "openai": "paid",
    "anthropic": "paid",
    "gemini": "paid",
    "openai_compatible": "paid",
}

# The four model-team roles.
ROLES = ("brain", "classify", "hook", "translate")


def provider_default_model(provider: str) -> str:
    return DEFAULT_MODELS.get(provider, DEFAULT_MODELS["local"])


def provider_label(provider: str) -> str:
    return LABELS.get(provider, provider)


def provider_cost_tier(provider: str) -> str:
    return COST_TIER.get(provider, "free")
