"""
AIClipper Clip Generation Service

Cuts clips from the source video and optionally applies face-aware 9:16
cropping for vertical short-form output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.utils.config import get_settings
from backend.utils.ffmpeg import (
    apply_dynamic_crop,
    convert_shorts_format,
    cut_clip,
    probe_achieved_start,
)
from backend.utils.logging import get_logger, timed

logger = get_logger("services.clip_generator")


def _crop_data_for_range(
    crop_data: list[dict[str, Any]],
    start_time: float,
    end_time: float,
) -> list[dict[str, Any]]:
    """
    Filter and re-base crop data to the clip time range.

    Args:
        crop_data: Full-video crop timeline from face tracking.
        start_time: Clip start in seconds.
        end_time: Clip end in seconds.

    Returns:
        Filtered crop data list with ``time`` values relative to clip start.
    """
    filtered: list[dict[str, Any]] = []
    for entry in crop_data:
        t = entry["time"]
        if start_time <= t <= end_time:
            rebased = dict(entry)
            rebased["time"] = round(t - start_time, 3)
            filtered.append(rebased)
    return filtered


@timed(logger_name="processing")
def generate_clip(
    video_path: Path,
    output_path: Path,
    start_time: float,
    end_time: float,
    crop_data: list[dict[str, Any]] | None = None,
) -> tuple[Path, float]:
    """
    Generate a single clip from a source video.

    Steps:
        1. Cut the time range from the source video (stream-copy for speed).
        2. Convert to YouTube Shorts format (1080x1920 with blurred background).
        3. Return the final clip path AND its ACTUAL (keyframe-snapped) start.

    Args:
        video_path: Source video path.
        output_path: Desired output file path (e.g. ``clip_001.mp4``).
        start_time: Clip start in seconds.
        end_time: Clip end in seconds.
        crop_data: Optional face-tracking crop timeline
            (each entry: ``time``, ``crop_x``, ``crop_y``, ``crop_w``, ``crop_h``).

    Returns:
        ``(Path, achieved_start_time)`` — the path to the generated clip file and
        the source-video timestamp its first frame actually starts at.  Because
        the raw cut uses ``-c copy`` (input seeking), FFmpeg starts at the last
        keyframe at or before ``start_time``, so ``achieved_start_time`` may be a
        few hundred ms earlier than the requested value.  Callers MUST rebase
        subtitles against this achieved value, not ``start_time``, or captions
        land early by the keyframe offset.

    Raises:
        RuntimeError: If FFmpeg operations fail.
    """
    settings = get_settings()
    duration = end_time - start_time

    logger.info(
        f"Generating clip {start_time:.1f}s - {end_time:.1f}s "
        f"({duration:.1f}s) from '{video_path.name}'"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. Cut the raw segment ──────────────────────────────────────────
    raw_cut_path = output_path.with_name(output_path.stem + "_raw" + output_path.suffix)
    cut_clip(
        input_path=video_path,
        output_path=raw_cut_path,
        start_time=start_time,
        duration=duration,
        reencode=False,  # fast stream-copy
    )

    # ── 1b. Determine the ACTUAL start of the rendered clip ─────────────
    # ``-c copy`` input seeking snaps to the last keyframe at/before the
    # requested ``start_time``, so the clip's true first frame is a little
    # earlier.  Probe the snapped keyframe so subtitles can be rebased to the
    # real first frame.  ``-t duration`` is applied from that snap point, so
    # ``achieved_end = achieved_start + duration``.
    achieved_start = probe_achieved_start(video_path, start_time)
    logger.info(
        f"Clip requested start={start_time:.3f}s -> achieved={achieved_start:.3f}s "
        f"(offset {start_time - achieved_start:+.3f}s)"
    )

    # ── 2. Convert to YouTube Shorts format ────────────────────────────────
    #      Use a face-aware vertical crop when tracking data is available.
    try:
        use_face_crop = (
            settings.face_crop_enabled
            and crop_data is not None
        )
        crop_timeline: list[dict[str, Any]] = []
        if use_face_crop:
            crop_timeline = _crop_data_for_range(crop_data, start_time, end_time)
            # Require at least a few samples with usable crop coordinates.
            use_face_crop = (
                len(crop_timeline) >= 2
                and all(
                    all(k in e for k in ("crop_x", "crop_y", "crop_w", "crop_h"))
                    for e in crop_timeline
                )
            )

        if use_face_crop:
            apply_dynamic_crop(raw_cut_path, output_path, crop_timeline)
        else:
            convert_shorts_format(raw_cut_path, output_path)
    finally:
        # Clean up intermediate raw cut
        try:
            if raw_cut_path.exists():
                raw_cut_path.unlink()
        except OSError:
            pass

    logger.info(f"Clip generated: {output_path.name} (achieved_start={achieved_start:.3f}s)")
    return output_path, achieved_start

