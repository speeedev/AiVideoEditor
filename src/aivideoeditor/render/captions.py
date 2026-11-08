"""Generate ASS subtitles from transcript words mapped onto the timeline."""

from __future__ import annotations

from dataclasses import dataclass

from ..timeline.mapping import TimelineWord
from ..timeline.model import Captions


@dataclass
class CaptionLine:
    start: float
    end: float
    words: list[TimelineWord]


def group_words(words: list[TimelineWord], cfg: Captions, *, pause: float = 0.5) -> list[CaptionLine]:
    lines: list[CaptionLine] = []
    cur: list[TimelineWord] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            too_long = w.end - cur[0].start > cfg.max_duration
            if (len(cur) >= cfg.max_words or gap > pause or too_long
                    or w.clip_id != cur[-1].clip_id and gap > 0.15
                    or cur[-1].text.rstrip().endswith((".", "?", "!"))):
                lines.append(CaptionLine(cur[0].start, cur[-1].end, cur))
                cur = []
        cur.append(w)
    if cur:
        lines.append(CaptionLine(cur[0].start, cur[-1].end, cur))
    # Hold each line a little after its last word (min 0.6 s on screen),
    # but never into the next line.
    for i, ln in enumerate(lines):
        end = max(ln.end + 0.25, ln.start + 0.6)
        if i + 1 < len(lines):
            end = min(end, lines[i + 1].start)
        ln.end = max(end, ln.start + 0.2)
    return lines


@dataclass(frozen=True)
class CaptionEvent:
    """One on-screen state of the captions: a line of words, optionally with one highlighted."""

    start: float
    end: float
    texts: tuple[str, ...]
    highlight: int | None = None


def caption_events(words: list[TimelineWord], cfg: Captions) -> list[CaptionEvent]:
    events: list[CaptionEvent] = []
    for line in group_words(words, cfg):
        texts = tuple((w.text.upper() if cfg.uppercase else w.text) for w in line.words)
        if not cfg.highlight_words:
            events.append(CaptionEvent(line.start, line.end, texts))
            continue
        for i, w in enumerate(line.words):
            a = w.start if i else line.start
            b = line.words[i + 1].start if i + 1 < len(line.words) else line.end
            if b - a >= 0.02:
                events.append(CaptionEvent(a, b, texts, i))
    return events


def ass_color(color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS colour)."""
    c = color.lstrip("#")
    if len(c) != 6:
        c = "FFFFFF"
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[TimelineWord], cfg: Captions, width: int, height: int) -> str:
    font_size = max(10, int(round(cfg.size * height)))
    margin_v = max(0, int(round((1.0 - cfg.y) * height)))
    outline = max(1, font_size // 12)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{cfg.font},{font_size},{ass_color(cfg.color)},{ass_color(cfg.highlight_color)},"
        f"{ass_color(cfg.outline_color)},&H64000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,"
        f"{int(width * 0.06)},{int(width * 0.06)},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    hi = ass_color(cfg.highlight_color)
    for ev in caption_events(words, cfg):
        parts = [(f"{{\\c{hi}}}{_escape(t)}{{\\r}}" if j == ev.highlight else _escape(t))
                 for j, t in enumerate(ev.texts)]
        events.append(f"Dialogue: 0,{_ts(ev.start)},{_ts(ev.end)},Default,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"
