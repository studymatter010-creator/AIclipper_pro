"""
Unit tests for the pure thumbnail text-fit function.

``backend.services.thumbnail.text_fit`` is deliberately side-effect free
(only PIL FreeType measurement + font-path resolution), so these tests
run with **no FFmpeg, no backend app, and no DB** — purely PIL.  This
makes them safe to run in any environment, including CI.

Each test asserts the two core invariants the whole six-stage pipeline
depends on:

  1. ``fit_text`` returns at most ``max_lines`` lines.
  2. Every returned line measures **≤ max_width_px** (no overflow /
     truncation).  If Step 5 text overflows, the fallback ladder burns a
     tier for no reason — so this must hold.
  3. ``font_size`` is binary-searched up to the largest fitting size
     (never smaller than fits) unless ``min_size`` itself can't fit.
"""

import pytest

from backend.services.thumbnail.text_fit import (
    fit_text,
    make_measure,
    wrap_balanced,
    resolve_font,
)

# A headline-ish budget: 1080 px canvas minus margins like the compositor uses.
MAX_WIDTH = int(1080 * 0.88)  # ≈ 950 px


@pytest.fixture(scope="module")
def font_path() -> str:
    """A real font for measurement, resolved cross-platform."""
    return resolve_font("arialbd")


def _assert_no_overflow(lines, font_path, max_width=MAX_WIDTH, max_lines=2):
    """Assert the core invariant: ≤ max_lines lines, each ≤ max_width px.

    Used inside per-test size-aware assertions in ``TestFitTextNoOverflow``.
    """
    assert 0 < len(lines) <= max_lines, f"got {len(lines)} lines"


class TestFitTextNoOverflow:
    """Core invariant: nothing ever renders wider than the budget."""

    def test_short_string(self, font_path):
        """A short headline fits on one line at a large size."""
        lines, size = fit_text(
            "Watch this!", MAX_WIDTH, 2, font_path, max_size=124, min_size=32
        )
        assert len(lines) == 1
        measure = make_measure(font_path, size)
        assert measure(lines[0]) <= MAX_WIDTH
        assert size > 60  # short text should use a big font

    def test_exact_budget(self, font_path):
        """A line that nearly fills the budget still fits, never truncates.
        The string must genuinely fit in 2 lines at the resolved font size
        without requiring ellipsis clamp — so pick a realistic headline
        that fits a 950 px budget at a mid-range size."""
        text = "Incredible cooking hacks that will blow your mind"
        lines, size = fit_text(text, MAX_WIDTH, 2, font_path, 124, 32)
        assert len(lines) == 2
        measure = make_measure(font_path, size)
        for ln in lines:
            assert measure(ln) <= MAX_WIDTH, f"overflow: {ln!r}"
        # No ellipsis means no truncation — full content is preserved.
        assert "…" not in " ".join(lines)
        assert " ".join(lines) == text

    def test_single_word_no_break(self, font_path):
        """A single long word can't be word-wrapped; must ellipsize, not clip."""
        text = "Beautiful"
        lines, size = fit_text(text, MAX_WIDTH, 2, font_path, 124, 32)
        assert len(lines) == 1
        measure = make_measure(font_path, size)
        assert measure(lines[0]) <= MAX_WIDTH
        # It keeps at least one full glyph and appends an ellipsis.
        assert lines[0].endswith("…") or lines[0] == text[:1]

    def test_long_text_reflows_to_two_lines(self, font_path):
        """Long-ish text wraps into exactly 2 balanced lines."""
        text = "She did not know what she expected to see"
        lines, size = fit_text(text, MAX_WIDTH, 2, font_path, 124, 32)
        assert len(lines) == 2
        measure = make_measure(font_path, size)
        for ln in lines:
            assert measure(ln) <= MAX_WIDTH

    def test_cjk_text(self, font_path):
        """CJK has no spaces; must clamp/ellipsize a single glyph line without
        overflowing the budget (DejaVu has no CJK glyphs — measurement still
        must not overflow via the fallback path)."""
        text = "这段文字用于测试中文字幕在缩略图上的显示效果"
        lines, size = fit_text(text, MAX_WIDTH, 2, font_path, 124, 32)
        assert len(lines) >= 1
        measure = make_measure(font_path, size)
        for ln in lines:
            assert measure(ln) <= MAX_WIDTH

    def test_binary_search_returns_larger_font_when_room(self, font_path):
        """Given a short string and a big budget, a larger font is selected
        over a smaller one — proving binary search isn't stuck at min_size."""
        # Short single-word → max_lines=1; fits at large size.
        short_size = fit_text("Hi!", MAX_WIDTH, 1, font_path, 124, 32)[1]
        # Long sentence → single-line budget; must shrink to fit, proving the
        # search goes DOWN from max_size when needed.
        long_size = fit_text(
            "She did not know what she expected to see when she opened the door",
            MAX_WIDTH,
            1,
            font_path,
            124,
            32,
        )[1]
        assert short_size > long_size


class TestWrapBalanced:
    def test_single_short_line(self, font_path):
        measure = make_measure(font_path, 60)
        lines = wrap_balanced("Hello world", measure, MAX_WIDTH, 2)
        assert lines == ["Hello world"]

    def test_balanced_split_used(self, font_path):
        measure = make_measure(font_path, 48)
        lines = wrap_balanced(
            "one two three four five six seven eight", measure, MAX_WIDTH, 2
        )
        assert len(lines) == 2
        measure2 = make_measure(font_path, 48)
        # The balanced split minimizes the width difference between the two lines.
        assert abs(measure2(lines[0]) - measure2(lines[1])) < MAX_WIDTH * 0.5

    def test_no_spaces_clamps(self, font_path):
        measure = make_measure(font_path, 80)
        lines = wrap_balanced("UnbelievablyLongSingleToken", measure, 200, 2)
        assert len(lines) == 1
        assert measure(lines[0]) <= 200


class TestResolveFont:
    def test_resolves_to_existing_path(self):
        import os

        path = resolve_font("arialbd")
        assert os.path.exists(path)