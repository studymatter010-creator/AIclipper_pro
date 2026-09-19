"""
AIClipper Auto-Editor Service

One-click video editing pipeline that produces publish-ready YouTube Shorts:
1. Branded animated intro (3 seconds)
2. Animated word-by-word captions (ASS)
3. Cinematic color grading
4. Enhanced thumbnail with title text
5. AI-generated metadata (title, description, hashtags)

All processing uses FFmpeg (CPU-only, no GPU required).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import shutil
from pathlib import Path
from typing import Any

from backend.utils.config import get_settings
from backend.utils.logging import get_logger, timed
from backend.services.subtitles import (
    _CJK_RE,
    generate_ass_with_highlights,
    generate_ass_from_segments,
    generate_ass_professional,
    translate_segments_to_language,
)
from backend.database import crud
from backend.database.engine import get_session_context

logger = get_logger("services.auto_editor")

# Windows font path (with CJK fallback so Chinese subtitles render correctly)
FONT_FILE = "C:/Windows/Fonts/arial.ttf"
CJK_FONT_FILE = "C:/Windows/Fonts/msyh.ttc"  # Microsoft YaHei

# Part A (2026-09-13): user-selectable caption font families.  A readable name
# (shown in the subtitle style picker) maps to a Windows TrueType/OpenType path
# that FFmpeg's drawtext can load.  Only fonts essentially always present on a
# stock Windows install are listed, so the dropdown never points at a gap and
# there is never anything to auto-download.  (Auto-download is noted as a future
# need for exotic fonts; for ship-ready fonts we simply map to the local file.)
_FONT_MAP: dict[str, str] = {
    "Arial":            "C:/Windows/Fonts/arial.ttf",
    "Arial Black":      "C:/Windows/Fonts/ariblk.ttf",
    "Arial Bold":       "C:/Windows/Fonts/arialbd.ttf",
    "Verdana":          "C:/Windows/Fonts/verdana.ttf",
    "Tahoma":           "C:/Windows/Fonts/tahoma.ttf",
    "Impact":           "C:/Windows/Fonts/impact.ttf",
    "Segoe UI":         "C:/Windows/Fonts/segoeui.ttf",
    "Courier New":      "C:/Windows/Fonts/cour.ttf",
    "Times New Roman":  "C:/Windows/Fonts/times.ttf",
}


def _resolve_caption_font(name: str | None) -> str:
    """Map a caption font **name** (from the style picker) to a font **path**."""
    if not name:
        return FONT_FILE
    if name in _FONT_MAP:
        return _FONT_MAP[name]
    if Path(name).exists():  # already an explicit path
        return name
    return FONT_FILE


# ── Whisper hallucination guard ─────────────────────────────────────────────
# Whisper (especially small) frequently hallucinates the same filler phrases at
# the very start or end of a clip — particularly in the first ~3 seconds of
# near-silence.  These show up as "random lines" that have nothing to do with
# the actual spoken content.  The set below covers the most common English,
# multilingual, and token-level hallucinations seen with pywhispercpp.
_HALLUCINATION_PATTERNS: set[str] = {
    "thank you", "thanks for watching", "thanks for watching!", "thank you for watching",
    "subscribe", "subscribe!", "please subscribe", "subscribe to my channel",
    "like and subscribe", "like and subscribe!",
    "hello", "hello!", "hi", "hi!", "welcome", "welcome!",
    "so", "um", "uh", "hmm", "ok", "okay", "well",
    "bye", "bye!", "goodbye", "see you", "see you!",
    "go", "oh", "ah", "huh", "yeah", "yep", "no", "nope",
    "a", "the", "you", "i", "we", "he", "she", "it",
    "[music]", "[music].", "[music]...", "[silence]",
    "[applause]", "[laughter]", "[noise]",
    "♪", "♪♪", "♪♪♪",
    "the the", "i i", "you you", "we we", "a a",
    "是的", "嗯", "啊", "谢谢", "谢谢观看",
}


def _resolve_caption_source(
    fresh_segs: list[dict],
    fresh_words: list[dict],
    reused_segs: list[dict] | None,
    reused_words: list[dict] | None,
) -> tuple[str, list[dict], list[dict]]:
    """Decide which caption source wins, and WHY (kept for the DEBUG logs).

    Precedence (2026-09-13 Part A): transcript REUSE is now primary.  It slices
    + rebases the stored FULL-VIDEO transcript to the clip's ACTUAL
    (keyframe-snapped) bounds recorded at cut time, so it is both contextually
    accurate AND correctly timed against the rendered clip's first frame — no
    isolated-audio hallucination risk.  FRESH transcription of the clip's audio
    is the last-resort fallback (only when the stored transcript doesn't cover
    the range).

    Note ``auto_edit_clip`` currently decides this inline; this helper is kept
    so the precedence is documented in one place.

    Returns ``(source, segs, words)`` where ``source`` is ``"reuse"``, ``"fresh"``
    or ``"none"``.
    """
    if reused_segs or reused_words:
        return "reuse", reused_segs or [], reused_words or []
    if fresh_segs or fresh_words:
        return "fresh", fresh_segs, fresh_words
    return "none", [], []


def _sanitize_segments(segments: list[dict], clip_duration: float | None = None) -> list[dict]:
    """Strip Whisper hallucinations and fix common timestamp errors.

    Known issue: Whisper hallucinates at clip boundaries (first/last 3 s of
    near-silence), producing garbage like "Thank you for watching" or single
    repeated words.  These show up as "random lines at the beginning" of the
    burned-in subtitles.

    Two complementary hallucination filters are applied:
    * Confidence-based (general): the faster-whisper engine reports per-segment
      ``avg_logprob`` (mean log-probability of the token sequence) and
      ``no_speech_prob`` (probability the audio contains no speech).  Low
      ``avg_logprob`` or high ``no_speech_prob`` reliably identifies fabricated
      text — including *novel* hallucinations a known-phrase list can't catch.
      When these fields are absent (whisper.cpp, or reused transcripts) they are
      ignored, so real content is never dropped.
    * Known-phrase (specific): a blacklist of the most common filler phrases.

    Fixes applied (in order):
    1. Drop segments with empty / whitespace-only text.
    2. Clamp timestamps to [0, clip_duration] when duration is known.
    3. Drop segments faster-whisper flags as likely non-speech (confidence).
    4. Drop segments whose text matches a known hallucination pattern.
    5. Drop segments shorter than 0.08 s (garbled token noise).
    6. Drop consecutive duplicate text entries (stutter hallucinations).
    7. Clamp start < end after all other fixes.
    8. Merge contiguous micro-cues into readable caption lines so dense
       fast-whisper / whisper.cpp output keeps full coverage.
    """
    if not segments:
        return []

    # Confidence thresholds (faster-whisper): avg_logprob below this, or
    # no_speech_prob above this, marks the cue as fabricated/non-speech.
    #
    # Loosened 2026-09-09 from (-1.0 / 0.60) to (-1.5 / 0.75): the stricter
    # values were silently eating REAL speech. Reused transcripts (the stored
    # full-video source) frequently carry no_speech_prob in the 0.60-0.75 band
    # for genuine dialogue at clip boundaries / over pauses, so dropping there
    # produced the "subtitles stop for 16s then reappear" gaps. Looser values
    # keep hallucination removal (avg_logprob still far below real speech) while
    # preserving coverage. Override per-run via env if needed.
    _AVG_LOGPROB_MIN = float(os.environ.get("WHISPER_AVG_LOGPROB_MIN", "-1.5"))
    _NO_SPEECH_PROB_MAX = float(os.environ.get("WHISPER_NO_SPEECH_PROB_MAX", "0.75"))

    cleaned: list[dict] = []
    prev_text = ""
    # Per-reason counters for diagnostics.
    _removed_empty = 0
    _removed_no_speech = 0
    _removed_low_logprob = 0
    _removed_short = 0
    _removed_hallucination = 0
    _removed_prefix = 0
    _removed_duplicate = 0

    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            _removed_empty += 1
            continue

        start = float(seg.get("start", 0))
        end = float(seg.get("end", 0))

        # Clamp to valid clip range.
        if clip_duration and clip_duration > 0:
            start = max(0.0, min(start, clip_duration))
            end = max(0.0, min(end, clip_duration))

        # ── Confidence-based filter (general hallucination detector) ────
        # Only applies when the fields are present (faster-whisper). A cue
        # flagged both "not speech" AND low-confidence is a hallucination.
        has_no_speech = seg.get("no_speech_prob") is not None
        has_logprob = seg.get("avg_logprob") is not None
        if has_no_speech or has_logprob:
            no_speech = float(seg.get("no_speech_prob") or 0.0)
            avg_logprob = float(seg.get("avg_logprob") or 0.0)
            # strong no-speech signal alone is enough to drop
            if has_no_speech and no_speech > _NO_SPEECH_PROB_MAX:
                logger.debug(
                    f"Filtered by no_speech_prob={no_speech:.3f}: {text!r}"
                )
                _removed_no_speech += 1
                continue
            # low log-prob combined with a non-trivial no-speech signal
            if has_logprob and avg_logprob < _AVG_LOGPROB_MIN:
                if not has_no_speech or no_speech > 0.2:
                    logger.debug(
                        f"Filtered by avg_logprob={avg_logprob:.3f}: {text!r}"
                    )
                    _removed_low_logprob += 1
                    continue

        # Duration sanity: drop only truly sub-80ms garbled noise.  Fast-whisper
        # VAD / whisper.cpp legitimately emit cues of 0.05-0.30s containing real
        # speech, so we keep them here and merge them into lines below.
        if end - start < 0.08:
            _removed_short += 1
            continue

        # Hallucination filter (case-insensitive, strip punctuation).
        _norm = text.lower().rstrip(".!?,;:…")
        if _norm in _HALLUCINATION_PATTERNS:
            logger.debug(f"Filtered hallucination: {text!r}")
            _removed_hallucination += 1
            continue
        # Also filter longer text that BEGINS with a hallucination phrase, but
        # only when that phrase is followed by a word boundary (a space).  Using
        # a bare ``startswith(hp)`` is catastrophic: entries like "so", "a",
        # "we", "the", "you", "i" are single tokens, so ANY real word that merely
        # starts with one of them — e.g. "solid"⊃"so", "awesome"⊃"a",
        # "weekend"⊃"we", "their"⊃"the" — would be wrongly dropped, eating
        # genuine spoken content.  Requiring ``hp + " "`` only catches true
        # *phrase* prefixes like "thank you for watching please".
        if len(_norm) < 35 and any(
            _norm.startswith(hp + " ") for hp in _HALLUCINATION_PATTERNS
        ):
            logger.debug(f"Filtered hallucination prefix: {text!r}")
            _removed_prefix += 1
            continue

        # Deduplicate consecutive stutter hallucinations.
        if text.lower() == prev_text.lower():
            logger.debug(f"Filtered duplicate: {text!r}")
            _removed_duplicate += 1
            continue

        # Ensure timestamps are sane.
        if end <= start:
            end = start + 0.5

        cleaned_seg: dict = {"start": start, "end": end, "text": text}
        # Preserve any nested word timestamps (karaoke highlighting) — these may
        # be clip-rebased already; dropping them would lose word-level sync.
        if seg.get("words"):
            cleaned_seg["words"] = seg["words"]
        cleaned.append(cleaned_seg)
        prev_text = text

    n_removed = len(segments) - len(cleaned)
    if n_removed:
        reasons = []
        if _removed_empty:
            reasons.append(f"empty={_removed_empty}")
        if _removed_no_speech:
            reasons.append(f"no_speech={_removed_no_speech}")
        if _removed_low_logprob:
            reasons.append(f"low_logprob={_removed_low_logprob}")
        if _removed_short:
            reasons.append(f"short={_removed_short}")
        if _removed_hallucination:
            reasons.append(f"hallucination={_removed_hallucination}")
        if _removed_prefix:
            reasons.append(f"prefix={_removed_prefix}")
        if _removed_duplicate:
            reasons.append(f"duplicate={_removed_duplicate}")
        reason_str = ", ".join(reasons) if reasons else "unknown"
        logger.info(
            f"Segment sanitization: {len(segments)} → {len(cleaned)} "
            f"({n_removed} removed: {reason_str})"
        )

    # ── Merge dense micro-cues into readable caption lines ─────────────
    # fast-whisper VAD / whisper.cpp emit many tiny contiguous cues.  Merging
    # them yields longer captions that cover the speech instead of a hopeless
    # scatter of half-second flashes.  This is a no-op when cues are already
    # well-formed (long, well-separated segments).
    merged = _merge_adjacent_segments(cleaned)
    if len(merged) != len(cleaned):
        logger.info(
            f"Segment merge: {len(cleaned)} → {len(merged)} caption lines"
        )
        cleaned = merged

    # Final safety net: if we lost nearly everything and some short cues were
    # dropped, re-run the merger over ALL cues (including sub-80ms ones) so we
    # still get coverage rather than zero subtitles.
    non_empty = sum(1 for s in segments if str(s.get("text", "")).strip())
    if non_empty > 0 and len(cleaned) < max(1, int(non_empty * 0.2)) and _removed_short:
        logger.warning(
            f"Sanitizer kept only {len(cleaned)}/{non_empty} — merging all cues "
            f"to preserve coverage"
        )
        cleaned = _merge_adjacent_segments(segments)
        logger.info(f"Post-merge rescue: kept {len(cleaned)} segments")

    return cleaned


def _merge_adjacent_segments(
    segments: list[dict],
    max_shorts_to_merge: int = 1000,  # noqa: ARG001 - reserved
) -> list[dict]:
    """Merge dense, very short speech cues into readable caption lines.

    fast-whisper VAD / whisper.cpp split speech into many tiny cues (often
    0.05-0.25s) that don't carry enough time alone to be sensible subtitles.
    Instead of dropping them (which destroys coverage), we GROUP cues that are
    contiguous in time (gap <= ``_MERGE_GAP``) into a single caption line whose
    start is the first cue's start, end is the last cue's end, and text is the
    joined text.  Short lines of real speech therefore still get shown, and
    coverage approaches the clip duration.
    """
    if not segments:
        return []

    _MERGE_GAP = 0.2   # seconds of silence tolerated between merged cues
    _MAX_LEN = 3.0     # do not merge a line beyond this duration (seconds)
    _MAX_CHARS = 120   # do not merge a line beyond this many characters

    cues = [dict(s) for s in segments if str(s.get("text", "")).strip()]
    cues.sort(key=lambda c: float(c.get("start", 0)))
    if not cues:
        return []

    out: list[dict] = []
    cur: dict | None = None

    for cue in cues:
        if cur is None:
            cur = {
                "start": float(cue.get("start", 0)),
                "end": float(cue.get("end", 0)),
                "text": str(cue.get("text", "")).strip(),
            }
            continue

        gap = float(cue.get("start", 0)) - cur["end"]
        joined_len = len(cur["text"]) + len(str(cue.get("text", "")).strip()) + 1
        if (
            gap <= _MERGE_GAP
            and (float(cue.get("end", 0)) - cur["start"]) <= _MAX_LEN
            and joined_len <= _MAX_CHARS
        ):
            # Contiguous speech — attach to the current line.
            cur["end"] = max(cur["end"], float(cue.get("end", 0)))
            cur["text"] = (cur["text"] + " " + str(cue.get("text", "")).strip()).strip()
        else:
            out.append(cur)
            cur = {
                "start": float(cue.get("start", 0)),
                "end": float(cue.get("end", 0)),
                "text": str(cue.get("text", "")).strip(),
            }

    if cur is not None:
        out.append(cur)

    # Snap each line to [0, clip_duration] is left to the caller; just round.
    for line in out:
        line["start"] = round(line["start"], 3)
        line["end"] = round(line["end"], 3)
    return out


def _rebase_transcript_segments(
    segments: list[dict] | None,
    clip_start: float,
    clip_end: float,
) -> list[dict]:
    """Filter transcript cues overlapping ``[clip_start, clip_end]`` and rebase
    them so ``clip_start`` becomes 0 (clip-relative, the same shape the caption
    pipeline expects from Whisper output).

    Segments that straddle a boundary are trimmed to the window rather than
    dropped, so no spoken content is lost at the edges of the clip.

    Any NESTED word-level timestamps (``seg["words"]``) are rebased and clipped
    the same way as the segment boundaries, so karaoke-style highlighting —
    which reads per-word ``start``/``end`` — stays in sync with the clip-relative
    timeline instead of drifting to absolute full-video offsets.
    """
    if not segments:
        return []
    if clip_end is None or clip_end <= clip_start:
        return []

    duration = round(clip_end - clip_start, 3)
    out: list[dict] = []
    for seg in segments:
        try:
            s = float(seg.get("start", 0))
            e = float(seg.get("end", 0))
        except (TypeError, ValueError):
            continue
        # Keep only cues that overlap the clip's time range.
        if e <= clip_start or s >= clip_end:
            continue
        new = dict(seg)
        new["start"] = round(max(s, clip_start) - clip_start, 3)
        new["end"] = round(min(e, clip_end) - clip_start, 3)
        # Trim segment boundaries to [0, clip_duration] — a segment overhanging
        # the clip edge is clamped, not dropped.
        new["end"] = min(new["end"], duration)
        new["start"] = max(0.0, new["start"])
        if new["end"] <= new["start"]:
            continue
        # Rebase nested per-word timestamps (karaoke highlighting) so they stay
        # clip-relative too, not absolute full-video offsets.
        nested = seg.get("words")
        if nested:
            rebased_words: list[dict] = []
            for w in nested:
                try:
                    ws = float(w.get("start", 0))
                    we = float(w.get("end", 0))
                except (TypeError, ValueError):
                    continue
                if we <= clip_start or ws >= clip_end:
                    continue
                nw = dict(w)
                nw["start"] = round(max(ws, clip_start) - clip_start, 3)
                nw["end"] = round(min(we, clip_end) - clip_start, 3)
                nw["end"] = min(nw["end"], duration)
                nw["start"] = max(0.0, nw["start"])
                if nw["end"] <= nw["start"]:
                    continue
                rebased_words.append(nw)
            if rebased_words:
                new["words"] = rebased_words
        out.append(new)
    return out


def get_clip_transcript(clip, transcript) -> tuple[list[dict] | None, list[dict] | None]:
    """Slice + rebase the stored full-video transcript to the clip's window.

    This is the SAME approach the main clip-generation pipeline (Path A) uses:
    the full-video transcript is captured with Whisper having the whole video as
    context, so the words are accurate.  We reuse that source of truth instead
    of re-running Whisper on the isolated clip (which hallucinates filler text
    at clip boundaries with no surrounding context).

    Returns ``(segments, words)`` in clip-relative 0-based time — the exact
    shape ``auto_edit_clip`` expects from fresh Whisper output — or
    ``(None, None)`` when there is no stored transcript covering the clip's
    time range (the caller then falls back to re-transcription).

    Args:
        clip: A ``Clip`` ORM object (needs ``start_time`` / ``end_time``).
        transcript: A ``Transcript`` ORM object (or ``None``) for ``clip.video_id``.
    """
    if transcript is None:
        return None, None

    # Rebase against the ACTUAL (keyframe-snapped) clip bounds recorded at cut
    # time, falling back to the requested window.  The rendered clip's first
    # frame sits at ``achieved_start_time`` (a little BEFORE ``start_time``), so
    # rebasing against the requested value made captions land EARLY by that
    # offset.  Part A: use the achieved truth (Part A) whenever we have it.
    clip_start = getattr(clip, "achieved_start_time", None)
    if clip_start is None:
        clip_start = getattr(clip, "start_time", None)
    clip_end = getattr(clip, "achieved_end_time", None)
    if clip_end is None:
        clip_end = getattr(clip, "end_time", None)
    if clip_start is None or clip_end is None or clip_end <= clip_start:
        return None, None

    # content_json = list of {start, end, text}; tolerate a wrapped dict too.
    raw_segs = transcript.content_json or []
    if isinstance(raw_segs, dict):
        raw_segs = raw_segs.get("segments") or raw_segs.get("content") or []
    raw_words = transcript.word_timestamps_json or []
    if isinstance(raw_words, dict):
        raw_words = raw_words.get("words") or raw_words.get("word_timestamps") or []

    segs = _rebase_transcript_segments(raw_segs, clip_start, clip_end)
    words = _rebase_transcript_segments(raw_words, clip_start, clip_end)

    if not segs and not words:
        return None, None
    return segs, words


def _get_creation_flags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def _run_ffmpeg_safe(args: list[str], description: str = "FFmpeg") -> None:
    settings = get_settings()
    cmd = [settings.ffmpeg_path] + args
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
            creationflags=_get_creation_flags(),
        )
        if result.returncode != 0:
            # Log the FULL stderr so we can diagnose failures from logs alone.
            stderr_full = (result.stderr or "").strip()
            if stderr_full:
                for line in stderr_full.split("\n")[-20:]:
                    logger.error(f"  FFmpeg stderr: {line}")
            real_errors = [
                line for line in stderr_full.split("\n")
                if any(kw in line.lower() for kw in ["error", "invalid", "no such", "failed"])
                and "fontconfig" not in line.lower()
            ]
            msg = f"{description} failed (rc={result.returncode})"
            if real_errors:
                msg += f": {'; '.join(real_errors[:5])}"
            logger.error(msg)
            raise RuntimeError(msg)
        else:
            logger.debug(f"{description} succeeded")
    except FileNotFoundError:
        raise RuntimeError(f"FFmpeg not found at '{settings.ffmpeg_path}'.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{description} timed out.")


def _escape_drawtext(text: str) -> str:
    text = text.replace("\\", "").replace("'", "").replace('"', "")
    text = text.replace(":", " ").replace(";", " ").replace("%", " pct")
    return text[:57] + "..." if len(text) > 60 else text


def _ass_filter_supported() -> bool:
    """Does the configured FFmpeg build the ``ass`` filter (libass + fontconfig)?

    Phase 4/5 prerequisite: ASS burn can only succeed (and thus the
    'ASS-success-not-fallback' criterion can PASS) when this filter exists.
    Cached per process.
    """
    if hasattr(_ass_filter_supported, "_cached"):
        return _ass_filter_supported._cached  # type: ignore[attr-defined]
    settings = get_settings()
    try:
        result = subprocess.run(
            [settings.ffmpeg_path, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=60,
            creationflags=_get_creation_flags(),
        )
        out = (result.stdout or "") + (result.stderr or "")
        supported = " ass " in f" {out} " or "\n ass" in out
    except Exception:  # noqa: BLE001
        supported = False
    logger.info(f"[ASS CAPABILITY] ffmpeg ass/libass+fontconfig supported={supported}")
    _ass_filter_supported._cached = supported  # type: ignore[attr-defined]
    return supported


def _check_render_capabilities() -> None:
    """Log the caption-rendering capabilities so boot/edit logs show exactly
    which renderer can succeed (Phase 4/5 verification aid)."""
    try:
        _ass_filter_supported()
    except Exception:  # noqa: BLE001
        pass


def _smoke_test_drawtext(src: Path) -> bool:
    """One-shot test: can FFmpeg burn a simple drawtext onto this clip?

    Creates a 2-second test clip with a single drawtext overlay.  If this
    fails, the drawtext fallback path is broken on this FFmpeg build and
    subtitles will never render.  Result is cached so we only test once per
    process lifetime.
    """
    if not hasattr(_smoke_test_drawtext, "_cached"):
        _smoke_test_drawtext._cached = None  # type: ignore[attr-defined]
    if _smoke_test_drawtext._cached is not None:  # type: ignore[attr-defined]
        return _smoke_test_drawtext._cached  # type: ignore[attr-defined]

    settings = get_settings()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    test_out = settings.temp_dir / "drawtext_smoke_test.mp4"
    font_path = Path(FONT_FILE)
    font_esc = font_path.as_posix().replace(":", "\\:") if font_path.exists() else FONT_FILE

    # Simple drawtext — no timing gate, just test the filter renders at all.
    vf = (
        f"drawtext=fontfile='{font_esc}':text='TEST':"
        f"fontsize=48:fontcolor=white:borderw=3:bordercolor=black:"
        f"x=(w-text_w)/2:y=h-th-50"
    )
    cmd = [
        settings.ffmpeg_path,
        "-i", str(src.resolve()),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "copy",
        "-t", "2", "-y", str(test_out.resolve()),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            creationflags=_get_creation_flags(),
        )
        ok = result.returncode == 0 and test_out.exists() and test_out.stat().st_size > 100
        if ok:
            logger.info("[DRAWTEXT SMOKE] PASS — drawtext filter works on this FFmpeg build")
        else:
            logger.error(
                f"[DRAWTEXT SMOKE] FAIL — drawtext filter is BROKEN on this FFmpeg build. "
                f"rc={result.returncode}"
            )
            for line in (result.stderr or "").split("\n")[-10:]:
                if line.strip():
                    logger.error(f"  [DRAWTEXT SMOKE] {line.strip()}")
        _smoke_test_drawtext._cached = ok  # type: ignore[attr-defined]
        return ok
    except Exception as exc:
        logger.error(f"[DRAWTEXT SMOKE] FAIL — exception: {exc}")
        _smoke_test_drawtext._cached = False  # type: ignore[attr-defined]
        return False
    finally:
        try:
            test_out.unlink(missing_ok=True)
        except OSError:
            pass


@timed(logger_name="processing")
def generate_intro(
    source_clip: Path,
    output_path: Path,
    duration: float = 3.0,
    watermark: str = "",
) -> Path:
    """
    Generate a cinematic "scene preview" intro from the source clip itself.

    Rather than a flat text title-card, this picks the single most visually
    interesting frame in the clip (via the FFmpeg ``thumbnail`` filter) and
    animates it with a slow push-in (Ken Burns) zoom.  The result looks like
    the opening of the original video and flows seamlessly into the clip.

    Args:
        source_clip: The generated clip to pull the opening frame from.
        output_path: Destination ``.mp4`` path.
        duration: Intro length in seconds.
        watermark: Optional small top-left channel watermark text (e.g. "@handle").

    Returns:
        Path to the generated intro.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_frame = output_path.with_name(output_path.stem + "_frame.png")
    fps = 30
    frames = int(duration * fps)

    # ── 1. Grab the most representative frame ────────────────────────────
    grab_args = [
        "-i", str(source_clip.resolve()),
        "-vf", "thumbnail=30",
        "-frames:v", "1",
        "-q:v", "2",
        "-y", str(tmp_frame.resolve()),
    ]
    _run_ffmpeg_safe(grab_args, "Extract intro frame")

    # ── 2. Ken-Burns slow zoom over the extracted frame ──────────────────
    #      Crop to 9:16, then push-in from center for a natural opening.
    #      Add a subtle dark gradient at the top for channel watermark.
    overlay_wm = ""
    if watermark:
        wm = _escape_drawtext(watermark)
        fe = FONT_FILE.replace(":", "\\:")
        overlay_wm = (
            f",drawtext=fontfile='{fe}':text='{wm}':fontsize=34"
            f":fontcolor=white@0.85:x=40:y=40:borderw=2:bordercolor=black@0.6"
        )
    zoom_vf = (
        "scale=2160:3840:force_original_aspect_ratio=increase,"
        "crop=2160:3840,"
        f"zoompan=z='min(zoom+0.0010,1.14)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s=1080x1920:fps={fps}"
        f"{overlay_wm}"
    )
    args = [
        "-loop", "1", "-i", str(tmp_frame.resolve()),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-map", "0:v", "-map", "1:a",
        "-vf", zoom_vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-ar", "44100",
        "-t", str(duration), "-y", str(output_path.resolve()),
    ]
    try:
        _run_ffmpeg_safe(args, "Generate scene-preview intro")
        if not output_path.exists() or output_path.stat().st_size < 100:
            raise RuntimeError("Intro generation produced no output")
        logger.info(f"Scene-preview intro generated: {output_path.stat().st_size} bytes")
        return output_path
    finally:
        try:
            if tmp_frame.exists():
                tmp_frame.unlink()
        except OSError:
            pass


