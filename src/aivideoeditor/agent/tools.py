"""The editing tools Claude uses, with validated inputs and readable results."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..analysis.frames import format_tc
from ..analysis.transcribe import Word
from ..timeline import layouts, ops
from ..timeline.autocut import filler_ranges, pause_ranges, repeated_phrases
from ..timeline.describe import describe_timeline, format_words
from ..timeline.mapping import TimelineWord, asset_clips, silences_on_timeline, snap_to_pauses
from ..timeline.model import Rect, Transform
from ..timeline.ops import EditError
from .schema import tool_schema

ToolOutput = str | list[dict[str, Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[Any, Any], ToolOutput]
    mutates: bool             # snapshot before, validate + save after, roll back on failure
    locks: bool = False       # changes the timeline without a snapshot (undo)

    def definition(self, *, eager: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": tool_schema(self.model),
        }
        if eager:
            d["eager_input_streaming"] = True
        return d


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, model: type[BaseModel], *, mutates: bool = False, locks: bool = False):
    def deco(fn: Callable[[Any, Any], ToolOutput]):
        doc = (fn.__doc__ or "").strip()
        REGISTRY[name] = ToolSpec(name, " ".join(line.strip() for line in doc.splitlines()), model, fn,
                                  mutates, locks or mutates)
        return fn
    return deco


def image_block(path: Path) -> dict[str, Any]:
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


# ------------------------------------------------------------ input models --

class Empty(BaseModel):
    pass


class RangeIn(BaseModel):
    start: float = Field(description="Range start in seconds")
    end: float = Field(description="Range end in seconds")


class RectIn(BaseModel):
    x: float = Field(ge=0, le=1, description="Left edge, 0..1 of source width")
    y: float = Field(ge=0, le=1, description="Top edge, 0..1 of source height")
    w: float = Field(gt=0, le=1, description="Width, 0..1 of source width")
    h: float = Field(gt=0, le=1, description="Height, 0..1 of source height")


class GetTimelineIn(BaseModel):
    start: float | None = Field(None, description="Only show clips after this timeline time")
    end: float | None = Field(None, description="Only show clips before this timeline time")


class GetTranscriptIn(BaseModel):
    start: float | None = Field(None, description="Range start (seconds, in the chosen timebase)")
    end: float | None = Field(None, description="Range end (seconds, in the chosen timebase)")
    mode: Literal["phrases", "words"] = Field(
        "phrases", description="'phrases' for reading; 'words' gives every word's start time for precise cuts")
    asset_id: str | None = Field(None, description="Asset whose speech to show (default: primary audio)")
    timebase: Literal["timeline", "source"] = Field(
        "timeline", description="'timeline' = current edit (cut material is absent); "
                                "'source' = raw recording times, including material already cut")


class FindCandidatesIn(BaseModel):
    kind: Literal["pauses", "fillers", "retakes"]
    asset_id: str | None = Field(None, description="Asset to analyse (default: primary audio)")
    min_pause: float = Field(0.7, description="pauses: only pauses longer than this (s)")
    keep: float = Field(0.2, description="pauses: silence to keep on each side of a shortened pause (s)")


class ViewTimelineIn(BaseModel):
    times: list[float] = Field(min_length=1, max_length=12, description="Timeline times to look at")
    detail: Literal["overview", "detail"] = Field(
        "overview", description="'overview': small tiles (up to 12); 'detail': large frames (up to 4)")


class ViewSourceIn(BaseModel):
    asset_id: str
    times: list[float] = Field(min_length=1, max_length=12, description="Times inside the source file")
    detail: Literal["overview", "detail"] = "overview"


class VisualActivityIn(BaseModel):
    asset_id: str | None = Field(None, description="Video asset (default: the screen recording)")
    start: float | None = None
    end: float | None = None


class RippleDeleteIn(BaseModel):
    ranges: list[RangeIn] = Field(min_length=1, description="Timeline ranges to remove, all relative to the "
                                                            "current timeline (they are applied together)")
    snap_to_pauses: bool = Field(True, description="Move range edges into the nearest pause (±0.25 s) so "
                                                   "words are not clipped")
    tracks: list[str] | None = Field(None, description="Limit to these track ids (default: all unlocked "
                                                       "tracks, which keeps everything in sync)")


class MoveRangeIn(BaseModel):
    start: float = Field(description="Range start (timeline time)")
    end: float = Field(description="Range end (timeline time)")
    to: float = Field(description="Timeline time (outside the range) where the moved part should start, "
                                  "in current timeline time; 0 moves it to the beginning")


class LiftIn(BaseModel):
    start: float
    end: float
    tracks: list[str] = Field(min_length=1, description="Track ids to clear (the gap stays)")


class SplitIn(BaseModel):
    time: float
    tracks: list[str] | None = None


class PlaceClipIn(BaseModel):
    track_id: str
    asset_id: str
    source_in: float = Field(description="Start inside the asset (s)")
    source_out: float = Field(description="End inside the asset (s)")
    start: float = Field(description="Timeline position (s)")
    mode: Literal["overwrite", "insert"] = Field(
        "overwrite", description="'insert' pushes later material on all unlocked tracks to make room")
    volume: float = Field(1.0, ge=0, le=4)
    speed: float = Field(1.0, gt=0.05, le=16)


class AddTrackIn(BaseModel):
    kind: Literal["video", "audio"]
    name: str = ""
    role: str | None = Field(None, description="screen, camera, mic, music, broll, sfx or other")


class SetTrackIn(BaseModel):
    track_id: str
    name: str | None = None
    muted: bool | None = None
    hidden: bool | None = None
    locked: bool | None = Field(None, description="Locked tracks are not affected by ripple edits")
    duck: bool | None = Field(None, description="Audio: automatically lower this track under speech")
    volume: float | None = Field(None, ge=0, le=4)
    denoise: bool | None = Field(None, description="Audio: reduce steady background noise (fan, hum, room)")
    enhance: bool | None = Field(None, description="Audio: voice polish (rumble high-pass + gentle compression)")


class AudioMasteringIn(BaseModel):
    normalize: bool = Field(True, description="Normalise the final mix to the loudness target")
    target_lufs: float = Field(-14.0, ge=-30, le=-8, description="-14 for YouTube/streaming, -16 for podcasts")


POSITIONS_9 = Literal["top_left", "top", "top_right", "left", "center", "right",
                      "bottom_left", "bottom", "bottom_right", "full"]


class AddOverlayIn(BaseModel):
    asset_id: str = Field(description="Image (logo, slide, screenshot) or video (b-roll) asset")
    start: float = Field(description="Timeline start")
    end: float = Field(description="Timeline end")
    source_in: float = Field(0.0, description="Video assets: start inside the file")
    position: POSITIONS_9 = "top_right"
    size: float = Field(0.18, gt=0.02, le=1.0, description="Width as a fraction of the canvas (ignored for 'full')")
    opacity: float = Field(1.0, gt=0, le=1)
    fade: float = Field(0.3, ge=0, le=3, description="Fade in/out seconds")
    volume: float = Field(0.0, ge=0, le=2, description="Video overlays: audio level (0 = silent)")


class MoveClipIn(BaseModel):
    clip_id: str
    start: float
    track_id: str | None = None
    overwrite: bool = False


class TrimClipIn(BaseModel):
    clip_id: str
    start: float | None = Field(None, description="New timeline start of the clip")
    end: float | None = Field(None, description="New timeline end of the clip")


class DeleteClipsIn(BaseModel):
    clip_ids: list[str] = Field(min_length=1)
    ripple: bool = False


class SetClipsIn(BaseModel):
    clip_ids: list[str] = Field(min_length=1)
    volume: float | None = Field(None, ge=0, le=4)
    fade_in: float | None = Field(None, ge=0, le=10)
    fade_out: float | None = Field(None, ge=0, le=10)
    visible: bool | None = None
    opacity: float | None = Field(None, ge=0, le=1)
    fit: Literal["contain", "cover", "stretch"] | None = None
    box: RectIn | None = Field(None, description="Where the clip is drawn on the canvas (0..1)")


class ApplyLayoutIn(BaseModel):
    start: float
    end: float
    layout: Literal["screen_only", "camera_only", "pip", "side_by_side", "stack"]
    pip_corner: Literal["bottom_right", "bottom_left", "top_right", "top_left"] = "bottom_right"
    pip_size: float = Field(0.26, ge=0.05, le=0.6, description="PiP width as a fraction of the canvas width")
    split: float | None = Field(None, ge=0.25, le=0.85, description="side_by_side / stack divider position")


class ZoomIn(BaseModel):
    start: float
    end: float
    region: RectIn | None = Field(description="Region of the source frame to fill the box with; null resets")
    track_id: str | None = Field(None, description="Video track (default: the screen track)")
    ease: float = Field(0.4, ge=0, le=2, description="Seconds of smooth zoom-in at the start and zoom-out at the "
                                                     "end of the range; 0 = instant punch-in")


class SpeedUpIn(BaseModel):
    start: float
    end: float
    factor: float = Field(gt=0.1, le=16, description="2 = twice as fast")
    mute: bool = Field(True, description="Silence audio in the sped-up part")


class AddMusicIn(BaseModel):
    asset_id: str
    start: float = 0.0
    end: float | None = Field(None, description="Timeline end (default: end of the edit)")
    source_in: float = 0.0
    volume: float = Field(0.15, ge=0, le=2)
    duck: bool = True
    fade_in: float = Field(1.0, ge=0)
    fade_out: float = Field(2.5, ge=0)


class AddTextIn(BaseModel):
    start: float
    end: float
    text: str = Field(min_length=1)
    position: Literal["top", "center", "bottom", "lower_third"] = "top"
    x: float | None = Field(None, ge=0, le=1, description="Custom centre x (overrides position)")
    y: float | None = Field(None, ge=0, le=1, description="Custom centre y (overrides position)")
    size: float = Field(0.06, gt=0.01, le=0.3, description="Font size as a fraction of canvas height")
    color: str = "white"
    box: bool = True


class UpdateTextIn(BaseModel):
    text_id: str
    start: float | None = None
    end: float | None = None
    text: str | None = None
    x: float | None = Field(None, ge=0, le=1)
    y: float | None = Field(None, ge=0, le=1)
    size: float | None = Field(None, gt=0.01, le=0.3)
    color: str | None = None
    box: bool | None = None


class RemoveTextIn(BaseModel):
    text_id: str


class SetCaptionsIn(BaseModel):
    enabled: bool
    source_asset: str | None = None
    max_words: int | None = Field(None, ge=1, le=20)
    highlight_words: bool | None = None
    uppercase: bool | None = None
    size: float | None = Field(None, gt=0.01, le=0.2)
    y: float | None = Field(None, ge=0.1, le=1.0)


class SetCanvasIn(BaseModel):
    preset: Literal["landscape_1080p", "landscape_1440p", "landscape_720p", "landscape_4k", "vertical_1080x1920",
                    "square_1080", "custom"] = "landscape_1080p"
    width: int | None = Field(None, ge=128, le=7680, description="custom only")
    height: int | None = Field(None, ge=128, le=7680, description="custom only")
    fps: float | None = Field(None, ge=10, le=120, description="Frame rate (default: keep)")


class SaveTextIn(BaseModel):
    filename: str = Field(description="File name such as 'youtube-description.md' or 'chapters.txt'")
    content: str = Field(min_length=1)


class AddMarkerIn(BaseModel):
    time: float
    note: str


class UndoIn(BaseModel):
    steps: int = Field(1, ge=1, le=20)


class RenderPreviewIn(BaseModel):
    frames: int = Field(8, ge=0, le=12, description="Evenly spaced frames from the rendered file to look at")


class AssembleIn(BaseModel):
    layout: Literal["screen_only", "camera_only", "pip", "side_by_side", "stack"] | None = None


# ----------------------------------------------------------------- helpers --

NO_SPEECH = ("This project has no recording with audio, so there is no speech to read. Edit from the "
             "picture: scan_video and get_visual_activity show what happens on screen and where; view_source "
             "and view_timeline show frames.")


def _primary(ctx, asset_id: str | None) -> str:
    aid = asset_id or ctx.session.primary_audio()
    if aid is None:
        raise EditError("there is no recording with audio in this project")
    ctx.session.project.asset(aid)
    return aid


def _track_for_role(ctx, role: str) -> str:
    for t in ctx.session.timeline.video_tracks:
        if t.role == role:
            return t.id
    raise EditError(f"no video track with role '{role}'")


def _words_in(words: list[TimelineWord], start: float | None, end: float | None) -> list[TimelineWord]:
    return [w for w in words if (start is None or w.end > start) and (end is None or w.start < end)]


def _source_words(words: list[Word], asset_id: str, start: float | None, end: float | None) -> list[TimelineWord]:
    out = []
    for w in words:
        if (start is None or w.end > start) and (end is None or w.start < end):
            out.append(TimelineWord(w.start, w.end, w.text, w.prob, asset_id, w.start, w.end))
    return out


def _sources(ctx, asset_id: str | None) -> list[str]:
    """The recordings to read speech from: the given one, or every scene's speech recording."""
    if asset_id:
        return [_primary(ctx, asset_id)]
    return ctx.session.speech_sources()


