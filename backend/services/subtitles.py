"""
AIClipper Subtitle Generation Service

Generates SRT, WebVTT, and ASS (with word-level highlight) subtitle files
from transcript segment and word data.  Includes a professional-grade
ASS generator with glow effects, CJK font detection and karaoke-style
word highlighting for Premiere-quality burned-in captions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend.utils.logging import get_logger, timed

logger = get_logger("services.subtitles")


# ── CJK detection ──────────────────────────────────────────────────────

_CJK_RE = re.compile(
    r"[一-鿿㐀-䶿⺀-⻿　-〿"
    r"＀-￯⼀-⿟\U00020000-\U0002a6df]"
)


def _has_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK characters."""
    return bool(_CJK_RE.search(text))


def _cjk_font_for(font: str, text: str) -> str:
    """Return the best font name for *text*: CJK font if text contains
    Chinese/CJK characters, otherwise the passed *font*."""
    if _has_cjk(text):
        return "Microsoft YaHei"
    return font


# ── Translation helpers ────────────────────────────────────────────────

_TRANSLATION_PROMPT = (
    "You are a professional subtitle translator for short-form videos. "
    "Translate the following subtitle segments into {target_lang}. "
    "{scripts_note}"
    "Keep translations concise (suitable for on-screen subtitles). "
    "Maintain the meaning and energy. Reply with ONLY the translated text, "
    "one segment per line, no numbering, no commentary."
)

# Language codes the caption UI offers, mapped to the natural-language name
# the translate specialist should produce. CJK scripts WITHOUT word spaces
# (Chinese, Japanese) render at segment level — word-fragmented ASS would break.
# Space-separated scripts (Korean, Hindi) keep word-level karaoke highlighting.
SUBTITLE_LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"name": "English", "keep_original": True, "segment_level": False},
    "zh": {"name": "Simplified Chinese", "scripts_note": "", "segment_level": True},
    "ko": {"name": "Korean", "scripts_note": "Convert to Hangul script. ", "segment_level": False},
    "hi": {"name": "Hindi", "scripts_note": "Convert to Devanagari script. ", "segment_level": False},
    "ja": {"name": "Japanese", "scripts_note": "Use standard Japanese writing (kanji + kana). ", "segment_level": True},
}


def _translate_with_ollama(text: str, target_lang: str = "Simplified Chinese") -> str | None:
    """Translate *text* to *target_lang* via the model TEAM's translate specialist."""
    from backend.services.model_team import chat as team_chat

    lang_meta = SUBTITLE_LANGUAGES.get(target_lang.lower())
    display = (lang_meta or {}).get("name", target_lang) if lang_meta else target_lang
    scripts_note = (lang_meta or {}).get("scripts_note", "")
    system = _TRANSLATION_PROMPT.format(target_lang=display, scripts_note=scripts_note)
    # Route through the team: the light "translate" specialist handles subtitle
    # work while the heavy brain stays free for metadata/scoring.
    return team_chat(
        "translate",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
        num_predict=2048,
    )


def _translate_with_nllb(segments: list[dict[str, Any]], target_lang: str) -> list[dict[str, Any]] | None:
    """PHASE 1: Translate segments via NLLB-200 (CTranslate2), the preferred
    local engine.  Returns None (caller falls back to Ollama) if the engine is
    unavailable."""
    try:
        from backend.services import nllb_translate
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"NLLB module import failed ({exc}); using Ollama translate")
        return None
    try:
        if nllb_translate.available():
            translated = nllb_translate.translate_segments(segments, target_lang)
            if translated is not None:
                return translated
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"NLLB segment translate error ({exc}); using Ollama translate")
    return None