# Regex used to detect CJK (Chinese/Japanese/Korean) for font selection in the
# drawtext caption fallback (Arial has no CJK glyphs).
_CJK_CAPTION_RE = re.compile(r"[一-鿿㐀-䶿぀-ヿ가-힯]")


def _dt_caption_escape(text: str) -> str:
    """Escape caption text for embedding inside an FFmpeg drawtext text= option."""
    s = (
        text.replace("%", " ")
        .replace("\\", "")
        .replace("'", "")
        .replace('"', "")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", " ")
    )
    return s[:90].strip()


# ── Text width fitting (drawtext overflow guard) ──────────────────────────
# drawtext has NO notion of text width: a long line simply runs past the frame
# edge (the "secret imposter sabotages and commits n…" bug).  So before we emit
# a drawtext filter we measure the RENDERED pixel width and, if it exceeds the
# usable canvas, wrap to up to 2 lines and / or shrink the font size.  As a last
# resort a too-wide word is hard-clamped with an ellipsis so no caption line can
# ever run off-canvas.
#
# Measurement uses PIL's FreeType rasteriser (FontMetrics) because it shares
# metrics with FFmpeg's drawtext (both use FreeType), so the prediction is close
# to what drawtext actually draws. Where the font can't be loaded (tests / CI
# without Windows fonts) we fall back to a deliberately *conservative* estimate
# (it over-measures), which errs on the side of wrapping more / smaller — never
# overflow.