def _pauses(ctx) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for aid in ctx.session.speech_sources():
        sp = ctx.session.project.speech(aid)
        if sp:
            out += silences_on_timeline(ctx.session.timeline, aid, sp)
    return sorted(out)


def _duration_line(ctx) -> str:
    return f"Timeline is now {format_tc(ctx.session.timeline.duration)} ({ctx.session.timeline.duration:.2f}s)."


def _source_aspect(ctx, track_id: str) -> float:
    track = ctx.session.timeline.track(track_id)
    for c in track.clips:
        info = ctx.session.project.asset(c.asset_id).info
        if info.get("width") and info.get("height"):
            return info["width"] / info["height"]
    return 16 / 9


# ------------------------------------------------------------ inspection --

@tool("get_project_overview", Empty)
def get_project_overview(ctx, args: Empty) -> ToolOutput:
    """Assets (role, length, resolution, sync offset) and which analysis (transcript, pauses, visual activity)
    is available."""
    return ctx.overview()


@tool("get_timeline", GetTimelineIn)
def get_timeline(ctx, args: GetTimelineIn) -> ToolOutput:
    """Tracks and clips of the current edit with ids, timeline/source times, volumes, layout boxes and zooms.
    Use a range for long timelines."""
    return describe_timeline(ctx.session.timeline, start=args.start, end=args.end)


