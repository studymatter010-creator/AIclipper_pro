"""
Stage 3 — Crop Engine.

Crops a frame to full-bleed 1080×1920 portrait.  If a face bounding box
is provided, positions the face in the upper ~60 % of the frame (the
bottom ~30–35 % will be covered by headline text in Stage 5).  No text,
no overlays — just the cropped image.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from backend.utils.logging import get_logger

logger = get_logger("thumbnail.crop")

TARGET_W = 1080
TARGET_H = 1920
_TARGET_ASPECT = TARGET_W / TARGET_H  # 0.5625


def crop_to_portrait(
    frame_path: Path,
    face_bbox: tuple[int, int, int, int] | None = None,
) -> Path:
    """Crop *frame_path* to a full-bleed 1080×1920 portrait image.

    If ``face_bbox`` is ``(x, y, w, h)`` the face is positioned in the
    upper ~60 % of the canvas so the headline text zone below doesn't
    obscure the subject.  Without a face, the crop is centred.

    Returns the path to the cropped output (a temp JPEG at quality 95).
    """
    from PIL import Image

    img = Image.open(frame_path)
    src_w, src_h = img.size

    if face_bbox is not None:
        fx, fy, fw, fh = face_bbox
        face_cx = fx + fw / 2

        # Compute a crop window that maintains the target aspect ratio
        # and positions the face in the upper portion of the result.
        crop_h = min(src_h, int(fh * 3.5))  # generous vertical window
        crop_w = int(crop_h * _TARGET_ASPECT)
        if crop_w > src_w:
            crop_w = src_w
            crop_h = int(crop_w / _TARGET_ASPECT)

        # Position: horizontally centred on face, vertically bias face upward.
        crop_x = int(face_cx - crop_w / 2)
        crop_y = int(fy + fh * 0.25 - crop_h * 0.30)

        # Clamp to image bounds.
        crop_x = max(0, min(crop_x, src_w - crop_w))
        crop_y = max(0, min(crop_y, src_h - crop_h))

        img = img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
    else:
        # Centred crop to target aspect ratio.
        src_aspect = src_w / src_h
        if src_aspect > _TARGET_ASPECT:
            # Source is wider — crop sides.
            crop_h = src_h
            crop_w = int(crop_h * _TARGET_ASPECT)
            crop_x = (src_w - crop_w) // 2
            crop_y = 0
        else:
            # Source is taller — crop top/bottom.
            crop_w = src_w
            crop_h = int(crop_w / _TARGET_ASPECT)
            crop_x = 0
            crop_y = (src_h - crop_h) // 2
        img = img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))

    img = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)

    out = Path(tempfile.mkdtemp(prefix="thumb_crop_")) / "cropped.jpg"
    img.save(str(out), "JPEG", quality=95)
    logger.info(
        f"Cropped {src_w}x{src_h} → {TARGET_W}x{TARGET_H} "
        f"{'(face-aware)' if face_bbox else '(centred)'}: {out.name}"
    )
    return out
