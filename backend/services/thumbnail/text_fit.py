"""
Pure text-fit function for thumbnail headlines.

Binary-searches font size from ``max_size`` down to ``min_size``,
word-wrapping the text into at most ``max_lines`` lines with a balanced
split at each candidate size.  Returns the LARGEST size where every line
fits within ``max_width_px``.

This module has **no image I/O** — it only measures text width via PIL
FreeType metrics and is fully unit-testable in isolation.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Callable


# ── Font resolution (cross-platform) ──────────────────────────────────

_FONT_DIR: dict[str, Path] = {
    "Windows": Path("C:/Windows/Fonts"),
    "Linux": Path("/usr/share/fonts/truetype/dejavu"),
    "Darwin": Path("/System/Library/Fonts"),
}

_FONT_ALIASES: dict[str, dict[str, str]] = {
    "ariblk":   {"Windows": "ariblk.ttf",   "Linux": "DejaVuSans-Bold.ttf"},
    "arialbd":  {"Windows": "arialbd.ttf",   "Linux": "DejaVuSans-Bold.ttf"},
    "arial":    {"Windows": "arial.ttf",     "Linux": "DejaVuSans.ttf"},
    "arialuni": {"Windows": "arialuni.ttf",  "Linux": "DejaVuSans.ttf"},
    "seguisym": {"Windows": "seguisym.ttf",  "Linux": "DejaVuSans.ttf"},
    "seguiemj": {"Windows": "seguiemj.ttf",  "Linux": "DejaVuSans.ttf"},
}


def resolve_font(name: str, fallback: str = "arial") -> str:
    """Resolve a font name to an existing absolute path.

    Accepts a Windows path (``C:/Windows/Fonts/ariblk.ttf``), a bare
    name (``ariblk``), or a full filesystem path.  Returns the best
    match on the current OS; falls back through *fallback*, then any
    available TTF in the platform font directory.
    """
    system = platform.system()
    font_dir = _FONT_DIR.get(system, _FONT_DIR["Linux"])

    def _try(candidate: str) -> str | None:
        for key, variants in _FONT_ALIASES.items():
            if key in candidate.lower():
                filename = variants.get(system, variants.get("Linux"))
                path = font_dir / filename
                if path.exists():
                    return str(path)
        # If the caller passed a full path that exists, use it directly
        p = Path(candidate)
        if p.exists():
            return str(p)
        return None

    result = _try(name)
    if result:
        return result
    result = _try(fallback)
    if result:
        return result
    # Last resort: any .ttf in the font directory
    for f in sorted(font_dir.glob("*.ttf")):
        return str(f)
    return str(Path(name))


# ── Text measurement ───────────────────────────────────────────────────

_MEASURE_CACHE: dict[tuple[str, int], Callable[[str], float]] = {}


def make_measure(font_path: str, size: int) -> Callable[[str], float]:
    """Return ``measure(text) -> pixel_width`` using PIL FreeType.

    Caches the loaded FreeType font so per-cue / per-size measurements
    don't reload the font object repeatedly.
    """
    key = (font_path, size)
    cached = _MEASURE_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        from PIL import ImageFont

        font = ImageFont.truetype(font_path, int(size))

        def _m(text: str) -> float:
            return float(font.getlength(str(text or "")))

        measure = _m
    except Exception:
        # Conservative per-char estimate (over-measures → safer)
        def _m(text: str) -> float:
            return 0.60 * int(size) * len(str(text or "")) + 8.0

        measure = _m

    _MEASURE_CACHE[key] = measure
    return measure


# ── Balanced word-wrap ─────────────────────────────────────────────────

def _clamp_ellipsis(
    word: str,
    measure: Callable[[str], float],
    max_width: float,
) -> str:
    """Shrink *word* so ``measure(word + '…') <= max_width``; min one glyph."""
    if measure(word) <= max_width:
        return word
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


def wrap_balanced(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    max_lines: int = 2,
) -> list[str]:
    """Word-wrap *text* into at most *max_lines* lines, each ≤ *max_width*.

    Uses balanced splitting: finds the word split that minimizes the width
    difference between lines while keeping both within *max_width*.  Falls
    back to ellipsis-clamp for unavoidable overflows.
    """
    text = (str(text or "")).strip()
    if not text:
        return []
    if measure(text) <= max_width:
        return [text]
    if " " not in text:
        return [_clamp_ellipsis(text, measure, max_width)]

    words = text.split()
    if max_lines <= 1:
        return [_clamp_ellipsis(text, measure, max_width)]

    # Balanced 2-line reflow: find the split that minimizes width difference.
    best_split: int | None = None
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
        return [
            " ".join(words[:best_split]),
            " ".join(words[best_split:]),
        ]

    # No balanced 2-line split exists — fill line 1 as far as it will go,
    # ellipsized remainder on line 2.
    carry = 0
    for k in range(len(words), 0, -1):
        if measure(" ".join(words[:k])) <= max_width:
            carry = k
            break
    l1 = (
        " ".join(words[:carry])
        if carry
        else _clamp_ellipsis(words[0], measure, max_width)
    )
    if carry >= len(words):
        return [l1]
    tail = " ".join(words[carry:])
    l2 = _clamp_ellipsis(tail, measure, max_width)
    return [l1, l2]


# ── Public API ─────────────────────────────────────────────────────────


def _measures_ok(lines: list[str], measure: Callable[[str], float], max_width: float) -> bool:
    """True if every line fits within the width budget."""
    return bool(lines) and all(measure(ln) <= max_width for ln in lines)


def _uses_ellipsis(lines: list[str], mid: int, min_size: int) -> bool:
    """True if the wrap truncated any line (ellipsis marker present) and we
    are above *min_size* — meaning a smaller font could have avoided the
    truncation.  Ellipsization is not allowed above min_size (matches the
    'never truncate' contract); at min_size it's the only safety net left."""
    if mid == min_size:
        return False
    return any("…" in ln for ln in lines)


def fit_text(
    text: str,
    max_width_px: int,
    max_lines: int,
    font_path: str,
    max_size: int,
    min_size: int,
) -> tuple[list[str], int]:
    """Binary-search font size for *text* to fit within *max_width_px*.

    Returns ``(lines, font_size)`` where *lines* is the balanced word-wrap
    at the largest fitting size.  **Rejects ellipsized (truncated) wraps
    above *min_size*** — if a candidate size has to chop a line with ``…``,
    a smaller font that fits without truncation is preferred, so the font
    is never inflated at the cost of chopped content.  Only at *min_size* is
    an ellipsized wrap accepted as a final safety net (Stage 4 should have
    bounded the input, so this is rare).
    """
    lo, hi = min_size, max_size
    best_size = min_size
    best_lines: list[str] = []

    while lo <= hi:
        mid = (lo + hi) // 2
        measure = make_measure(font_path, mid)
        lines = wrap_balanced(text, measure, float(max_width_px), max_lines)
        if _measures_ok(lines, measure, float(max_width_px)) and not _uses_ellipsis(lines, mid, min_size):
            best_size = mid
            best_lines = lines
            lo = mid + 1  # try larger
        else:
            hi = mid - 1  # too big / truncates — try smaller

    if not best_lines:
        # Fallback: force-render at min_size.  A mini_size ellipsized wrap is
        # the only tolerable truncation — Stage 4 should have bounded the
        # input so this is a rare safety net.
        measure = make_measure(font_path, min_size)
        best_lines = wrap_balanced(text, measure, float(max_width_px), max_lines)
        best_size = min_size

    return best_lines, best_size