@tool("get_transcript", GetTranscriptIn)
def get_transcript(ctx, args: GetTranscriptIn) -> ToolOutput:
    """What is said and when. In the timeline timebase the times are positions in the current edit, already
    accounting for every cut (and covering every scene); '✂' marks a cut, '(pause Xs)' a silence, '?' a
    low-confidence word."""
    if args.asset_id is None and ctx.session.primary_audio() is None:
        return NO_SPEECH
    proj = ctx.session.project
    sources = _sources(ctx, args.asset_id)
    have = [sid for sid in sources if proj.transcript(sid) is not None]
    if not have:
        return (f"No transcript for {', '.join(sources) or 'this project'}. Run `ave analyze` with transcription, "
                f"or rely on pauses (find_edit_candidates kind=pauses) and visuals.")
    if args.timebase == "source":
        if len(have) > 1:
            return ("Source times are per recording; pass asset_id (one of: " + ", ".join(have) + ").")
        aid = have[0]
        words = _source_words(list(proj.transcript(aid).words()), aid, args.start, args.end)
        name = aid
    else:
        words = _words_in(ctx.session.words(args.asset_id), args.start, args.end)
        name = ", ".join(have)
    lang = proj.transcript(have[0]).language or "?"
    missing = [sid for sid in sources if sid not in have]
    header = f"Transcript of {name} ({args.timebase} time, language {lang}):\n"
    if missing:
        header += f"(no transcript yet for {', '.join(missing)})\n"
    text = format_words(words, mode=args.mode)
    if len(text) > 60000:
        text = text[:60000] + "\n… (truncated: request a narrower range)"
    return header + text


@tool("find_edit_candidates", FindCandidatesIn)
def find_edit_candidates(ctx, args: FindCandidatesIn) -> ToolOutput:
    """Proposes ranges to cut (nothing is changed): long pauses, filler words (um/uh, eee/ıı) or repeated
    phrases that suggest a retake. Review them, then pass the ones you agree with to ripple_delete."""
    if args.asset_id is None and ctx.session.primary_audio() is None:
        return NO_SPEECH
    proj = ctx.session.project
    tl = ctx.session.timeline
    sources = _sources(ctx, args.asset_id)
    if args.kind == "pauses":
        ranges: list[tuple[float, float]] = []
        analysed = False
        for aid in sources:
            sp = proj.speech(aid)
            if sp is not None:
                analysed = True
                ranges += pause_ranges(tl, aid, sp, min_pause=args.min_pause, keep=args.keep)
        if not analysed:
            return f"No pause analysis for {', '.join(sources)} (run `ave analyze`)."
        ranges.sort()
        total = sum(b - a for a, b in ranges)
        lines = [f"{len(ranges)} pauses longer than {args.min_pause}s would be shortened, saving {total:.1f}s:"]
        lines += [f"  [{a:.2f}, {b:.2f}]  ({b - a:.2f}s)" for a, b in ranges[:400]]
        return "\n".join(lines)
    transcripts = [(aid, proj.transcript(aid)) for aid in sources]
    transcripts = [(aid, tr) for aid, tr in transcripts if tr is not None]
    if not transcripts:
        return f"No transcript for {', '.join(sources)}, so {args.kind} cannot be detected."
    if args.kind == "fillers":
        found = sorted(f for aid, tr in transcripts for f in filler_ranges(tl, aid, tr))
        if not found:
            return "No filler words found in the transcript (Whisper may have omitted them; check pauses too)."
        return "Filler words:\n" + "\n".join(f"  [{a:.2f}, {b:.2f}] {w!r}" for a, b, w in found[:400])
    found = sorted((f for aid, tr in transcripts for f in repeated_phrases(tl, aid, tr)),
                   key=lambda f: f["first_take"])
    if not found:
        return "No obvious repeated phrases (retakes) found."
    lines = ["Possible retakes (an earlier take followed by a restart of the same words):"]
    for f in found[:100]:
        a, b = f["first_take"]
        lines.append(f"  first take [{a:.2f}, {b:.2f}] restart at {f['retake_starts']:.2f}: {f['phrase']!r}")
    lines.append("Read the transcript around each one before cutting: keep the best (usually the last) take.")
    lines.append("Whole scenes recorded more than once are listed by get_scenes.")
    return "\n".join(lines)


@tool("view_timeline", ViewTimelineIn)
def view_timeline(ctx, args: ViewTimelineIn) -> ToolOutput:
    """Look at the composited output (layouts, zooms, texts, captions) at the given timeline times."""
    tl = ctx.session.timeline
    times = [min(max(t, 0.0), max(tl.duration - 1e-3, 0.0)) for t in args.times]
    if args.detail == "detail":
        times = times[:4]
        sheet = ctx.session.timeline_sheet(times, thumb_width=768, columns=2)
    else:
        sheet = ctx.session.timeline_sheet(times, thumb_width=384, columns=4)
    return [{"type": "text", "text": f"Timeline frames at {', '.join(format_tc(t) for t in times)}:"},
            image_block(sheet)]


