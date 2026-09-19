"""
AIClipper Content Classifier

Classifies video content type (podcast, interview, tutorial, vlog, etc.)
and content density (low/medium/high) from the transcript.  This allows
the scoring engine to adapt its weights per genre — e.g. podcasts reward
dialogue and emotion, tutorials reward practical value and clarity.

Ported from AI-Youtube-Shorts-Generator's content-type detection, adapted
for AIClipper's Ollama-based LLM backend.  Degrades gracefully: if Ollama
is unreachable, returns sensible defaults.
"""

from __future__ import annotations

from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("services.content_classifier")

_CONTENT_TYPE_PROMPT = (
    "Analyze this video transcript sample and classify the content type.\n"
    "Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.\n"
    "Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).\n"
    "Respond with JSON only: {\"content_type\": \"...\", \"density\": \"...\"}"
)

# Genre-specific scoring weight overrides.  Keys match the content_type
# returned by the classifier.  Each dict adjusts the base weights so the
# scorer emphasises the right signals for that genre.
GENRE_WEIGHT_OVERRIDES: dict[str, dict[str, float]] = {
    "podcast": {
        "emotion": 0.30, "dialogue": 0.28, "scene_change": 0.04,
        "audio": 0.14, "face": 0.06, "reaction": 0.18,
    },
    "interview": {
        "emotion": 0.26, "dialogue": 0.26, "scene_change": 0.06,
        "audio": 0.14, "face": 0.10, "reaction": 0.18,
    },
    "tutorial": {
        "emotion": 0.14, "dialogue": 0.30, "scene_change": 0.12,
        "audio": 0.16, "face": 0.14, "reaction": 0.14,
    },
    "lecture": {
        "emotion": 0.12, "dialogue": 0.32, "scene_change": 0.10,
        "audio": 0.18, "face": 0.14, "reaction": 0.14,
    },
    "vlog": {
        "emotion": 0.24, "dialogue": 0.18, "scene_change": 0.16,
        "audio": 0.14, "face": 0.14, "reaction": 0.14,
    },
    "commentary": {
        "emotion": 0.22, "dialogue": 0.24, "scene_change": 0.12,
        "audio": 0.16, "face": 0.10, "reaction": 0.16,
    },
    "debate": {
        "emotion": 0.24, "dialogue": 0.22, "scene_change": 0.08,
        "audio": 0.16, "face": 0.12, "reaction": 0.18,
    },
}


def classify_content(transcript: dict[str, Any]) -> dict[str, str]:
    """
    Classify the video's content type and density from the transcript.

    Uses the first ~25 segments (capped at 3000 chars) to classify.

    Returns:
        ``{"content_type": "podcast", "density": "high"}``
    """
    settings = get_settings()

    segments = transcript.get("segments", [])
    if not segments:
        logger.info("No transcript segments; defaulting to other/medium")
        return {"content_type": "other", "density": "medium"}

    # Build a sample of the first 25 segments, capped at 3000 chars
    sample = " ".join(s.get("text", "") for s in segments[:25])[:3000]
    full_text = transcript.get("full_text") or sample
    prompt = f"{_CONTENT_TYPE_PROMPT}\n\nTranscript sample:\n{sample}"

    # Cache: re-classifying the SAME video (e.g. during dev/testing) should not
    # re-run the 3B classify inference.  Key on a hash of the transcript.
    from backend.utils.cache import get as cache_get, set as cache_set, transcript_hash
    cache_key = "classify:" + transcript_hash(full_text)
    cached = cache_get(cache_key)
    if cached is not None:
        content_type = str(cached.get("content_type", "other")).lower()
        density = str(cached.get("density", "medium")).lower()
        logger.info(f"Content classified (cached): type={content_type}, density={density}")
        return {"content_type": content_type, "density": density}

    try:
        # Route through the model TEAM: the light "classify" specialist takes
        # this fast, single-shot, JSON task so the heavy brain stays free.
        # Ollama's native format:"json" mode constrains the output to valid
        # JSON, so no regex/fence-stripping monkey-patching is needed.
        from backend.services.model_team import chat_json

        result = chat_json(
            "classify",
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            num_predict=256,
        )
        if result is None:
            raise RuntimeError("classify specialist returned no content")

        content_type = str(result.get("content_type", "other")).strip().lower()
        density = str(result.get("density", "medium")).strip().lower()

        # Validate
        valid_types = {"podcast", "interview", "tutorial", "lecture", "commentary", "debate", "vlog", "other"}
        if content_type not in valid_types:
            content_type = "other"
        valid_densities = {"low", "medium", "high"}
        if density not in valid_densities:
            density = "medium"

        logger.info(f"Content classified: type={content_type}, density={density}")
        try:
            cache_set(cache_key, {"content_type": content_type, "density": density})
        except Exception:  # noqa: BLE001 - caching is best-effort
            pass
        return {"content_type": content_type, "density": density}

    except Exception as exc:
        logger.info(f"Content classification failed ({exc}); defaulting to other/medium")
        return {"content_type": "other", "density": "medium"}


def get_genre_weights(content_type: str | None, base_weights: dict[str, float]) -> dict[str, float]:
    """
    Return scoring weights adjusted for the detected content genre.

    If the genre has specific overrides, those are merged on top of the base
    weights.  Unknown genres use the base weights unchanged.
    """
    if not content_type or content_type not in GENRE_WEIGHT_OVERRIDES:
        return dict(base_weights)

    overrides = GENRE_WEIGHT_OVERRIDES[content_type]
    merged = dict(base_weights)
    merged.update(overrides)

    # Normalise so weights sum to 1.0 (same convention as the config)
    total = sum(merged.values())
    if total > 0:
        merged = {k: round(v / total, 4) for k, v in merged.items()}

    logger.info(f"Genre-aware weights for '{content_type}': {merged}")
    return merged