def translate_segments_to_language(
    segments: list[dict[str, Any]],
    target_lang: str = "zh",
) -> list[dict[str, Any]]:
    """Translate segment text to *target_lang* via the model TEAM.

    ``target_lang`` is one of the ``SUBTITLE_LANGUAGES`` codes. Returns a new
    list of segments with ``text`` replaced by the translations. Falls back to
    original text if translation fails or the language is English (keep as-is).
    """
    if not segments:
        return segments
    meta = SUBTITLE_LANGUAGES.get(target_lang.lower())
    if not meta or meta.get("keep_original"):
        return segments

    # ── PHASE 1: prefer NLLB-200 (CTranslate2) ─────────────────────────
    # Deterministic, local, purpose-built for translation.  Returns translated
    # segments directly when available; falls back to the Ollama specialist
    # below.
    nllb_result = _translate_with_nllb(segments, target_lang)
    if nllb_result is not None:
        # Sanity-check CJK like the Ollama path (defensive; NLLB-200 zh output
        # will contain CJK, but guard against a model/config mis-download).
        if target_lang.lower() in ("zh", "ja"):
            joined = " ".join(s.get("text", "") for s in nllb_result)
            if not _has_cjk(joined):
                logger.warning(
                    f"NLLB {meta['name']} translation returned NO CJK "
                    f"(first 120: {joined[:120]!r}) — using original text"
                )
                return segments
        logger.info(
            f"Translated {len(segments)} segments to {meta['name']} (engine=nllb)"
        )
        return nllb_result

    # ── Fallback: Ollama translate specialist (batch mode) ─────────────
    raw_text = "\n".join(s.get("text", "") for s in segments)
    if not raw_text.strip():
        return segments

    # Diagnose the translation lifecycle: the role is acquired ONCE for the whole
    # clip (a single batch call — NOT a per-segment acquire/release loop), so a
    # "translate role evicted mid-translation" bug is structurally impossible here.
    # The failure mode we actually guard against below is a PARTIAL response.
    logger.info(
        f"[TRANSLATE] engine=ollama segments={len(segments)} target={meta['name']} "
        f"(single acquire for whole clip; no per-segment eviction window)"
    )
    translated = _translate_with_ollama(raw_text, meta["name"])
    if not translated:
        logger.error(
            f"[TRANSLATE] {meta['name']} translation returned NOTHING — captions will "
            f"stay {meta.get('keep_original') and 'as-is' or 'in the original language'} "
            f"(BROKEN edit; surfaced loudly, not silently degraded)"
        )
        return segments

    translated_lines = [l.strip() for l in translated.strip().split("\n") if l.strip()]
    logger.info(
        f"[TRANSLATE] ollama returned {len(translated_lines)}/{len(segments)} lines"
    )

    # ── Robust 1:1 mapping (fix for "only first line translated") ──────
    # If the model returns FEWER lines than segments (token truncation, a
    # single-line response, or wrapped output), the OLD code silently padded the
    # tail with the ORIGINAL English — producing a Chinese-first-line-then-
    # English subtitle file with no error. We now LOG LOUDLY and translate each
    # missing segment INDIVIDUALLY so every cue gets the target language.
    result: list[dict[str, Any]] = []
    missing = 0
    for i, seg in enumerate(segments):
        new_seg = dict(seg)
        if i < len(translated_lines):
            new_seg["text"] = translated_lines[i]
        else:
            missing += 1
            logger.warning(
                f"[TRANSLATE] segment {i} missing from batch response; "
                f"translating individually: {seg.get('text', '')[:40]!r}"
            )
            one = _translate_with_ollama(seg.get("text", ""), meta["name"])
            one_txt = (one or "").strip()
            if one_txt and "\n" not in one_txt:
                new_seg["text"] = one_txt
            else:
                logger.error(
                    f"[TRANSLATE] segment {i} individual translation FAILED -> "
                    f"keeping original (BROKEN mixed-language caption)"
                )
                new_seg["text"] = seg.get("text", "")
        result.append(new_seg)

    # ── Loud confirmation / failure surfacing ─────────────────────────
    # For CJK targets every cue must contain CJK characters; any English remnant
    # is a BROKEN user-facing result, not graceful degradation — raise it loudly.
    untranslated = 0
    if target_lang.lower() in ("zh", "ja"):
        for i, s in enumerate(result):
            if not _CJK_RE.search(s.get("text", "")):
                untranslated += 1
        if untranslated:
            logger.error(
                f"[TRANSLATE] BROKEN result: {untranslated}/{len(result)} segments lack "
                f"{meta['name']} characters (partial translation). Captions will be "
                f"mixed-language — review the edit."
            )
        else:
            logger.info(f"[TRANSLATE] ALL {len(result)} segments have CJK chars -> ok")

    logger.info(
        f"Translated {len(segments)} segments to {meta['name']} (engine=ollama, "
        f"individually-recovered={missing})"
    )
    return result


