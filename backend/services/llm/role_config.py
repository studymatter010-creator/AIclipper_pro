"""
Per-role LLM provider routing (BYOK).

Each model-team role (brain/classify/hook/translate) can be served by Local
(Ollama) or any configured API provider, each with its own model identifier.

Routing config is persisted in the user settings table (keys
``llm_role_<role>_provider`` / ``llm_role_<role>_model``) and cached in memory
here so the sync ``model_team.chat``/``chat_json`` path never needs a DB
session per call. The cache is seeded at startup by the Settings loader.

Defaults are always Local/Ollama, so the app is fully functional with zero API
keys configured.
"""
from __future__ import annotations

import threading
from typing import Any

from backend.utils.config import get_settings

_lock = threading.Lock()

# role -> (provider, model).  None=not-yet-configured (use default below).
_CACHE: dict[str, tuple[str, str]] = {}

ROLES = ("brain", "classify", "hook", "translate")

# settings keys used to persist a role's routing choice.
def provider_key(role: str) -> str: return f"llm_role_{role}_provider"
def model_key(role: str) -> str: return f"llm_role_{role}_model"


def _default_local_model(role: str) -> str:
    s = get_settings()
    if role == "brain":
        return s.ollama_model or s.team_ollama_default or "qwen3:8b"
    return {
        "classify": s.team_classify,
        "hook": s.team_hook,
        "translate": s.team_translate,
    }.get(role) or s.team_ollama_default


def get(role: str) -> tuple[str, str]:
    """Return (provider, model) for *role* — always valid, defaults to local."""
    with _lock:
        cached = _CACHE.get(role)
    if cached:
        provider, model = cached
        if not model:
            from backend.services.llm.providers_meta import provider_default_model
            model = (
                _default_local_model(role)
                if provider == "local"
                else provider_default_model(provider)
            )
        return provider, model
    return "local", _default_local_model(role)


def set_role(role: str, provider: str, model: str | None = None) -> tuple[str, str]:
    """Set the routing for *role*. ``model=None`` picks the provider default or
    the role's local default when provider=='local'."""
    from backend.services.llm.providers_meta import provider_default_model
    if not provider or provider == "local":
        provider = "local"
        resolved_model = model or _default_local_model(role)
    else:
        resolved_model = model or provider_default_model(provider)
    with _lock:
        _CACHE[role] = (provider, resolved_model)
    return provider, resolved_model


def load_from_settings(settings: dict[str, Any]) -> None:
    """Seed the cache from the persisted per-user settings dict (called once at
    startup).  Unknown roles/values are ignored and keep their defaults."""
    with _lock:
        for role in ROLES:
            prov = settings.get(provider_key(role))
            mdl = settings.get(model_key(role))
            if not prov or prov == "local":
                continue
            _CACHE[role] = (prov, mdl or None)


def active() -> dict[str, dict]:
    """Return {role: {provider, model}} for every role (for status/UI)."""
    out = {}
    for role in ROLES:
        provider, model = get(role)
        out[role] = {"provider": provider, "model": model}
    return out


def reset() -> None:
    with _lock:
        _CACHE.clear()
