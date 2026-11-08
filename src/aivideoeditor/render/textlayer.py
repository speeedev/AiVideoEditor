"""Text overlays and captions drawn with Pillow, for FFmpeg builds without drawtext/libass.

Some FFmpeg builds (for example Homebrew's slim ``ffmpeg`` formula) ship
without the ``drawtext`` and ``ass`` filters. In that case every distinct
state of the text layer (which titles are visible, which caption line and
highlighted word) is rendered once as a transparent canvas-sized PNG, and
each render chunk overlays a short image sequence (concat demuxer with
per-image durations) on top of the composited video.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

from ..media.ffmpeg import ffmpeg_bin
from ..timeline.mapping import TimelineWord
from ..timeline.model import Timeline
from .captions import CaptionEvent, caption_events


@functools.lru_cache(maxsize=4)
def _filters(binary: str) -> frozenset[str]:
    try:
        out = subprocess.run([binary, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    names = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.fullmatch(r"[A-Z.|]{2,4}", parts[0]):
            names.add(parts[1])
    return frozenset(names)


def ffmpeg_can_draw_text() -> bool:
    filters = _filters(ffmpeg_bin())
    return "drawtext" in filters and "ass" in filters


def use_pillow_text() -> bool:
    """Draw text ourselves? AVE_TEXT_RENDERER=pillow|ffmpeg forces a choice; default auto-detects."""
    mode = os.environ.get("AVE_TEXT_RENDERER", "auto").lower()
    if mode == "pillow":
        return True
    if mode == "ffmpeg":
        return False
    return not ffmpeg_can_draw_text()


_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "arialbd.ttf",
)


@functools.lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.ImageFont:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _rgba(color: str, default: str = "white") -> tuple[int, int, int, int]:
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


@dataclass(frozen=True)
class _State:
    texts: tuple[str, ...]          # ids of visible text overlays
    caption: int | None             # index into the caption events

    @property
    def empty(self) -> bool:
        return not self.texts and self.caption is None


class TextLayer:
    def __init__(self, tl: Timeline, words: list[TimelineWord] | None, width: int, height: int, workdir: Path):
        self.tl = tl
        self.width, self.height = width, height
        self.workdir = Path(workdir)
        self.texts = {x.id: x for x in tl.texts}
        self.events: list[CaptionEvent] = caption_events(words, tl.captions) if (tl.captions.enabled and words) else []
        self.spans = self._spans()
        self._files: dict[_State, str] = {}

    @property
    def visible(self) -> bool:
        return any(not st.empty for _, _, st in self.spans)

    def _spans(self) -> list[tuple[float, float, _State]]:
        total = max(self.tl.duration, 0.0)
        cuts = {0.0, total}
        for x in self.tl.texts:
            cuts.update((min(max(x.start, 0.0), total), min(max(x.end, 0.0), total)))
        for ev in self.events:
            cuts.update((min(max(ev.start, 0.0), total), min(max(ev.end, 0.0), total)))
        points = sorted(cuts)
        spans: list[tuple[float, float, _State]] = []
        for a, b in zip(points, points[1:]):
            if b - a <= 1e-6:
                continue
            mid = (a + b) / 2
            texts = tuple(x.id for x in self.tl.texts if x.start <= mid < x.end)
            cap = next((i for i in range(len(self.events) - 1, -1, -1)
                        if self.events[i].start <= mid < self.events[i].end), None)
            st = _State(texts, cap)
            if spans and spans[-1][2] == st and abs(spans[-1][1] - a) < 1e-6:
                spans[-1] = (spans[-1][0], b, st)
            else:
                spans.append((a, b, st))
        return spans

    # -- drawing ------------------------------------------------------------
    def _image(self, st: _State) -> str:
        if st in self._files:
            return self._files[st]
        name = f"txt_{len(self._files):05d}.png"
        img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        for tid in st.texts:
            self._draw_text(draw, self.texts[tid])
        if st.caption is not None:
            self._draw_caption(draw, self.events[st.caption])
        img.save(self.workdir / name, compress_level=1)
        self._files[st] = name
        return name

    def _draw_text(self, draw: ImageDraw.ImageDraw, x) -> None:
        size = max(8, int(round(x.size * self.height)))
        font = _font(size)
        bbox = draw.multiline_textbbox((0, 0), x.text, font=font, align="center")
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cx, cy = x.x * self.width, x.y * self.height
        left, top = cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]
        if x.box:
            pad = max(4, size // 3)
            draw.rectangle([cx - w / 2 - pad, cy - h / 2 - pad, cx + w / 2 + pad, cy + h / 2 + pad],
                           fill=_rgba(x.box_color, "black"))
        draw.multiline_text((left, top), x.text, font=font, fill=_rgba(x.color), align="center")

    def _draw_caption(self, draw: ImageDraw.ImageDraw, ev: CaptionEvent) -> None:
        cfg = self.tl.captions
        size = max(10, int(round(cfg.size * self.height)))
        font = _font(size)
        stroke = max(1, size // 12)
        space = draw.textlength(" ", font=font)
        max_w = self.width * 0.88
        # Greedy wrap into lines of (word index, text, width).
        lines: list[list[tuple[int, str, float]]] = [[]]
        line_w = 0.0
        for i, t in enumerate(ev.texts):
            tw = draw.textlength(t, font=font) + 2 * stroke
            if lines[-1] and line_w + space + tw > max_w:
                lines.append([])
                line_w = 0.0
            line_w += (space if lines[-1] else 0) + tw
            lines[-1].append((i, t, tw))
        ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (size, size // 4)
        line_h = ascent + descent + stroke * 2
        bottom = cfg.y * self.height
        y = bottom - line_h * len(lines)
        normal, hi, outline = _rgba(cfg.color), _rgba(cfg.highlight_color), _rgba(cfg.outline_color, "black")
        for line in lines:
            total = sum(tw for _, _, tw in line) + space * (len(line) - 1)
            x = (self.width - total) / 2
            for i, t, tw in line:
                draw.text((x + stroke, y + stroke), t, font=font, fill=hi if i == ev.highlight else normal,
                          stroke_width=stroke, stroke_fill=outline)
                x += tw + space
            y += line_h

    # -- per chunk ----------------------------------------------------------
    def concat_list(self, t0: float, t1: float, name: str) -> str | None:
        """Write an image-sequence list covering [t0, t1); None when nothing is visible there."""
        parts = [(max(a, t0), min(b, t1), st) for a, b, st in self.spans if b > t0 and a < t1]
        if not parts or all(st.empty for _, _, st in parts):
            return None
        lines = []
        last = ""
        covered = t0
        for a, b, st in parts:
            if a > covered + 1e-6:     # a gap in the spans: transparent
                lines += [f"file '{self._image(_State((), None))}'", f"duration {a - covered:.6f}"]
            last = self._image(st)
            lines += [f"file '{last}'", f"duration {b - a:.6f}"]
            covered = b
        lines.append(f"file '{last}'")   # concat demuxer: repeat the last entry so its duration counts
        (self.workdir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return name
