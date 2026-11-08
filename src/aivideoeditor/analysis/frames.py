"""Still-frame extraction and contact sheets for Claude's vision input."""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..media.ffmpeg import run_ffmpeg


def extract_frame(path: str | Path, t: float, out: str | Path, max_width: int = 768) -> Path:
    """Write the frame at ``t`` seconds as JPEG, downscaled to ``max_width``."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-ss", f"{max(t, 0.0):.3f}", "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "4", "-y", str(out),
    ])
    if not out.exists():
        # Seeking past the last frame produces no output; fall back to a bit earlier.
        run_ffmpeg([
            "-sseof", "-0.5", "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "4", "-y", str(out),
        ])
    return out


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheet(
    path: str | Path,
    times: Sequence[float],
    out: str | Path,
    *,
    columns: int = 4,
    thumb_width: int = 384,
    labels: Sequence[str] | None = None,
) -> Path:
    """Grid of frames with a timestamp label on each tile.

    One contact sheet costs far fewer image tokens than many full frames
    and gives Claude an overview of a long stretch of footage.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tiles: list[Image.Image] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(times):
            p = extract_frame(path, t, Path(tmp) / f"f{i:03d}.jpg", max_width=thumb_width)
            if p.exists():
                tiles.append(Image.open(p).convert("RGB"))
            else:
                tiles.append(Image.new("RGB", (thumb_width, thumb_width * 9 // 16), (40, 0, 0)))
        if not tiles:
            raise ValueError("no frames to put on the contact sheet")
        tw, th = tiles[0].size
        cols = max(1, min(columns, len(tiles)))
        rows = (len(tiles) + cols - 1) // cols
        pad = 4
        sheet = Image.new("RGB", (cols * tw + (cols + 1) * pad, rows * th + (rows + 1) * pad), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)
        font = _font(max(12, th // 9))
        for i, tile in enumerate(tiles):
            if tile.size != (tw, th):
                tile = tile.resize((tw, th))
            r, c = divmod(i, cols)
            x, y = pad + c * (tw + pad), pad + r * (th + pad)
            sheet.paste(tile, (x, y))
            label = labels[i] if labels else format_tc(times[i])
            bbox = draw.textbbox((0, 0), label, font=font)
            draw.rectangle([x, y, x + bbox[2] + 10, y + bbox[3] + 8], fill=(0, 0, 0))
            draw.text((x + 5, y + 3), label, fill=(255, 230, 0), font=font)
        sheet.save(out, quality=80)
    return out


def format_tc(t: float) -> str:
    t = max(t, 0.0)
    m, s = divmod(t, 60.0)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"
