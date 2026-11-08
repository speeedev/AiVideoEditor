"""Human/LLM-readable text views of the timeline and transcript."""

from __future__ import annotations

from ..analysis.frames import format_tc
from .mapping import TimelineWord
from .model import Timeline, Track


def _fmt_clip(c, track: Track) -> str:
    parts = [f"{c.id:>5} {format_tc(c.start)}–{format_tc(c.end)} ({c.duration:6.2f}s)",
             f"{c.asset_id} src {c.source_in:.2f}–{c.source_out:.2f}"]
    if abs(c.speed - 1) > 1e-6:
        parts.append(f"speed {c.speed:.3g}x")
    if track.kind == "video" or c.volume != 1:
        parts.append(f"vol {c.volume:.2f}")
    if track.kind == "video":
        tr = c.transform
        if not c.visible:
            parts.append("HIDDEN")
        else:
            box = "full" if (tr.x, tr.y, tr.w, tr.h) == (0, 0, 1, 1) else \
                f"box x{tr.x:.2f} y{tr.y:.2f} w{tr.w:.2f} h{tr.h:.2f}"
            parts.append(f"{box} {tr.fit}")
            if tr.crop:
                parts.append(f"zoom x{tr.crop.x:.2f} y{tr.crop.y:.2f} w{tr.crop.w:.2f} h{tr.crop.h:.2f}")
            if tr.opacity < 1:
                parts.append(f"opacity {tr.opacity:.2f}")
    if c.fade_in or c.fade_out:
        parts.append(f"fade {c.fade_in:.2f}/{c.fade_out:.2f}")
    if c.label:
        parts.append(f'"{c.label}"')
    return "  ".join(parts)


def describe_timeline(tl: Timeline, *, start: float | None = None, end: float | None = None,
                      max_clips_per_track: int = 40) -> str:
    cv = tl.canvas
    lines = [f"Canvas {cv.width}x{cv.height} @ {cv.fps:g} fps · duration {format_tc(tl.duration)} "
             f"({tl.duration:.2f}s)"]
    if start is not None or end is not None:
        lines.append(f"Showing {format_tc(start or 0)}–{format_tc(end if end is not None else tl.duration)}")
    lines.append("Tracks (video tracks listed bottom layer first):")
    for t in tl.tracks:
        flags = [f for f, on in (("muted", t.muted), ("hidden", t.hidden), ("locked", t.locked),
                                 ("ducks under voice", t.duck)) if on]
        if t.volume != 1:
            flags.append(f"track vol {t.volume:.2f}")
        lines.append(f"  {t.id} [{t.kind}] {t.name!r} role={t.role or '-'} "
                     f"{len(t.clips)} clips{(' · ' + ', '.join(flags)) if flags else ''}")
        clips = [c for c in t.clips
                 if (start is None or c.end > start) and (end is None or c.start < end)]
        shown = clips[:max_clips_per_track]
        for c in shown:
            lines.append("    " + _fmt_clip(c, t))
        if len(clips) > len(shown):
            lines.append(f"    … {len(clips) - len(shown)} more clips (ask for a narrower range)")
    if tl.texts:
        lines.append("Text overlays:")
        for x in tl.texts:
            lines.append(f"  {x.id} {format_tc(x.start)}–{format_tc(x.end)} {x.text!r} "
                         f"at ({x.x:.2f},{x.y:.2f}) size {x.size:.3f}")
    cap = tl.captions
    lines.append(f"Captions: {'ON from ' + str(cap.source_asset) if cap.enabled else 'off'}")
    if tl.markers:
        lines.append("Markers:")
        for m in tl.markers:
            lines.append(f"  {m.id} {format_tc(m.time)} {m.note}")
    return "\n".join(lines)


def format_words(words: list[TimelineWord], *, mode: str = "phrases", pause: float = 0.45,
                 max_words: int = 14, low_conf: float = 0.45) -> str:
    """Transcript text with timeline timestamps.

    ``phrases``: one line per phrase, split at pauses / cuts.
    ``words``: every word with its start time (for precise cuts).
    Cuts between clips are shown as ``✂``; long pauses as ``(pause Xs)``.
    Low-confidence words are marked with ``?``.
    """
    if not words:
        return "(no transcribed speech in this range)"
    out: list[str] = []
    if mode == "words":
        line: list[str] = []
        for i, w in enumerate(words):
            if i and w.clip_id != words[i - 1].clip_id:
                line.append("✂")
            mark = "?" if w.prob < low_conf else ""
            line.append(f"[{w.start:.2f}]{w.text}{mark}")
            if len(line) >= 10:
                out.append(" ".join(line))
                line = []
        if line:
            out.append(" ".join(line))
        return "\n".join(out)

    phrase: list[TimelineWord] = []

    def flush():
        if phrase:
            text = " ".join(w.text + ("?" if w.prob < low_conf else "") for w in phrase)
            out.append(f"[{format_tc(phrase[0].start)}–{format_tc(phrase[-1].end)}] {text}")

    for i, w in enumerate(words):
        if phrase:
            prev = phrase[-1]
            gap = w.start - prev.end
            cut = w.clip_id != prev.clip_id
            if gap >= pause or cut or len(phrase) >= max_words:
                flush()
                phrase = []
                if cut:
                    out.append("  ✂")
                if gap >= pause:
                    out.append(f"  (pause {gap:.1f}s)")
        phrase.append(w)
    flush()
    return "\n".join(out)
