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
    tl = Timeline(canvas=project.data.canvas.model_copy())
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
