"""
AIClipper Model Team — coordinated AI specialists.

AIClipper runs a small *team* of local models instead of one do-everything
model.  Each role is an expert tuned for one complex task, and they cooperate
by handing work to each other in sequence:

    brain      -> qwen3:8b          heavy reasoner: metadata, clip scoring,
    (planned)                         hook selection, general language work
    classify   -> qwen2.5:3b        fast content-type / density classifier
    hook       -> qwen2.5:3b        snap-hook opening-line extractor
    translate  -> qwen2.5:3b        Chinese (zh) subtitle translator
    (ASR, not via Ollama): Whisper small  -> speech-to-text (EN+ZH)

BALANCE & SEQUENCE (why a 16 GB machine doesn't OOM)
-----------------------------------------------------
Ollama can hold only a couple of models resident at once alongside Whisper.
The sequencer here keeps a running, best-effort account of which team models
are resident and, before loading a heavy model, unloads the least-recently
used ones (via ``keep_alive=0``) until the live set fits inside a configured
RAM budget.  The heavy ``brain`` is deliberately NOT kept resident while a
specialist is doing fast, single-shot work, and vice-versa — so experts
"take turns" rather than all piling into memory.

Every helper degrades gracefully: if Ollama is down or a model is missing,
single-shot helpers return their natural "no result" value so the pipeline
continues with heuristics (matching the rest of AIClipper).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import requests

from backend.utils.config import get_settings

logger = logging.getLogger("services.model_team")

# Approximate resident RAM (MB) per Ollama model, used only by the sequencer's
# bookkeeping to decide what to unload.  These are rough; Ollama itself is the
# ultimate authority on what actually fits.
#
# NOTE (2026-09-12, PART 1 A/B candidates): the 20B-brain candidates
# (gpt-oss-20b, deepseek-r1-distill-qwen-14b) each need MORE than the whole
# default 9000 MB budget on their own, so on a 16 GB machine they cannot
# coexist with the Whisper transcriber.  That is not a sequencer bug — it is a
# physical ceiling.  `acquire()` now logs an explicit warning when the chosen
# model alone exceeds the budget so the user knows to either raise the budget
# on a big-RAM box or keep the lighter default brain.  See
# scripts/benchmark_models.py for the A/B comparison before adopting any.
_RAM_ESTIMATE_MB: dict[str, int] = {
    "qwen3:8b": 5400,
    "qwen2.5:3b": 2600,
    "qwen2.5:7b": 5200,
    "qwen3:4b": 3200,
    "qwen3:1.7b": 1600,
    "llama3.2:3b": 2500,
    # Part-1 upgrade candidates (Q4 budgets).
    "gpt-oss-20b": 13000,            # 20B MoE (~13 GB Q4) — brain candidate
    "deepseek-r1-distill-qwen-14b": 9200,   # 14B (~9.2 GB Q4) — brain candidate
    "qwen3.5-4b": 3200,              # ~4B — classify/hook/translate candidate
}
_UNKNOWN_RAM_MB = 2000

# When we last touched a model (monotonic-ish clock), for LRU eviction.
_LOADED: dict[str, float] = {}


def _ram_mb(model: str) -> int:
    return _RAM_ESTIMATE_MB.get(model, _UNKNOWN_RAM_MB)


# ── Generalized ramp sequencer (non-Ollama heavy processes) ─────────────
# PHASE 1: Demucs, WhisperX, NLLB-200 and bge-small-en are heavy local
# processes that must cooperate for RAM on a 16GB machine — not just the
# Ollama models.  Each registers a weight and an unload callable so the
# sequencer can evict them LRU-style against the same budget as the Ollama
# team.

_PROC_RAM_MB: dict[str, int] = {
    "demucs": 2200,      # htdemucs 6-stem torch, CPU
    "whisperx": 1500,    # wav2vec2-base aligner
    "nllb": 700,         # distilled-600M int8
    "bge": 300,          # bge-small-en embedder
    "faster-whisper": 1800,  # distil-large-v3 int8
}
# Registered processes: name -> {ram_mb, unload: callable|None, last_used}
_PROCESSES: dict[str, dict] = {}
_LOADED_PROC: dict[str, float] = {}


def register_process(name: str, *, ram_mb: int | None = None, unload=None) -> None:
    """Register an external heavy process with the RAM sequencer so it can be
    evicted when a heavier model needs the budget.  ``unload`` is called to
    release memory (e.g. ``del`` + gc) before another model loads."""
    _PROCESSES[name] = {
        "ram_mb": ram_mb or _PROC_RAM_MB.get(name, _UNKNOWN_RAM_MB),
        "unload": unload,
    }


def acquire_process(name: str) -> bool:
    """
    Mark an external heavy process as in-use, evicting LRU victims (Ollama
    models OR other processes) until the whole live set fits the budget.
    Returns True if the process may proceed (always True; eviction is
    best-effort).
    """
    settings = get_settings()
    now = time.time()
    _LOADED_PROC[name] = now

    budget = int(settings.team_ram_budget_mb or 9000)
    resident = sum(_ram_mb(m) for m in list(_LOADED))
    resident += sum(_PROCESSES[n]["ram_mb"] for n in list(_LOADED_PROC))

    # Evict least-recently-used victims (Ollama models and other processes)
    # until the estimate fits.  Never evict the process we just acquired.
    guard = 0
    while resident > budget and len(_LOADED) + len(_LOADED_PROC) > 1 and guard < 50:
        victims = []
        victims += [(ts, "model", m) for m, ts in _LOADED.items()]
        victims += [(ts, "proc", n) for n, ts in _LOADED_PROC.items()]
        victims = [v for v in victims if v[2] != name]
        if not victims:
            break
        victims.sort(key=lambda v: v[0])  # oldest first
        _, kind, victim = victims[0]
        try:
            if kind == "model":
                _unload(victim)
                resident -= _ram_mb(victim)
                del _LOADED[victim]
            else:
                p = _PROCESSES.get(victim)
                if p and p.get("unload"):
                    try:
                        p["unload"]()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"Failed to unload process '{victim}': {exc}")
                resident -= _PROCESSES[victim]["ram_mb"]
                del _LOADED_PROC[victim]
        except KeyError:
            pass
        guard += 1
    return True


def release_process(name: str) -> None:
    """Release a process from the resident set (frees its estimate but does not
    unload its model — callers that want to free memory call their unload)."""
    _LOADED_PROC.pop(name, None)


def resolve(role: str) -> str:
    """Return the model name for a team *role* (brain, classify, hook, ...)."""
    settings = get_settings()
    if role == "brain":
        return settings.ollama_model
    if role == "classify":
        return settings.team_classify
    if role == "hook":
        return settings.team_hook
    if role == "translate":
        return settings.team_translate
    logger.warning(f"Unknown model-team role '{role}'; defaulting to brain")
    return settings.ollama_model


def roster() -> dict[str, str]:
    """Return {role: model} for every team member (for startup/status)."""
    return {r: resolve(r) for r in ("brain", "classify", "hook", "translate")}


def resident() -> dict:
    """Report the sequencer's current resident estimate (Ollama models + heavy
    processes) for diagnostics/status UI."""
    settings = get_settings()
    models = {m: _ram_mb(m) for m in list(_LOADED)}
    procs = {n: _PROCESSES[n]["ram_mb"] for n in list(_LOADED_PROC)}
    return {
        "models": models,
        "processes": procs,
        "resident_mb": sum(models.values()) + sum(procs.values()),
        "budget_mb": int(settings.team_ram_budget_mb or 9000),
    }


# ── RAM sequencer (balance + sequence) ──────────────────────────────────

def _ollama_base() -> str:
    return get_settings().ollama_host.rstrip("/")


def _unload(model: str) -> None:
    """Ask Ollama to release a model from memory (keep_alive=0). Best effort."""
    try:
        requests.post(
            f"{_ollama_base()}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=15,
        )
        logger.debug(f"Sequencer unloaded model '{model}'")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Sequencer could not unload '{model}': {exc}")


def acquire(role: str) -> str:
    """
    Mark a role's model as "in use", evicting LRU models if our (approximate)
    resident budget would be exceeded.  Returns the model name to call.
    """
    settings = get_settings()
    model = resolve(role)
    now = time.time()

    # Freshly requested => most-recently used.
    _LOADED[model] = now

    budget = int(settings.team_ram_budget_mb or 9000)
    # PART 1 (2026-09-12): if the chosen model needs more than the whole budget
    # on its own (e.g. a 20B brain on a 16 GB box), say so explicitly instead of
    # silently running everything side-by-side into an OOM.
    model_ram = _ram_mb(model)
    if model_ram > budget:
        logger.warning(
            f"Model '{model}' needs ≈{model_ram} MB — more than the configured "
            f"RAM budget of {budget} MB. It will run BUT cannot share RAM with "
            f"the transcriber on a 16 GB machine. Raise team_ram_budget_mb for "
            f"this model, or keep the lighter default brain."
        )
    resident = sum(_ram_mb(m) for m in list(_LOADED))
    resident += sum(_PROCESSES[n]["ram_mb"] for n in list(_LOADED_PROC))

    # Evict least-recently-used victims (Ollama models AND external heavy
    # processes), never the one we're acquiring, until the estimate fits.
    guard = 0
    while resident > budget and (len(_LOADED) + len(_LOADED_PROC)) > 1 and guard < 50:
        victims = []
        victims += [(ts, "model", m) for m, ts in _LOADED.items()]
        victims += [(ts, "proc", n) for n, ts in _LOADED_PROC.items()]
        victims = [v for v in victims if v[2] != model]
        if not victims:
            break
        victims.sort(key=lambda v: v[0])  # oldest first
        _, kind, victim = victims[0]
        try:
            if kind == "model":
                _unload(victim)
                resident -= _ram_mb(victim)
                del _LOADED[victim]
            else:
                p = _PROCESSES.get(victim)
                if p and p.get("unload"):
                    try:
                        p["unload"]()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"Failed to unload process '{victim}': {exc}")
                resident -= _PROCESSES[victim]["ram_mb"]
                del _LOADED_PROC[victim]
        except KeyError:
            pass
        guard += 1

    return model


# ── Comfortable completion ──────────────────────────────────────────────

def _ollama_chat_once(
    role: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    num_predict: int,
    timeout: int,
    as_json: bool = False,
) -> str | None:
    """One raw /api/chat round-trip; returns the assistant text or ``None``."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    if as_json:
        # Ollama's native structured-output mode: the model is constrained to
        # emit well-formed JSON, so downstream parsing no longer needs to
        # compensate for free-form text.
        payload["format"] = "json"
    resp = requests.post(
        f"{_ollama_base()}/api/chat",
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return (resp.json().get("message") or {}).get("content") or None


# ── BYOK routing ─────────────────────────────────────────────────────────
# Each role is resolved to (provider, provider_instance).  "local" keeps using
# the existing Ollama sequencer path _unchanged_.  Any API provider routes
# through the pluggable LLM layer, retries with rate-limit-aware backoff, and
# FALLS BACK to local on final failure so a paid outage never stalls (or kills)
# the pipeline.  Cost is recorded per provider for the session estimate.

def _route(role: str):
    from backend.services.llm.role_config import get
    from backend.services.llm.providers import build_provider
    provider, model = get(role)
    if provider == "local":
        return "local", None, resolve(role)
    try:
        prov = build_provider(provider, model)
    except Exception:  # noqa: BLE001 — bad config shouldn't break the team
        logger.warning(
            f"role '{role}' provider {provider} could not be built; using local"
        )
        return "local", None, resolve(role)
    return provider, prov, model


def _is_rate_limit(exc: Exception) -> bool:
    from backend.services.llm.providers import RateLimitError
    return isinstance(exc, RateLimitError)


def _chat_api(provider, prov, role, messages, temperature, num_predict, timeout,
              retries, retry_delay, as_json) -> Any | None:
    """Route one role through an API provider; fall back to local on failure."""
    settings = get_settings()
    attempts = 1 + max(0, int(retries))
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            fn = prov.chat_json if as_json else prov.chat
            return fn(messages, temperature=temperature, num_predict=num_predict,
                      timeout=timeout or settings.ollama_timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts - 1:
                # Respect rate limits: back off harder on 429/quota so we don't
                # hammer a (possibly free-tier) key into a ban.
                delay = retry_delay * 3 if _is_rate_limit(exc) else retry_delay
                logger.debug(
                    f"api '{provider}/{prov.model}' attempt {attempt + 1} failed "
                    f"({exc}); retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
    # Final failure -> fall back to local for this job, clearly logged.
    logger.warning(
        f"API provider '{provider}' failed for role '{role}' after {attempts} "
        f"attempt(s): {last}. Falling back to local for this call."
    )
    try:
        from backend.services.llm.providers import OllamaProvider
        local_prov = OllamaProvider(resolve(role))
        if as_json:
            return local_prov.chat_json(messages, temperature=temperature, num_predict=num_predict)
        return local_prov.chat(messages, temperature=temperature, num_predict=num_predict)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Fallback to local also failed for role '{role}': {exc}")
        return None


def chat(
    role: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    num_predict: int = 512,
    timeout: int | None = None,
    retries: int = 1,
    retry_delay: float = 1.5,
) -> str | None:
    """
    Single-shot completion for a team *role*, with one short automatic retry.

    Routes through the configured provider for *role* (Local/Ollama by default,
    or any BYOK API provider).  On an API failure it retries with rate-limit-
    aware backoff, then falls back to local for this call and logs it.

    Returns the assistant text, or ``None`` if every backend is unreachable.
    """
    settings = get_settings()
    provider, prov, model = _route(role)
    if provider == "local":
        local_model = acquire(role)
        attempts = 1 + max(0, int(retries))
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return _ollama_chat_once(
                    role, local_model, messages, temperature, num_predict,
                    timeout or settings.ollama_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < attempts - 1:
                    logger.debug(
                        f"model_team.chat('{role}'/{local_model}) attempt {attempt + 1} "
                        f"failed ({exc}); retrying in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
        logger.info(f"model_team.chat('{role}'/{local_model}) failed after {attempts} attempts: {last}")
        return None
    return _chat_api(provider, prov, role, messages, temperature, num_predict,
                     timeout, retries, retry_delay, as_json=False)


def chat_json(
    role: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    num_predict: int = 512,
    timeout: int | None = None,
    retries: int = 1,
    retry_delay: float = 1.5,
) -> dict[str, Any] | None:
    """
    Structured-JSON completion for a team *role*.

    Local uses Ollama's native ``format: "json"``; API providers return parsed
    JSON from their own response translation.  On failure, retries then falls
    back to local.  Returns the parsed dict or ``None``.
    """
    import json as _json

    settings = get_settings()
    provider, prov, model = _route(role)
    if provider == "local":
        local_model = acquire(role)
        attempts = 1 + max(0, int(retries))
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                text = _ollama_chat_once(
                    role, local_model, messages, temperature, num_predict,
                    timeout or settings.ollama_timeout,
                    as_json=True,
                )
                if not text:
                    raise ValueError("empty response")
                clean = text.strip()
                if clean.startswith("```"):
                    clean = clean.strip("`").removeprefix("json").strip()
                return _json.loads(clean)
            except Exception as exc:  # noqa: BLE001 - transient failures retry
                last = exc
                if attempt < attempts - 1:
                    logger.debug(
                        f"chat_json('{role}'/{local_model}) attempt {attempt + 1} failed "
                        f"({exc}); retrying in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
        logger.info(f"chat_json('{role}'/{local_model}) failed after {attempts} attempts: {last}")
        return None
    return _chat_api(provider, prov, role, messages, temperature, num_predict,
                     timeout, retries, retry_delay, as_json=True)


# ── Hook specialist ─────────────────────────────────────────────────────

_HOOK_PROMPT = (
    "You are a viral-shorts hook expert. Read the clip transcript and write ONE "
    "attention-grabbing opening line (≤ 40 characters, no quotes, no hashtags) that "
    "would stop a scrolling viewer in the first 3 seconds. Reply with only the line."
)


def analyze_hook(transcript_text: str) -> str:
    """
    Ask the dedicated *hook* specialist for the best opening line.

    Returns an empty string on failure so the caller falls back to the brain's
    hook_sentence unchanged.
    """
    if not transcript_text:
        return ""
    text = chat(
        "hook",
        [{"role": "user", "content": f"{_HOOK_PROMPT}\n\nTranscript:\n{transcript_text}"}],
        temperature=0.5,
        num_predict=64,
    )
    return (text or "").strip()


# NOTE (2026-09-09): `generate_thumbnail` FLUX img2img removed —
# `flux.1-schnell` is not a valid Ollama model, so this path could
# never run. Real-frame thumbnails via FFmpeg are used instead.
