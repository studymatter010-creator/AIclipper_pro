"""
AIClipper NLLB Translation Service.

PHASE 1 (2026-09-11): replaces the Ollama "translate" specialist
(qwen2.5:3b) with Meta's NLLB-200-distilled-600M running locally via
CTranslate2.  NLLB-200 is purpose-built for many-to-many translation, has
native Simplified-Chinese output, and — at the distilled-600M size — is far
faster and more deterministic than prompting a 3B conversation model to
translate.  For subtitle work determinism is a feature: the same source text
always yields the same translation.

Engine notes (documented CTranslate2 NLLB pattern)
-------------------------------------------------
* The HF model is converted to CTranslate2 format once
  (``ctranslate2.convert_..`` / ``ctranslate2`` auto-download) and loaded via
  ``ctranslate2.models.Translator``.
* NLLB tokenizes with sentencepiece; sentences are prefixed with a source
  language-tag token (e.g. ``__eng_Latn__``) and decoded against a
  ``target_prefix`` language tag.  We use sentencepiece ``EncodeAsPieces`` /
  ``DecodePieces`` directly — no transformers / torch needed just to tokenize.
* RAM: distilled-600M fp32 ≈ ~2.4GB.  We load int8 on CPU for a tight 16GB
  budget alongside Demucs/WhisperX/qwen3:8b.  The module cooperates with the
  generalized RAM sequencer so the model can be evicted/unloaded on demand.

Graceful degradation mirrors the rest of AIClipper: if ctranslate2 or the
model is unavailable, translation raises/unavailable so the caller
(``subtitles.py``) falls back to the Ollama specialist, then to original text.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import timed

logger = logging.getLogger("services.nllb_translate")

# NLLB-200 FLORES-200 language-tag tokens.  Keyed by the same SUBTITLE_LANGUAGES
# codes the UI offers (en/zh/ko/hi/ja).  Controls script for each target.
_NLLB_LANG_TAG: dict[str, str] = {
    "en": "eng_Latn",
    "zh": "zho_Hans",   # Simplified Chinese
    "ko": "kor_Hang",   # Korean (Hangul)
    "hi": "hin_Deva",   # Hindi (Devanagari)
    "ja": "jpn_Jpan",   # Japanese
}

_DEFAULT_SRC = "eng_Latn"

_MODEL: Any = None            # ctranslate2 Translator
_SP: Any = None               # sentencepiece processor
_LOCK = threading.Lock()
_LOADED_NAME: str | None = None


def _sp_model_path() -> str:
    """Resolve the NLLB sentencepiece model file."""
    settings = get_settings()
    p = settings.nllb_sentencepiece_model
    if p and Path(p).is_file():
        return str(p)
    # Fallback: inside the ctranslate2 cache dir for the HF model.
    cache = Path.home() / ".cache" / "ctranslate2" / "facebook" / "nllb-200-distilled-600M"
    for name in ("sentencepiece.model", "spm.model", "nllb.spm"):
        cand = cache / name
        if cand.is_file():
            return str(cand)
    return "facebook/nllb-200-distilled-600M/sentencepiece.model"


def _load_module() -> bool:
    """Load CTranslate2 + sentencepiece once (thread-safe). Returns True if ready."""
    global _MODEL, _SP, _LOADED_NAME
    if _MODEL is not None:
        return True
    with _LOCK:
        if _MODEL is not None:
            return True
        try:
            import ctranslate2  # type: ignore[import-untyped]
            import sentencepiece as spm  # type: ignore[import-untyped]

            settings = get_settings()
            model_name = settings.nllb_model or "facebook/nllb-200-distilled-600M"
            _MODEL = ctranslate2.models.Translator(
                model_name,
                device=settings.nllb_device,      # "cpu"
                compute_type=settings.nllb_compute,  # "int8" on CPU
            )
            _SP = spm.SentencePieceProcessor(model_file=_sp_model_path())
            _LOADED_NAME = model_name
            logger.info(
                f"NLLB-200 translator loaded: model={model_name} "
                f"device={settings.nllb_device} compute={settings.nllb_compute}"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"NLLB-200 unavailable ({exc}); using fallback")
            return False


def available() -> bool:
    """True if the NLLB translator can be loaded."""
    return _load_module()


def unload() -> None:
    """Release the NLLB model from memory (called by the RAM sequencer when a
    heavier model needs the budget)."""
    global _MODEL, _SP
    with _LOCK:
        _MODEL = None
        _SP = None
    try:
        import gc
        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    logger.debug("NLLB translator unloaded")


def _register() -> None:
    """Register this heavy module with the generalized RAM sequencer."""
    try:
        from backend.services import model_team
        model_team.register_process("nllb", ram_mb=700, unload=unload)
    except Exception:  # noqa: BLE001
        pass


# Register at import so the sequencer can evict NLLB as needed.
_register()


def _tag(code: str) -> str:
    return f"__{_NLLB_LANG_TAG.get(code, _DEFAULT_SRC)}__"


@timed(logger_name="processing")
def translate_batch(sentences: list[str], target_lang: str = "zh") -> list[str] | None:
    """Translate a list of sentences to *target_lang*; returns same-length list
    or None on any failure (caller falls back)."""
    try:
        from backend.services import model_team
        model_team.acquire_process("nllb")
    except Exception:  # noqa: BLE001
        pass
    if not _load_module():
        return None
    src_tag = _tag("en")
    tgt_tag = _tag(target_lang)
    settings = get_settings()

    try:
        # Prefix each source sentence with its language tag.
        inputs = [_SP.EncodeAsPieces(src_tag + s) for s in sentences]
        results = _MODEL.translate_batch(
            inputs,
            source_lang_id=None,
            target_lang_id=None,
            target_prefix=[[tgt_tag]] * len(sentences),
            beam_size=settings.nllb_beam,
            repetition_penalty=settings.nllb_repetition_penalty,
            max_batch_size=settings.nllb_max_batch,
            max_decoding_length=settings.nllb_max_length,
        )
        out: list[str] = []
        for r in results:
            out.append(_SP.DecodePieces(r.hypotheses[0]).strip())
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"NLLB batch translate failed ({exc}); using fallback")
        return None


@timed(logger_name="processing")
def translate_segments(
    segments: list[dict[str, Any]],
    target_lang: str = "zh",
) -> list[dict[str, Any]] | None:
    """
    Translate a list of subtitle segment dicts to *target_lang* (a
    SUBTITLE_LANGUAGES code, e.g. 'zh').  Returns None if the engine is
    unavailable so the caller falls back to Ollama / original text.

    Preserves segment structure (start/end/confidence); only ``text`` changes.
    """
    if not segments:
        return segments
    sentences = [s.get("text", "").strip() for s in segments if s.get("text", "").strip()]
    if not sentences:
        return segments

    translated = translate_batch(sentences, target_lang)
    if not translated or len(translated) != len(sentences):
        logger.warning(
            f"NLLB returned {len(translated or [])} lines for {len(sentences)} "
            f"sentences; caller will fall back"
        )
        return None

    out: list[dict[str, Any]] = []
    idx = 0
    for seg in segments:
        new_seg = dict(seg)
        if seg.get("text", "").strip() and idx < len(translated):
            new_seg["text"] = translated[idx]
            idx += 1
        out.append(new_seg)
    logger.info(
        f"NLLB translated {idx}/{len(segments)} segments to '{target_lang}' (engine=nllb)"
    )
    return out