@tool("view_source", ViewSourceIn)
def view_source(ctx, args: ViewSourceIn) -> ToolOutput:
    """Look at frames of a raw video asset at source times (e.g. to find what is on screen, or a region
    to zoom into)."""
    asset = ctx.session.project.asset(args.asset_id)
    times = [min(max(t, 0.0), max(asset.duration - 0.05, 0.0)) for t in args.times]
    if args.detail == "detail":
        times = times[:4]
        sheet = ctx.session.source_sheet(args.asset_id, times, thumb_width=768, columns=2)
    else:
        sheet = ctx.session.source_sheet(args.asset_id, times, thumb_width=384, columns=4)
    w, h = asset.info.get("width"), asset.info.get("height")
    return [{"type": "text", "text": f"{args.asset_id} ({w}x{h}) source frames at "
                                     f"{', '.join(format_tc(t) for t in times)}:"},
            image_block(sheet)]


def _default_video_asset(ctx) -> str | None:
    proj = ctx.session.project
    return (next((a.id for a in proj.assets.values() if a.role == "screen" and a.has_video), None)
            or next((a.id for a in proj.assets.values() if a.has_video and not a.is_image), None))


def _activity_on_timeline(ctx, aid: str, lo: float, hi: float):
    """Active spans (with change regions) and hard cuts of an asset, projected onto the timeline."""
    vis = ctx.session.project.visual(aid)
    tl = ctx.session.timeline
    active: list[tuple[float, float, list[float] | None]] = []
    cuts: list[float] = []
    if vis is None:
        return None, active, cuts
    for _, c in asset_clips(tl, aid):
        for (a, b), region in zip(vis.active, vis.regions):
            s0, s1 = max(a, c.source_in), min(b, c.source_out)
            if s1 > s0:
                ta, tb = c.timeline_time(s0), c.timeline_time(s1)
                if tb > lo and ta < hi:
                    active.append((max(ta, lo), min(tb, hi), region))
        for t in vis.scene_cuts:
            if c.source_in <= t < c.source_out and lo <= c.timeline_time(t) <= hi:
                cuts.append(c.timeline_time(t))
    active.sort(key=lambda x: x[0])
    return vis, active, sorted(cuts)


def _region_text(r: list[float] | None) -> str:
    if not r:
        return ""
    x, y, w, h = r
    if w > 0.9 and h > 0.9:
        return " [whole screen]"
    return f" [x{x:.2f}-{x + w:.2f} y{y:.2f}-{y + h:.2f}]"


@tool("get_visual_activity", VisualActivityIn)
def get_visual_activity(ctx, args: VisualActivityIn) -> ToolOutput:
    """When and where something changes on screen (typing, cursor, scrolling, windows opening) and where hard cuts
    are, mapped onto the timeline. Regions are normalised [x0-x1 y0-y1] of the source frame: use them to place
    zooms. Idle stretches are candidates for speed_up or cutting."""
    aid = args.asset_id or _default_video_asset(ctx)
    if aid is None:
        return "No video assets."
    tl = ctx.session.timeline
    lo = args.start if args.start is not None else 0.0
    hi = args.end if args.end is not None else tl.duration
    vis, active, cuts = _activity_on_timeline(ctx, aid, lo, hi)
    if vis is None:
        return f"No visual analysis for '{aid}' (run `ave analyze`)."
    idle: list[tuple[float, float]] = []
    cur = lo
    for a, b, _ in active:
        if a - cur >= 3.0:
            idle.append((cur, a))
        cur = max(cur, b)
    if hi - cur >= 3.0:
        idle.append((cur, hi))
    act = ", ".join(f"{a:.1f}–{b:.1f}{_region_text(r)}" for a, b, r in active[:200]) or "none"
    lines = [f"Visual activity of {aid} on the timeline {format_tc(lo)}–{format_tc(hi)}:",
             "Active (screen changing): " + act,
             "Idle ≥3s (static screen): " + (", ".join(f"{a:.1f}–{b:.1f}" for a, b in idle[:200]) or "none"),
             "Hard cuts / big changes: " + (", ".join(f"{t:.2f}" for t in cuts) or "none")]
    return "\n".join(lines)


class ScanVideoIn(BaseModel):
    start: float | None = Field(None, description="Timeline range start (default: beginning)")
    end: float | None = Field(None, description="Timeline range end (default: end)")
    frames: int = Field(12, ge=4, le=16, description="How many frames to look at")
    asset_id: str | None = Field(None, description="Video whose activity guides the frame choice (default: screen)")


@tool("scan_video", ScanVideoIn)
def scan_video(ctx, args: ScanVideoIn) -> ToolOutput:
    """Look through a stretch of the edit at the moments where the picture changes (plus evenly spaced fill) in
    one contact sheet. The fastest way to understand footage without narration, e.g. a silent screen recording:
    scan the whole video first, then scan narrower ranges."""
    tl = ctx.session.timeline
    if tl.duration <= 0:
        return "The timeline is empty."
    lo = max(0.0, args.start if args.start is not None else 0.0)
    hi = min(tl.duration, args.end if args.end is not None else tl.duration)
    if hi <= lo:
        raise EditError("end must be after start")
    aid = args.asset_id or _default_video_asset(ctx)
    candidates: list[float] = []
    if aid:
        _, active, cuts = _activity_on_timeline(ctx, aid, lo, hi)
        for a, b, _ in active:
            candidates.append(min(a + 0.4, b))           # just after something starts changing
            if b - a > 6:
                candidates.append((a + b) / 2)
            candidates.append(max(b - 0.2, a))           # the settled result
        candidates += [min(t + 0.3, hi) for t in cuts]
    n = args.frames
    span = hi - lo
    min_gap = span / (n * 1.5)
    chosen: list[float] = []
    for t in sorted(candidates):
        if lo <= t < hi and all(abs(t - c) >= min_gap for c in chosen):
            chosen.append(t)
    # Fill the gaps with evenly spaced frames so quiet parts are seen too.
    k = 0
    while len(chosen) < n and k < 4 * n:
        k += 1
        gaps = sorted([lo] + chosen + [hi])
        a, b = max(zip(gaps, gaps[1:]), key=lambda g: g[1] - g[0])
        chosen.append((a + b) / 2)
    if len(chosen) > n:
        step = len(chosen) / n
        chosen = [sorted(chosen)[int(i * step)] for i in range(n)]
    times = sorted(min(max(t, 0.0), tl.duration - 1e-3) for t in chosen)
    sheet = ctx.session.timeline_sheet(times, thumb_width=384, columns=4)
    return [{"type": "text", "text": f"Frames of the edit {format_tc(lo)}–{format_tc(hi)} at "
                                     f"{', '.join(f'{t:.1f}' for t in times)} (chosen at visual changes):"},
            image_block(sheet)]