_FONT_MEASURE_CACHE: dict[tuple[str, int], Any] = {}


def _make_text_measure(font_name: str, size: int) -> Any:
    """Return ``measure(text) -> pixel_width`` for ``font_name`` at ``size``.

    Caches the loaded FreeType font so per-cue / per-size measurements don't
    reload the font object repeatedly.
    """
    key = (font_name, int(size))
    cached = _FONT_MEASURE_CACHE.get(key)
    if cached is not None:
        return cached
    measure = None
    try:
        from PIL import ImageFont

        font = ImageFont.truetype(font_name, int(size))

        def _m(text: str) -> float:
            return float(font.getlength(str(text or "")))

        measure = _m
    except Exception:  # noqa: BLE001 - font missing/unreadable -> heuristic fallback
        # Conservative per-char estimate (over-measures => safer).
        def _m(text: str) -> float:
            return 0.60 * int(size) * len(str(text or "")) + 8.0

        measure = _m
    _FONT_MEASURE_CACHE[key] = measure
    return measure


def _clamp_ellipsis(word: str, measure: Any, max_width: float) -> str:
    """Shrink *word* so ``measure(word + ellipsis) <= max_width``; at minimum
    return a single glyph. Never returns text wider than the canvas."""
    if measure(word) <= max_width:
        return word
    # Not even one glyph + ellipsis fits -> return a single glyph at most.
    if measure(word[:1] + "…") > max_width:
        return word[:1]
    lo, hi, best = 1, len(word), word[:1] + "…"
    while lo <= hi:
        mid = (lo + hi) // 2
        if measure(word[:mid] + "…") <= max_width:
            best, lo = word[:mid] + "…", mid + 1
        else:
            hi = mid - 1
    return best


def _wrap_text(text: str, measure: Any, max_width: float, ascii_only: bool = False) -> list[str]:
    """Greedy-wrap *text* to lines each <= *max_width*, capped at 2 lines.

    CJK text has no word spaces, so it's broken by character run.  Any single
    token wider than the canvas is hard-clamped with an ellipsis.  Returns at
    most 2 lines; if the input needs more, the excess is folded into the second
    line up to the cap (callers shrink the font to fully fit).
    """
    text = (str(text or "")).strip()
    if not text:
        return []
    if measure(text) <= max_width:
        return [text]
    # A single unbroken token (a long English word or a CJK run — no spaces)
    # becomes ONE ellipsized, always-fitting line.  Breaking it across lines
    # leaves a broken fragment ("superca/lifragi") with no cut marker, which is
    # worse than a clean "supercali…".
    if " " not in text:
        return [_clamp_ellipsis(text, measure, max_width)]
    words = text.split()
    # Balanced 2-line reflow: find the word split that minimizes the width
    # difference between the two lines, while keeping both within max_width.
    # This produces visually balanced headlines instead of lopsided splits.
    best_split = None
    best_diff = float("inf")
    for split in range(1, len(words)):
        l1 = " ".join(words[:split])
        if measure(l1) > max_width:
            break  # any larger split only makes line 1 wider
        l2 = " ".join(words[split:])
        if measure(l2) <= max_width:
            diff = abs(measure(l1) - measure(l2))
            if diff < best_diff:
                best_diff = diff
                best_split = split
    if best_split is not None:
        return [" ".join(words[:best_split]), " ".join(words[best_split:])]
    # No balanced 2-line split exists (too much text for the canvas).  Fill line
    # 1 as far as it will go, then put the ellipsized remainder on line 2 — the
    # canonical "hard clamp with ellipsis as a last resort".
    carry = 0
    for k in range(len(words), 0, -1):
        if measure(" ".join(words[:k])) <= max_width:
            carry = k
            break
    l1 = " ".join(words[:carry]) if carry else _clamp_ellipsis(words[0], measure, max_width)
    if carry >= len(words):
        return [l1]
    tail = " ".join(words[carry:])
    l2 = _clamp_ellipsis(tail, measure, max_width)
    return [l1, l2]


def _fit_caption(
    text: str, font_path: str, max_width: float,
    base_size: int = 60, min_size: int = 30,
) -> tuple[int, list[str]]:
    """Pick a ``(fontsize, lines)`` so *text* renders within *max_width*.

    Tries progressively smaller sizes, wrapping to 2 lines at each size; returns
    the first size whose 2-line wrap fully fits, else the smallest size with the
    hard-clamped wrap (guaranteed to stay on-canvas).
    """
    # Part 1 (2026-09-12): fine 4px step-down from base_size down to min_size so
    # a headline shrinks just enough to fit (instead of the old coarse base->0.85
    # ->0.7->0.55 jumps that dropped the type size more than necessary). Ellipsis
    # truncation is reserved for the absolute last resort in the hard fallback.
    step = 4
    for size in range(base_size, min_size - 1, -step):
        if size <= 0:
            continue
        measure = _make_text_measure(font_path, size)
        lines = _wrap_text(text, measure, max_width)
        if lines and all(measure(ln) <= max_width for ln in lines):
            # Part 1 (2026-09-12): only a FULL wrap (no ellipsis truncation) is
            # acceptable above the minimum size.  An ellipsized line means the
            # text couldn't fit at this size, so we STEP DOWN instead of showing
            # a truncated headline at a bigger size.  Ellipsis is the absolute
            # last resort, applied only at ``min_size``.
            if size > min_size and any("\u2026" in ln for ln in lines):
                continue
            return size, lines
    # Hard fallback: smallest size, clamped lines (always on-canvas).
    measure = _make_text_measure(font_path, min_size)
    return min_size, _wrap_text(text, measure, max_width, ascii_only=True)


