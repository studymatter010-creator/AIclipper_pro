"""
Integration + fallback-ladder tests for the thumbnail engine.

These run the **full** six-stage pipeline (extract → score → crop → copy →
compose → validate) against real clips, and verify the three-tier fallback
ladder by forcing tiers to fail on purpose.

They need real media and FFmpeg, so they live under ``@pytest.mark.integration``
and **skip** cleanly when a usable environment isn't available.  Logs
``thumbnail_tier=N | filename | score | headline`` for every render so the
verification can be eyeballed in CI logs or the terminal.
"""

import os
import sys
from pathlib import Path

import pytest

# Resolve project root (tests/ -> project root) so the output clips can be found.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (PROJECT_ROOT / "outputs").exists(),
        reason="no ./outputs/ clips directory available",
    ),
]


def _ffmpeg_available() -> bool:
    """Best-effort check that an ffmpeg binary is on the system."""
    import shutil

    return shutil.which("ffmpeg") is not None


def _discover_clips(limit: int = 5) -> list[Path]:
    """Return up to *limit* real clip files from ./outputs/."""
    out = PROJECT_ROOT / "outputs"
    clips = sorted(
        [p for p in out.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mkv")],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return clips[:limit]


@pytest.fixture(scope="module")
def clips():
    """Up to 5 real clips for integration testing, or skip if none/missing ffmpeg."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not found on PATH")
    found = _discover_clips(5)
    if not found:
        pytest.skip("no clips found in ./outputs/")
    return found


def test_engine_full_pipeline_tier1(clips, tmp_path):
    """Tier 1 (full design) must fire for normal clips."""
    from backend.services.thumbnail import render_thumbnail

    any_tier1 = False
    for clip in clips:
        hook = "This is a test hook sentence for the thumbnail!"
        title = clip.stem.replace("_", " ").title()
        out = tmp_path / f"{clip.stem}_thumb.jpg"
        path, tier, score = render_thumbnail(
            clip, hook, title, out, face_data=None
        )
        print(
            f"[thumbnail_tier={tier}] {clip.name} score={score:.4f} "
            f"-> {out.name}"
        )
        assert out.exists(), f"{clip.name} produced no thumbnail"
        assert out.stat().st_size > 1024, f"{clip.name} thumbnail too small"
        assert tier in (1, 2, 3), f"{clip.name} unexpectedly tier 0"
        if tier == 1:
            any_tier1 = True

    # Tier 1 is the expected default — failing to hit it on ANY clip means a
    # design-tier bug worth investigating (the spec's own litmus test).
    assert any_tier1, (
        "No clip reached Tier 1 — check thumbnail_tier logs; "
        "Tier 1 should fire for normal inputs"
    )


def test_engine_force_compositor_throw(tier_clips_fixture, tmp_path):
    """Force Stage 5 (compositor.compose) to throw → engine falls to Tier 2."""
    from backend.services.thumbnail import engine

    clip = tier_clips_fixture
    out = tmp_path / "forced_tier2.jpg"

    def _boom(*_a, **_k):
        raise RuntimeError("Fabricated Stage-5 failure for test")

    original = engine.compositor.compose
    engine.compositor.compose = _boom
    try:
        path, tier, _score = engine.render_thumbnail(
            clip, "Forced Tier 2 headline!", "Fallback", out
        )
    finally:
        engine.compositor.compose = original

    assert tier == 2, f"expected Tier 2, got {tier}"
    assert out.exists() and out.stat().st_size > 1024


def test_engine_force_tier2_throw(tier_clips_fixture, tmp_path):
    """Force Tier 2 (_compose_tier2) to throw → engine falls to Tier 3."""
    from backend.services.thumbnail import engine

    clip = tier_clips_fixture
    out = tmp_path / "forced_tier3.jpg"

    def _boom(*_a, **_k):
        raise RuntimeError("Fabricated Tier-2 failure for test")

    # Disable the full compositor too, so we start from a Tier-2 attempt,
    # then force the Tier-2 compositor to also fail.
    original_compose = engine.compositor.compose
    original_tier2 = engine.compositor.compose_tier2
    engine.compositor.compose = _boom
    engine.compositor.compose_tier2 = _boom
    try:
        path, tier, _score = engine.render_thumbnail(
            clip, "Forced Tier 3 headline!", "Fallback", out
        )
    finally:
        engine.compositor.compose = original_compose
        engine.compositor.compose_tier2 = original_tier2

    assert tier == 3, f"expected Tier 3, got {tier}"
    assert out.exists() and out.stat().st_size > 1024


def test_engine_style_override_reaches_tier1(
    clips, tmp_path
):
    """A ``style`` dict (thumbnail override flags) passed to the engine must
    reach the Tier-1 compositor and change the rendered pixels.  This is the
    contract that makes UI-facing thumbnail overrides actually do something;
    if the plumbing silently dropped ``style`` this test fails."""
    import numpy as np
    from PIL import Image

    from backend.services.thumbnail import engine

    clip = clips[0]
    base = tmp_path / "ovr_default.jpg"
    styled = tmp_path / "ovr_red.jpg"

    # Same clip, same headline; default vs cta_fill=red override.
    head = "Override test headline, watch this one please!"
    _, tb, _ = engine.render_thumbnail(clip, head, "Fallback", base)
    _, ts, _ = engine.render_thumbnail(
        clip, head, "Fallback", styled,
        style={"cta_fill": (255, 0, 0)},
    )
    assert tb == 1 and ts == 1, f"expected Tier 1 for both, got {tb}/{ts}"

    a = np.array(Image.open(styled).convert("RGB")).astype(int)
    # Red pixels (r>200, g<80, b<80) inside the CTA band (y 1690..1830).
    zone = a[1690:1830, :, :]
    red = (zone[..., 0] > 200) & (zone[..., 1] < 80) & (zone[..., 2] < 80)
    assert int(red.sum()) > 500, (
        "cta_fill=red override produced no red pixels in the CTA zone — "
        "override flag did not reach the compositor"
    )


def test_compositor_palette_merge_defaults():
    """Omitted override keys fall back to defaults; unknown keys are ignored;
    None keeps the untouched defaults.  Proves the override flags are safe."""
    from backend.services.thumbnail.compositor import (
        DEFAULT_PALETTE,
        _resolve_palette,
    )

    assert _resolve_palette(None) is DEFAULT_PALETTE
    merged = _resolve_palette({"cta_show": False, "bogus": 123})
    assert merged["cta_show"] is False
    assert merged["cta_fill"] == DEFAULT_PALETTE["cta_fill"]
    assert "bogus" not in merged


@pytest.fixture
def tier_clips_fixture():
    """A single real clip to drive fallback-ladder force-failure tests."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not found on PATH")
    found = _discover_clips(1)
    if not found:
        pytest.skip("no clips found in ./outputs/")
    return found[0]