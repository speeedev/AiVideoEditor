"""Scenes: groups of recordings that capture the same moment.

Sync puts recordings that share audio (a camera and the mic next to it) into
one group. Recordings that do not overlap in time — separate scenes, or
several takes of the same scene — each form their own group. Groups are laid
out one after another in recording order; the editor (Claude) then decides
the final order and which takes to keep.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .project import Asset, Project

SYNC_ROLES = ("screen", "camera", "mic")
SPEECH_RANK = {"mic": 0, "camera": 1, "screen": 2}


def parse_time(value: str | None) -> float | None:
    """ISO-8601 recording time from media tags -> epoch seconds (None for missing/bogus values)."""
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)            # +0300 -> +03:00 (Apple tags)
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)                       # trim sub-microsecond digits
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.year < 2000:                                           # 1904/1970 placeholders
        return None
    return dt.timestamp()


def recorded_window(asset: "Asset") -> tuple[float, float, str]:
    """(start, end, source) of when a recording was made, in epoch seconds.

    Uses the creation time stored in the file when present (recording start);
    otherwise the file's modification time, which is usually the end of the
    recording.
    """
    dur = max(asset.duration, 0.0) if not asset.is_image else 0.0
    created = parse_time(asset.info.get("created"))
    if created is not None:
        return created, created + dur, "tag"
    try:
        mtime = os.path.getmtime(asset.path)
    except OSError:
        mtime = 0.0
    return mtime - dur, mtime, "file"


def recorded_at(asset: "Asset") -> float:
    return recorded_window(asset)[0]


def natural_key(text: str) -> list:
    """'IMG_2.mov' sorts before 'IMG_10.mov'."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", os.path.basename(text))]


def group_of(asset: "Asset") -> int:
    return asset.sync.group if asset.sync is not None else 0


def scene_groups(project: "Project") -> list[list["Asset"]]:
    """Recordings grouped by moment, in recording order (members sorted by role)."""
    groups: dict[int, list["Asset"]] = {}
    for a in project.assets.values():
        if a.role in SYNC_ROLES and (a.has_video or a.has_audio):
            groups.setdefault(group_of(a), []).append(a)
    ordered = sorted(groups.values(),
                     key=lambda g: (min(recorded_at(a) for a in g), min(natural_key(a.path) for a in g)))
    for g in ordered:
        g.sort(key=lambda a: (SPEECH_RANK.get(a.role, 9), natural_key(a.path)))
    return ordered


def speech_source(group: list["Asset"]) -> str | None:
    """The recording whose audio carries the speech of a scene (mic > camera > screen)."""
    with_audio = [a for a in group if a.has_audio]
    if not with_audio:
        return None
    return min(with_audio, key=lambda a: (SPEECH_RANK.get(a.role, 9), natural_key(a.path))).id


def speech_sources(project: "Project") -> list[str]:
    """One speech recording per scene, in recording order."""
    out = []
    for g in scene_groups(project):
        sid = speech_source(g)
        if sid:
            out.append(sid)
    return out
