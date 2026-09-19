"""
Thumbnail Engine — orchestrates all six stages.

This is the **single entry point** the rest of the app calls.  Every
generated thumbnail logs ``thumbnail_tier=N`` (1, 2, or 3) — the single
most important debugging signal for the thumbnail pipeline.

All compositing now renders in **PIL** (:mod:`compositor`), not FFmpeg, so
thumbnail generation is independent of the machine's FFmpeg build (a known
Windows FFmpeg ``jpg -> jpg`` drawtext access-violation, rc=3221225477, used
to collapse the whole ladder to a bare crop).

Tier definitions:
  **Tier 1 (full design):** glow + scrim + corner mark + CTA
    + border + hook-sentence headline via ``text_fit``.  No "AI" signage.
  **Tier 2 (simplified):** full-bleed crop + dark scrim + headline text.
  **Tier 3 (last resort):** plain cropped frame + solid-color bottom
    strip + headline in a basic system font.
  **Tier 0:** all tiers failed — the raw cropped frame is used as a
    placeholder (should be treated as a bug to investigate).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from backend.utils.logging import get_logger, timed

from . import compositor, copy, crop, candidates, scoring, validator

logger = get_logger("thumbnail.engine")


@timed(logger_name="processing")
def render_thumbnail(
    video_path: Path,
    hook_sentence: str | None,
    title: str | None,
    output_path: Path,
    face_data: list[dict] | None = None,
    style: dict | None = None,
) -> tuple[Path, int, float]:
    """Run the full six-stage thumbnail pipeline.

    Parameters
    ----------
    video_path:
        Path to the clip's **clean, unsubtitled** render.
    hook_sentence, title:
        Clip metadata — *hook_sentence* is preferred for the headline.
    output_path:
        Where to write the final thumbnail JPEG.
    face_data:
        Optional pre-computed face-tracking data from the pipeline.
        Currently unused (the scorer detects faces via Haar cascade),
        but reserved for future wiring of BlazeFace coordinates.
    style:
        Optional thumbnail override flags (design tokens).  Forwarded to the
        Tier-1 compositor's ``palette`` — e.g. ``{"cta_show": False}``,
        ``{"cta_fill": (255, 0, 0)}``.  Omitted keys fall back to the
        compositor's defaults, so passing nothing or ``{}`` is behaviourally
        identical to the old call.

    Returns
    -------
    (final_path, tier, score):
        *final_path* is the output file; *tier* is 1, 2, 3, or 0; *score*
        is the Stage-2 composite score of the best frame (or 0.0 if the
        pipeline failed before scoring). ``thumbnail_tier=N`` is logged
        for every generated thumbnail.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    style = style or {}
    # ── Part B: user overrides layered onto the auto pipeline ───────────
    # 'headline' overrides the clip-derived hook; 'template' picks the composer
    # ("full_bleed" default vs "minimal"); 'frame_index' overrides the auto
    # best-frame selection with a specific filmstrip choice.
    override_headline = (style.get("headline") or "").strip() or None
    template = str(style.get("template") or "full_bleed").lower()
    if template not in ("full_bleed", "minimal"):
        template = "full_bleed"
    frame_index = style.get("frame_index")

    # ── Stage 4: Copy (headline selection — independent of frames) ──────
    headline = override_headline or copy.select_headline(hook_sentence, title)
    strap = "WATCH THE FULL CLIP"

    # ── Stage 1: Frame Candidate Extractor ──────────────────────────────
    frame_candidates = candidates.extract_candidates(video_path)
    if not frame_candidates:
        logger.error(
            f"No candidates extracted from {video_path.name}; "
            "cannot generate thumbnail"
        )
        return output_path, 0, 0.0

    # ── Stage 2: Frame Scorer ───────────────────────────────────────────
    # Respect a user's filmstrip choice when it's a valid index; otherwise keep
    # the auto best-frame.  Candidate extraction is deterministic (even spacing
    # over the clip), so index N in the editor's filmstrip == index N here.
    if isinstance(frame_index, int) and 0 <= frame_index < len(frame_candidates):
        chosen = scoring.select_best([frame_candidates[frame_index]])
        editor_override = True
    else:
        frame_index = None
        chosen = scoring.select_best(frame_candidates)
        editor_override = False
    if chosen is None:
        logger.error(
            f"No scorable frames from {video_path.name}; "
            "cannot generate thumbnail"
        )
        return output_path, 0, 0.0
    best_frame, score, _breakdown, face_bbox = chosen
    if editor_override:
        logger.info(
            f"Thumbnail: using user-selected frame index {frame_index} "
            f"(score={score:.4f})"
        )

    # ── Stage 3: Crop Engine ────────────────────────────────────────────
    try:
        cropped = crop.crop_to_portrait(best_frame, face_bbox)
    except Exception as exc:
        logger.warning(f"Stage 3 crop failed ({exc}); trying centred crop")
        try:
            cropped = crop.crop_to_portrait(best_frame, None)
        except Exception as exc2:
            logger.error(f"Stage 3 centred crop also failed: {exc2}")
            return output_path, 0, score

    # ── Stages 5 + 6: Compositor + Validator + Fallback Ladder ──────────
    tier = 0
    result = output_path

    # ── Tier 1: full design ─────────────────────────────────────────────
    # Skipped entirely when the user asks for the "minimal" template so the
    # simplified Tier-2 look is the intended render, not a fallback.
    if template != "minimal":
        try:
            compositor.compose(cropped, headline, strap, score, output_path, palette=style)
            ok, reason = validator.validate_image(output_path)
            if ok:
                tier = 1
            else:
                logger.warning(f"Tier 1 validation failed: {reason}")
        except Exception as exc:
            logger.warning(f"Tier 1 compositor failed: {exc}")

    # ── Tier 2: simplified (crop + scrim + headline) ────────────────────
    if tier == 0:
        logger.warning("Tier 1 failed; attempting Tier 2 (simplified)")
        try:
            compositor.compose_tier2(cropped, headline, output_path)
            ok, reason = validator.validate_image(output_path)
            if ok:
                tier = 2
            else:
                logger.warning(f"Tier 2 validation failed: {reason}")
        except Exception as exc:
            logger.warning(f"Tier 2 failed: {exc}")

    # ── Tier 3: last resort (plain crop + system font) ──────────────────
    if tier == 0:
        logger.warning("Tier 2 failed; attempting Tier 3 (last resort)")
        try:
            compositor.compose_tier3(cropped, headline, output_path)
            if output_path.exists() and output_path.stat().st_size > 100:
                tier = 3
        except Exception as exc:
            logger.error(f"Tier 3 FAILED (this should never happen): {exc}")

    # ── Tier 0: absolute last resort ────────────────────────────────────
    if tier == 0:
        logger.error(
            f"All tiers failed for {video_path.name} — "
            "using raw cropped frame as placeholder"
        )
        try:
            shutil.copy2(str(cropped), str(output_path))
        except Exception:
            logger.exception("Even raw-frame fallback failed")
        tier = 0

    # ── Log the tier — THE single most important debug signal ────────────
    logger.info(
        f"thumbnail_tier={tier} | {video_path.name} | "
        f"score={score:.4f} | headline={headline!r}"
    )
    return result, tier, score
