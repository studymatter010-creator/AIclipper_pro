"""
Stage 5 (Tier 1 + 2 + 3) — Compositor, rendered in native PIL.

Renders all thumbnail overlays with PIL / numpy instead of shelling out to
FFmpeg.  This makes thumbnail generation *independent of the machine's
FFmpeg build* — on some Windows FFmpeg builds a ``jpg -> jpg`` drawtext
filtergraph raises ``STATUS_ACCESS_VIOLATION`` (rc=3221225477) and the whole
fallback ladder collapsed to a bare crop.  PIL cannot hit that crash path.

Design: glow/vignette, stepped bottom scrim, headline via
``text_fit.fit_text`` (never ad-hoc sizing), corner mark,
a **raised** CTA button (dark plate + drop shadow + taller silhouette + lime
triangle/strap — so it stays readable over busy video), and a 2 px brand
border.

Every tier is a separate pure function so the engine's fallback ladder can
force-fail one and observe the next:
  * :func:`compose`        — Tier 1, full design.
  * :func:`compose_tier2`  — Tier 2, crop + scrim + headline only.
  * :func:`compose_tier3`  — Tier 3, plain crop + solid strip + headline.

A ``palette`` dict is threaded through ``compose`` so callers can override
any design token (the "thumbnail override flags").  Omitted keys fall back
to :data:`DEFAULT_PALETTE`; nothing else in the pipeline needs to change.
"""

from __future__ import annotations

from pathlib import Path

from backend.utils.logging import get_logger, timed

from . import text_fit

logger = get_logger("thumbnail.compositor")

# ── Design tokens ──────────────────────────────────────────────────────

W, H = 1080, 1920
ACCENT = (45, 212, 232)  # 0x2DD4E8 --accent-primary (cyan)
LIME = (163, 230, 53)  # 0xa3e635 CTA accent

DEFAULT_PALETTE: dict = {
    "accent": ACCENT,          # brand cyan (border, underline, top edge)
    "cta_fill": LIME,          # triangle + strap colour inside the button
    "cta_plate": (10, 10, 16), # near-black plate (max contrast on any video)
    "cta_edge": ACCENT,        # raised highlight across the top of the plate
    "cta_show": True,          # render the CTA button at all
    "stripe_y": 1810,          # y of the brand underline anchoring the headline
}


def _resolve_palette(palette: dict | None) -> dict:
    """Merge caller overrides over the design defaults.

    ``None`` returns the untouched defaults; a dict overrides only the keys it
    provides.  Unknown keys are ignored (so a stale override never breaks a
    render).  This is the single merge point the engine calls, which makes the
    override flags easy to verify in a unit test.
    """
    if not palette:
        return DEFAULT_PALETTE
    merged = dict(DEFAULT_PALETTE)
    merged.update(palette)
    return merged


# ── Font helpers ───────────────────────────────────────────────────────


def get_fonts() -> dict[str, str]:
    """Resolve font paths for headline, bold, body, symbol, and CJK."""
    fonts = {
        "headline": text_fit.resolve_font("ariblk", "arialbd"),
        "bold": text_fit.resolve_font("arialbd", "arial"),
        "body": text_fit.resolve_font("arial"),
        "symbol": text_fit.resolve_font("seguisym", "arial"),
    }
    # Best-effort CJK font for Chinese headlines.
    try:
        from PIL import ImageFont

        for cand in [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        ]:
            if Path(cand).exists():
                ImageFont.truetype(cand, 40)  # validate it loads
                fonts["cjk"] = cand
                break
    except Exception:
        pass
    if "cjk" not in fonts:
        fonts["cjk"] = fonts["body"]
    return fonts


def drawtext_escape(text: str) -> str:
    """Kept for API compatibility (old FFmpeg path).  Unused by PIL."""
    return text


def font_escape(path: str) -> str:
    """Kept for API compatibility (old FFmpeg path).  Unused by PIL."""
    return path


def run_ffmpeg(args: list[str], desc: str = "FFmpeg") -> None:
    """Kept for API compatibility.  PIL compositing never calls this."""
    raise RuntimeError(
        f"run_ffmpeg is not used by the PIL compositor "
        f"(FFmpeg refused for thumbnail compositing: {desc})"
    )


# ── PIL drawing helpers ────────────────────────────────────────────────


def _load(cropped_path: Path):
    from PIL import Image

    img = Image.open(cropped_path).convert("RGB")
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)
    return img