# ---------------------------------------------------------------- editing --

@tool("ripple_delete", RippleDeleteIn, mutates=True)
def ripple_delete(ctx, args: RippleDeleteIn) -> ToolOutput:
    """Remove time ranges from the edit and close the gaps, on all unlocked tracks at once so camera, screen
    and microphone stay in sync. This is the main cutting tool (dead air, mistakes, retakes, off-topic parts).
    Give all ranges in current timeline time in one call."""
    tl = ctx.session.timeline
    ranges = [(r.start, r.end) for r in args.ranges]
    notes: list[str] = []
    if args.snap_to_pauses:
        pauses = _pauses(ctx)
        snapped = []
        for a, b in ranges:
            a2 = snap_to_pauses(a, pauses) if a > 0 else a
            b2 = snap_to_pauses(b, pauses) if b < tl.duration else b
            if b2 - a2 < 0.02:
                a2, b2 = a, b
            if abs(a2 - a) > 1e-3 or abs(b2 - b) > 1e-3:
                notes.append(f"[{a:.2f}, {b:.2f}] snapped to [{a2:.2f}, {b2:.2f}]")
            snapped.append((a2, b2))
        ranges = snapped
    words = ctx.session.words() if ctx.session.primary_audio() else []
    for a, b in ranges:
        for w in words:
            for edge in (a, b):
                if w.start + 0.03 < edge < w.end - 0.03:
                    notes.append(f"warning: edge {edge:.2f} cuts into the word {w.text!r} ({w.start:.2f}–{w.end:.2f})")
    before = tl.duration
    removed = ops.ripple_delete(tl, ranges, args.tracks)
    msg = [f"Removed {removed:.2f}s in {len(ranges)} range(s) ({format_tc(before)} → {format_tc(tl.duration)})."]
    return "\n".join(msg + notes[:40])


@tool("move_range", MoveRangeIn, mutates=True)
def move_range(ctx, args: MoveRangeIn) -> ToolOutput:
    """Move a part of the edit to another place, on all unlocked tracks at once (picture and sound stay in sync):
    reorder scenes or sections. The part is taken out (its gap closes) and inserted at `to`."""
    tl = ctx.session.timeline
    moved = ops.move_range(tl, args.start, args.end, args.to)
    if not moved:
        return "Nothing moved: the target is already where the range starts or ends."
    dest = args.to if args.to <= args.start else args.to - moved
    return (f"Moved {format_tc(args.start)}–{format_tc(args.end)} ({moved:.2f}s); it now plays at "
            f"{format_tc(dest)}–{format_tc(dest + moved)}.")


def _scene_rows(ctx) -> list[dict]:
    from ..scenes import recorded_window, scene_groups, speech_source
    proj = ctx.session.project
    tl = ctx.session.timeline
    rows = []
    for g in scene_groups(proj):
        ids = {a.id for a in g}
        clips = [c for _, c in tl.all_clips() if c.asset_id in ids]
        src = speech_source(g)
        words = [w for w in ctx.session.words(src)] if src and proj.transcript(src) else []
        all_words = [w.text for w in proj.transcript(src).words()] if src and proj.transcript(src) else []
        t0, _, how = recorded_window(g[0])
        rows.append({
            "assets": [a.id for a in g],
            "range": (min(c.start for c in clips), max(c.end for c in clips)) if clips else None,
            "source_len": max(a.duration for a in g if not a.is_image),
            "recorded": t0, "recorded_how": how,
            "first": " ".join(w.text for w in words[:14]),
            "last": " ".join(w.text for w in words[-10:]) if len(words) > 14 else "",
            "bigrams": _bigrams(all_words),
        })
    rows.sort(key=lambda r: (r["range"] is None, r["range"][0] if r["range"] else 0.0))
    return rows


def _bigrams(words: list[str]) -> set[tuple[str, str]]:
    import re
    norm = [re.sub(r"[^\wçğıöşü]", "", w.lower()) for w in words]
    norm = [w for w in norm if w]
    return set(zip(norm, norm[1:]))


@tool("get_scenes", Empty)
def get_scenes(ctx, args: Empty) -> ToolOutput:
    """Separate scenes/takes in this project (recordings not made at the same moment), in timeline order, with
    their current timeline range, recording time, the start and end of what is said, and which scenes look like
    takes of the same script. Reorder with move_range; remove a take with ripple_delete on its range."""
    import time as _time
    rows = _scene_rows(ctx)
    if len(rows) <= 1:
        return ("All recordings belong to one scene (recorded at the same time). Retakes inside it show up in "
                "find_edit_candidates kind=retakes and in the transcript.")
    lines = [f"{len(rows)} scenes/takes (initially in recording order):"]
    for i, r in enumerate(rows, start=1):
        when = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(r["recorded"]))
        when += "" if r["recorded_how"] == "tag" else " (file time)"
        if r["range"]:
            a, b = r["range"]
            where = f"[{format_tc(a)}–{format_tc(b)}] ({b - a:.1f}s on the timeline)"
        else:
            where = "NOT on the timeline"
        lines.append(f"Scene {i} {where}, recorded {when}, recordings: {', '.join(r['assets'])}")
        if r["first"]:
            lines.append(f"  starts: «{r['first']}»" + (f"  ends: «{r['last']}»" if r["last"] else ""))
        for j, o in enumerate(rows, start=1):
            if j == i or not r["bigrams"] or not o["bigrams"]:
                continue
            same = len(r["bigrams"] & o["bigrams"]) / min(len(r["bigrams"]), len(o["bigrams"]))
            if same >= 0.5:
                lines.append(f"  similar to scene {j} ({same:.0%} same word pairs): probably another take")
    lines.append("Decide the order from the content and the brief, keep the best take of a repeated scene, "
                 "then check the result with get_transcript and view_timeline.")
    return "\n".join(lines)


@tool("lift", LiftIn, mutates=True)
def lift(ctx, args: LiftIn) -> ToolOutput:
    """Clear a time range on specific tracks without closing the gap (e.g. remove b-roll, leave a hole)."""
    ops.lift(ctx.session.timeline, args.start, args.end, args.tracks)
    return f"Cleared {args.start:.2f}–{args.end:.2f} on {', '.join(args.tracks)}."


