"""
Stage 6 — Validator + Fallback Ladder.

Validates a composited thumbnail for image integrity (dimensions,
blankness, file size).  The three-tier fallback ladder is orchestrated
by :mod:`engine`; this module provides the validation checks that gate
each tier's output.
"""

from __future__ import annotations

from pathlib import Path

from backend.utils.logging import get_logger

logger = get_logger("thumbnail.validator")


def validate_image(
    thumbnail_path: Path,
    min_width: int = 540,
    min_height: int = 960,
) -> tuple[bool, str]:
    """Validate a thumbnail image.

    Checks:
      1. File exists and is non-trivial (> 1 KB).
      2. Image dimensions are within reasonable bounds.
      3. Image isn't mostly black or mostly white (histogram check).

    Returns ``(is_valid, reason)``.
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        # Validation best-effort — if PIL/numpy unavailable, skip checks.
        return True, "PIL/numpy unavailable; skipping validation"

    if not thumbnail_path.exists():
        return False, "File does not exist"

    fsize = thumbnail_path.stat().st_size
    if fsize < 1024:
        return False, f"File too small ({fsize} bytes)"

    try:
        img = Image.open(thumbnail_path)
        w, h = img.size
    except Exception as exc:
        return False, f"Cannot open image: {exc}"

    if w < min_width or h < min_height:
        return False, f"Image too small: {w}x{h} (need ≥{min_width}x{min_height})"

    # Histogram check: reject mostly-black or mostly-white images.
    try:
        arr = np.array(img.convert("L"))  # grayscale
        mean_val = float(arr.mean())
        if mean_val < 15:
            return False, f"Image mostly black (mean={mean_val:.1f})"
        if mean_val > 240:
            return False, f"Image mostly white (mean={mean_val:.1f})"
    except Exception:
        pass  # histogram check is best-effort

    return True, "ok"
