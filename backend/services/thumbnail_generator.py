"""
AIClipper Thumbnail Generation Service

Extracts candidate frames from the best moments in a clip, scores each for
visual quality (sharpness, brightness, face presence), and exports the top
candidates as PNG and JPG.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from backend.utils.config import get_settings
from backend.utils.ffmpeg import extract_frame, get_video_duration
from backend.utils.logging import get_logger, timed

logger = get_logger("services.thumbnail_generator")


# ── Scoring helpers ─────────────────────────────────────────────────────

def _score_sharpness(image_array: np.ndarray) -> float:
    """
    Estimate image sharpness via Laplacian variance.

    Higher variance → sharper image.
    """
    try:
        import cv2  # type: ignore[import-untyped]
    except ImportError:
        return 0.5
    gray = cv2.cvtColor(image_array, cv2.COLOR_BGR2GRAY) if image_array.ndim == 3 else image_array
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    return float(lap.var())


def _score_brightness(image_array: np.ndarray) -> float:
    """
    Score brightness: ideal is around 120-180 (mid-range).

    Returns a 0-1 score where 1 = ideal brightness.
    """
    try:
        import cv2  # type: ignore[import-untyped]
    except ImportError:
        return 0.5
    gray = cv2.cvtColor(image_array, cv2.COLOR_BGR2GRAY) if image_array.ndim == 3 else image_array
    mean_brightness = float(np.mean(gray))
    # Bell curve centred at 140
    ideal = 140.0
    score = max(0.0, 1.0 - abs(mean_brightness - ideal) / ideal)
    return score


def _score_face_presence(image_array: np.ndarray) -> float:
    """
    Quick face presence check using OpenCV's Haar cascade.

    Returns 1.0 if face(s) found, 0.0 otherwise.  Never raises: thumbnail
    generation must survive a missing/empty cascade classifier file.

    WHY the guard: on the user's Windows OpenCV 5.0.0 wheel the bundled
    ``haarcascade_frontalface_default.xml`` is empty/absent, so
    ``CascadeClassifier(...).empty()`` is True and ``detectMultiScale``
    aborts with ``!empty()``.  That crash took down the whole pipeline.
    Face presence is just one 30%-weight scoring signal, so a missing
    classifier should degrade the score to 0.0 instead of crashing.
    """
    try:
        import cv2  # type: ignore[import-untyped]
    except ImportError:
        return 0.0

    gray = cv2.cvtColor(image_array, cv2.COLOR_BGR2GRAY) if image_array.ndim == 3 else image_array
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"  # type: ignore[attr-defined]
    try:
        cascade = cv2.CascadeClassifier(str(cascade_path))
        # Empty classifier => the XML data is missing/corrupt on this build.
        if cascade.empty():
            logger.warning("Haar cascade classifier is empty; skipping face scoring.")
            return 0.0
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(40, 40)
        )
        return 1.0 if len(faces) > 0 else 0.0
    except Exception as exc:  # noqa: BLE001 - face scoring must never crash the pipeline
        logger.warning(f"Face detection skipped ({exc}); using score 0.0")
        return 0.0


def _score_expressiveness(image_array: np.ndarray) -> float:
    """
    Score facial expressiveness via MediaPipe FaceLandmarker (mouth-open distance
    + eyebrow-raise), used as a 4th candidate-selection signal alongside
    sharpness / exposure / face-size (Change 5, 2026-09-12).

    Returns a score in ``[0.5, 1.0]``: ``0.5`` = neutral (either no expressive
    face detected, OR MediaPipe / the FaceLandmarker model is unavailable) and
    ``1.0`` = strongly expressive (wide-open mouth and/or raised brows).  The
    floor at 0.5 means a missing landmark model never *punishes* a candidate —
    it simply doesn't reward expression.

    This runs per-candidate-frame and is guarded so a missing dependency can
    never take down thumbnail generation.
    """
    try:
        import numpy as _np
        import cv2  # type: ignore[import-untyped]
        import mediapipe as mp  # type: ignore[import-untyped]
        from mediapipe.tasks import python as mp_python  # type: ignore[import-untyped]
        from mediapipe.tasks.python import vision as mp_vision  # type: ignore[import-untyped]
    except ImportError:
        return 0.5

    model_path = get_settings().mediapipe_landmark_model_path
    if not model_path.exists():
        return 0.5

    # Convert BGR -> RGB (MediaPipe expects RGB).
    rgb = cv2.cvtColor(image_array, cv2.COLOR_BGR2RGB)
    mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

    base_options = mp_python.BaseOptions(model_asset_path=str(model_path))
    options = mp_vision.FaceLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.IMAGE,
        num_faces=1,
    )
    try:
        with mp_vision.FaceLandmarker.create_from_options(options) as lm:
            result = lm.detect(mp_img)
            if not result.face_landmarks:
                return 0.5
            pts = [(ld.x, ld.y) for ld in result.face_landmarks[0]]

            def _d(i: int, j: int) -> float:
                x0, y0 = pts[i]; x1, y1 = pts[j]
                return float(((x0 - x1) ** 2 + (y0 - y1) ** 2) ** 0.5)

            # Canonical MediaPipe face-mesh indices.
            mouth_top, mouth_bot = 13, 14
            mouth_l, mouth_r = 61, 291
            brow_l, eye_l = 107, 159
            brow_r, eye_r = 336, 386

            mouth_w = max(_d(mouth_l, mouth_r), 1e-4)
            mouth_open = _d(mouth_top, mouth_bot) / mouth_w       # MAR
            brow_raise_l = _d(brow_l, eye_l)
            brow_raise_r = _d(brow_r, eye_r)
            face_w = max(_d(33, 263), 1e-4)                        # eye-to-eye span

            # Expression energy = blended mouth-open + brow-raise, normalised to
            # [0,1]; a wide-open mouth (MAR ~0.5+) or raised brows (~0.18 of face
            # width) each push it toward 1.0.
            mouth_e = min(1.0, mouth_open / 0.45)
            brow_e = min(1.0, ((brow_raise_l + brow_raise_r) / 2.0) / (0.20 * face_w))
            energy = min(1.0, 0.5 * mouth_e + 0.5 * brow_e)
            # Floor at 0.5 (neutral) so a flat face is never penalised.
            return round(0.5 + 0.5 * energy, 4)
    except Exception as exc:  # noqa: BLE001 - must never crash thumbnail gen
        logger.warning(f"Expressiveness scoring skipped ({exc}); using neutral 0.5")
        return 0.5


# ── Public API ──────────────────────────────────────────────────────────

@timed(logger_name="processing")
def generate_thumbnails(
    video_path: Path,
    output_dir: Path,
    clip_start: float,
    clip_end: float,
    num_candidates: int = 5,
) -> list[dict[str, Any]]:
    """
    Generate and score thumbnail candidates from a clip range.

    Candidate frames are sampled at evenly-spaced intervals across the
    clip.  Each is scored for sharpness, brightness, and face presence.
    The highest-scoring candidate is marked ``is_selected = True`` and
    exported in both PNG and JPG.

    Args:
        video_path: Source video path.
        output_dir: Directory to write thumbnail images.
        clip_start: Clip start in seconds.
        clip_end: Clip end in seconds.
        num_candidates: Number of frames to sample and evaluate.

    Returns:
        A list of thumbnail dicts::

            [
                {
                    "path": str,
                    "score": float,
                    "format": str,        # "png" or "jpg"
                    "is_selected": bool,
                },
                ...
            ]
    """
    try:
        import cv2  # type: ignore[import-untyped]
    except ImportError:
        logger.error("OpenCV not installed; cannot generate thumbnails.")
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    duration = clip_end - clip_start

    if duration <= 0:
        logger.warning("Invalid clip range for thumbnails.")
        return []

    # ── 1. Determine sample timestamps ──────────────────────────────────
    if num_candidates <= 1:
        sample_times = [clip_start + duration / 2]
    else:
        step = duration / (num_candidates + 1)
        sample_times = [clip_start + step * (i + 1) for i in range(num_candidates)]

    # ── 2. Extract and score each frame ─────────────────────────────────
    scored: list[dict[str, Any]] = []

    for idx, ts in enumerate(sample_times):
        frame_path = output_dir / f"thumb_candidate_{idx:03d}.jpg"
        try:
            extract_frame(video_path, frame_path, ts)
        except RuntimeError as exc:
            logger.warning(f"Frame extraction at {ts:.1f}s failed: {exc}")
            continue

        img = cv2.imread(str(frame_path))
        if img is None:
            continue

        sharpness = _score_sharpness(img)
        brightness = _score_brightness(img)
        face = _score_face_presence(img)
        expr = _score_expressiveness(img)   # Change 5: facial expressiveness

        # Weighted composite score (Change 5: expressiveness now a 4th signal,
        # alongside sharpness / exposure / face presence).
        composite = (
            0.35 * min(sharpness / 500.0, 1.0)
            + 0.20 * brightness
            + 0.25 * face
            + 0.20 * expr
        )

        scored.append({
            "path": str(frame_path),
            "score": round(composite, 4),
            "format": "jpg",
            "is_selected": False,
            "timestamp": ts,
            "_img": img,
        })

    if not scored:
        logger.warning("No valid thumbnail candidates produced.")
        return []

    # ── 3. Select best and export formats ───────────────────────────────
    scored.sort(key=lambda x: x["score"], reverse=True)
    scored[0]["is_selected"] = True

    results: list[dict[str, Any]] = []

    for entry in scored:
        img = entry.pop("_img")
        entry.pop("timestamp", None)
        results.append(entry)

        # For the selected thumbnail, also save a PNG version
        if entry["is_selected"]:
            png_path = Path(entry["path"]).with_suffix(".png")
            cv2.imwrite(str(png_path), img)
            results.append({
                "path": str(png_path),
                "score": entry["score"],
                "format": "png",
                "is_selected": True,
            })

    logger.info(
        f"Thumbnails generated: {len(scored)} candidates, "
        f"best score={scored[0]['score']:.3f}"
    )

    # NOTE (2026-09-09): the generative FLUX thumbnail pass was REMOVED —
    # `flux.1-schnell` is not a valid Ollama model (`ollama pull` fails with
    # "file does not exist"), so the AI-enhance path could never run. The best
    # real frame is the reliable, always-working thumbnail.

    return results


def select_best_frame(
    video_path: Path,
    num_candidates: int = 6,
    duration: float | None = None,
) -> tuple[Path, float] | None:
    """Extract and score NUM_CANDIDATES frames, return the best ``(Path, score)``.

    Part 3 (2026-09-12): the composed thumbnail pipeline now scores several
    candidate frames BEFORE composing (face presence + size, sharpness, exposure,
    facial expressiveness) and hands the highest-scoring frame to the FFmpeg
    composition, so the poster isn't just a blind mid-clip pick.  Scoring is the
    same weighted composite used by :func:`generate_thumbnails`.

    Returns ``(frame_path, score)`` for the best candidate, or ``None`` if no
    frame could be extracted/scored (callers then fall back to the internal
    ``thumbnail=300`` pick).
    """
    import tempfile

    try:
        import cv2  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("OpenCV not installed; cannot score thumbnail frames.")
        return None

    dur = duration if duration is not None else float(get_video_duration(video_path))
    if dur <= 0:
        logger.info(
            f"Cannot determine duration for {video_path.name}; falling back to "
            "thumbnail=300 pick."
        )
        return None
    if num_candidates <= 1:
        sample_times = [dur / 2.0]
    else:
        step = dur / (num_candidates + 1)
        sample_times = [step * (i + 1) for i in range(num_candidates)]

    tmp = tempfile.mkdtemp(prefix="thumb_best_")
    best: tuple[Path, float] | None = None
    for idx, ts in enumerate(sample_times):
        frame_path = Path(tmp) / f"cand_{idx:03d}.jpg"
        try:
            extract_frame(video_path, frame_path, ts)
            img = cv2.imread(str(frame_path))
            if img is None:
                continue
        except Exception:  # noqa: BLE001 - a bad frame must not abort selection
            continue
        sharp = _score_sharpness(img)
        bright = _score_brightness(img)
        face = _score_face_presence(img)
        expr = _score_expressiveness(img)
        composite = (
            0.35 * min(sharp / 500.0, 1.0)
            + 0.20 * bright
            + 0.25 * face
            + 0.20 * expr
        )
        if best is None or composite > best[1]:
            best = (frame_path, round(composite, 4))

    if best is None:
        logger.warning("No scorable candidate frames extracted.")
        return None
    logger.info(
        f"Best thumbnail frame for {video_path.name}: "
        f"score={best[1]:.3f} ({num_candidates} candidates scored)"
    )
    return best