@tool("split", SplitIn, mutates=True)
def split(ctx, args: SplitIn) -> ToolOutput:
    """Split clips at a timeline time (all unlocked tracks by default)."""
    ids = ops.split_at(ctx.session.timeline, args.time, args.tracks)
    return f"Split at {args.time:.2f}; new clips: {', '.join(ids) or 'none (nothing spans that time)'}."


@tool("place_clip", PlaceClipIn, mutates=True)
def place_clip(ctx, args: PlaceClipIn) -> ToolOutput:
    """Put a piece of an asset on a track (b-roll, an extra take, restoring cut material from the source)."""
    proj = ctx.session.project
    asset = proj.asset(args.asset_id)
    track = ctx.session.timeline.track(args.track_id)
    if track.kind == "video" and not asset.has_video:
        raise EditError(f"{args.asset_id} has no video; put it on an audio track")
    if args.source_out > asset.duration + 0.05:
        raise EditError(f"source_out {args.source_out:.2f} is past the end of {args.asset_id} ({asset.duration:.2f}s)")
    clip = ops.place_clip(ctx.session.timeline, args.track_id, args.asset_id, args.source_in, args.source_out,
                          args.start, speed=args.speed, volume=args.volume, mode=args.mode, label=args.asset_id)
    return f"Placed {clip.id} on {args.track_id} at {format_tc(clip.start)}–{format_tc(clip.end)}. " + _duration_line(ctx)


@tool("add_track", AddTrackIn, mutates=True)
def add_track(ctx, args: AddTrackIn) -> ToolOutput:
    """Add an empty video or audio track (new video tracks go on top)."""
    t = ops.add_track(ctx.session.timeline, args.kind, args.name, args.role)
    return f"Added track {t.id} ({t.kind}, role {t.role})."


@tool("set_track", SetTrackIn, mutates=True)
def set_track(ctx, args: SetTrackIn) -> ToolOutput:
    """Change track properties: mute, hide, lock (exclude from ripple edits), duck under speech, volume, name."""
    props = args.model_dump(exclude={"track_id"})
    t = ops.set_track(ctx.session.timeline, args.track_id, **props)
    return (f"Track {t.id}: muted={t.muted} hidden={t.hidden} locked={t.locked} duck={t.duck} volume={t.volume} "
            f"denoise={t.denoise} enhance={t.enhance}.")


@tool("set_audio_mastering", AudioMasteringIn, mutates=True)
def set_audio_mastering(ctx, args: AudioMasteringIn) -> ToolOutput:
    """Loudness normalisation of the final mix (on by default at -14 LUFS)."""
    tl = ctx.session.timeline
    tl.audio.normalize = args.normalize
    tl.audio.target_lufs = args.target_lufs
    return f"Final mix normalisation {'on at ' + str(args.target_lufs) + ' LUFS' if args.normalize else 'off'}."


def _overlay_box(position: str, size: float, aspect: float, canvas_aspect: float) -> tuple[float, float, float, float]:
    if position == "full":
        return 0.0, 0.0, 1.0, 1.0
    w = size
    h = min(1.0, w * canvas_aspect / aspect)
    if h >= 1.0:
        h, w = 1.0, aspect / canvas_aspect
    mx, my = 0.03 / canvas_aspect, 0.03
    col = "left" if "left" in position else "right" if "right" in position else "center"
    row = "top" if position.startswith("top") else "bottom" if position.startswith("bottom") else "middle"
    x = mx if col == "left" else 1 - w - mx if col == "right" else (1 - w) / 2
    y = my if row == "top" else 1 - h - my if row == "bottom" else (1 - h) / 2
    return max(0.0, x), max(0.0, y), w, h


@tool("add_overlay", AddOverlayIn, mutates=True)
def add_overlay(ctx, args: AddOverlayIn) -> ToolOutput:
    """Show an image (logo, slide, screenshot) or a b-roll video above everything else for a time range, on an
    overlay track that ripple edits keep in place relative to the content."""
    proj = ctx.session.project
    tl = ctx.session.timeline
    asset = proj.asset(args.asset_id)
    if not asset.has_video:
        raise EditError(f"{args.asset_id} has no picture")
    if args.end <= args.start:
        raise EditError("end must be after start")
    length = args.end - args.start
    if not asset.is_image and args.source_in + length > asset.duration + 0.05:
        raise EditError(f"{args.asset_id} is only {asset.duration:.2f}s long")
    w, h = asset.info.get("width") or 16, asset.info.get("height") or 9
    x, y, bw, bh = _overlay_box(args.position, args.size, w / h, tl.canvas.aspect)
    track = next((t for t in tl.video_tracks if t.role == "overlay" and
                  not any(c.start < args.end - 1e-6 and c.end > args.start + 1e-6 for c in t.clips)), None)
    if track is None:
        track = ops.add_track(tl, "video", name="overlay", role="overlay")
    clip = ops.place_clip(tl, track.id, args.asset_id, args.source_in, args.source_in + length, args.start,
                          volume=args.volume, label=args.asset_id,
                          transform=Transform(x=x, y=y, w=bw, h=bh, fit="contain" if args.position != "full"
                                              else "cover", opacity=args.opacity))
    clip.fade_in = clip.fade_out = min(args.fade, length / 2)
    return f"Overlay {clip.id} ({args.asset_id}) on {track.id} at {format_tc(clip.start)}–{format_tc(clip.end)}, {args.position}."


@tool("move_clip", MoveClipIn, mutates=True)
def move_clip(ctx, args: MoveClipIn) -> ToolOutput:
    """Move a clip to another time and/or track of the same kind."""
    c = ops.move_clip(ctx.session.timeline, args.clip_id, args.start, args.track_id, overwrite=args.overwrite)
    return f"Moved {c.id} to {format_tc(c.start)}–{format_tc(c.end)}."


@tool("trim_clip", TrimClipIn, mutates=True)
def trim_clip(ctx, args: TrimClipIn) -> ToolOutput:
    """Move one clip's start/end edges (timeline time) without shifting its content or other clips."""
    c = ops.trim_clip(ctx.session.timeline, args.clip_id, start=args.start, end=args.end)
    return f"{c.id} now {format_tc(c.start)}–{format_tc(c.end)} (source {c.source_in:.2f}–{c.source_out:.2f})."


