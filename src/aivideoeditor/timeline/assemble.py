"""Build the first version of the timeline from synced raw recordings.

Every recording is laid down once, at its position on the shared clock,
so the result is a "multicam" stack where all tracks are in sync. From
there the agent edits by cutting time ranges across all tracks (which
keeps them in sync) and by switching layouts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .layouts import apply_layout
from .model import Timeline
from .ops import add_track, place_clip

if TYPE_CHECKING:  # pragma: no cover
    from ..project import Asset, Project

VIDEO_ROLES = ("screen", "camera")  # bottom to top
AUDIO_ROLES = ("mic",)


def _placement(asset: "Asset") -> tuple[float, float]:
    """(start on the reference clock, speed) for an asset."""
    if asset.sync is None:
        return 0.0, 1.0
    s = asset.sync.drift_ppm * 1e-6
    speed = 1.0 - s
    return asset.sync.offset / speed, speed


def camera_aspect(project: "Project") -> float:
    for a in project.assets.values():
        if a.role == "camera" and a.info.get("width") and a.info.get("height"):
            return a.info["width"] / a.info["height"]
    return 16 / 9


def assemble(project: "Project", *, layout: str | None = None) -> Timeline:
    # The current timeline's canvas wins (it may have been changed, e.g. to vertical).
    tl = Timeline(canvas=project.timeline.canvas.model_copy())
    assets = [a for a in project.assets.values()
              if (a.role in VIDEO_ROLES and a.has_video) or (a.role in AUDIO_ROLES and a.has_audio)]
    if not assets:
        raise ValueError("add at least one screen, camera or mic recording before assembling")

    starts = {a.id: _placement(a) for a in assets}
    origin = min(st for st, _ in starts.values())

    has_mic = any(a.role == "mic" for a in assets)
    has_camera_audio = any(a.role == "camera" and a.has_audio for a in assets)

    def default_volume(a: "Asset") -> float:
        if a.role == "mic":
            return 1.0
        if has_mic:
            return 0.0
        if a.role == "camera":
            return 1.0
        return 0.0 if has_camera_audio else 1.0

    for role in VIDEO_ROLES:
        for a in [x for x in assets if x.role == role]:
            track = add_track(tl, "video", name=f"{role}: {a.id}", role=role)
            st, speed = starts[a.id]
            place_clip(tl, track.id, a.id, 0.0, a.duration, st - origin, speed=speed,
                       volume=default_volume(a), label=a.id)
    for a in [x for x in assets if x.role == "mic"]:
        track = add_track(tl, "audio", name=f"mic: {a.id}", role="mic")
        st, speed = starts[a.id]
        place_clip(tl, track.id, a.id, 0.0, a.duration, st - origin, speed=speed,
                   volume=1.0, label=a.id)

    roles = {t.role for t in tl.video_tracks}
    if layout is None:
        if {"screen", "camera"} <= roles:
            layout = "pip"
        elif "camera" in roles:
            layout = "camera_only"
        elif "screen" in roles:
            layout = "screen_only"
    if layout and tl.duration > 0:
        apply_layout(tl, 0.0, tl.duration, layout, camera_aspect=camera_aspect(project))
    return tl


def primary_audio_asset(project: "Project") -> str | None:
    """The asset whose transcript drives editing (mic > camera > screen)."""
    for role in ("mic", "camera", "screen"):
        for a in project.assets.values():
            if a.role == role and a.has_audio:
                return a.id
    return None


STILL_LENGTH = 5.0  # seconds an image is shown when dropped on the timeline


def add_asset_to_timeline(project: "Project", tl: Timeline, asset_id: str, start: float = 0.0,
                          track_id: str | None = None):
    """Put a whole recording (or a 5 s still image) on the timeline, like dragging it in an editor.

    Uses the given track when the kind matches, otherwise a free track with the
    asset's role, otherwise a new track. Sensible defaults: a camera dropped
    over a screen recording becomes picture-in-picture, video sound is muted
    when an external mic is on the timeline, music is a ducked bed.
    """
    from .layouts import layout_transforms
    from .model import Transform
    from .ops import add_track, place_clip

    a = project.asset(asset_id)
    start = max(0.0, float(start))
    if a.is_image:
        kind, role, length = "video", "overlay", STILL_LENGTH
    elif a.has_video and a.role not in ("mic", "music", "sfx"):
        kind, role, length = "video", a.role, a.duration
    else:
        kind, role, length = "audio", a.role, a.duration
    if length <= 0:
        raise ValueError(f"{asset_id} has no duration")
    end = start + length

    def free(t) -> bool:
        return not any(c.start < end - 1e-6 and c.end > start + 1e-6 for c in t.clips)

    track = None
    if track_id:
        cand = tl.track(track_id)
        if cand.kind == kind and free(cand):
            track = cand
    if track is None:
        track = next((t for t in tl.tracks if t.kind == kind and t.role == role and free(t)), None)
    if track is None:
        track = add_track(tl, kind, name=f"{role}: {a.id}", role=role)

    has_mic = any(t.role == "mic" and t.clips for t in tl.audio_tracks) and role != "mic"
    volume = 1.0
    if role == "music":
        volume = 0.15
        track.locked, track.duck = True, True
    elif kind == "video" and has_mic:
        volume = 0.0

    transform = Transform(fit="contain")
    if role == "camera":
        w, h = a.info.get("width") or 16, a.info.get("height") or 9
        over_screen = any(t.role == "screen" and t.clips for t in tl.video_tracks)
        transform = (layout_transforms("pip", tl.canvas, camera_aspect=w / h)["camera"] if over_screen
                     else Transform(fit="cover"))
    clip = place_clip(tl, track.id, a.id, 0.0, length, start, volume=volume, transform=transform, label=a.id)
    if role == "music":
        clip.fade_in, clip.fade_out = 1.0, 2.5
    elif a.is_image:
        clip.fade_in = clip.fade_out = 0.3
    return clip
