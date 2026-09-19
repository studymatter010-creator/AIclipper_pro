"""
AIClipper Semantic-Boundary Detection Service

PHASE 2 (2026-09-12): detects *meaningful* clip boundaries using a two-step
approach:

1. Words/segs from the ASR (WhisperX-forced-aligned ``words`` when present,
   falling back to faster-whisper word timestamps) are grouped into SENTENCES.
2. ``bge-small-en`` embeddings of each sentence are compared via cosine
   similarity; a sharp topic shift (big drop in sentence-to-sentence
   similarity) marks a "paragraph boundary" in the talk track.

Those boundaries become candidate clip ANCHORS: a clip is well-placed when it
STARTS close to a sentence boundary (you land on a fresh thought, not mid-word)
and ENDS at a boundary + trailing silence gap (the thought resolves rather than
being cut mid-line).

The module deliberately PRODUCES ANCHOR WINDOWS ONLY. Scoring is left entirely
to ``clip_scoring.score_clips`` (unchanged logic) — this service just supplies
better-placed candidate windows than a blind 1-second sliding sweep.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import timed

logger = logging.getLogger("services.semantic_boundaries")

# A sentence-to-sentence embedding cosine below this value at a given seam is a
# strong signal of a topic/paragraph shift worth anchoring on.
_DEFAULT_THRESHOLD = 0.65

# Silence gap (seconds) — a boundary should be placed where a sentence ends AND
# there's a natural pause, so a clip cut doesn't sound clipped. Heuristic when
# we lack real VAD silence data.
_DEFAULT_MAX_SILENCE = 0.5


def _words_by_language(words: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Guess the dominant script from the first words (en vs CJK).

    Returns ``(iso, words)`` where ``iso`` is ``'en'``, ``'zh'``/``'ja'``
    (both take the CJK path) or ``'mixed'``. CJK has no spaces, so the
    sentence-grouping strategy differs (see ``_group_cjk``).
    """
    sample = "".join(
        str(w.get("word", "") or "") for w in words[:80]
    ).strip()
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ")
    if not sample:
        return "en", words
    ratio = cjk / len(sample)
    if ratio > 0.2:
        return "zh", words  # Chinese or Japanese
    return "en", words