@tool("delete_clips", DeleteClipsIn, mutates=True)
def delete_clips(ctx, args: DeleteClipsIn) -> ToolOutput:
    """Delete clips by id; with ripple=true the gap is closed on all unlocked tracks."""
    ops.delete_clips(ctx.session.timeline, args.clip_ids, ripple=args.ripple)
    return f"Deleted {', '.join(args.clip_ids)}. " + _duration_line(ctx)


@tool("set_clip_properties", SetClipsIn, mutates=True)
def set_clip_properties(ctx, args: SetClipsIn) -> ToolOutput:
    """Volume, fades, visibility, opacity, fit mode or canvas box of clips."""
    tl = ctx.session.timeline
    props: dict[str, Any] = {k: getattr(args, k) for k in ("volume", "fade_in", "fade_out", "visible")}
    clips = ops.set_clips(tl, args.clip_ids, **props)
    for c in clips:
        tr = c.transform.model_dump()
        if args.opacity is not None:
            tr["opacity"] = args.opacity
        if args.fit is not None:
            tr["fit"] = args.fit
        if args.box is not None:
            tr.update(x=args.box.x, y=args.box.y, w=args.box.w, h=args.box.h)
        c.transform = Transform.model_validate(tr)
    return f"Updated {', '.join(c.id for c in clips)}."


@tool("apply_layout", ApplyLayoutIn, mutates=True)
def apply_layout(ctx, args: ApplyLayoutIn) -> ToolOutput:
    """Arrange screen and camera for a time range: screen_only, camera_only, pip (camera in a corner over the
    screen), side_by_side, or stack (screen above camera, for vertical videos)."""
    from ..timeline.assemble import camera_aspect
    ids = layouts.apply_layout(ctx.session.timeline, args.start, args.end, args.layout,
                               camera_aspect=camera_aspect(ctx.session.project), pip_corner=args.pip_corner,
                               pip_size=args.pip_size, split=args.split)
    return f"Layout {args.layout} applied to {format_tc(args.start)}–{format_tc(args.end)} ({len(ids)} clip pieces)."


@tool("zoom", ZoomIn, mutates=True)
def zoom(ctx, args: ZoomIn) -> ToolOutput:
    """Punch in on a region of the source (normalised 0..1 of the source frame) for a time range, typically to
    make small UI text readable on a screen recording. The region is widened to the box's aspect ratio.
    region=null removes the zoom."""
    track_id = args.track_id or _track_for_role(ctx, "screen")
    if args.region is not None:
        r = args.region
        if r.x + r.w > 1.0001 or r.y + r.h > 1.0001:
            raise EditError("region must lie inside the frame (x+w <= 1, y+h <= 1)")
    region = Rect(**args.region.model_dump()) if args.region else None
    ids = layouts.zoom_range(ctx.session.timeline, track_id, args.start, args.end, region,
                             source_aspect=_source_aspect(ctx, track_id), ease=args.ease)
    c = ctx.session.timeline.find_clip(ids[0])[1]
    crop = c.transform.crop
    detail = f" Effective region x{crop.x:.2f} y{crop.y:.2f} w{crop.w:.2f} h{crop.h:.2f}." if crop else ""
    return f"Zoom {'set' if region else 'removed'} on {track_id} {format_tc(args.start)}–{format_tc(args.end)}.{detail}"


@tool("speed_up", SpeedUpIn, mutates=True)
def speed_up(ctx, args: SpeedUpIn) -> ToolOutput:
    """Play a range faster on all unlocked tracks (e.g. long typing, installs, loading screens) and close up
    the timeline. Audio is muted there by default."""
    new = ops.speed_range(ctx.session.timeline, args.start, args.end, args.factor, mute=args.mute)
    return (f"{format_tc(args.start)}–{format_tc(args.end)} now plays at {args.factor:g}x and lasts {new:.2f}s. "
            + _duration_line(ctx))


@tool("add_music", AddMusicIn, mutates=True)
def add_music(ctx, args: AddMusicIn) -> ToolOutput:
    """Add a music bed on a new locked audio track that ducks under speech, with fades."""
    proj = ctx.session.project
    tl = ctx.session.timeline
    asset = proj.asset(args.asset_id)
    if not asset.has_audio:
        raise EditError(f"{args.asset_id} has no audio")
    content_end = max((t.end for t in tl.tracks if t.role != "music"), default=tl.duration)
    end = args.end if args.end is not None else content_end
    if end <= args.start:
        raise EditError("music end must be after start")
    length = min(end - args.start, asset.duration - args.source_in)
    if length <= 0:
        raise EditError("source_in is past the end of the music file")
    track = ops.add_track(tl, "audio", name=f"music: {args.asset_id}", role="music")
    clip = ops.place_clip(tl, track.id, args.asset_id, args.source_in, args.source_in + length, args.start,
                          volume=args.volume, label=args.asset_id)
    clip.fade_in, clip.fade_out = args.fade_in, args.fade_out
    track.locked = True
    track.duck = args.duck
    note = "" if length >= end - args.start - 0.05 else f" (music is shorter than requested: {length:.1f}s)"
    return f"Music {args.asset_id} on {track.id} at {format_tc(clip.start)}–{format_tc(clip.end)}{note}."


POSITIONS = {"top": (0.5, 0.1), "center": (0.5, 0.5), "bottom": (0.5, 0.82), "lower_third": (0.3, 0.78)}


@tool("add_text", AddTextIn, mutates=True)
def add_text(ctx, args: AddTextIn) -> ToolOutput:
    """Add a title or text overlay (chapter titles, key points, names)."""
    x, y = POSITIONS[args.position]
    t = ops.add_text(ctx.session.timeline, args.start, args.end, args.text,
                     x=args.x if args.x is not None else x, y=args.y if args.y is not None else y,
                     size=args.size, color=args.color, box=args.box)
    return f"Added text {t.id} {format_tc(t.start)}–{format_tc(t.end)}."


@tool("update_text", UpdateTextIn, mutates=True)
def update_text(ctx, args: UpdateTextIn) -> ToolOutput:
    """Change a text overlay."""
    t = ops.update_text(ctx.session.timeline, args.text_id, **args.model_dump(exclude={"text_id"}))
    return f"Updated text {t.id}."


@tool("remove_text", RemoveTextIn, mutates=True)
def remove_text(ctx, args: RemoveTextIn) -> ToolOutput:
    """Remove a text overlay."""
    ops.remove_text(ctx.session.timeline, args.text_id)
    return f"Removed text {args.text_id}."


