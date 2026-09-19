"""
Stage 1 — Frame Candidate Extractor.

Extracts 4-6 candidate frames spread across the clip's duration.
No scoring, no cropping — pure extraction.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from backend.utils.ffmpeg import extract_frame, get_video_duration
from backend.utils.logging import get_logger

logger = get_logger("thumbnail.candidates")


def extract_candidates(
    video_path: Path,
    num_candidates: int = 6,
    clip_start: float | None = None,
    clip_end: float | None = None,
) -> list[Path]:
    """Extract *num_candidates* frames spread across the clip's duration.

    If *clip_start* and *clip_end* are given, samples within that range;
    otherwise samples across the full video.

    Returns a list of Paths to extracted JPEG frames (in a temp dir).
    On failure returns an empty list.
    """
    try:
        import cv2  # noqa: F401 — ensure opencv is importable
    except ImportError:
        logger.error("OpenCV not installed; cannot extract candidates.")
        return []

    if clip_start is not None and clip_end is not None:
        duration = clip_end - clip_start
        base = clip_start
    else:
        duration = float(get_video_duration(video_path))
        base = 0.0

    if duration <= 0:
        logger.warning(f"Invalid duration ({duration}) for {video_path.name}")
        return []

    if num_candidates <= 1:
        sample_times = [base + duration / 2.0]
    else:
        step = duration / (num_candidates + 1)
        sample_times = [base + step * (i + 1) for i in range(num_candidates)]

    tmp = tempfile.mkdtemp(prefix="thumb_cands_")
    candidates: list[Path] = []

    for idx, ts in enumerate(sample_times):
        frame_path = Path(tmp) / f"cand_{idx:03d}.jpg"
        try:
            extract_frame(video_path, frame_path, ts)
            if frame_path.exists() and frame_path.stat().st_size > 100:
                candidates.append(frame_path)
            else:
                logger.warning(
                    f"Candidate {idx} at {ts:.1f}s: file missing or too small"
                )
        except Exception as exc:
            logger.warning(f"Candidate {idx} extraction at {ts:.1f}s failed: {exc}")

    logger.info(
        f"Extracted {len(candidates)}/{num_candidates} candidate frames "
        f"from {video_path.name}"
    )
    return candidates
