"""
AIClipper Transcription Service

Extracts audio from video and runs speech-to-text, returning structured
transcript data with segment- and word-level timestamps.

The PRIMARY engine is *faster-whisper* (CTranslate2). It uses a Silero VAD
(``vad_filter=True``) that silence-gates audio *before* transcription — the
standard fix for Whisper hallucinating filler text ("Thank you for watching",
single repeated words) at clip boundaries and in near-silence.  It also exposes
per-segment confidence (``avg_logprob`` / ``no_speech_prob``) and native
word-level timestamps with probabilities.  If faster-whisper is unavailable the
old *pywhispercpp* (whisper.cpp) engine is used as an automatic fallback so the
pipeline never breaks.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from backend.utils.config import get_settings
from backend.utils.ffmpeg import extract_audio
from backend.utils.logging import get_logger, timed

logger = get_logger("services.transcription")


# ── WhisperX forced alignment (OPTIONAL) ────────────────────────────────

def _try_whisperx_align(
    audio_path: Path,
    segments: list[dict[str, Any]],
    language: str | None,
) -> list[dict[str, Any]] | None:
    """Refine word timestamps via WhisperX forced alignment (wav2vec2 aligner).

    This loads ONLY the wav2vec2 phoneme aligner — NOT a second Whisper ASR — so
    it's memory-frugal.  It forced-aligns the faster-whisper *segments* to the
    audio, producing a flat accurate word-timestamp list.  This is the standard
    fix for Whisper's word-audio desync (words shown slightly early/late, or
    word-level karaoke drifting from the speech).

    Returns a flat list of ``{"word", "start", "end", "probability"}`` or
    ``None`` on ANY failure (missing package, model download error, OOM) so the
    caller always falls back to faster-whisper's native word timestamps —
    transcription never breaks because whisperx is unavailable.
    """
    settings = get_settings()
    if not settings.whisperx_align:
        return None
    # Allow a fast runtime override/env toggle without config reload.
    if not os.environ.get("WHISPERX_ALIGN"):
        return None

    if not segments:
        return None

    # whisperx.environment has heavy imports (torch, librosa) — defer until first
    # use so the normal faster-whisper path never pays that cost.
    try:
        import whisperx  # type: ignore[import-untyped]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"whisperx not available ({exc}); using native word timestamps")
        return None

    align_model = settings.whisperx_align_model
    align_lang = settings.whisperx_language or (language or "en")
    try:
        logger.info(
            f"WhisperX forced alignment: model='{align_model}' language='{align_lang}'"
        )
        audio = whisperx.load_audio(str(audio_path))
        # Segment dicts must look like whisperx's output (start/end/text).
        align_in = [
            {"start": s["start"], "end": s["end"], "text": s.get("text", "")}
            for s in segments if s.get("start") is not None and s.get("end") is not None
        ]
        if not align_in:
            return None
        model_a, metadata = whisperx.load_align_model(
            language_code=align_lang, device="cpu", model_name=align_model
        )
        aligned = whisperx.align(
            align_in, model_a, metadata, audio, device="cpu",
            return_char_alignments=False,
        )
        word_segments = aligned.get("word_segments") or []
        words: list[dict[str, Any]] = [
            {
                "word": (w.get("word", "") or "").strip(),
                "start": round(float(w.get("start", 0.0)), 3),
                "end": round(float(w.get("end", 0.0)), 3),
                "probability": round(float(w.get("score", 0.0) or 0.0), 4),
            }
            for w in word_segments
            if (w.get("word", "") or "").strip()
        ]
        logger.info(
            f"WhisperX aligned {len(words)} words (replacing native word timestamps)"
        )
        return words
    except Exception as exc:  # noqa: BLE001 - alignment must never break the pipeline
        logger.warning(
            f"WhisperX alignment failed ({exc}); using native word timestamps"
        )
        return None


def _interpolate_word_timestamps(
    text: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """
    Split segment text into words and linearly interpolate timestamps.

    Args:
        text: The segment's text string.
        start_ms: Segment start in milliseconds.
        end_ms: Segment end in milliseconds.

    Returns:
        List of word dicts: ``{"word": str, "start": float, "end": float}``
        where times are in **seconds**.
    """
    words = text.split()
    if not words:
        return []

    total_chars = sum(len(w) for w in words)
    if total_chars == 0:
        return []

    duration_ms = end_ms - start_ms
    cursor_ms = start_ms
    result: list[dict[str, Any]] = []

    for word in words:
        word_duration = int(duration_ms * (len(word) / total_chars))
        word_end = cursor_ms + word_duration
        result.append({
            "word": word,
            "start": round(cursor_ms / 1000.0, 3),
            "end": round(word_end / 1000.0, 3),
        })
        cursor_ms = word_end

    # Snap last word's end to segment boundary
    if result:
        result[-1]["end"] = round(end_ms / 1000.0, 3)

    return result


# ── faster-whisper (primary engine) ─────────────────────────────────────

def _faster_whisper_model_identifier(model_name: str | None) -> str:
    """Resolve the model identifier faster-whisper should load.

    faster-whisper (CTranslate2) accepts a model *name* (e.g. "small",
    "medium", "large-v3") or a path to a CTranslate2 model *directory*.  It
    CANNOT load whisper.cpp ``ggml-*.bin`` weights, so when the caller passes a
    ``.bin`` path we fall back to the configured ``whisper_model`` name instead.
    """
    settings = get_settings()
    if model_name:
        m = str(model_name)
        base = m.replace("\\", "/").split("/")[-1]
        if base.lower().endswith(".bin"):
            # ggml-<name>.bin -> <name>  (faster-whisper sizes drop the ggml- prefix)
            name = base[5:] if base.lower().startswith("ggml-") else base
            name = name.split(".")[0]
            if name in {"tiny", "base", "small", "medium", "large",
                        "large-v1", "large-v2", "large-v3",
                        "large-v3-turbo", "distil-large-v3",
                        "tiny.en", "base.en", "small.en", "medium.en"}:
                return name
            return "small"  # unknown .bin -> safe default
        return m  # already a name or a CTranslate2 directory path
    return settings.whisper_model or "small"


def _transcribe_with_faster_whisper(
    audio_path: Path,
    language: str,
    n_threads: int,
    model_name: str | None,
    progress_callback: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Transcribe using faster-whisper. Raises ImportError if not installed.

    ``progress_callback`` is a plain sync callable invoked with a 0.0-1.0
    fraction as chunks of the audio are transcribed, so a long transcription
    can surface live progress instead of appearing frozen.
    """
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    model_ident = _faster_whisper_model_identifier(model_name)
    logger.info(
        f"faster-whisper engine: model='{model_ident}' threads={n_threads} "
        f"language='{language}' vad_filter=True"
    )
    model = WhisperModel(
        model_ident,
        device="cpu",
        compute_type="int8",
        cpu_threads=n_threads,
    )

    transcribe_lang = None if language == "auto" else language
    # vad_filter=True silence-gates audio first -> removes edge-of-clip
    # hallucinations. word_timestamps=True gives native per-word timestamps.
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=transcribe_lang,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=True,
        condition_on_previous_text=False,
    )

    segments: list[dict[str, Any]] = []
    all_words: list[dict[str, Any]] = []
    full_text_parts: list[str] = []

    audio_duration = getattr(info, "duration", None)

    emitted_last = -1.0
    for seg in segments_iter:
        # Live progress: fraction of the audio covered so far.
        if progress_callback is not None and audio_duration:
            frac = min(1.0, max(0.0, float(getattr(seg, "end", 0.0)) / float(audio_duration)))
            if frac - emitted_last >= 0.005 or frac >= 1.0:
                try:
                    progress_callback(frac)
                except Exception:
                    pass
                emitted_last = frac

        text = (getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        avg_logprob = getattr(seg, "avg_logprob", None)
        no_speech_prob = getattr(seg, "no_speech_prob", None)
        segments.append({
            "start": round(float(getattr(seg, "start", 0.0)), 3),
            "end": round(float(getattr(seg, "end", 0.0)), 3),
            "text": text,
            "avg_logprob": round(float(avg_logprob), 4) if avg_logprob is not None else None,
            "no_speech_prob": round(float(no_speech_prob), 4) if no_speech_prob is not None else None,
        })
        full_text_parts.append(text)

        words = getattr(seg, "words", None) or []
        for w in words:
            wtxt = (getattr(w, "word", "") or "").strip()
            if not wtxt:
                continue
            all_words.append({
                "word": wtxt,
                "start": round(float(getattr(w, "start", 0.0)), 3),
                "end": round(float(getattr(w, "end", 0.0)), 3),
                "probability": round(float(getattr(w, "probability", None) or 0.0), 4),
            })

    # Some transcripts return no native word timestamps -> interpolate them.
    if not all_words and segments:
        for seg in segments:
            all_words.extend(_interpolate_word_timestamps(
                seg["text"], int(seg["start"] * 1000), int(seg["end"] * 1000)
            ))

    detected_language = getattr(info, "language", None) or ("en" if language == "auto" else language)

    logger.info(
        f"faster-whisper transcription complete: {len(segments)} segments, "
        f"{len(all_words)} words, language='{detected_language}'"
    )
    return {
        "language": detected_language,
        "segments": segments,
        "full_text": " ".join(full_text_parts),
        "words": all_words,
        "engine": "faster-whisper",
    }


# ── whisper.cpp (pywhispercpp fallback engine) ──────────────────────────

_SAFE_FALLBACK_MODEL = "ggml-small.bin"  # ~466MB — fits 16GB alongside Ollama qwen3:8b


def _load_whisper_model(whisper_model_cls, model_path: str, n_threads: int, depth: int = 0):
    """
    Construct a Whisper model, transparently falling back to the small
    multilingual model if the requested model fails to load.

    WHY: Whisper-medium (1.5GB file + ~3GB runtime) OOM-aborts on this 16GB
    machine while Ollama qwen3:8b is resident.  With the config default now
    small this rarely triggers, but it protects against an explicitly
    requested medium/large model (or a corrupted/missing file) killing the
    whole pipeline instead of degrading to readable captions.

    NOTE: a hard GGML_ASSERT/OOM at the C level aborts the process and can't
    always be caught here; this handles the recoverable load failures.
    """
    fallbacks = [_SAFE_FALLBACK_MODEL]
    if Path(model_path).is_file() and model_path != str(Path(model_path).parent / _SAFE_FALLBACK_MODEL):
        fallbacks.append(model_path)  # keep the original as a last retry
    for candidate in fallbacks:
        candidate_path = (
            str(Path(model_path).parent / candidate)
            if Path(model_path).is_file() and not Path(candidate).is_absolute()
            else candidate
        )
        try:
            return whisper_model_cls(candidate_path, n_threads=n_threads)
        except Exception as exc:  # noqa: BLE001 - recoverable load failure
            logger.warning(
                f"Whisper model load failed ({candidate_path}): {exc}. "
                f"Retrying with a safe model..."
            )
    raise RuntimeError("All Whisper models failed to load.")


def _transcribe_with_pywhispercpp(
    audio_path: Path,
    language: str,
    n_threads: int,
    model_path: str,
    progress_callback: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Transcribe using whisper.cpp via pywhispercpp (fallback engine)."""
    if progress_callback is not None:
        try:
            progress_callback(0.0)
        except Exception:
            pass
    from pywhispercpp.model import Model as WhisperModel  # type: ignore[import-untyped]

    logger.info(
        f"whisper.cpp (fallback) engine: model='{model_path}' threads={n_threads} "
        f"language='{language}'"
    )
    model = _load_whisper_model(WhisperModel, model_path, n_threads)

    # Use language="" (empty string) to let Whisper auto-detect the actual
    # spoken language.
    transcribe_lang = "" if language == "auto" else language
    raw_segments = model.transcribe(str(audio_path), language=transcribe_lang)

    segments: list[dict[str, Any]] = []
    all_words: list[dict[str, Any]] = []
    full_text_parts: list[str] = []

    for seg in raw_segments:
        # pywhispercpp returns t0/t1 in CENTISECONDS (hundredths of a second),
        # NOT milliseconds.  Divide by 100 to get seconds.
        start_s = round(seg.t0 / 100.0, 3)
        end_s = round(seg.t1 / 100.0, 3)
        text = seg.text.strip()
        if not text:
            continue
        # whisper.cpp exposes no confidence -> neutral defaults so the
        # confidence-based hallucination filter leaves real content alone.
        segments.append({
            "start": start_s,
            "end": end_s,
            "text": text,
            "avg_logprob": None,
            "no_speech_prob": None,
        })
        full_text_parts.append(text)
        # Convert centiseconds to milliseconds for _interpolate_word_timestamps
        # (which expects ms and divides by 1000 internally).
        all_words.extend(_interpolate_word_timestamps(text, seg.t0 * 10, seg.t1 * 10))

    # ── Sanity check: timestamps should span a reasonable portion of the audio ──
    # If the last segment's end time is <20% of the audio duration, the
    # centisecond→seconds conversion is likely wrong (e.g. divided by 1000
    # instead of 100).  Log a loud warning so this never silently regresses.
    if segments:
        last_end = segments[-1].get("end", 0)
        try:
            from backend.utils.ffmpeg import get_video_duration
            audio_duration = get_video_duration(audio_path)
            if audio_duration and audio_duration > 0:
                ratio = last_end / audio_duration
                if ratio < 0.2:
                    logger.warning(
                        f"Timestamp sanity check FAILED: last segment ends at "
                        f"{last_end:.1f}s but audio is {audio_duration:.1f}s "
                        f"(ratio={ratio:.2f}).  Timestamps may be compressed — "
                        f"check centisecond→seconds conversion."
                    )
                elif ratio > 5.0:
                    logger.warning(
                        f"Timestamp sanity check FAILED: last segment ends at "
                        f"{last_end:.1f}s but audio is only {audio_duration:.1f}s "
                        f"(ratio={ratio:.2f}).  Timestamps may be expanded."
                    )
                else:
                    logger.info(
                        f"Timestamp sanity check OK: last segment {last_end:.1f}s, "
                        f"audio {audio_duration:.1f}s, ratio={ratio:.2f}"
                    )
        except Exception:
            pass  # duration unavailable — skip check

    detected_language = language
    if language == "auto":
        try:
            _detected = model.auto_detect_language(str(audio_path))
            detected_language = _detected[0][0]  # ((lang, prob), probs_dict)
        except Exception:
            detected_language = "en"

    logger.info(
        f"whisper.cpp transcription complete: {len(segments)} segments, "
        f"{len(all_words)} words, language='{detected_language}'"
    )
    return {
        "language": detected_language,
        "segments": segments,
        "full_text": " ".join(full_text_parts),
        "words": all_words,
        "engine": "whisper.cpp",
    }


@timed(logger_name="processing")
def transcribe_video(
    video_path: Path,
    language: str = "auto",
    model_name: str | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """
    Transcribe a video file and return structured transcript data.

    Uses the faster-whisper engine by default (Silero VAD silence-gating +
    native word timestamps + per-segment confidence).  Falls back to the
    pywhispercpp / whisper.cpp engine when faster-whisper is not installed.

    Args:
        video_path: Path to the source video file.
        language: ISO language code or ``"auto"`` for auto-detection.
        model_name: Whisper model name/path.  For the faster-whisper engine this
            is a model name (e.g. "small") or a CTranslate2 directory.  A
            whisper.cpp ``.bin`` path is translated to its matching name.

    Returns:
        A dict with the following keys::

            {
                "language": str,
                "segments": [
                    {
                        "start": float,
                        "end": float,
                        "text": str,
                        "avg_logprob": float | None,   # faster-whisper only
                        "no_speech_prob": float | None # faster-whisper only
                    }
                ],
                "full_text": str,
                "words": [
                    {"word": str, "start": float, "end": float}
                ],
                "engine": str,   # "faster-whisper" | "whisper.cpp"
            }

    Raises:
        RuntimeError: If neither transcription engine is installed or audio
            extraction fails.
    """
    settings = get_settings()

    # Determine the model path (used by the whisper.cpp fallback).
    if model_name is not None:
        from pathlib import Path as _P
        mp = _P(model_name)
        model_path = str(mp) if mp.is_file() else model_name
    else:
        model_path = str(settings.whisper_model_path)

    # ── 1. Extract audio to a temp WAV ──────────────────────────────────
    temp_dir = Path(tempfile.mkdtemp(prefix="aiclipper_audio_"))
    audio_path = temp_dir / "audio_16k.wav"
    try:
        extract_audio(video_path, audio_path, sample_rate=16000, mono=True)
        logger.info(f"Audio extracted to {audio_path}")

        n_threads = settings.whisper_threads

        # ── 2. Primary: faster-whisper (VAD silence-gating + confidence) ─
        try:
            wt = _transcribe_with_faster_whisper(
                audio_path, language, n_threads, model_name,
                progress_callback=progress_callback,
            )
            # ── 2b. Optional WhisperX forced alignment of word timestamps ──
            # If enabled and whisperx is available, replace faster-whisper's
            # native word timestamps with wav2vec2-aligned ones (better karaoke
            # sync).  Falls back to the native words on any error.
            aligned_words = _try_whisperx_align(
                audio_path, wt.get("segments", []), wt.get("language")
            )
            if aligned_words:
                wt["words"] = aligned_words
                wt["word_engine"] = "whisperx"
            else:
                wt["word_engine"] = wt.get("engine", "faster-whisper")
            return wt
        except ImportError:
            logger.warning(
                "faster-whisper is not installed; falling back to pywhispercpp. "
                "Install it with: pip install faster-whisper"
            )
        except Exception as exc:  # noqa: BLE001 - any ASR failure is recoverable
            logger.warning(
                f"faster-whisper transcription failed ({exc}); "
                f"falling back to pywhispercpp."
            )

        # ── 3. Fallback: whisper.cpp (pywhispercpp) ─────────────────────
        try:
            return _transcribe_with_pywhispercpp(
                audio_path, language, n_threads, model_path,
                progress_callback=progress_callback,
            )
        except ImportError:
            logger.error(
                "Neither faster-whisper nor pywhispercpp is installed. "
                "Install one of: pip install faster-whisper / pip install pywhispercpp"
            )
            raise RuntimeError(
                "Neither faster-whisper nor pywhispercpp is installed."
            )

    finally:
        # ── 4. Clean up temp audio ──────────────────────────────────────
        try:
            if audio_path.exists():
                audio_path.unlink()
            temp_dir.rmdir()
        except OSError as exc:
            logger.warning(f"Temp cleanup failed: {exc}")