def _grade(img):
    """Warm, punchy grade + soft vignette (replaces the FFmpeg filter chain)."""
    import numpy as np

    from PIL import Image, ImageEnhance, ImageFilter

    img = ImageEnhance.Color(img).enhance(1.35)
    img = ImageEnhance.Contrast(img).enhance(1.14)
    img = ImageEnhance.Brightness(img).enhance(1.02)
    img = img.filter(ImageFilter.UnsharpMask(radius=5, percent=80, threshold=0))

    # Warm tint (approximate the colour-balance step).
    arr = np.array(img).astype(np.float32)
    arr[..., 0] += 6.0
    arr[..., 2] -= 6.0
    arr = np.clip(arr, 0, 255)

    # Radial vignette.
    ysz, xsz = img.size[1], img.size[0]
    yy, xx = np.mgrid[0:ysz, 0:xsz]
    cx, cy = xsz / 2.0, ysz / 2.0
    r = np.sqrt(((xx - cx) / xsz) ** 2 + ((yy - cy) / ysz) ** 2)
    r = np.clip(r, 0, 1.0)
    scale = 1.0 - 0.42 * np.clip((r - 0.45) / 0.55, 0, 1) ** 2
    arr *= scale[..., None]

    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _darken_region(img, y0, y1, alpha):
    """Blend a rectangular region toward black by *alpha* (ffmpeg drawbox fill)."""
    import numpy as np

    from PIL import Image

    arr = np.array(img).astype(np.float32)
    arr[y0:y1, :] *= 1.0 - alpha
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _font(fonts: dict[str, str], key: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(fonts[key], int(size))


def _has_cjk(text: str) -> bool:
    return any("\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" for ch in text)


def _draw_headline(img, headline, fonts):
    """Draw the balanced headline via text_fit; returns a new Image."""
    from PIL import ImageDraw

    head_max = int(W * 0.88)
    font_key = "cjk" if _has_cjk(headline) else "headline"
    lines, size = text_fit.fit_text(
        headline, head_max, max_lines=2, font_path=fonts[font_key],
        max_size=124, min_size=32,
    )
    font = _font(fonts, font_key, size)
    line_h = int(size * 1.18)
    head_y0 = 1530 if len(lines) == 1 else 1470
    draw = ImageDraw.Draw(img, "RGBA")
    for i, hline in enumerate(lines):
        hy = head_y0 + i * line_h
        x = (W - draw.textlength(hline, font=font)) / 2.0
        draw.text((x + 5, hy + 6), hline, font=font, fill=(0, 0, 0, 170))
        draw.text(
            (x, hy), hline, font=font, fill="white",
            stroke_width=10, stroke_fill=(0, 0, 0, 235),
        )
    return img


def _sparkle(draw, cx, cy, r, fill):
    """4-point concave sparkle (✦-like) via alternating-radii polygon."""
    import math

    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rad = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    draw.polygon(pts, fill=fill)


def _corner_mark(img, fonts, palette):
    """Top-right dark dot chip + accent 4-point sparkle."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img, "RGBA")
    cx, cy, r = 1006, 74, 30
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, 180))
    _sparkle(draw, 1018, 88, 13, (*palette["accent"], 255))
    return img


def _cta_button(img, strap, fonts, palette):
    """Draw a **raised** play button and return a new Image.

    Unlike the old flat triangle+text, this CTA is given real depth so it
    stays legible on busy footage:

      * a near-black rounded plate (high contrast over ANY background),
      * a soft drop shadow under the plate (the "height" / raised feel),
      * a taller silhouetted plate with a lime/cyan top-edge highlight,
      * the vector play triangle + strap in CTA lime, centred on the plate.

    Zero emoji/font-glyph dependencies — the play glyph is a drawn polygon.
    Pure function: takes an RGB image, returns an RGB image.
    """
    from PIL import Image, ImageDraw, ImageFilter

    lime = palette["cta_fill"]
    plate = palette["cta_plate"]
    top_edge = palette["cta_edge"]

    strap_font = _font(fonts, "bold", 58)
    draw = ImageDraw.Draw(img, "RGBA")
    tw = draw.textlength(strap, font=strap_font)

    tri_w, tri_h = 44, 62
    gap = 22
    pad_x, pad_y = 34, 28
    plate_w = (tri_w + gap + tw) + 2 * pad_x
    plate_h = tri_h + 2 * pad_y  # taller silhouette (~118 px)
    x0 = (W - plate_w) / 2.0
    y0 = 1690.0
    x1, y1 = x0 + plate_w, y0 + plate_h
    radius = 26

    # ── Soft drop shadow under the plate → reads as RAISED above the video ──
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow, "RGBA").rounded_rectangle(
        [x0 + 8, y0 + 18, x1 + 8, y1 + 18], radius=radius, fill=(0, 0, 0, 175)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    img = Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")

    # ── Plate body (near-black + thin accent outline) ─────────────────────
    draw.rounded_rectangle(
        [x0, y0, x1, y1], radius=radius,
        fill=(*plate, 236), outline=(*top_edge, 210), width=3,
    )
    # Raised top-edge highlight so the button feels three-dimensional.
    draw.rounded_rectangle(
        [x0 + 8, y0 - 8, x1 - 8, y0 + 12], radius=radius, fill=(*top_edge, 255),
    )

    # ── Centred content: vector play triangle + strap ─────────────────────
    ty = y0 + (plate_h - tri_h) / 2.0
    tx = x0 + pad_x
    draw.polygon(
        [(tx, ty), (tx, ty + tri_h), (tx + tri_w, ty + tri_h / 2.0)],
        fill=(*lime, 255), outline=(0, 0, 0, 235),
    )
    strappy = ty + (tri_h - 58) / 2.0
    draw.text(
        (tx + tri_w + gap, strappy), strap, font=strap_font,
        fill=(*lime, 255), stroke_width=4, stroke_fill=(0, 0, 0, 240),
    )
    return img


def _underline(img, palette):
    """Solid brand-cyan bar anchoring the headline block."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img, "RGBA")
    y = palette["stripe_y"]
    draw.rectangle([0, y, W, y + 6], fill=(*palette["accent"], 240))
    return img


def _border(img, palette):
    """2 px cyan brand border around the canvas."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([0, 0, W - 1, H - 1], outline=(*palette["accent"], 255), width=2)
    return img


def _save(img, output_path: Path) -> None:
    img.convert("RGB").save(str(output_path), "JPEG", quality=92)


# ── Tier 1 — full design ───────────────────────────────────────────────
# All palette keys honoured here so the override flags have a real effect.


@timed(logger_name="processing")
def compose(
    cropped_path: Path,
    headline: str,
    strap: str,
    score: float,
    output_path: Path,
    palette: dict | None = None,
) -> Path:
    """Render the full composited thumbnail (Tier 1) with PIL.

    *palette* (optional) overrides design tokens — the thumbnail override
    flags.  Omitted keys fall back to :data:`DEFAULT_PALETTE`.
    """
    pal = _resolve_palette(palette)
    fonts = get_fonts()
    img = _load(cropped_path)
    img = _grade(img)
    img = _darken_region(img, 1180, H, 0.22)
    img = _darken_region(img, 1300, H, 0.30)
    img = _darken_region(img, 1420, H, 0.42)
    # No "AI SHORT" badge / AI signage is drawn — the top-left is left clean.
    img = _corner_mark(img, fonts, pal)
    img = _draw_headline(img, headline, fonts)
    if pal.get("cta_show", True):
        img = _cta_button(img, strap, fonts, pal)
    img = _underline(img, pal)
    img = _border(img, pal)
    _save(img, output_path)
    logger.info(
        f"Composed Tier 1 thumbnail: {output_path.stat().st_size} bytes "
        f"| score={score}"
    )
    return output_path


# ── Tier 2 — simplified (crop + scrim + headline) ──────────────────────


@timed(logger_name="processing")
def compose_tier2(
    cropped_path: Path,
    headline: str,
    output_path: Path,
) -> Path:
    """Tier 2: crop + darker scrim + headline only.  No glow/badge/CTA/border."""
    fonts = get_fonts()
    img = _load(cropped_path)
    img = _darken_region(img, 1180, H, 0.30)
    img = _darken_region(img, 1300, H, 0.45)
    img = _darken_region(img, 1420, H, 0.60)
    img = _draw_headline(img, headline, fonts)
    _save(img, output_path)
    logger.info(f"Composed Tier 2 thumbnail: {output_path.stat().st_size} bytes")
    return output_path


# ── Tier 3 — last resort (plain strip + headline) ──────────────────────


@timed(logger_name="processing")
def compose_tier3(
    cropped_path: Path,
    headline: str,
    output_path: Path,
) -> Path:
    """Tier 3: plain crop + solid dark strip + headline in the body font."""
    from PIL import ImageDraw

    fonts = get_fonts()
    img = _load(cropped_path)
    img = _darken_region(img, 1400, H, 0.70)
    font = _font(fonts, "body", 48)
    draw = ImageDraw.Draw(img, "RGBA")
    safe = headline[:50]
    x = (W - draw.textlength(safe, font=font)) / 2.0
    draw.text((x, 1550), safe, font=font, fill="white",
              stroke_width=4, stroke_fill=(0, 0, 0, 255))
    _save(img, output_path)
    logger.info(f"Composed Tier 3 thumbnail: {output_path.stat().st_size} bytes")
    return output_path