def translate_segments_to_chinese(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate segment text to Simplified Chinese using Ollama.

    Thin wrapper kept for backward compatibility with existing callers.
    """
    return translate_segments_to_language(segments, "zh")


def translate_words_to_chinese(
    words: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate word-level timestamps to Chinese.

    Since word-level translation is imprecise, we translate at the segment
    level and redistribute the Chinese characters across the original word
    timestamps proportionally by character count.
    """
    if not words or not segments:
        return words

    translated_segs = translate_segments_to_chinese(segments)
    if translated_segs == segments:
        return words  # Translation failed, return original

    # Build a mapping of original segment text → Chinese segment text
    seg_map: dict[str, str] = {}
    for orig, trans in zip(segments, translated_segs):
        orig_text = orig.get("text", "").strip()
        trans_text = trans.get("text", "").strip()
        if orig_text and trans_text:
            seg_map[orig_text] = trans_text

    # Redistribute Chinese characters across word timestamps
    result: list[dict[str, Any]] = []
    for w in words:
        new_w = dict(w)
        result.append(new_w)

    # Simple approach: for each word, if its parent segment was translated,
    # replace it with the Chinese equivalent proportionally
    for seg in segments:
        orig_text = seg.get("text", "").strip()
        trans_text = seg_map.get(orig_text, "")
        if not trans_text:
            continue

        # Find words belonging to this segment
        seg_words = [
            w for w in result
            if seg.get("start", 0) <= w.get("start", 0) <= seg.get("end", 0)
        ]
        if not seg_words:
            continue

        # Distribute Chinese characters across words
        total_orig_len = sum(len(w.get("word", "")) for w in seg_words) or 1
        char_idx = 0
        for w in seg_words:
            orig_len = len(w.get("word", ""))
            char_count = max(1, int(len(trans_text) * (orig_len / total_orig_len)))
            w["word"] = trans_text[char_idx:char_idx + char_count]
            char_idx += char_count

        # Remaining characters go to last word
        if char_idx < len(trans_text) and seg_words:
            seg_words[-1]["word"] += trans_text[char_idx:]

    logger.info(f"Translated {len(words)} words to Chinese")
    return result


# ── Time formatting helpers ─────────────────────────────────────────────

def _format_srt_time(seconds: float) -> str:
    """Format seconds as SRT time: ``HH:MM:SS,mmm``."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_vtt_time(seconds: float) -> str:
    """Format seconds as VTT time: ``HH:MM:SS.mmm``."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _format_ass_time(seconds: float) -> str:
    """Format seconds as ASS time: ``H:MM:SS.cc`` (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ── Segment filtering ──────────────────────────────────────────────────

def _filter_segments(
    segments: list[dict[str, Any]],
    clip_start: float,
    clip_end: float | None,
) -> list[dict[str, Any]]:
    """
    Filter segments to those overlapping ``[clip_start, clip_end]`` and
    rebase their timestamps so ``clip_start`` becomes 0.
    """
    filtered: list[dict[str, Any]] = []
    for seg in segments:
        seg_start: float = seg["start"]
        seg_end: float = seg["end"]
        if clip_end is not None and seg_start >= clip_end:
            continue
        if seg_end <= clip_start:
            continue
        # Clamp to window
        adj_start = max(seg_start, clip_start) - clip_start
        adj_end = (min(seg_end, clip_end) if clip_end is not None else seg_end) - clip_start
        new_seg = dict(seg)
        new_seg["start"] = round(adj_start, 3)
        new_seg["end"] = round(adj_end, 3)
        filtered.append(new_seg)
    return filtered


# ── Public API ──────────────────────────────────────────────────────────

@timed(logger_name="processing")
def generate_srt(
    segments: list[dict[str, Any]],
    output_path: Path,
    clip_start: float = 0.0,
    clip_end: float | None = None,
) -> Path:
    """
    Generate an SRT subtitle file from transcript segments.

    Args:
        segments: Transcript segments (each with ``start``, ``end``, ``text``).
        output_path: Destination ``.srt`` file path.
        clip_start: Start time of the clip in the original video (for offset).
        clip_end: End time of the clip in the original video.

    Returns:
        Path to the written SRT file.
    """
    filtered = _filter_segments(segments, clip_start, clip_end)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for idx, seg in enumerate(filtered, start=1):
        lines.append(str(idx))
        lines.append(
            f"{_format_srt_time(seg['start'])} --> {_format_srt_time(seg['end'])}"
        )
        lines.append(seg.get("text", ""))
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"SRT written: {output_path.name} ({len(filtered)} cues)")
    return output_path


@timed(logger_name="processing")
def generate_vtt(
    segments: list[dict[str, Any]],
    output_path: Path,
    clip_start: float = 0.0,
    clip_end: float | None = None,
) -> Path:
    """
    Generate a WebVTT subtitle file from transcript segments.

    Args:
        segments: Transcript segments (each with ``start``, ``end``, ``text``).
        output_path: Destination ``.vtt`` file path.
        clip_start: Start time of the clip in the original video.
        clip_end: End time of the clip in the original video.

    Returns:
        Path to the written VTT file.
    """
    filtered = _filter_segments(segments, clip_start, clip_end)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = ["WEBVTT", ""]
    for idx, seg in enumerate(filtered, start=1):
        lines.append(str(idx))
        lines.append(
            f"{_format_vtt_time(seg['start'])} --> {_format_vtt_time(seg['end'])}"
        )
        lines.append(seg.get("text", ""))
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"VTT written: {output_path.name} ({len(filtered)} cues)")
    return output_path


@timed(logger_name="processing")
def generate_ass_with_highlights(
    words: list[dict[str, Any]],
    output_path: Path,
    clip_start: float = 0.0,
    clip_end: float | None = None,
    font: str = "Arial",
    font_size: int = 65,
    color: str = "#FFFFFF",
    highlight_color: str = "#FFD700",
) -> Path:
    """
    Generate an ASS subtitle file with per-word highlight (karaoke-style).

    Each word is rendered in the default colour, and the currently spoken
    word is shown in ``highlight_color``.  This creates the "bouncing
    word" effect popular on short-form platforms.

    Args:
        words: Word-level timestamps (each with ``word``, ``start``, ``end``).
        output_path: Destination ``.ass`` file path.
        clip_start: Start time of the clip in the original video.
        clip_end: End time of the clip in the original video.
        font: Font family name.
        font_size: Font point size.
        color: Default text colour (hex ``#RRGGBB``).
        highlight_color: Active-word colour (hex ``#RRGGBB``).

    Returns:
        Path to the written ASS file.
    """
    # Filter words to clip range and rebase
    filtered = _filter_segments(words, clip_start, clip_end)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _hex_to_ass_color(hex_color: str) -> str:
        """Convert ``#RRGGBB`` to ASS ``&HBBGGRR&``."""
        h = hex_color.lstrip("#")
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H{b}{g}{r}&"

    primary_color = _hex_to_ass_color(color)
    highlight_ass = _hex_to_ass_color(highlight_color)

    # ASS header
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{font_size},{primary_color},&H000000FF,"
        "&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,30,30,60,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    # Group words into display lines (≈ 6 words per line)
    words_per_line = 6
    dialogue_lines: list[str] = []

    for chunk_start in range(0, len(filtered), words_per_line):
        chunk = filtered[chunk_start : chunk_start + words_per_line]
        if not chunk:
            continue

        line_start = chunk[0]["start"]
        line_end = chunk[-1]["end"]

        # Build karaoke text: for each word, apply highlight colour during
        # its active time using ASS \kf (smooth karaoke fill) tags.
        text_parts: list[str] = []
        for i, w in enumerate(chunk):
            word_dur_cs = int(round((w["end"] - w["start"]) * 100))
            word_dur_cs = max(word_dur_cs, 1)
            # {\kf<dur>} renders the word with a fill effect
            # {\1c<color>} sets the primary colour for the highlighted word
            text_parts.append(
                f"{{\\kf{word_dur_cs}}}{{\\1c{highlight_ass}}}{w['word']}"
            )

        # Add a bounce pop-in animation to the start of the line
        pop_anim = "{\\fscx0\\fscy0\\t(0,150,\\fscx110\\fscy110)\\t(150,250,\\fscx100\\fscy100)}"
        text = pop_anim + " ".join(text_parts)
        
        start_str = _format_ass_time(line_start)
        end_str = _format_ass_time(line_end)
        dialogue_lines.append(
            f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}"
        )

    content = header + "\n".join(dialogue_lines) + "\n"
    output_path.write_text(content, encoding="utf-8")

    logger.info(
        f"ASS written: {output_path.name} "
        f"({len(filtered)} words, {len(dialogue_lines)} lines)"
    )
    return output_path


@timed(logger_name="processing")
def generate_ass_from_segments(
    segments: list[dict[str, Any]],
    output_path: Path,
    clip_start: float = 0.0,
    clip_end: float | None = None,
    font: str = "Arial",
    font_size: int = 65,
    color: str = "#FFFFFF",
    highlight_color: str = "#FFD700",
) -> Path:
    """
    Generate an ASS subtitle file from whole transcript segments
    (no word-level timestamps needed).

    Used as a robust fallback when word timestamps are unavailable, so
    burned-in captions still work for every clip.  Text is styled in the
    primary colour with a dark outline and a semi-transparent box for
    readability on any background.

    Args:
        segments: Transcript segments (each with ``start``, ``end``, ``text``).
        output_path: Destination ``.ass`` file path.
        clip_start: Start time of the clip in the original video.
        clip_end: End time of the clip in the original video.
        font: Font family name.
        font_size: Font point size.
        color: Default text colour (hex ``#RRGGBB``).
        highlight_color: Accent colour (used for a pop-in animation).

    Returns:
        Path to the written ASS file.
    """
    filtered = _filter_segments(segments, clip_start, clip_end)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _hex_to_ass_color(hex_color: str) -> str:
        h = hex_color.lstrip("#")
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H{b}{g}{r}&"

    primary_color = _hex_to_ass_color(color)
    accent_ass = _hex_to_ass_color(highlight_color)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{font_size},{primary_color},&H000000FF,"
        "&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,2,1,2,40,40,70,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    max_lines = 2
    lines_per_cue = 2
    dialogue_lines: list[str] = []

    for seg in filtered:
        text: str = seg.get("text", "").strip()
        if not text:
            continue
        chunks = [
            text[i : i + 42]
            for i in range(0, len(text), 42)
        ][:max_lines]
        display_text = "\\N".join(chunks)

        # Show the whole cue in the accent colour with a pop-in animation.
        pop = "{\\fscx0\\fscy0\\t(0,150,\\fscx110\\fscy110)\\t(150,250,\\fscx100\\fscy100)}"
        full = f"{pop}{{\\1c{accent_ass}}}{_escape_ass_text(display_text)}"

        start_str = _format_ass_time(seg["start"])
        end_str = _format_ass_time(seg["end"])
        dialogue_lines.append(
            f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{full}"
        )

    if not dialogue_lines:
        # Keep an empty cue set so the burn filter still has a valid file.
        dialogue_lines.append(
            "Dialogue: 0,0:00:00.00,0:00:00.10,Default,,0,0,0,,"
        )

    content = header + "\n".join(dialogue_lines) + "\n"
    output_path.write_text(content, encoding="utf-8")

    logger.info(
        f"ASS (segments) written: {output_path.name} ({len(dialogue_lines)} cues)"
    )
    return output_path


@timed(logger_name="processing")
def generate_ass_professional(
    words: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
    output_path: Path | None = None,
    clip_start: float = 0.0,
    clip_end: float | None = None,
    font: str = "Arial",
    font_size: int = 72,
    color: str = "#FFFFFF",
    highlight_color: str = "#FFD700",
    glow_enabled: bool = True,
    is_cjk: bool | None = None,
) -> Path:
    """Generate a professional Premiere-style ASS subtitle file.

    Creates visually stunning burned-in captions with:
    - CJK font auto-detection (Microsoft YaHei for Chinese, configurable for others)
    - Glow/light effect around text (via layered shadow + outline)
    - Word-by-word karaoke highlighting with smooth fill animation
    - Pop-in bounce animation at line start
    - Semi-transparent dark outline for readability on any background
    - Proper vertical video positioning (bottom center, safe margins)

    Falls back to segment-level rendering when word timestamps are absent.

    Args:
        words: Word-level timestamps (preferred). Each: ``{word, start, end}``.
        segments: Segment-level data (fallback). Each: ``{start, end, text}``.
        output_path: Destination ``.ass`` file path.
        clip_start: Start time of the clip in the original video.
        clip_end: End time of the clip in the original video.
        font: Base font family (auto-switched to CJK font if needed).
        font_size: Font point size.
        color: Default text colour (hex ``#RRGGBB``).
        highlight_color: Active-word colour (hex ``#RRGGBB``).
        glow_enabled: Add a soft glow/bloom effect around the text.

    Returns:
        Path to the written ASS file.
    """
    if output_path is None:
        raise ValueError("output_path is required")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _hex_to_ass(hex_color: str) -> str:
        """Convert ``#RRGGBB`` to ASS ``&HBBGGRR&``."""
        h = hex_color.lstrip("#")
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H{b}{g}{r}&"

    def _hex_to_ass_alpha(hex_color: str, alpha: int = 0) -> str:
        """Convert ``#RRGGBB`` to ASS ``&HAABBGGRR`` with alpha (0=opaque, 255=invisible)."""
        h = hex_color.lstrip("#")
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H{alpha:02X}{b}{g}{r}&"

    # Detect if text is CJK-heavy to auto-select font.
    # If the caller passed is_cjk explicitly, honour it (avoids the common
    # bug where word-level timestamps are English but segment text is Chinese).
    if is_cjk is None:
        sample_text = ""
        # Check BOTH words and segments — translated Chinese text lives in
        # segments, while word timestamps may still be the original English.
        if segments:
            sample_text = " ".join(s.get("text", "") for s in segments[:10])
        if words and not _has_cjk(sample_text):
            sample_text = " ".join(w.get("word", "") for w in words[:50])
        is_cjk = _has_cjk(sample_text)

    actual_font = _cjk_font_for(font, "是" if is_cjk else "")

    # CJK text needs slightly larger font for readability
    actual_font_size = font_size
    if is_cjk:
        actual_font_size = max(font_size, 76)

    primary = _hex_to_ass(color)
    highlight = _hex_to_ass(highlight_color)
    outline_col = _hex_to_ass_alpha("#000000", 0)  # solid black outline
    shadow_col = _hex_to_ass_alpha("#000000", 120)  # semi-transparent shadow

    # Glow effect: uses a second style with a colored shadow
    glow_col = _hex_to_ass_alpha(highlight_color, 100) if glow_enabled else shadow_col

    # BorderStyle 1 = outline + shadow; Outline=3 for thick readable edge;
    # Shadow=2 for depth; Alignment=2 = bottom-center (vertical safe zone)
    # MarginV=90 keeps subtitles clear of the bottom edge on phone screens
    outline_w = "3" if not is_cjk else "4"
    shadow_w = "4" if glow_enabled else "2"

    # ── ASS Header ──────────────────────────────────────────────────────
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{actual_font},{actual_font_size},{primary},"
        f"&H000000FF,{outline_col},{shadow_col},"
        f"-1,0,0,0,100,100,1,0,1,{outline_w},{shadow_w},"
        f"2,40,40,90,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    dialogue_lines: list[str] = []

    # ── Word-level karaoke rendering ────────────────────────────────────
    if words:
        filtered = _filter_segments(words, clip_start, clip_end)
        words_per_line = 5 if is_cjk else 6  # CJK chars are wider

        for chunk_start in range(0, len(filtered), words_per_line):
            chunk = filtered[chunk_start:chunk_start + words_per_line]
            if not chunk:
                continue

            line_start = chunk[0]["start"]
            line_end = chunk[-1]["end"]

            # Build karaoke text with per-word highlighting
            text_parts: list[str] = []
            for w in chunk:
                word_dur_cs = max(int(round((w["end"] - w["start"]) * 100)), 1)
                word_text = _escape_ass_text(w.get("word", ""))
                if not word_text.strip():
                    continue
                # \kf = smooth karaoke fill from left to right
                # \1c = primary colour (highlight) for the active word
                text_parts.append(
                    f"{{\\kf{word_dur_cs}}}{{\\1c{highlight}}}{word_text}"
                )

            if not text_parts:
                continue

            # Pop-in bounce animation: scale 0→110%→100% over 250ms
            pop = (
                "{\\fscx0\\fscy0"
                "\\t(0,120,\\fscx110\\fscy110)"
                "\\t(120,250,\\fscx100\\fscy100)}"
            )

            # Optional glow pulse: gentle opacity flicker on the line
            glow_tag = ""
            if glow_enabled:
                glow_tag = "{\\4c&H00FFFF&\\4a&H40&}"

            text = pop + glow_tag + " ".join(text_parts)
            start_str = _format_ass_time(line_start)
            end_str = _format_ass_time(line_end)
            dialogue_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}"
            )

    # ── Segment-level fallback ──────────────────────────────────────────
    elif segments:
        filtered_segs = _filter_segments(segments, clip_start, clip_end)
        max_chars = 16 if is_cjk else 42
        max_lines = 2

        for seg in filtered_segs:
            text: str = seg.get("text", "").strip()
            if not text:
                continue

            # Word-wrap at character limit
            chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)][:max_lines]
            display_text = "\\N".join(chunks)
            display_text = _escape_ass_text(display_text)

            # Whole-cue accent colour with pop-in
            pop = (
                "{\\fscx0\\fscy0"
                "\\t(0,120,\\fscx110\\fscy110)"
                "\\t(120,250,\\fscx100\\fscy100)}"
            )
            glow_tag = "{\\4c&H00FFFF&\\4a&H40&}" if glow_enabled else ""
            full = f"{pop}{glow_tag}{{\\1c{highlight}}}{display_text}"

            start_str = _format_ass_time(seg["start"])
            end_str = _format_ass_time(seg["end"])
            dialogue_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{full}"
            )

    if not dialogue_lines:
        dialogue_lines.append(
            "Dialogue: 0,0:00:00.00,0:00:00.10,Default,,0,0,0,,"
        )

    content = header + "\n".join(dialogue_lines) + "\n"
    output_path.write_text(content, encoding="utf-8")

    logger.info(
        f"Professional ASS written: {output_path.name} "
        f"({len(dialogue_lines)} cues, font={actual_font}, "
        f"cjk={is_cjk}, glow={glow_enabled})"
    )
    return output_path


def _escape_ass_text(text: str) -> str:
    """Escape ASS syntax characters that could break the dialogue line."""
    return (
        text.replace("\\", "\\q")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", "\\N")
    )