def _group_sentences_en(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group word timestamps into sentence spans using punctuation / pauses.

    Each returned sentence: ``{start, end, text}``. ``start`` = first word
    time, ``end`` = last word time + a small spoken tail.
    """
    sentences: list[dict[str, Any]] = []
    if not words:
        return sentences

    cur_words: list[dict[str, Any]] = []
    cur_start: float | None = None

    def _flush() -> None:
        nonlocal cur_words
        if not cur_words:
            return
        text = " ".join(str(w.get("word", "") or "") for w in cur_words).strip()
        sentences.append({
            "start": cur_start,
            "end": cur_words[-1].get("end", cur_words[-1].get("start", 0.0)),
            "text": text,
        })
        cur_words = []

    for w in words:
        wtext = str(w.get("word", "") or "").strip()
        if not wtext:
            continue
        if cur_start is None:
            cur_start = w.get("start", 0.0)
        cur_words.append(w)
        # End of sentence: terminal punctuation, or a long inter-word pause.
        if wtext.endswith((".", "!", "?", "。", "！", "？")):
            _flush()

    if cur_words:
        _flush()

    return sentences


def _group_sentences_cjk(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CJK texts have no spaces, so group by token position + punctuation.

    Consecutive words map to a phrase up to a terminal CJK punctuation mark
    (。！？…) or a long pause. Pragmatic ceiling on tokens per sentence to keep
    embedding batches reasonable.
    """
    sentences: list[dict[str, Any]] = []
    if not words:
        return sentences

    _TOKENS_MAX = 20
    cur_words: list[dict[str, Any]] = []
    cur_start: float | None = None

    def _flush() -> None:
        nonlocal cur_words
        if not cur_words:
            return
        text = "".join(str(w.get("word", "") or "") for w in cur_words).strip()
        sentences.append({
            "start": cur_start,
            "end": cur_words[-1].get("end", cur_words[-1].get("start", 0.0)),
            "text": text,
        })
        cur_words = []

    for w in words:
        wtext = str(w.get("word", "") or "").strip()
        if not wtext:
            continue
        if cur_start is None:
            cur_start = w.get("start", 0.0)
        cur_words.append(w)
        if wtext.endswith(("。", "！", "？", "…")) or len(cur_words) >= _TOKENS_MAX:
            _flush()

    if cur_words:
        _flush()

    return sentences


def group_into_sentences(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort sentence grouping from per-word timestamps.

    Returns ordered ``[{start, end, text}]``. Empty if no usable words.
    """
    if not words:
        return []
    lang, _w = _words_by_language(words)
    if lang == "zh":
        return _group_sentences_cjk(words)
    return _group_sentences_en(words)


def _embed_all(sentences: list[dict[str, Any]]) -> list[list[float]] | None:
    """One batched embedding pass over all sentence texts."""
    from backend.services import embeddings
    return embeddings.embed_sentences([s["text"] for s in sentences])


def detect_semantic_boundaries(
    words: list[dict[str, Any]],
    threshold: float | None = None,
    max_silence: float | None = None,
) -> list[dict[str, Any]]:
    """Detect semantic paragraph boundaries in a spoken track.

    Args:
        words: flat per-word list ``[{word, start, end, probability}]`` (from
            WhisperX forced alignment when available, else faster-whisper).
        threshold: topic-shift cosine threshold (default 0.65).
        max_silence: tolerated end-of-sentence pause (default 0.5s).

    Returns ordered list of boundary dicts ``[{index, time, prev_text,
    next_text, similarity, silence}]`` where ``time`` is a good place to CUT
    (end-of-sentence + natural pause). Empty if unavailable.
    """
    if not words:
        return []
    threshold = float(threshold) if threshold is not None else float(
        get_settings().semantic_threshold
    )
    max_silence = float(max_silence) if max_silence is not None else _DEFAULT_MAX_SILENCE

    sentences = group_into_sentences(words)
    if not sentences:
        logger.info("[SEMANTIC] no sentences grouped from words")
        return []

    logger.info(f"[SEMANTIC] grouped {len(sentences)} sentences from {len(words)} words")

    vecs = _embed_all(sentences)
    if not vecs or len(vecs) < 2:
        logger.info("[SEMANTIC] bge embedding unavailable for word set")
        return []

    from backend.services import embeddings
    anchors = [
        i for i in range(1, len(vecs))
        if embeddings.cosine_similarity(vecs[i - 1], vecs[i]) < threshold
    ]
    if not anchors:
        logger.info("[SEMANTIC] no topic-shift anchors (all seams above threshold)")
        return []

    boundaries: list[dict[str, Any]] = []
    for i in anchors:
        prev = sentences[i - 1]
        nxt = sentences[i]
        # Cut time = end of previous sentence, nudged into the silent gap
        # before the next sentence (capped by max_silence so we never overshoot).
        gap = nxt["start"] - prev["end"]
        cut = prev["end"] + min(max_silence, gap) if gap > 0 else prev["end"]
        boundaries.append({
            "index": i,
            "time": round(cut, 3),
            "prev_text": prev["text"][:80],
            "next_text": nxt["text"][:80],
            "similarity": round(embeddings.cosine_similarity(vecs[i - 1], vecs[i]), 4),
            "silence": round(max(0.0, gap), 3),
        })

    logger.info(f"[SEMANTIC] {len(boundaries)} topic-shift boundaries detected")
    return boundaries


@timed(logger_name="processing")
def semantic_candidate_windows(
    words: list[dict[str, Any]],
    clip_durations: list[int],
    video_duration: float,
    snap_to_silence: bool = True,
    window_pad: float = 0.25,
) -> list[dict[str, Any]]:
    """Build clip candidate windows anchored on semantic boundaries.

    For each requested clip duration and each detected topic-shift boundary,
    emit a candidate whose START snaps to just-past a boundary (so you land on
    a fresh thought at/near a sentence start) and whose END snaps close to the
    NEXT boundary (so the thought resolves). Windows that don't overlap any
    boundary fall back to being skipped — the plain sliding sweep already
    covers the whole timeline, so we only ADD well-placed windows here.

    Returns a list of ``{"start", "end", "duration", "anchor_boundary"}``
    candidate windows (unsorted, de-duplicated). Empty when no boundaries.
    """
    if not words or not clip_durations:
        return []

    boundaries = detect_semantic_boundaries(words)
    if not boundaries:
        return []

    times = [b["time"] for b in boundaries]
    seen: set[tuple[float, float]] = set()
    windows: list[dict[str, Any]] = []

    for dur in clip_durations:
        if dur > video_duration:
            continue
        dur_leeway = float(dur)
        for b in boundaries:
            t = b["time"]
            # Start the window just after a boundary so it opens on the fresh
            # sentence; clamp to [0, video_duration - dur].
            start = min(max(0.0, t + window_pad), max(0.0, video_duration - dur))
            end = start + dur_leeway
            # Prefer to END at the next boundary within a small tolerance so the
            # resolved thought aligns; otherwise keep the fixed duration end.
            for nxt in times:
                if nxt > start and abs((nxt - start) - dur_leeway) <= 1.5:
                    end = nxt
                    break
            if end - start < 10:
                end = start + dur_leeway
            end = min(end, video_duration)
            start = max(0.0, end - dur_leeway)
            key = (round(start, 2), round(end, 2))
            if key in seen:
                continue
            seen.add(key)
            windows.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "anchor_boundary": b.get("index"),
                "semantic_similarity": b.get("similarity"),
            })

    logger.info(
        f"[SEMANTIC] built {len(windows)} semantic-anchored candidate windows "
        f"across durations {clip_durations}"
    )
    return windows