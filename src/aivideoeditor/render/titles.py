"""Title and text-overlay images (one transparent canvas-sized PNG per text).

The renderer overlays these images with FFmpeg (fades and slides are
animated there), and the live player in the web UI draws the very same
image, so what you see while editing is what gets rendered. Pillow does
the drawing, so no FFmpeg text filters are needed.

Styles (templates):
* ``plain``       - text at (x, y), optional box behind it
* ``title``       - large centred title with a soft shadow, optional subtitle
* ``lower_third`` - name strap at the lower left: accent bar, text and subtitle
* ``badge``       - small rounded label in the accent colour
* ``end_card``    - darkened full frame with a centred title and subtitle
"""

from __future__ import annotations

import functools

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from ..timeline.model import TextOverlay

_BOLD = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
)
_REGULAR = (
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)


@functools.lru_cache(maxsize=64)
def font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    for name in (_BOLD if bold else _REGULAR):
        try:
            return ImageFont.truetype(name, max(6, size))
        except OSError:
            continue
    try:
        return ImageFont.load_default(max(6, size))
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def rgba(color: str, default: str = "white") -> tuple[int, int, int, int]:
    """'white', '#FFD400', 'black@0.55' -> RGBA."""
    alpha = 1.0
    if "@" in color:
        color, a = color.split("@", 1)
        try:
            alpha = float(a)
        except ValueError:
            alpha = 1.0
    try:
        rgb = ImageColor.getrgb(color.strip() or default)
    except ValueError:
        rgb = ImageColor.getrgb(default)
    return rgb[0], rgb[1], rgb[2], int(round(max(0.0, min(alpha, 1.0)) * 255))


def _size(draw: ImageDraw.ImageDraw, text: str, f) -> tuple[float, float, tuple]:
    bbox = draw.multiline_textbbox((0, 0), text, font=f, align="center", spacing=4)
    return bbox[2] - bbox[0], bbox[3] - bbox[1], bbox


def _text_at(draw, cx: float, cy: float, text: str, f, fill, *, anchor_left: bool = False) -> tuple[float, float]:
    w, h, bbox = _size(draw, text, f)
    left = (cx if anchor_left else cx - w / 2) - bbox[0]
    top = cy - h / 2 - bbox[1]
    draw.multiline_text((left, top), text, font=f, fill=fill, align="left" if anchor_left else "center", spacing=4)
    return w, h


def render_text_image(x: TextOverlay, width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    size = max(8, int(round(x.size * height)))
    color = rgba(x.color)
    accent = rgba(x.accent, "#3B82F6")
    style = x.style

    if style == "end_card":
        draw.rectangle([0, 0, width, height], fill=(0, 0, 0, 170))
        f = font(int(size * 1.2))
        _, h = _size(draw, x.text, f)[:2]
        cy = x.y * height
        _text_at(draw, x.x * width, cy - (h * 0.4 if x.subtitle else 0), x.text, f, color)
        if x.subtitle:
            _text_at(draw, x.x * width, cy + h * 0.75, x.subtitle, font(int(size * 0.55), bold=False),
                     (*color[:3], 220))
        return img

    if style == "title":
        f = font(int(size * 1.15))
        w, h, _ = _size(draw, x.text, f)
        cx, cy = x.x * width, x.y * height
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        _text_at(sd, cx + size * 0.06, cy + size * 0.08, x.text, f, (0, 0, 0, 200))
        if x.subtitle:
            _text_at(sd, cx, cy + h * 0.95, x.subtitle, font(int(size * 0.5), bold=False), (0, 0, 0, 180))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(2, size // 12))))
        draw = ImageDraw.Draw(img)
        _text_at(draw, cx, cy, x.text, f, color)
        if x.subtitle:
            _text_at(draw, cx, cy + h * 0.95, x.subtitle, font(int(size * 0.5), bold=False), (*color[:3], 230))
        return img

    if style == "lower_third":
        f = font(size)
        fs = font(int(size * 0.6), bold=False)
        w, h, _ = _size(draw, x.text, f)
        ws, hs = (_size(draw, x.subtitle, fs)[:2] if x.subtitle else (0, 0))
        pad = size * 0.45
        bar = max(4, size // 6)
        left = x.x * width
        box_w = max(w, ws) + pad * 2
        box_h = h + (hs + pad * 0.5 if x.subtitle else 0) + pad * 1.4
        top = x.y * height - box_h / 2
        draw.rectangle([left, top, left + bar, top + box_h], fill=accent)
        draw.rectangle([left + bar, top, left + bar + box_w, top + box_h], fill=rgba(x.box_color, "black"))
        _text_at(draw, left + bar + pad, top + pad * 0.7 + h / 2, x.text, f, color, anchor_left=True)
        if x.subtitle:
            _text_at(draw, left + bar + pad, top + pad * 1.2 + h + hs / 2, x.subtitle, fs, (*color[:3], 215),
                     anchor_left=True)
        return img

    if style == "badge":
        f = font(int(size * 0.8))
        w, h, _ = _size(draw, x.text, f)
        pad_x, pad_y = size * 0.6, size * 0.32
        cx, cy = x.x * width, x.y * height
        draw.rounded_rectangle([cx - w / 2 - pad_x, cy - h / 2 - pad_y, cx + w / 2 + pad_x, cy + h / 2 + pad_y],
                               radius=(h + 2 * pad_y) / 2, fill=accent)
        _text_at(draw, cx, cy, x.text, f, color)
        return img

    # plain
    f = font(size)
    w, h, _ = _size(draw, x.text, f)
    cx, cy = x.x * width, x.y * height
    if x.box:
        pad = max(4, size // 3)
        draw.rectangle([cx - w / 2 - pad, cy - h / 2 - pad, cx + w / 2 + pad, cy + h / 2 + pad],
                       fill=rgba(x.box_color, "black"))
    _text_at(draw, cx, cy, x.text, f, color)
    return img


def animation_duration(x: TextOverlay) -> float:
    """Length of the in/out animation (0 for none)."""
    if x.animation == "none":
        return 0.0
    return min(0.45, (x.end - x.start) / 3)
