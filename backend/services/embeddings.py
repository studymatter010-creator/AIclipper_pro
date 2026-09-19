"""
AIClipper Sentence Embedding Service.

PHASE 1 (2026-09-11): wraps ``BAAI/bge-small-en-v1.5`` (sentence-transformers,
~134MB) to embed subtitle/transcript sentences into dense vectors.  These
embeddings drive Phase 2's semantic clip-boundary detection (anchor candidate
sentences at meaningful topic shifts) and any similarity/retrieval work.

The module registers with the generalized RAM sequencer (``model_team``) so the
torch embedder can be evicted under memory pressure.  Every call degrades
gracefully to an empty result if sentence-transformers/torch is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import timed

logger = logging.getLogger("services.embeddings")

_MODEL = None
_MODEL_NAME: str | None = None
_MAX_BATCH = 32


def _load() -> bool:
    global _MODEL, _MODEL_NAME
    if _MODEL is not None:
        return True
    try:
        settings = get_settings()
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

        _MODEL = SentenceTransformer(settings.bge_model)
        _MODEL_NAME = settings.bge_model
        logger.info(f"bge embedder loaded: model={settings.bge_model}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"bge embedder unavailable ({exc})")
        return False


def embeddings_available() -> bool:
    return _load()


def unload_bge() -> None:
    global _MODEL
    _MODEL = None
    try:
        import gc
        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    logger.debug("bge embedder unloaded")


def _register() -> None:
    try:
        from backend.services import model_team
        model_team.register_process("bge", ram_mb=300, unload=unload_bge)
    except Exception:  # noqa: BLE001
        pass


_register()


@timed(logger_name="processing")
def embed_sentences(sentences: list[str], *, normalize: bool = True) -> list[list[float]] | None:
    """
    Embed a list of sentences into dense vectors (recommended query-instruction
    prefix applied for bge retrieval consistency).

    Returns a list (same order) of float vectors, or None if the embedder is
    unavailable / input is empty.
    """
    if not sentences:
        return []
    if not _load():
        return None

    try:
        from backend.services import model_team
        model_team.acquire_process("bge")
    except Exception:  # noqa: BLE001
        pass

    try:
        texts = [("Represent this sentence for searching relevant passages: " + s) if s else " " for s in sentences]
        vecs = _MODEL.encode(texts, normalize_embeddings=normalize, batch_size=_MAX_BATCH)
        return [v.tolist() for v in vecs]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"bge embed failed ({exc})")
        return None


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Cosine similarity between two float vectors (0.0 if degenerate)."""
    n = min(len(v1), len(v2))
    if n == 0:
        return 0.0
    import math
    dot = sum(v1[i] * v2[i] for i in range(n))
    n1 = math.sqrt(sum(x * x for x in v1[:n]))
    n2 = math.sqrt(sum(x * x for x in v2[:n]))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)