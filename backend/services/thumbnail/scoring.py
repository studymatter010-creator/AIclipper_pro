"""
Stage 2 — Frame Scorer.

Scores candidate frames on face presence/size, sharpness (Laplacian
variance), exposure (histogram), and expressiveness (MediaPipe
FaceLandmarker if available, else a 0.5 neutral floor).  Returns the
single best candidate + its numeric score.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from backend.utils.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("thumbnail.scoring")

# ── Scoring weights ────────────────────────────────────────────────────

W_SHARPNESS = 0.35
W_FACE = 0.25
W_BRIGHTNESS = 0.20
W_EXPRESSIVENESS = 0.20


# ── Individual scoring signals ─────────────────────────────────────────


def score_sharpness(image: np.ndarray) -> float:
    """Laplacian variance — higher = sharper.  Raw, unnormalised."""
    try:
        import cv2
    except ImportError:
        return 0.5
    gray = (
        cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    )
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    return float(lap.var())


def score_brightness(image: np.ndarray) -> float:
    """Bell-curve score centred at ideal brightness 140.  Returns 0–1."""
    try:
        import cv2
    except ImportError:
        return 0.5
    gray = (
        cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    )
    mean_b = float(np.mean(gray))
    return max(0.0, 1.0 - abs(mean_b - 140.0) / 140.0)


def score_face_presence(image: np.ndarray) -> float:
    """1.0 if face(s) detected via Haar cascade, 0.0 otherwise."""
    try:
        import cv2
    except ImportError:
        return 0.0
    gray = (
        cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    )
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    try:
        cascade = cv2.CascadeClassifier(str(cascade_path))
        if cascade.empty():
            return 0.0
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(40, 40)
        )
        return 1.0 if len(faces) > 0 else 0.0
    except Exception as exc:
        logger.warning(f"Face detection skipped ({exc}); score 0.0")
        return 0.0


def score_face_bbox(
    image: np.ndarray,
) -> tuple[int, int, int, int] | None:
    """Return the largest face bounding box ``(x, y, w, h)`` or ``None``."""
    try:
        import cv2
    except ImportError:
        return None
    gray = (
        cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    )
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    try:
        cascade = cv2.CascadeClassifier(str(cascade_path))
        if cascade.empty():
            return None
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(40, 40)
        )
        if len(faces) == 0:
            return None
        largest = max(faces, key=lambda f: f[2] * f[3])
        return tuple(int(v) for v in largest)  # type: ignore[return-value]
    except Exception:
        return None


def score_expressiveness(image: np.ndarray) -> float:
    """MediaPipe FaceLandmarker expressiveness (mouth-open + brow-raise).

    Returns 0.5 (neutral floor) if model unavailable or no face detected.
    Never penalises a candidate — it simply doesn't reward expression.
    """
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
    except ImportError:
        return 0.5

    model_path = get_settings().mediapipe_landmark_model_path
    if not model_path.exists():
        return 0.5

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
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
                x0, y0 = pts[i]
                x1, y1 = pts[j]
                return float(((x0 - x1) ** 2 + (y0 - y1) ** 2) ** 0.5)

            mouth_w = max(_d(61, 291), 1e-4)
            mouth_open = _d(13, 14) / mouth_w  # MAR
            brow_raise_l = _d(107, 159)
            brow_raise_r = _d(336, 386)
            face_w = max(_d(33, 263), 1e-4)

            mouth_e = min(1.0, mouth_open / 0.45)
            brow_e = min(
                1.0, ((brow_raise_l + brow_raise_r) / 2.0) / (0.20 * face_w)
            )
            energy = min(1.0, 0.5 * mouth_e + 0.5 * brow_e)
            return round(0.5 + 0.5 * energy, 4)
    except Exception as exc:
        logger.warning(
            f"Expressiveness scoring skipped ({exc}); using neutral 0.5"
        )
        return 0.5


# ── Composite score ────────────────────────────────────────────────────


def composite_score(
    sharpness: float,
    brightness: float,
    face: float,
    expressiveness: float,
) -> float:
    """Weighted composite of the four scoring signals.  Returns 0–1."""
    return round(
        W_SHARPNESS * min(sharpness / 500.0, 1.0)
        + W_BRIGHTNESS * brightness
        + W_FACE * face
        + W_EXPRESSIVENESS * expressiveness,
        4,
    )


# ── Single-candidate scorer ────────────────────────────────────────────


def score_candidate(
    frame_path: Path,
) -> tuple[float, dict[str, float], tuple[int, int, int, int] | None]:
    """Score a single candidate frame.

    Returns ``(composite_score, breakdown_dict, face_bbox_or_None)``.
    """
    try:
        import cv2
    except ImportError:
        return 0.0, {}, None

    img = cv2.imread(str(frame_path))
    if img is None:
        return 0.0, {}, None

    sharp = score_sharpness(img)
    bright = score_brightness(img)
    face = score_face_presence(img)
    expr = score_expressiveness(img)
    bbox = score_face_bbox(img)

    comp = composite_score(sharp, bright, face, expr)
    breakdown = {
        "sharpness": round(sharp / 500.0, 4),
        "brightness": round(bright, 4),
        "face": round(face, 4),
        "expressiveness": round(expr, 4),
    }
    return comp, breakdown, bbox


# ── Best-candidate selector ────────────────────────────────────────────


def select_best(
    candidates: list[Path],
) -> tuple[Path, float, dict[str, float], tuple[int, int, int, int] | None] | None:
    """Pick the highest-scoring candidate.

    Returns ``(path, score, breakdown, face_bbox)`` or ``None``.
    """
    if not candidates:
        return None

    best: tuple[
        Path, float, dict[str, float], tuple[int, int, int, int] | None
    ] | None = None

    for c in candidates:
        comp, breakdown, bbox = score_candidate(c)
        if best is None or comp > best[1]:
            best = (c, comp, breakdown, bbox)

    if best is not None:
        logger.info(
            f"Best frame: {best[0].name} score={best[1]:.4f} "
            f"(from {len(candidates)} candidates)"
        )
    return best
