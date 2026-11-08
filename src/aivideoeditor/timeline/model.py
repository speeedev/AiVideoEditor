"""Timeline data model.

All times are in seconds. ``start``/``end`` are timeline positions,
``source_in``/``source_out`` are positions inside the asset file.
Spatial values (transform, crop, text position) are normalised to the
canvas (0..1), so layouts survive a change of output resolution.

Video tracks are stacked in list order: the first video track is the
bottom layer. A clip on a video track also contributes its asset's audio
(scaled by ``volume``); set ``volume`` to 0 when another recording (for
example an external microphone) carries the sound.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

EPS = 1e-6


class Canvas(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    background: str = "black"

    @property
    def aspect(self) -> float:
        return self.width / self.height


class Rect(BaseModel):
    """Normalised rectangle (0..1) relative to a frame."""

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

    @model_validator(mode="after")
    def _check(self) -> "Rect":
        if self.w <= 0 or self.h <= 0:
            raise ValueError("rect width/height must be positive")
        if self.x < -EPS or self.y < -EPS or self.x + self.w > 1 + 1e-4 or self.y + self.h > 1 + 1e-4:
            raise ValueError("rect must lie inside 0..1")
        return self


class Transform(BaseModel):
    """Placement of a video clip on the canvas.

    ``x, y, w, h`` is the box on the canvas the clip is drawn into.
    ``fit`` decides how the (optionally cropped) source fills the box.
    ``crop`` selects a region of the source frame: use it for punch-in
    zooms on screen recordings.
    """

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    fit: Literal["contain", "cover", "stretch"] = "contain"
    crop: Rect | None = None
    opacity: float = 1.0

    @model_validator(mode="after")
    def _check(self) -> "Transform":
        if self.w <= 0 or self.h <= 0:
            raise ValueError("transform width/height must be positive")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("opacity must be within 0..1")
        return self


class Clip(BaseModel):
    id: str
    asset_id: str
    start: float
    source_in: float
    source_out: float
    speed: float = 1.0
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    visible: bool = True
    transform: Transform = Field(default_factory=Transform)
    label: str = ""

    @property
    def duration(self) -> float:
        return (self.source_out - self.source_in) / self.speed

    @property
    def end(self) -> float:
        return self.start + self.duration

    def source_time(self, t: float) -> float:
        """Source position shown at timeline time ``t``."""
        return self.source_in + (t - self.start) * self.speed

    def timeline_time(self, s: float) -> float:
        """Timeline position where source time ``s`` plays."""
        return self.start + (s - self.source_in) / self.speed

    def contains(self, t: float) -> bool:
        return self.start - EPS <= t < self.end - EPS


class Track(BaseModel):
    id: str
    kind: Literal["video", "audio"]
    name: str = ""
    role: str | None = None   # asset role this track is for (screen, camera, mic, music...)
    clips: list[Clip] = Field(default_factory=list)
    muted: bool = False
    hidden: bool = False
    locked: bool = False      # locked tracks are not touched by ripple edits
    duck: bool = False        # audio: lower this track while someone speaks
    volume: float = 1.0

    @property
    def end(self) -> float:
        return max((c.end for c in self.clips), default=0.0)

    def sort(self) -> None:
        self.clips.sort(key=lambda c: c.start)

    def clip_at(self, t: float) -> Clip | None:
        for c in self.clips:
            if c.contains(t):
                return c
        return None


class TextOverlay(BaseModel):
    id: str
    start: float
    end: float
    text: str
    x: float = 0.5           # anchor (centre) position, normalised
    y: float = 0.1
    size: float = 0.06       # font size as a fraction of canvas height
    color: str = "white"
    box: bool = True
    box_color: str = "black@0.55"
    font: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


class Captions(BaseModel):
    """Burned-in subtitles generated from a transcript mapped onto the timeline."""

    enabled: bool = False
    source_asset: str | None = None
    max_words: int = 6
    max_duration: float = 3.0
    size: float = 0.055
    y: float = 0.9           # vertical position of the caption baseline (0 top .. 1 bottom)
    uppercase: bool = False
    highlight_words: bool = False
    color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    outline_color: str = "#000000"
    font: str = "DejaVu Sans"


class Marker(BaseModel):
    id: str
    time: float
    note: str


class Timeline(BaseModel):
    canvas: Canvas = Field(default_factory=Canvas)
    tracks: list[Track] = Field(default_factory=list)
    texts: list[TextOverlay] = Field(default_factory=list)
    captions: Captions = Field(default_factory=Captions)
    markers: list[Marker] = Field(default_factory=list)
    next_id: int = 1

    # -- ids --------------------------------------------------------------
    def new_id(self, prefix: str) -> str:
        i = self.next_id
        self.next_id += 1
        return f"{prefix}{i}"

    def new_track_id(self, kind: str) -> str:
        prefix = "V" if kind == "video" else "A"
        used = {t.id for t in self.tracks}
        n = 1
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"

    # -- lookup -----------------------------------------------------------
    @property
    def duration(self) -> float:
        ends = [t.end for t in self.tracks] + [x.end for x in self.texts]
        return max(ends, default=0.0)

    @property
    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    @property
    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    def track(self, track_id: str) -> Track:
        for t in self.tracks:
            if t.id == track_id:
                return t
        known = ", ".join(t.id for t in self.tracks) or "(none)"
        raise KeyError(f"unknown track '{track_id}'. Tracks: {known}")

    def find_clip(self, clip_id: str) -> tuple[Track, Clip]:
        for t in self.tracks:
            for c in t.clips:
                if c.id == clip_id:
                    return t, c
        raise KeyError(f"unknown clip '{clip_id}'")

    def clips_of_asset(self, asset_id: str) -> list[tuple[Track, Clip]]:
        return [(t, c) for t in self.tracks for c in t.clips if c.asset_id == asset_id]

    def all_clips(self) -> list[tuple[Track, Clip]]:
        return [(t, c) for t in self.tracks for c in t.clips]

    def cut_points(self) -> list[float]:
        pts = {0.0}
        for _, c in self.all_clips():
            pts.add(round(c.start, 6))
            pts.add(round(c.end, 6))
        return sorted(pts)