def _build_drawtext_caption_vf(
    segments: list[dict],
    output_width: int,
    output_height: int,
    font: str | None = None,
    color: str | None = None,
    size_scale: float = 1.0,
    position: str = "bottom",
    outline: bool = True,
) -> str:
    """Build a `-vf` drawtext chain (one caption per segment).

    Used as a guaranteed fallback when the `ass` filter is unavailable or
    broken on a given FFmpeg build.  Provides readable, styled captions that
    always render because they only rely on the plain `drawtext` filter.

    Part A (2026-09-13): the drawtext path now honours the subtitle *style*
    picker — a user-chosen size multiplier, vertical position (bottom/top/
    center) and outline toggle, plus a resolved caption font family.  This is
    the renderer that actually ships on the user's Windows FFmpeg build (ASS
    fails on libass-without-fontconfig), so making IT style-aware is what makes
    the live preview match the real output.
    """
    if not segments:
        return ""
    sample = " ".join(str(s.get("text", "")) for s in segments[:5])
    if _CJK_CAPTION_RE.search(sample):
        font_path = CJK_FONT_FILE
    else:
        font_path = _resolve_caption_font(font or "Arial")
    font_esc, _ = _font_path(font_path, FONT_FILE)

    # Honour a user-picked text colour (e.g. "#FFFFFF" -> "0xFFFFFF").
    raw = (color or "").strip()
    fontcolor = ("0x" + raw[1:]) if raw.startswith("#") and len(raw) >= 7 else (raw or "white")

    # Outline stylistic treatment: strong border + drop shadow vs a thin border.
    if outline:
        stroke = "borderw=5:bordercolor=black@0.75:shadowcolor=black@0.55:shadowx=3:shadowy=3:"
    else:
        stroke = "borderw=2:bordercolor=black@0.60:"

    vf: list[str] = []
    # Hard bound on the number of drawtext filters (each is a per-frame
    # evaluation and very large graphs can choke FFmpeg).  IMPORTANT (2026-09-13):
    # the OLD code returned early at 80 lines, silently DROPPING every caption
    # past the 80th — because segments are time-ordered, that truncated the END
    # of the clip, producing the classic "subtitles vanish in the last few
    # seconds" failure on cue-dense shorts.  Now we shed the OLDEST lines so the
    # newest (tail = clip ending) always survive; the end of the clip is never
    # truncated.
    _MAX_LINES = 120
    _shed_head = 0
    # Usable canvas width: leave a small horizontal margin so text never touches
    # the frame edge (previously long lines ran off BOTH sides).
    max_width = float(output_width) * 0.90
    line_gap = 6
    for seg in segments:
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", 0))
        except (TypeError, ValueError):
            continue
        text = str(seg.get("text", "")).strip()
        if not text or end <= start:
            continue
        # Measure + fit the rendered text so no caption line exceeds the frame.
        fit_size, lines = _fit_caption(text, font_path, max_width)
        # Style size multiplier on top of auto-fit, clamped to a sane range.
        size = max(16, min(200, int(round(fit_size * size_scale))))
        line_h = int(size * 1.4)  # estimated rendered line height for stacking
        for i, line in enumerate(lines):
            esc = _dt_caption_escape(line)
            if not esc:
                continue
            y = _caption_position_y(position, len(lines), i, line_h, line_gap)
            vf.append(
                f"drawtext=fontfile='{font_esc}':text='{esc}':"
                f"fontsize={size}:fontcolor={fontcolor}:"
                f"{stroke}"
                f"x=(w-text_w)/2:y={y}:"
                f"enable='between(t,{start:.2f},{end:.2f})'"
            )
            if len(vf) > _MAX_LINES:
                vf.pop(0)  # drop the OLDEST cue, never the tail
                _shed_head += 1
    if _shed_head:
        logger.warning(
            f"[SUBTITLE DEBUG] drawtext graph exceeded {_MAX_LINES} caption lines; "
            f"shed {_shed_head} OLDEST cue(s) so the clip's END keeps subtitles"
        )
    else:
        logger.info(
            f"[SUBTITLE DEBUG] drawtext caption lines: {len(vf)} (end-of-clip "
            f"preserved; last cue enabled to {segments[-1].get('end') if segments else 'n/a'}s)"
        )
    return ",".join(vf)


def _caption_position_y(
    position: str, n_lines: int, i: int, line_h: int, line_gap: int
) -> str:
    """Return an FFmpeg ``y=`` expression for a caption line's vertical position.

    ``position`` is ``bottom`` (default), ``top`` or ``center``.  ``n_lines`` is
    the number of stacked lines for the cue and ``i`` the 0-based line index.
    The expressions use FFmpeg's runtime values (``h`` frame height, ``th`` text
    height) so they adapt to any resolution.
    """
    if position == "top":
        if n_lines == 1:
            return "150"
        return f"{170 + i * (line_h + line_gap)}"
    if position == "center":
        if n_lines == 1:
            return "(h-th)/2"
        block = n_lines * line_h + (n_lines - 1) * line_gap
        return f"(h-{block})/2+{i * (line_h + line_gap)}"
    # bottom (default): leave a ~150px margin below the text baseline zone
    if n_lines == 1:
        return "h-th-150"
    # Stack the two lines so the WHOLE caption sits at the same baseline zone
    # as a single-line caption (y grows downward).
    return f"h-{int((n_lines - i) * line_h + (n_lines - 1 - i) * line_gap)}"


def _validate_ass_file(ass_path: Path) -> tuple[bool, str]:
    """Validate an ASS subtitle file before burning.

    Checks:
    - File exists and is non-empty
    - Contains [Script Info] and [Events] sections
    - At least one Dialogue line with valid H:MM:SS.cc timestamps
    - No NaN or negative timestamps in Dialogue entries

    Returns ``(is_valid, error_message)``.
    """
    if not ass_path.exists():
        return False, f"ASS file does not exist: {ass_path}"
    try:
        content = ass_path.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"Cannot read ASS file: {exc}"

    if not content.strip():
        return False, "ASS file is empty"

    if "[Script Info]" not in content:
        return False, "ASS file missing [Script Info] section"

    if "[Events]" not in content:
        return False, "ASS file missing [Events] section"

    # Check for at least one Dialogue line with valid timestamps.
    import re as _re
    ts_re = _re.compile(r"Dialogue:\s*\d+,(\d+:\d{2}:\d{2}\.\d{2}),(\d+:\d{2}:\d{2}\.\d{2})")
    matches = ts_re.findall(content)
    if not matches:
        return False, "ASS file has no Dialogue lines with valid timestamps"

    # Validate each timestamp: no NaN, no negative effective seconds.
    def _ass_to_seconds(ts: str) -> float:
        """Parse ASS ``H:MM:SS.cc`` → seconds.  ``:`` separates H, MM, ``SS.cc``."""
        parts = ts.split(":")
        if len(parts) != 3:
            raise ValueError(f"expected H:MM:SS.cc, got {ts!r}")
        h = int(parts[0])
        m = int(parts[1])
        s_cc = parts[2].split(".")
        if len(s_cc) != 2:
            raise ValueError(f"expected SS.cc, got {parts[2]!r}")
        s = int(s_cc[0])
        cc = int(s_cc[1])
        return h * 3600.0 + m * 60.0 + s + cc / 100.0

    for start_str, end_str in matches:
        try:
            s = _ass_to_seconds(start_str)
            e = _ass_to_seconds(end_str)
            if s != s or e != e:  # NaN check
                return False, f"NaN timestamp in ASS: start={start_str} end={end_str}"
            if e <= s:
                return False, f"Zero/negative duration in ASS: {start_str} → {end_str}"
        except (ValueError, IndexError):
            return False, f"Malformed timestamp in ASS: {start_str} / {end_str}"

    logger.debug(
        f"ASS validation passed: {len(matches)} dialogue cues, "
        f"[Script Info] + [Events] present"
    )
    return True, ""


def _burn_captions_pass(src: Path, dst: Path, vf: str) -> bool:
    """Run a caption-burn encode; return True if it produced output.

    NOTE (2026-09-12): audio is RE-ENCODED (not ``-c:a copy``). Copying audio
    that came from a looped/replaced music track (Audio Remix) carries broken
    packet timestamps, which makes players freeze after a few seconds even
    though the file probes cleanly. Re-encoding guarantees clean AV sync.
    """
    args = [
        "-i", str(src.resolve()), "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-y", str(dst.resolve()),
    ]
    _run_ffmpeg_safe(args, "Burn Captions")
    return dst.exists() and dst.stat().st_size > 100


# ── ASS burn failure diagnostics ───────────────────────────────────────────
# The `ass` filter has been failing silently on the user's Windows FFmpeg build
# for a long time (that's WHY drawtext is the reliable fallback).  We never knew
# the actual reason because the exception message alone ("Invalid argument") is
# uninformative.  Instead of just "ASS failed, trying drawtext", we capture the
# full FFmpeg stderr from the ASS attempt and classify the REAL root cause so
# it's visible in the logs next time.

def _classify_ass_failure(stderr: str) -> str:
    """Turn raw FFmpeg stderr from an ``ass`` filter attempt into a readable
    root-cause line (e.g. "ass filter not compiled" vs "libass built without
    fontconfig" vs a render/timing error).

    NOTE: FFmpeg always prints its build configuration (including
    ``--enable-fontconfig``) in stderr, so "fontconfig" appearing in stderr
    does NOT mean fontconfig is the problem.  We check for specific *errors*
    first, and only fall through to the fontconfig diagnosis if no earlier
    error matches."""
    s = (stderr or "").lower()
    if "no such filter" in s:
        return "ASS filter is NOT compiled into this FFmpeg build (no such filter 'ass')"
    # Check for original_size / option errors BEFORE fontconfig — FFmpeg's
    # build config always mentions fontconfig even when it's not the cause.
    if "unable to parse" in s and "original_size" in s:
        return "ASS filter could not parse original_size — likely a path/quoting issue in the filter string"
    if "invalid argument" in s and "original_size" in s:
        return "ASS filter rejected original_size option (wrong value — expected WxH, got something else)"
    if "libass" in s and ("error" in s or "invalid" in s):
        return "libass render error (see FFmpeg stderr lines below)"
    if "fontconfig" in s or "font-config" in s:
        return "libass was built WITHOUT fontconfig -> cannot resolve fonts (this is the classic Windows-build ASS killer)"
    if s.strip():
        return f"ASS burn failed at the FFmpeg level (see stderr lines below)"
    return "ASS burn failed with NO stderr captured"


def _attempt_ass_burn(
    src: Path, dst: Path, ass_path: Path,
    output_width: int, output_height: int,
) -> bool:
    """Try to burn an ASS subtitle file; on failure log the classified root
    cause + full stderr of THIS attempt. Returns True only on success."""
    settings = get_settings()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    tmp_ass = settings.temp_dir / f"{dst.stem}_captions.ass"
    shutil.copyfile(str(ass_path), str(tmp_ass))
    # Quote the filename so Windows drive-colon paths (C:/...) don't break
    # FFmpeg's filter-option parser, which uses unescaped colons as separators.
    # Single-quoted filenames are treated as literals by FFmpeg's option parser.
    sub_str = "'" + tmp_ass.as_posix().replace("'", "\\'") + "'"
    capt_vf = f"ass={sub_str}:original_size={output_width}x{output_height}"
    cmd = [settings.ffmpeg_path, "-i", str(src.resolve()), "-vf", capt_vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-c:a", "aac", "-b:a", "192k", "-y", str(dst.resolve())]
    result = None
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
            creationflags=_get_creation_flags(),
        )
    except FileNotFoundError:
        logger.error("ASS burn aborted: FFmpeg not found. ASS filter unavailable.")
        return False
    except subprocess.TimeoutExpired:
        logger.error("ASS burn aborted: FFmpeg timed out during ASS burn.")
        return False
    finally:
        try:
            if tmp_ass.exists():
                tmp_ass.unlink()
        except OSError:
            pass

    if result.returncode == 0 and dst.exists() and dst.stat().st_size > 100:
        return True

    stderr_full = (result.stderr or "").strip()
    # Highlight the single most useful error line (filter/format errors) first.
    real_errs = [
        ln for ln in stderr_full.split("\n") if ln.strip()
        if any(kw in ln.lower() for kw in ["error", "invalid", "no such", "filter", "fontconfig", "libass"])
    ]
    reason = _classify_ass_failure(stderr_full)
    logger.error(f"ASS burn FAILED — ROOT CAUSE: {reason}")
    for line in real_errs[:5]:
        logger.error(f"  ASS attempt stderr: {line.strip()}")
    if stderr_full:
        for line in stderr_full.split("\n")[-15:]:
            logger.error(f"  ASS attempt stderr: {line}")
    return False


def _group_words_for_caption(words: list[dict] | None, words_per_line: int = 5) -> list[dict]:
    """Group word-level timestamp entries into short caption phrases.

    Some transcripts carry only ``word_timestamps_json`` (a list of
    ``{word, start, end}``) with an empty ``content_json``.  The captions
    pipeline needs segment-style entries to burn text, so we chunk those
    words into readable phrases of ``words_per_line``.

    Returns a list of ``dict(start, end, text)`` (absolute timestamps) or an
    empty list when there is nothing usable.
    """
    if not words:
        return []
    clean = []
    for w in words:
        try:
            start = float(w.get("start", 0))
            end = float(w.get("end", 0))
        except (TypeError, ValueError):
            continue
        text = str(w.get("word", "")).strip()
        if not text or end <= start:
            continue
        clean.append({"start": start, "end": end, "text": text})
    if not clean:
        return []

    grouped: list[dict] = []
    for i in range(0, len(clean), words_per_line):
        chunk = clean[i:i + words_per_line]
        grouped.append({
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "text": " ".join(w["text"] for w in chunk),
        })
    return grouped


