"""
AIClipper Inference Result Cache

Lightweight, JSON-backed cache for expensive LLM inference results (the 8B brain
and 3B specialists).  Keyed on a hash of the *input* (e.g. the transcript text),
so re-processing the same video during development or testing short-circuits the
model call entirely instead of running 8B/3B inference again.

The cache is kept in-process for speed and persisted to a small JSON file under
the data directory so results survive a restart (best-effort: a failure to read
or write the file degrades to an empty in-memory cache, never an error).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("utils.cache")

# TTL for cached inference results, in seconds (default 24h).
DEFAULT_TTL_SECONDS = int(os.environ.get("AICLIPPER_CACHE_TTL", str(24 * 3600)))

_lock = threading.Lock()
_memory: dict[str, dict[str, Any]] = {}  # key -> {"value": Any, "expires": float}


def transcript_hash(*parts: Any) -> str:
    """Return a stable sha256 hex digest over the transcript text inputs."""
    h = hashlib.sha256()
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (dict, list)):
            part = json.dumps(part, sort_keys=True, ensure_ascii=False, default=str)
        h.update(str(part).encode("utf-8", errors="replace"))
    return h.hexdigest()


def _cache_path() -> str | None:
    try:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        return str(settings.data_dir / "inference_cache.json")
    except Exception:  # noqa: BLE001 - never let cache pathing break a call
        return None


def _load_disk_cache() -> None:
    """Load the persisted cache into memory once (best-effort)."""
    global _memory
    path = _cache_path()
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _memory = data
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Could not load inference cache ({exc}); starting empty")


def _save_disk_cache() -> None:
    if not _memory:
        return
    path = _cache_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_memory, fh)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Could not persist inference cache ({exc})")


def get(key: str) -> Any | None:
    """Return the cached value for *key*, or None if missing/expired."""
    with _lock:
        entry = _memory.get(key)
        if entry is None:
            return None
        expires = entry.get("expires", 0)
        if time.time() > expires:
            _memory.pop(key, None)
            return None
        return entry.get("value")


def set(key: str, value: Any, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    """Store *value* for *key* with a TTL, and persist best-effort."""
    with _lock:
        if not _memory:
            _load_disk_cache()
        _memory[key] = {"value": value, "expires": time.time() + ttl}
        _save_disk_cache()


def invalidate(prefix: str) -> int:
    """Remove all cached entries whose key starts with *prefix*; returns count."""
    with _lock:
        keys = [k for k in _memory if k.startswith(prefix)]
        for k in keys:
            _memory.pop(k, None)
        if keys:
            _save_disk_cache()
        return len(keys)
