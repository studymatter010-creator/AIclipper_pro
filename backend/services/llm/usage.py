"""
Session token/cost estimate for API providers (BYOK).

API calls cost real money (unlike local inference), so we keep a lightweight
running estimate of tokens used per provider this session and surface it in the
UI. Estimates use rough published per-1M-token prices and are presented as
approximate — never as an authoritative bill.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()

# Approx USD per 1M tokens: {provider: (input, output)}.  Best-effort public
# prices; Local (Ollama) is excluded (costs 0).
_PRICES: dict[str, tuple[float, float]] = {
    "openai": (0.15, 0.60),          # ~ gpt-4o-mini
    "anthropic": (0.80, 4.00),       # ~ claude-3-5-haiku
    "gemini": (0.35, 1.05),          # ~ gemini-1.5-flash
    "openai_compatible": (0.15, 0.60),
}

# provider -> {tokens, calls} summed this session.
_TOTALS: dict[str, dict] = {}


def record(provider: str, prompt_tokens: int, completion_tokens: int) -> None:
    if provider in (None, "local"):
        return
    with _lock:
        entry = _TOTALS.setdefault(provider, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        entry["input_tokens"] += int(prompt_tokens or 0)
        entry["output_tokens"] += int(completion_tokens or 0)
        entry["calls"] += 1


def snapshot() -> dict:
    """Return per-provider usage + estimated cost for this session."""
    with _lock:
        out = {}
        total_usd = 0.0
        for provider, entry in list(_TOTALS.items()):
            pin, pout = _PRICES.get(provider, (0.0, 0.0))
            usd = (entry["input_tokens"] / 1e6) * pin + (entry["output_tokens"] / 1e6) * pout
            total_usd += usd
            out[provider] = dict(entry, est_usd=round(usd, 4))
        return {"providers": out, "total_calls": sum(e["calls"] for e in _TOTALS.values()),
                "est_usd": round(total_usd, 4)}


def reset() -> None:
    with _lock:
        _TOTALS.clear()


def active_totals_any() -> bool:
    with _lock:
        return any(e["calls"] > 0 for e in _TOTALS.values())