@timed(logger_name="processing")
def apply_effects(
    input_path: Path,
    output_path: Path,
    ass_path: Path | None,
    energy_peaks: list[float] | None = None,
    caption_segments: list[dict] | None = None,
    caption_font: str | None = None,
    caption_color: str | None = None,
    output_width: int = 1080,
    output_height: int = 1920,
    with_glow: bool = False,
) -> Path:
    """
    Apply the cinematic effects pass.

    Grades, sharpens, burns captions and adds a subtle continuous camera
    "micro-zoom" so static clips keep motion.  When ``energy_peaks`` are given
    (a waveform envelope for the clip) the zoom pushes in harder on loud
    moments, giving the clip a lip-sync, beat-reactive feel.

    When ``with_glow`` is true the ASS subtitle renderer adds a soft glow
    layer (shadow + outline) to caption text for a cinematic "lit from within"
    look.  The flag is forwarded to :func:`generate_ass_professional`.

    Non-fatal ordering: the zoom is applied before captions so both compose.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vf_parts = [
        f"scale={output_width}:{output_height}",
        "setsar=1",
        "fps=30",
        "eq=contrast=1.08:brightness=0.02:saturation=1.12",
        "unsharp=5:5:0.6",
    ]

    # NOTE: Ken Burns zoom deliberately removed — the user does not want any
    # zoom or pan effect on the clip.  The clip plays at its native framing.

    vf = ",".join(vf_parts)

    # ── Pass 1: grade + Ken Burns + sharpen (no captions) ────────────────
    # Kept as its own encode so a caption-subtitle failure (e.g. some FFmpeg
    # builds error on the ass filter) can NEVER destroy the whole effects
    # pass.  We encode the intermediate at CRF 18 so the later caption pass
    # doesn't visibly stack generation loss.
    low_crf = "18"
    plain_path = output_path.with_name(f"{output_path.stem}_plain.mp4")
    args = [
        "-i", str(input_path.resolve()), "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", low_crf,
        "-c:a", "aac", "-ac", "2", "-ar", "44100",
        "-y", str(plain_path.resolve()),
    ]
    _run_ffmpeg_safe(args, "Grade & Ken Burns")
    if not plain_path.exists() or plain_path.stat().st_size < 100:
        raise RuntimeError("Grading pass produced no output")
    logger.info(f"Grading + Ken Burns: {plain_path.stat().st_size} bytes")

    # ── Pass 2: burn captions over the graded clip (non-fatal) ───────────
    # Preferred: the professional ASS filter (glow + karaoke).  Pass
    # original_size explicitly — left to auto-derive after a prior crop, some
    # FFmpeg builds die with:
    #   "Error applying option 'original_size' to filter 'ass': Invalid argument"
    # If that still fails we fall back to plain drawtext captions, which
    # always render.  Only if BOTH fail do we keep the uncaptioned graded
    # video.
    captions_ok = False
    # Validate the ASS file before burning so a bad file can't silently
    # produce nothing; fall through to drawtext if invalid.
    _valid_ass = False
    if ass_path and ass_path.is_file():
        _valid_ass, _ass_err = _validate_ass_file(ass_path)
        if not _valid_ass:
            logger.error(f"ASS invalid in apply_effects ({_ass_err}) — using drawtext")
    if _valid_ass:
        try:
            if _attempt_ass_burn(
                plain_path, output_path, ass_path,
                output_width, output_height,
            ):
                captions_ok = True
                logger.info("Captions burned via ASS filter")
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"ASS caption burn failed (falling back to drawtext): {exc}"
            )

    # Fallback: plain drawtext captions (guaranteed to render).
    if not captions_ok and caption_segments:
        try:
            dt_vf = _build_drawtext_caption_vf(
                caption_segments, output_width, output_height, caption_font, caption_color
            )
            if dt_vf and _burn_captions_pass(plain_path, output_path, dt_vf):
                captions_ok = True
                logger.info("Captions burned via drawtext fallback")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Drawtext caption fallback failed: {exc}")

    # No captioned file produced → fall back to the graded video.
    if not captions_ok:
        shutil.copyfile(str(plain_path), str(output_path))
        logger.info("Using graded video without captions")

    # Clean up the intermediate grading temp file.
    try:
        if plain_path.exists():
            plain_path.unlink()
    except OSError:
        pass

    if not output_path.exists() or output_path.stat().st_size < 100:
        raise RuntimeError("Effects pass produced no output")
    logger.info(f"Effects applied: {output_path.stat().st_size} bytes")
    return output_path


# Windows fonts used for the edited thumbnail composition.
_THUMB_BOLD_FONT = "C:/Windows/Fonts/arialbd.ttf"
# Heavier headline font (Arial Black, weight 900) for punchier cover type.
# Falls back to bold via _font_path if not installed on a given Windows box.
_THUMB_BLACK_FONT = "C:/Windows/Fonts/ariblk.ttf"
_THUMB_BODY_FONT = "C:/Windows/Fonts/arial.ttf"
_THUMB_EMOJI_FONT = "C:/Windows/Fonts/seguiemj.ttf"


def _drawtext_escape(text: str) -> str:
    """Escape a string for embedding inside an FFmpeg drawtext text= option.

    FFmpeg's filtergraph parser splits filters on ``,``, statements on ``;``
    and options on ``:`` — and ``=`` begins a key/value pair — so every one of
    those must be backslash-escaped so user-provided titles cannot break the
    graph.

    NOTE: we do NOT strip existing backslashes here.  Font paths are pre-escaped
    by :func:`_font_path` (e.g. ``C\\:/Windows/...``), and stripping the ``\\``
    here would undo that escaping and corrupt the path / break the filter.  This
    function is for TEXT VALUES only — callers pass already-escaped font paths
    directly.
    """
    s = (
        text.replace("'", "")
        .replace('"', "")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("=", "=")
    )
    return s.strip()


def _font_path(name: str, fallback: str) -> tuple[str, bool]:
    """Return ``(escaped_path, exists)`` for a Windows font.

    Escapes the drive colon so it survives the FFmpeg filter parser. If the
    font is missing we fall back so drawtext never points at a gap.
    """
    path = Path(name)
    if path.exists():
        return path.as_posix().replace(":", "\\:"), True
    fb = Path(fallback)
    if fb.exists():
        return fb.as_posix().replace(":", "\\:"), True
    return name.replace(":", "\\:"), False


def _thumbnail_lines(title: str, max_headline_chars: int = 44) -> tuple[str, str]:
    """Split a title into (headline, strap) — a punchy hook + a supporting tag.

    ``headline`` is the big clickbait line; ``strap`` is a short neon accent
    like "WATCH THE FULL CLIP".

    The source title is truncated to a character budget (``max_headline_chars``)
    so the big type can't render off-canvas — the width-fitting in
    ``generate_thumbnail`` then shrinks/wraps it as a final guarantee.
    """
    for filler in ["Check out this amazing", "Check out this", "Check out", "Watch this"]:
        if title.lower().startswith(filler.lower()):
            title = title[len(filler):].strip()

    # NOTE: we no longer force ALL-CAPS here. Thumbnail headlines are now sourced
    # from the cheap-and-punchy *hook_sentence* (when present), which AI has already
    # authored to be attention-grabbing — uppercasing it would just shout.  Sentence
    # case is fine and reads more like real cover copy.  Only the final punctuation
    # and the length clamp are normalised.
    words = title.split()
    if not words:
        headline = "WOW"
    elif len(words) <= 3:
        headline = title
    else:
        headline = " ".join(words[:5])
    if not headline.endswith(("!", "?", "...")):
        headline += "!"
    # Hard truncate to the budget (append ellipsis if we cut mid-phrase).
    if len(headline) > max_headline_chars:
        headline = headline[: max_headline_chars - 1].rstrip() + "…"
    return headline, "WATCH THE FULL CLIP"


# Windows fonts that are likely to actually contain the play triangle glyph
# U+25B6 ("▶"). Arial (the default body/bold font) DOES NOT have it — that's the
# source of the □ tofu box that appeared before "WATCH THE FULL CLIP".  We pick
# the first one present on the machine; arialuni / seguisym reliably do.
_THUMB_SYMBOL_FONTS = [
    "C:/Windows/Fonts/arialuni.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
]


def _pick_play_symbol() -> tuple[str | None, str | None, float]:
    """Return ``(raw_path, escaped_path, width@64)`` of the first installed font
    that can draw the play-triangle glyph ``▶``, else ``(None, None, 0.0)``."""
    size = 64
    for cand in _THUMB_SYMBOL_FONTS:
        if not Path(cand).exists():
            continue
        esc, _ = _font_path(cand, FONT_FILE)
        try:
            measure = _make_text_measure(cand, size)
            w = measure("▶")
        except Exception:  # noqa: BLE001 - cannot measure -> try next font
            continue
        if w and w > 0:
            return cand, esc, w
    return None, None, 0.0


def _append_cta_row(
    vf: list[str], strap: str, bold_esc: str, lime: str, y: int, W: int,
) -> None:
    """Append the centered ``play-triangle + strap`` CTA row to *vf*.

    The triangle is rendered as its own drawtext using a symbol font (so it never
    shows a tofu box), placed beside the strap text, and the whole row is
    centered on the canvas by measuring both pieces first.
    """
    strap_size = 58
    strap_path = _THUMB_BOLD_FONT
    strap_w = _make_text_measure(strap_path, strap_size)(strap) if strap else 0.0
    sym_path, sym_esc, sym_w = _pick_play_symbol()
    pad = 22
    total_w = (sym_w + pad + strap_w) if sym_path else strap_w
    x0 = int((W - total_w) / 2)
    if sym_path:
        vf.append(
            f"drawtext=fontfile='{sym_esc}':text='▶':fontsize=64:"
            f"x={x0}:y={y}:fontcolor={lime}:"
            f"borderw=4:bordercolor=black@0.9:shadowcolor=black@0.6:shadowx=2:shadowy=3"
        )
        x0 += int(sym_w) + pad
    if strap:
        vf.append(
            f"drawtext=fontfile='{bold_esc}':"
            f"text='{_drawtext_escape(strap)}':fontsize={strap_size}:"
            f"x={x0}:y={y}:fontcolor={lime}:"
            f"borderw=5:bordercolor=black@0.9:shadowcolor=black@0.6:shadowx=3:shadowy=3"
        )


@timed(logger_name="processing")
def generate_thumbnail(
    input_path: Path,
    text: str,
    output_path: Path,
    face_crop: tuple[int, int, int, int] | None = None,
    source_frame: Path | None = None,
) -> Path:
    """Render an edited, YouTube-style vertical (1080x1920) thumbnail.

    Change 1-4 redesign (2026-09-12): the old "small inset frame + blurred
    letterbox backdrop + quiet UI-copy headline" look is replaced with full-bleed
    creative cover art:
      * Change 1 — FULL-BLEED frame (no letterbox).  When a BlazeFace 9:16 crop
        box is supplied via ``face_crop`` (x, y, w, h) we crop to the face first,
        then scale to fill 1080x1920; otherwise we centre-crop.  A warm radial
        glow (warm colorbalance + radial vignette + pop sharpen) sells the
        "glowing subject" cover feel.
      * Change 2 — ``text`` is the attention-grabbing ``hook_sentence`` (the
        call site passes the hook first, falling back to the title).
      * Change 3 — bolder, heavier headline type (Arial Black / weight 900,
        larger base size, hard stroke outline) for stick-at-a-glance cover copy.
      * Change 4 — a 2px ``--accent-primary`` (#2DD4E8) brand frame around the
        canvas edge.

    Layer order (top→bottom): full-bleed graded frame → warm radial glow →
    soft bottom scrim → "AI SHORT" badge → emoji flourish → bold hook headline →
    lime strap + play-triangle CTA → 2px cyan brand border.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    headline, strap = _thumbnail_lines(text)

    W, H = 1080, 1920
    # Headline uses the heaviest available face; falls back via _font_path.
    head_font = _THUMB_BLACK_FONT
    head, head_ok = _font_path(head_font, _THUMB_BOLD_FONT)
    if not head_ok:
        head, _ = _font_path(_THUMB_BOLD_FONT, FONT_FILE)
    bold, _ = _font_path(_THUMB_BOLD_FONT, FONT_FILE)
    emoji, emoji_ok = _font_path(_THUMB_EMOJI_FONT, FONT_FILE)

    neon = "0x22c55e"
    lime = "0xa3e635"
    accent = "0x2DD4E8"          # --accent-primary brand cyan (Change 4)

    # The FFmpeg ``-i`` input: a pre-selected, scored best frame (Part 3,
    # 2026-09-12) fed in as an extracted JPG when the caller ran candidate
    # extraction -- this lets the composed poster use EXACTLY the highest-scoring
    # candidate frame instead of a blind internal ``thumbnail=300`` pick. When no
    # ``source_frame`` is supplied we fall back to the video + thumbnail=300.
    img_input = source_frame.resolve() if source_frame is not None else input_path.resolve()

    vf: list[str] = []
    # ── 1. Full-bleed frame ──────────────────────────────────────────────
    # Change 1: pick the best frame, frame it fully-bleed (face-aware crop if we
    #   have BlazeFace 9:16 coords, else centre), then grade warm + radial glow.
    if source_frame is None:
        vf.append("thumbnail=300")
    if face_crop is not None:
        fx, fy, fw, fh = face_crop
        # Crop to the face box first (keeps the subject centred/right of centre),
        # THEN scale to fill the 1080x1920 canvas (full-bleed, no letterbox).
        vf.append(f"crop={fw}:{fh}:{fx}:{fy}")
        vf.append(f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}")
    else:
        # Fallback (no face data): centre-crop full-bleed exactly as before.
        vf.append(f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}")
    # Warm radial glow + pop (replaces flat color-grade + mild vignette).
    vf += [
        "eq=contrast=1.14:brightness=0.02:saturation=1.35",
        "colorbalance=rs=0.05:bs=-0.04:rm=0.04:bm=-0.03:rh=0.03:bh=-0.02",
        "unsharp=5:5:0.8",
        "vignette=PI/3.4",
    ]

    # ── 2. Soft stepped bottom scrim (reads like a gradient falloff) ─────
    vf += [
        "drawbox=x=0:y=1180:w=1080:h=740:color=black@0.22:t=fill",
        "drawbox=x=0:y=1300:w=1080:h=620:color=black@0.30:t=fill",
        "drawbox=x=0:y=1420:w=1080:h=500:color=black@0.42:t=fill",
        # brand-cyan underline anchoring the text block
        "drawbox=x=0:y=1810:w=1080:h=6:color=0x2DD4E8@0.95:t=fill",
    ]

    # ── 3. "AI SHORT" badge pill (top-left) ──────────────────────────────
    vf.append(
        f"drawtext=fontfile='{bold}':"
        f"text='{_drawtext_escape('AI SHORT')}':fontsize=44:"
        f"x=48:y=58:fontcolor=black:box=1:boxcolor={accent}:boxborderw=20:borderw=0"
    )

    # ── 4. emoji flourish (top-right) — only when an emoji font is present ─
    if emoji_ok:
        vf.append(
            f"drawtext=fontfile='{emoji}':"
            f"text='{_drawtext_escape('🔥')}':fontsize=150:x=930:y=40:fontcolor=white"
        )

    # ── 5. BOLD hook headline (bottom, centred) — heavier + bigger + hard stroke ─
    # Change 3: Arial Black (900), larger base size, thicker stroke for legibility,
    #   still fit-to-width so the big type NEVER runs off the frame edge.
    head_max = int(W * 0.88)
    head_size, head_lines = _fit_caption(
        headline, _THUMB_BLACK_FONT, head_max, base_size=124, min_size=32
    )
    line_h = int(head_size * 1.18)
    head_y0 = 1530 if len(head_lines) == 1 else 1470
    for i, hline in enumerate(head_lines):
        hy = head_y0 + i * line_h
        vf.append(
            f"drawtext=fontfile='{head}':"
            f"text='{_drawtext_escape(hline)}':fontsize={head_size}:"
            f"x=(w-text_w)/2:y={hy}:fontcolor=white:"
            f"borderw=10:bordercolor=black@0.92:"
            f"shadowcolor=black@0.72:shadowx=5:shadowy=6"
        )
    if len(head_lines) > 1:
        logger.info(
            f"Thumbnail headline wrapped to 2 lines at fontsize {head_size}: "
            f"{head_lines!r}"
        )

    # ── 6. lime strap + play-triangle tag under the headline ──────────────
    strap_max = int(W * 0.86)
    _strap_size, _strap_lines = _fit_caption(
        strap, _THUMB_BOLD_FONT, strap_max, base_size=58, min_size=34
    )
    strap_render = " ".join(_strap_lines)
    _append_cta_row(vf, strap_render, bold, lime, y=1710, W=W)

    # ── 7. 2px cyan --accent-primary brand frame around the canvas edge ──
    # Change 4: draws a 2px border ring at the very frame edge (top layer).
    vf.append(f"drawbox=x=0:y=0:w={W}:h={H}:color={accent}:t=2")

    args = [
        "-i", str(img_input),
        "-vf", ",".join(vf),
        "-frames:v", "1",
        "-q:v", "1",
        "-y", str(output_path.resolve()),
    ]
    try:
        _run_ffmpeg_safe(args, "Generate Edited Thumbnail")
        if not output_path.exists() or output_path.stat().st_size < 100:
            raise RuntimeError("Thumbnail produced no output")
        logger.info(f"Edited thumbnail: {output_path.stat().st_size} bytes")
    except RuntimeError:
        # Fallback: plain best-frame grab so a thumbnail always exists.
        args2 = ["-ss", "1", "-i", str(img_input),
                 "-frames:v", "1", "-q:v", "2", "-y", str(output_path.resolve())]
        _run_ffmpeg_safe(args2, "Thumbnail fallback")
    return output_path


def burn_captions(
    input_path: Path,
    output_path: Path,
    ass_path: Path | None,
    caption_segments: list[dict] | None = None,
    caption_font: str | None = None,
    caption_color: str | None = None,
    caption_size_scale: float = 1.0,
    caption_position: str = "bottom",
    caption_outline: bool = True,
    output_width: int = 1080,
    output_height: int = 1920,
) -> Path:
    """
    Burn subtitles onto a clip with NO color grading / motion / glow.

    This is the minimal "subtitles only" edit pass: it takes the original clip
    and overlays the generated captions. Preferred renderer is the professional
    ASS filter; on FFmpeg builds that error on the ``ass`` filter it falls back
    to plain drawtext captions (guaranteed to render). If both fail, the
    original clip is returned unchanged so subtitles never destroy the edit.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    captions_ok = False
    # Validate the ASS file before burning so a bad file can't silently produce
    # no captions (or abort). If invalid, fall through to drawtext.
    valid_ass = False
    if ass_path and ass_path.is_file():
        valid_ass, ass_err = _validate_ass_file(ass_path)
        if not valid_ass:
            logger.error(f"ASS invalid in burn_captions ({ass_err}) — falling back to drawtext")
    logger.info(
        f"[BURN DEBUG] burn_captions called: ass_path={ass_path}, "
        f"valid_ass={valid_ass}, caption_segments={len(caption_segments or [])}, "
        f"input_exists={input_path.exists() if input_path else 'N/A'}"
    )
    # Preferred: burn the ASS file we generated.
    if valid_ass:
        try:
            if _attempt_ass_burn(
                input_path, output_path, ass_path,
                output_width, output_height,
            ):
                captions_ok = True
                logger.info("[BURN DEBUG] Captions burned via ASS filter successfully")
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"ASS caption burn failed unexpectedly (falling back to drawtext): {exc}"
            )

    # Fallback: plain drawtext captions (guaranteed to render).
    if not captions_ok and caption_segments:
        logger.info(
            f"[BURN DEBUG] Attempting drawtext fallback with {len(caption_segments)} cues..."
        )
        # Diagnose whether drawtext works at all on this FFmpeg build (cached).
        _smoke_test_drawtext(input_path)
        logger.info(
            f"[BURN DEBUG] drawtext smoke test result: "
            f"{'works' if _smoke_test_drawtext(input_path) else 'BROKEN'}"
        )
        try:
            dt_vf = _build_drawtext_caption_vf(
                caption_segments, output_width, output_height,
                font=caption_font, color=caption_color,
                size_scale=caption_size_scale,
                position=caption_position,
                outline=caption_outline,
            )
            logger.info(
                f"[BURN DEBUG] Drawtext filter length: {len(dt_vf)} chars, "
                f"first 200: {dt_vf[:200]!r}"
            )
            if dt_vf and _burn_captions_pass(input_path, output_path, dt_vf):
                captions_ok = True
                logger.info("[BURN DEBUG] Captions burned via drawtext fallback successfully")
            else:
                logger.warning("[BURN DEBUG] Drawtext burn returned False or empty vf")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[BURN DEBUG] Drawtext caption fallback FAILED: {exc}")

    # No captioned file produced → keep the original clip uncaptioned.
    if not captions_ok:
        shutil.copyfile(str(input_path), str(output_path))
        logger.warning("[BURN DEBUG] No captions burned; keeping original clip")

    if not output_path.exists() or output_path.stat().st_size < 100:
        raise RuntimeError("Caption burn produced no output")
    logger.info(f"Captions applied: {output_path.stat().st_size} bytes")
    return output_path


@timed(logger_name="processing")
def concat_videos(intro_path: Path, main_path: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve to absolute paths so FFmpeg can always find files
    abs_intro = intro_path.resolve()
    abs_main = main_path.resolve()
    abs_output = output_path.resolve()
    list_path = abs_output.with_name(f"{abs_output.stem}_concat.txt")

    list_path.write_text(
        f"file '{abs_intro.as_posix()}'\nfile '{abs_main.as_posix()}'"
    )
    args = [
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", "-y", str(abs_output)
    ]
    try:
        _run_ffmpeg_safe(args, "Concat Videos")
    finally:
        if list_path.exists():
            list_path.unlink()
    if not output_path.exists() or output_path.stat().st_size < 100:
        raise RuntimeError("Concat produced no output")
    logger.info(f"Final video: {output_path.stat().st_size} bytes")
    return output_path


def auto_edit_clip(clip_id: int, options: dict | None = None) -> dict:
    """
    Minimal "subtitle + thumbnail" auto-edit for a clip.

    Deliberately stripped down (per user request 2026-09-09): the editor ONLY
    burns subtitles onto the clip and produces a thumbnail. No intro card, no
    color grading, no cinematic glow, no AI metadata generation.

    Args:
        clip_id: The clip to edit.
        options: Optional dict of overrides:
            - with_captions: bool (default True) — burn subtitles
            - subtitle_language: 'en' (default) | 'zh' — Chinese translates
              the on-screen captions via the model-team translate specialist
            - caption_font / caption_color / caption_highlight: str — optional
              internal styling overrides (defaults applied when omitted)

    Thumbnail is ALWAYS the composed best-frame render (FFmpeg) — the dead
    FLUX/`flux.1-schnell` AI-enhance path was removed (that model does not
    exist in Ollama's registry, 2026-09-09).

    Subtitles come from a FRESH transcription of the clip's own audio
    (faster-whisper + VAD), which produces ground-truth text and timestamps
    that match the actual speech in the clip.  This eliminates the timing
    mismatch caused by reusing the stored full-video transcript (which may have
    inaccurate timestamps or be offset by keyframe-based clip cutting).  The
    stored full-video transcript is only used as a fallback when fresh
    transcription produces no results.
    """
    opts = options or {}
    try:
        _check_render_capabilities()
    except Exception:  # noqa: BLE001
        pass

    async def run_pipeline():
        async with get_session_context() as db:
            clip = await crud.get_clip(db, clip_id)
            if not clip:
                raise ValueError("Clip not found")
            clip_path = Path(clip.output_path) if clip.output_path else None
            if not clip_path or not clip_path.exists():
                raise ValueError("Clip output file not found")
            transcript = await crud.get_transcript_for_video(db, clip.video_id)

            caption_font = opts.get("caption_font", "Arial")
            caption_color = opts.get("caption_color", "#FFFFFF")
            caption_highlight = opts.get("caption_highlight", "#FFD700")
            with_captions = opts.get("with_captions", True)
            subtitle_language = opts.get("subtitle_language", "en")
            is_chinese = str(subtitle_language).lower().startswith(("zh", "cn"))

            # Part A (2026-09-13): subtitle *style* picker params forwarded to the
            # caption burn (drawtext path especially).  The frontend resolves a
            # preset (fancy/normal/bold) + control overrides into these concrete
            # values; the backend just applies them, so the live preview matches
            # the real render.
            caption_size_scale = float(opts.get("caption_size_scale", 1.0) or 1.0)
            if not 0.5 <= caption_size_scale <= 2.5:
                caption_size_scale = 1.0
            caption_position = str(opts.get("caption_position", "bottom") or "bottom")
            if caption_position not in ("bottom", "top", "center"):
                caption_position = "bottom"
            caption_outline = str(opts.get("caption_outline", True)).lower() in ("1", "true", "yes", "on")

            # ── Audio Remix (Phase 3) ──────────────────────────────────────
            # API overrides win; otherwise fall back to the values persisted on
            # the clip row (set at clip creation or by a previous edit).
            audio_mode = opts.get("audio_mode") or getattr(clip, "audio_mode", None) or "keep_original"
            vocals_gain = float(opts.get("vocals_gain", 1.0) if opts.get("vocals_gain") is not None
                                else getattr(clip, "vocals_gain", 1.0) or 1.0)
            music_gain = float(opts.get("music_gain", 1.0) if opts.get("music_gain") is not None
                               else getattr(clip, "music_gain", 1.0) or 1.0)
            replacement_track = (opts.get("replacement_track")
                                 or getattr(clip, "replacement_track", None)
                                 or None)
            audio_mode = audio_mode if audio_mode in ("keep_original", "mute_music", "replace_music") else "keep_original"

            output_dir = clip_path.parent
            thumb_dir = output_dir.parent / "thumbnails"
            thumb_dir.mkdir(parents=True, exist_ok=True)

            # Unique per-language output files so an English edit and a Chinese
            # edit can coexist WITHOUT overwriting each other — and the ORIGINAL
            # clip (clip_path / clips.output_path) is never touched.
            lang_tag = "zh" if is_chinese else "en"
            final_path = output_dir / f"edited_clip_{clip_id}_{lang_tag}.mp4"
            thumb_path = thumb_dir / f"edited_thumb_{clip_id}_{lang_tag}.jpg"
            ass_path = output_dir / f"clip_{clip_id}_{lang_tag}.ass"

            # ── Clip captions: reuse the full-video transcript (correctly rebased) ──
            # 2026-09-13 (Part A): the previous approach re-transcribed each clip's
            # isolated audio first.  That fixed the keyframe timing-lead but
            # reintroduced the ORIGINAL hallucination risk — Whisper on short,
            # context-free audio fabricates filler at the boundaries.  We now
            # invert it:
            #
            #   PRIMARY:  slice + rebase the stored FULL-VIDEO transcript to the
            #             clip's ACTUAL (keyframe-snapped) bounds, recorded on the
            #             Clip row at cut time (achieved_start_time / achieved_end_time).
            #             Because we rebase to the TRUE first frame — not the
            #             requested start_time — captions are both contextually
            #             accurate AND correctly timed against the rendered clip.
            #   FALLBACK: fresh transcription of the clip's own audio, ONLY when the
            #             stored transcript genuinely doesn't cover the clip's range
            #             (misses the achieved window).  This keeps a safety net
            #             without making isolated-audio transcription the default.
            clip_segs: list[dict] = []
            clip_words: list[dict] = []
            clip_duration = clip.duration if hasattr(clip, 'duration') else None
            clip_rebased_start = (
                getattr(clip, "achieved_start_time", None)
                or getattr(clip, "start_time", None)
            )
            if with_captions:
                logger.info(
                    f"[SUBTITLE DEBUG] clip_id={clip_id} "
                    f"clip_path={clip_path} exists={clip_path.exists() if clip_path else 'N/A'} "
                    f"duration={clip_duration} rebase_start={clip_rebased_start}"
                )

                # ── Primary: slice + rebase the FULL-VIDEO transcript ──────
                # Instant, contextually accurate, and (with the achieved-offset
                # fix) correctly timed against the rendered clip's first frame.
                reused_segs: list[dict] | None = None
                reused_words: list[dict] | None = None
                reuse_reason = "no transcript" if transcript is None else "no overlapping segments"
                if transcript is not None:
                    try:
                        reused_segs, reused_words = get_clip_transcript(clip, transcript)
                        if reused_segs or reused_words:
                            logger.info(
                                f"[SUBTITLE DEBUG] Transcript reuse: PRIMARY "
                                f"({len(reused_segs or [])} segments, "
                                f"{len(reused_words or [])} words) rebased to "
                                f"achieved_start={clip_rebased_start}"
                            )
                            reuse_reason = None
                        else:
                            logger.info(
                                f"[SUBTITLE DEBUG] Transcript reuse: empty ({reuse_reason})"
                            )
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            f"[SUBTITLE DEBUG] Transcript reuse FAILED: {e}"
                        )
                        reuse_reason = f"reuse raised: {e}"

                if reused_segs or reused_words:
                    clip_segs = reused_segs or []
                    clip_words = reused_words or []
                else:
                    # ── Fallback: FRESH transcription (only when reuse is empty) ──
                    # Rare: the stored transcript doesn't cover the clip's real
                    # range.  Rather than ship no captions, transcribe the clip's
                    # own audio.  Note this reintroduces the isolated-audio
                    # hallucination risk, which is why it is LAST, not first.
                    logger.info(
                        f"[SUBTITLE DEBUG] Transcript reuse unavailable ({reuse_reason}); "
                        f"falling back to fresh clip transcription for clip {clip_id}..."
                    )
                    try:
                        from backend.services.transcription import transcribe_video
                        ct = await asyncio.to_thread(
                            transcribe_video, clip_path, language="auto"
                        )
                        clip_segs = ct.get("segments", []) or []
                        clip_words = ct.get("words", []) or []
                        logger.info(
                            f"[SUBTITLE DEBUG] Fresh transcription (fallback) result: "
                            f"{len(clip_segs)} segments, {len(clip_words)} words, "
                            f"engine={ct.get('engine', 'unknown')}"
                        )
                        if clip_segs:
                            for i, s in enumerate(clip_segs[:3]):
                                logger.info(
                                    f"[SUBTITLE DEBUG]   seg[{i}]: "
                                    f"start={s.get('start'):.2f} end={s.get('end'):.2f} "
                                    f"text={s.get('text', '')!r}"
                                )
                        # No segment-level data? Group word timestamps into cues
                        # so subtitles still render across the whole clip.
                        if not clip_segs and clip_words:
                            clip_segs = _group_words_for_caption(
                                clip_words, words_per_line=8
                            )
                            logger.info(
                                f"[SUBTITLE DEBUG] Grouped {len(clip_words)} words "
                                f"into {len(clip_segs)} caption cues"
                            )
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            f"[SUBTITLE DEBUG] Fresh clip transcription FAILED: {e}"
                        )
                        clip_segs, clip_words = [], []

                # ── Sanitize (hallucination + confidence filter) ───────────
                logger.info(
                    f"[SUBTITLE DEBUG] Pre-sanitize: {len(clip_segs)} segments"
                )
                if clip_segs:
                    _pre = len(clip_segs)
                    clip_segs = _sanitize_segments(clip_segs, clip_duration)
                    logger.info(
                        f"[SUBTITLE DEBUG] Post-sanitize: {len(clip_segs)} segments "
                        f"({_pre - len(clip_segs)} removed)"
                    )
                if not clip_segs:
                    logger.warning(
                        "[SUBTITLE DEBUG] No speech detected in clip — "
                        "subtitles will be SKIPPED."
                    )

                # ── Diagnostics: timing sanity check ───────────────────────
                if clip_segs and clip_duration:
                    _first = clip_segs[0]
                    _last = clip_segs[-1]
                    covered = sum(
                        max(0.0, float(s.get("end", 0)) - max(0.0, float(s.get("start", 0))))
                        for s in clip_segs
                    )
                    logger.info(
                        f"Subtitle timing: first cue starts at "
                        f"{float(_first.get('start', 0)):.2f}s, "
                        f"last cue ends at {float(_last.get('end', 0)):.2f}s, "
                        f"clip duration={clip_duration:.2f}s, "
                        f"coverage={covered:.1f}s ({100*covered/clip_duration:.0f}%)"
                    )

            # Thumbnail headline (Change 2, 2026-09-12): prefer the clip's existing
            # attention-grabbing ``hook_sentence`` (authored by the hook LLM role
            # as the best opening line to stop scrolling) over the descriptive
            # ``title``.  Falls back to the title, then a stock line, if no hook
            # has been generated yet.
            thumbnail_text = (getattr(clip, "hook_sentence", None) or "").strip()
            title = thumbnail_text or (clip.title or "").strip() or "Wow, watch this!"
            logger.info(
                f"[THUMBNAIL HEADLINE] source="
                f"{'hook_sentence' if thumbnail_text else 'title'} -> {title!r}"
            )

            # ── Subtitle translation (English stays; Chinese translates) ──
            # Chinese renders at *segment* level: it has no word spaces, so
            # word-fragmented ASS would look broken. English keeps the original
            # transcript as-is. Translation works on the clip-relative cues.
            is_chinese = is_chinese and bool(clip_segs)
            use_segments_for_cjk = False
            if is_chinese:
                logger.info("subtitle_language=zh: translating captions to Chinese")
                try:
                    translated_segs = translate_segments_to_language(
                        clip_segs, "zh"
                    )
                    # CJK validation: compare first segment text to verify
                    # translation actually produced Chinese characters.
                    orig_sample = clip_segs[0].get("text", "")[:60] if clip_segs else ""
                    trans_sample = translated_segs[0].get("text", "")[:60] if translated_segs else ""
                    has_cjk = _CJK_RE.search(trans_sample) if trans_sample else False
                    logger.info(
                        f"[CJK CHECK] Translation result: has_cjk={has_cjk}, "
                        f"orig={orig_sample!r}, translated={trans_sample!r}"
                    )
                    # PHASE 4: CJK font-confirmation — verify the CJK-capable
                    # font file (Microsoft YaHei) is actually present so Chinese
                    # glyphs can render (the #1 cause of tofu boxes).
                    _cjk_font_on_disk = Path(CJK_FONT_FILE).exists()
                    logger.info(
                        f"[CJK FONT] confirmation: requested_font={CJK_FONT_FILE!r} "
                        f"exists={_cjk_font_on_disk} -> "
                        f"{'CJK glyphs will render' if _cjk_font_on_disk else 'font MISSING — Chinese may show as boxes'}"
                    )
                    if translated_segs != clip_segs:
                        clip_segs = translated_segs
                    # Always force segment-level for Chinese — word-level karaoke
                    # breaks CJK rendering (no word spaces).
                    use_segments_for_cjk = True
                    # Re-sanitize translated segments (translator may produce
                    # empty or garbled output for hallucinated input).
                    clip_segs = _sanitize_segments(clip_segs, getattr(clip, 'duration', None))
                except Exception as e:
                    logger.exception(
                        f"Subtitle translation to Chinese failed: {e}"
                    )

            # ── PHASE 4: source-of-truth marker ────────────────────────────────
            # States whether the caption word timestamps came from the stored
            # full-video WhisperX transcript (reuse) vs a fresh clip re-transcribe,
            # and whether WhisperX aligned words are present.
            _whisperx_words = any(
                isinstance(w.get("probability"), (int, float)) for w in clip_words
            ) if clip_words else False
            logger.info(
                f"[SOURCE OF TRUTH] captions from "
                f"{'full-video transcript (REUSE, stored)' if (reused_segs or reused_words) else 'FRESH clip re-transcription'}; "
                f"words={'whisperx-aligned' if _whisperx_words else 'native'}; "
                f"segments={len(clip_segs)}, words={len(clip_words)}"
            )

            # Step 2: ASS captions (professional style by default)
            logger.info(
                f"[SUBTITLE DEBUG] Step 2: Generating ASS for {len(clip_segs)} cues "
                f"(language={subtitle_language}, words={len(clip_words)})"
            )
            if with_captions and clip_segs:
                try:
                    # Chinese renders cleanly at segment level; English uses
                    # word-level karaoke highlighting. Times are already
                    # clip-relative (0-based), so do NOT re-offset them.
                    if use_segments_for_cjk or not clip_words:
                        prof_words = None
                        prof_segments = clip_segs
                    else:
                        prof_words = clip_words
                        prof_segments = clip_segs
                    generate_ass_professional(
                        words=prof_words,
                        segments=prof_segments,
                        output_path=ass_path,
                        clip_start=0.0, clip_end=None,
                        font=caption_font, color=caption_color,
                        highlight_color=caption_highlight,
                        glow_enabled=True,
                        is_cjk=is_chinese,
                    )
                except Exception as e:
                    logger.exception(f"ASS generation failed: {e}")

            # Step 3: Drawtext fallback cues — already clip-relative (0-based),
            # one cue per spoken segment, so subtitles cover the whole video.
            caption_segments: list[dict] | None = None
            if clip_segs:
                cue_list = [
                    {
                        "start": float(sg.get("start", 0)),
                        "end": float(sg.get("end", 0)),
                        "text": sg.get("text", ""),
                    }
                    for sg in clip_segs
                ]
                if cue_list:
                    caption_segments = cue_list

            # Pipeline-state diagnostics: log how many cues make it into the ASS
            # vs how many the drawtext fallback would use, so a failure to show
            # subtitles (or subtitles that stop early) is diagnosable from logs.
            # Pipeline-state diagnostics: log how many cues make it into the ASS
            # vs how many the drawtext fallback would use, so a failure to show
            # subtitles (or subtitles that stop early) is diagnosable from logs.
            logger.info(
                f"Caption pipeline state: {len(clip_segs)} sanitized segments, "
                f"{len(clip_words)} words, {len(caption_segments or [])} drawtext cues, "
                f"ASS file {'present' if ass_path.exists() else 'missing'}"
            )
            if clip_duration:
                covered = sum(max(0.0, float(s.get("end", 0)) - max(0.0, float(s.get("start", 0))))
                              for s in clip_segs)
                coverage_pct = 100.0 * covered / clip_duration
                logger.info(
                    f"Caption COVERAGE: {covered:.1f}s of {clip_duration:.1f}s clip "
                    f"({coverage_pct:.0f}%) across {len(clip_segs)} cues"
                )

                # ── PHASE 4: ≥90% coverage gate (WhisperX source-of-truth) ──
                # Never claims "success" on thin coverage — this FLAGS sub-90%
                # coverage so it's visible in logs without faking success.
                if coverage_pct < 90.0:
                    logger.warning(
                        f"[COVERAGE GATE] FAIL: caption coverage {coverage_pct:.0f}% "
                        f"< 90% threshold — transcript-to-speech coverage is thin. "
                        f"FLAGGED, NOT marked success. Review clip boundaries / "
                        f"ASR if subtitle gaps persist."
                    )
                else:
                    logger.info(
                        f"[COVERAGE GATE] PASS: caption coverage {coverage_pct:.0f}% "
                        f">= 90% threshold"
                    )

            # ── AUDIO REMIX (Phase 3) ──────────────────────────────────────
            # If the user asked to mute/replace the background music (or wants a
            # loudness tweak), render a remixed intermediate file from the CLEAN
            # clip and feed THAT into the caption burn below. keep_original with
            # unity gains skips this entirely (burn straight from the clip).
            render_input = clip_path
            wants_remix = audio_mode != "keep_original" or abs(vocals_gain - 1.0) > 1e-3 or abs(music_gain - 1.0) > 1e-3
            if wants_remix:
                try:
                    from backend.services.audio_remix import remix_audio, separate_stems
                    logger.info(
                        f"Audio Remix: mode={audio_mode} vocals_gain={vocals_gain} "
                        f"music_gain={music_gain} track={replacement_track!r}"
                    )
                    remixed_path = output_dir / f"remixed_{clip_id}_{lang_tag}.mp4"
                    stems = None if audio_mode == "keep_original" else separate_stems(clip)

                    track_path = Path(replacement_track) if replacement_track else None
                    remix_audio(
                        input_path=clip_path,
                        output_path=remixed_path,
                        mode=audio_mode,
                        vocals_gain=vocals_gain,
                        music_gain=music_gain,
                        replacement_track=track_path,
                        stems=stems,
                        auto_duck=True,
                    )
                    if remixed_path.exists() and remixed_path.stat().st_size > 0:
                        render_input = remixed_path
                        logger.info(f"Audio Remix: render_input set to {render_input}")
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"Audio Remix failed ({exc}); using original audio")

            # Step 4: Burn subtitles onto the clip (subtitles ONLY — no grading,
            # no intro, no glow). Non-fatal: falls back to the original clip.

            # Validate the generated ASS file BEFORE burning — an invalid ASS can
            # make FFmpeg silently fail or render nothing. If invalid, drop it and
            # fall back to drawtext (which never aborts).
            valid_ass, ass_err = _validate_ass_file(ass_path) if ass_path.exists() else (False, "no ASS file")
            if ass_path.exists() and not valid_ass:
                logger.error(
                    f"[SUBTITLE DEBUG] ASS validation failed: {ass_err} — "
                    f"using drawtext fallback"
                )
            burn_ass = ass_path if valid_ass else None
            logger.info(
                f"[SUBTITLE DEBUG] Step 4: Burning captions... "
                f"valid_ass={valid_ass}, burn_ass={'yes' if burn_ass else 'no'}, "
                f"drawtext cues={len(caption_segments or [])}"
            )
            if with_captions and (burn_ass or caption_segments):
                logger.info(
                    f"[BURN DEBUG] Calling burn_captions: ASS={'yes' if burn_ass else 'no'} "
                    f"(path={burn_ass}), drawtext={len(caption_segments or [])} cues, "
                    f"input={render_input}, output={final_path}"
                )
                try:
                    burn_captions(
                        input_path=render_input, output_path=final_path,
                        ass_path=burn_ass,
                        caption_segments=caption_segments,
                        caption_font=caption_font,
                        caption_color=caption_color,
                        caption_size_scale=caption_size_scale,
                        caption_position=caption_position,
                        caption_outline=caption_outline,
                    )
                except Exception as e:
                    logger.exception(f"Caption burn failed, using input video: {e}")
                    try:
                        if not final_path.exists() or final_path.stat().st_size < 100:
                            shutil.copyfile(str(render_input), str(final_path))
                    except Exception:
                        logger.exception("Failed to copy input video as fallback output")
            else:
                logger.warning(
                    f"[SUBTITLE DEBUG] No captions to burn "
                    f"(with_captions={with_captions}, "
                    f"burn_ass={'yes' if burn_ass else 'no'}, "
                    f"caption_segments={len(caption_segments or [])}); "
                    f"copying input video"
                )
                shutil.copyfile(str(render_input), str(final_path))

            # Step 5: Thumbnail — always the composed best-frame render (FFmpeg).
            # The FLUX AI-enhance pass was removed (flux.1-schnell isn't a valid
            # Ollama model, so it could never run). A real frame thumbnail is
            # guaranteed and that's what "fully working" means here.
            #
            # CRITICAL: extract from the CLEAN ORIGINAL clip (clip_path), NOT the
            # post-subtitle-burn final_path.  Burning subtitle text into the frame
            # then grabbing a thumbnail means the captured frame shows subtitle
            # glyphs baked in — that's a source of the "garbled/corrupted text"
            # the user saw.  The original frame is clean.
            thumb_source = clip_path if clip_path.exists() else final_path
            logger.info(
                f"Step 5: Generating thumbnail from "
                f"{'original clip' if thumb_source == clip_path else 'edited clip'}..."
            )
            # Part 3/2026-09-12 + thumbnail-engine: score candidate frames, pick
            # the best, crop to 1080x1920, and composite — now all handled by the
            # new six-stage pipeline (thumbnail/engine.py). It returns
            # (path, tier, score); 'thumbnail_tier=N' is logged per render.
            # The frame's score is persisted to thumbnails.score/format/width/height.
            from backend.services.thumbnail import render_thumbnail as _render_thumb
            # Thumbnail override flags (design tokens) come through the same
            # options dict as the other styling; e.g. `thumbnail_style`.
            # Forwarded as the engine's `style` -> Tier-1 compositor palette.
            _thumb_style = opts.get("thumbnail_style") or {}
            _thumb_tier = None
            _thumb_score = None
            try:
                if thumb_source.exists():
                    _thumb_out, _thumb_tier, _thumb_score = _render_thumb(
                        thumb_source,
                        hook_sentence=(thumbnail_text or None),
                        title=(clip.title or None),
                        output_path=thumb_path,
                        style=_thumb_style,
                    )
            except Exception as e:
                logger.exception(f"Thumbnail engine failed: {e}")
                _thumb_tier = None
                # Best-effort final fallback: plain single-frame grab.
                try:
                    args2 = ["-ss", "1", "-i", str(thumb_source.resolve()),
                             "-frames:v", "1", "-q:v", "2", "-y", str(thumb_path.resolve())]
                    _run_ffmpeg_safe(args2, "Thumbnail emergency fallback")
                except Exception:
                    logger.exception("Thumbnail emergency fallback also failed")

            # Step 5.5: Thumbnail-as-intro-frame — prepend the edited clip's
            # thumbnail as a short still-intro (default OFF, 0.8s). Applied to
            # final_path AFTER captions are burned AND the thumbnail is generated,
            # so publishing/downloading the edit opens on the thumbnail still. The
            # thumbnail FILE itself (thumb_path) is left clean — we only change the
            # video. Non-fatal: on failure we keep the pristine edited clip.
            try:
                intro_on, intro_dur = await crud.get_intro_setting(db)
                if intro_on and thumb_path.exists() and final_path.exists():
                    from backend.utils.ffmpeg import add_thumbnail_intro
                    final_path = add_thumbnail_intro(
                        final_path, thumb_path, intro_dur,
                    )
                    logger.info(
                        f"[INTRO] Prepended thumbnail intro to edited clip "
                        f"final_path ({final_path})"
                    )
            except Exception as e:
                logger.warning(f"[INTRO] Intro step skipped for edited clip: {e}")

            # Step 6: Record the edit in the DB — keep the ORIGINAL render as
            # clip.output_path (never overwritten/re-pointed) and save this
            # edit as a SEPARATE labelled entry. Editing the same clip again in
            # another language (or a fresh pass) appends a new labelled edit
            # ("Edit 1 - English", "Edit 2 - 中文", ...) instead of clobbering
            # the previous one. Metadata fields are NOT touched (metadata
            # generation removed).
            logger.info("Step 6: Updating database...")
            # Determine a stable sequence number for the label.
            existing = await crud.list_clip_edits(db, clip_id)
            seq = len(existing) + 1
            lang_name = "Chinese" if lang_tag == "zh" else "English"
            label = f"Edit {seq} - {lang_name}"
            # Back-compat pointer to the most recent edit, plus persist the
            # Audio Remix settings used so the UI/detail view reflects them.
            await crud.update_clip(db, clip_id,
                edited_output_path=str(final_path),
                audio_mode=audio_mode,
                vocals_gain=vocals_gain,
                music_gain=music_gain,
                replacement_track=replacement_track,
            )
            if final_path.exists():
                await crud.create_clip_edit(
                    db,
                    clip_id=clip_id,
                    label=label,
                    language=lang_tag,
                    output_path=str(final_path),
                    thumbnail_path=str(thumb_path) if thumb_path.exists() else None,
                )
            if thumb_path.exists():
                await crud.create_thumbnail(
                    db, clip_id=clip_id, filepath=str(thumb_path),
                    score=_thumb_score, format="jpg",
                    width=1080, height=1920, is_selected=True,
                )

            logger.info(f"Auto-edit complete for clip {clip_id}: {label}")
            return {
                "title": title, "output_path": str(final_path),
                "thumbnail_path": str(thumb_path) if thumb_path.exists() else None,
                "edit_label": label, "edit_language": lang_tag,
            }

    return asyncio.run(run_pipeline())