@tool("set_captions", SetCaptionsIn, mutates=True)
def set_captions(ctx, args: SetCaptionsIn) -> ToolOutput:
    """Turn burned-in captions (generated from the transcript, always in sync with the edit) on or off and
    style them."""
    props = args.model_dump()
    proj = ctx.session.project
    if args.enabled:
        src = props.get("source_asset") or ctx.session.timeline.captions.source_asset
        sources = [src] if src else ctx.session.speech_sources()
        if not any(proj.transcript(s) is not None for s in sources):
            raise EditError(f"no transcript for {', '.join(sources) or 'any recording'}; captions need one")
    cap = ops.set_captions(ctx.session.timeline, **props)
    source = cap.source_asset or "every scene's speech"
    return f"Captions {'on' if cap.enabled else 'off'} (source {source}, {cap.max_words} words/line)."


CANVAS_PRESETS = {"landscape_1080p": (1920, 1080), "landscape_1440p": (2560, 1440), "landscape_720p": (1280, 720),
                  "landscape_4k": (3840, 2160),
                  "vertical_1080x1920": (1080, 1920), "square_1080": (1080, 1080)}


@tool("set_canvas", SetCanvasIn, mutates=True)
def set_canvas(ctx, args: SetCanvasIn) -> ToolOutput:
    """Change the output frame size / frame rate (e.g. 1440p/2K, 4K, or vertical 1080x1920 for Shorts/Reels/TikTok). Layout boxes
    are relative, so re-apply suitable layouts (e.g. 'stack' for vertical) afterwards and check with
    view_timeline."""
    if args.preset == "custom":
        if not (args.width and args.height):
            raise EditError("custom canvas needs width and height")
        w, h = args.width, args.height
    else:
        w, h = CANVAS_PRESETS[args.preset]
    w, h = w - w % 2, h - h % 2
    tl = ctx.session.timeline
    tl.canvas.width, tl.canvas.height = w, h
    if args.fps:
        tl.canvas.fps = args.fps
    return f"Canvas is now {w}x{h} @ {tl.canvas.fps:g} fps."


@tool("save_text", SaveTextIn)
def save_text(ctx, args: SaveTextIn) -> ToolOutput:
    """Save a text deliverable next to the renders: YouTube title/description, chapter list with timestamps of
    the final edit, social post, notes for the editor. Saved under the project's exports/ folder."""
    import re
    name = re.sub(r"[^\w.\-]+", "_", Path(args.filename).name).strip("._") or "notes.txt"
    if not name.lower().endswith((".txt", ".md", ".srt", ".vtt", ".json", ".csv")):
        name += ".txt"
    out = ctx.session.project.root / "exports" / name
    out.parent.mkdir(exist_ok=True)
    out.write_text(args.content, encoding="utf-8")
    return f"Saved {out}"


@tool("add_marker", AddMarkerIn, mutates=True)
def add_marker(ctx, args: AddMarkerIn) -> ToolOutput:
    """Leave a note for the human editor at a timeline time (something to review, a missing asset, etc.)."""
    m = ops.add_marker(ctx.session.timeline, args.time, args.note)
    return f"Marker {m.id} at {format_tc(m.time)}."


@tool("assemble_timeline", AssembleIn, mutates=True)
def assemble_timeline(ctx, args: AssembleIn) -> ToolOutput:
    """Rebuild the edit from scratch: recordings of the same moment stacked in sync, separate scenes one after
    another in recording order, each with a default layout. Discards the current edit (it can be undone)."""
    from ..timeline.assemble import assemble
    ctx.session.project.timeline = assemble(ctx.session.project, layout=args.layout)
    return "Timeline rebuilt from the synced recordings.\n" + describe_timeline(ctx.session.timeline)


@tool("undo", UndoIn, locks=True)
def undo(ctx, args: UndoIn) -> ToolOutput:
    """Undo the last edit(s)."""
    done = 0
    for _ in range(args.steps):
        if not ctx.session.undo():
            break
        done += 1
    return f"Undid {done} edit(s). " + _duration_line(ctx)


@tool("render_preview", RenderPreviewIn)
def render_preview(ctx, args: RenderPreviewIn) -> ToolOutput:
    """Render a low-resolution preview video of the whole edit for the human, and look at frames taken from
    the rendered file. Slower than view_timeline; use it once the edit is in good shape."""
    from ..analysis.frames import make_sheet, extract_frame
    path = ctx.session.render(preview=True)
    tl = ctx.session.timeline
    out: list[dict[str, Any]] = [{"type": "text", "text": f"Preview rendered: {path} ({format_tc(tl.duration)})."}]
    if args.frames:
        n = args.frames
        times = [tl.duration * (i + 0.5) / n for i in range(n)]
        d = ctx.session.project.cache_dir("preview_frames")
        frames = [extract_frame(path, t, d / f"{i:02d}.jpg", max_width=384) for i, t in enumerate(times)]
        sheet = make_sheet(frames, [format_tc(t) for t in times], d / "sheet.jpg", columns=4)
        out.append(image_block(sheet))
    ctx.last_render = path
    return out


# ----------------------------------------------------------------- dispatch --

def tool_definitions(*, eager: bool = True) -> list[dict[str, Any]]:
    return [spec.definition(eager=eager) for spec in REGISTRY.values()]


def run_tool(ctx, name: str, raw_input: Any) -> tuple[ToolOutput, bool]:
    """Execute a tool. Returns (content, is_error)."""
    spec = REGISTRY.get(name)
    if spec is None:
        return f"Unknown tool '{name}'.", True
    if not isinstance(raw_input, dict):
        return f"Invalid input for {name}: expected an object.", True
    try:
        args = spec.model.model_validate(raw_input)
    except ValidationError as exc:
        errs = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])
        return f"Invalid input for {name}: {errs}", True
    session = ctx.session
    lock = getattr(ctx, "lock", None) if spec.locks else None
    if lock is not None:
        lock.acquire()
    try:
        return _execute(ctx, spec, args, name)
    finally:
        if lock is not None:
            lock.release()


def _execute(ctx, spec: ToolSpec, args: BaseModel, name: str) -> tuple[ToolOutput, bool]:
    session = ctx.session
    if spec.mutates:
        session.checkpoint(name)
    try:
        result = spec.handler(ctx, args)
        if spec.mutates:
            session.commit()
    except (EditError, KeyError, ValueError, FileNotFoundError) as exc:
        if spec.mutates:
            session.rollback()
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return f"{name} failed: {msg}", True
    except Exception as exc:  # noqa: BLE001 - report any failure back to the model
        if spec.mutates:
            session.rollback()
        return f"{name} failed with {type(exc).__name__}: {exc}", True
    return result, False
