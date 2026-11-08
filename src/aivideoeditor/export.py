"""Export the timeline to other editors.

* ``xml``  - Final Cut Pro 7 XML (xmeml), imported by Premiere Pro, DaVinci Resolve and Final Cut.
* ``otio`` - OpenTimelineIO JSON (DaVinci Resolve 18+, many pipelines).
* ``edl``  - CMX3600 EDL of the bottom video track and the main audio track.
* ``json`` - this tool's own timeline JSON.
* ``srt``  - subtitles of the edited video (from the transcript), for platforms that take caption files.

Cuts, track placement, speed and volume are exported. Layout boxes, zooms,
text overlays and captions are specific to this tool's renderer and are
not translated (render the video for those, or recreate them in the NLE).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from .timeline.model import Clip, Timeline, Track

if TYPE_CHECKING:  # pragma: no cover
    from .project import Project


def _frames(t: float, fps: float) -> int:
    return int(round(t * fps))


def _timebase(fps: float) -> tuple[int, bool]:
    ntsc = abs(fps - round(fps)) > 0.001
    return int(math.ceil(fps)) if ntsc else int(round(fps)), ntsc


def _file_url(path: str) -> str:
    p = Path(path).resolve().as_posix()
    if not p.startswith("/"):
        p = "/" + p  # Windows drive letters
    from urllib.parse import quote
    return "file://localhost" + quote(p)


def _audible_video_clips(tl: Timeline, assets) -> list[tuple[Track, Clip]]:
    out = []
    for t in tl.video_tracks:
        for c in t.clips:
            if c.volume > 0 and not t.muted and assets[c.asset_id].has_audio:
                out.append((t, c))
    return out


# --------------------------------------------------------------------- xmeml --

def to_xmeml(tl: Timeline, project: "Project") -> str:
    fps = tl.canvas.fps
    tb, ntsc = _timebase(fps)
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    assets = project.assets
    total = _frames(tl.duration, fps)
    file_ids: dict[str, str] = {}
    counter = [0]

    def file_elem(asset_id: str) -> str:
        a = assets[asset_id]
        if asset_id in file_ids:
            return f'<file id="{file_ids[asset_id]}"/>'
        fid = f"file-{len(file_ids) + 1}"
        file_ids[asset_id] = fid
        media = []
        if a.has_video:
            media.append(f"<video><samplecharacteristics><width>{a.info.get('width')}</width>"
                         f"<height>{a.info.get('height')}</height></samplecharacteristics></video>")
        if a.has_audio:
            media.append(f"<audio><samplecharacteristics><samplerate>{a.info.get('sample_rate') or 48000}"
                         f"</samplerate><depth>16</depth></samplecharacteristics>"
                         f"<channelcount>{a.info.get('channels') or 2}</channelcount></audio>")
        return (f'<file id="{fid}"><name>{escape(Path(a.path).name)}</name>'
                f"<pathurl>{escape(_file_url(a.path))}</pathurl>{rate}"
                f"<duration>{_frames(a.duration, fps)}</duration><media>{''.join(media)}</media></file>")

    def clipitem(c: Clip, kind: str, track_index: int) -> str:
        counter[0] += 1
        a = assets[c.asset_id]
        start, end = _frames(c.start, fps), _frames(c.end, fps)
        src_in = _frames(c.source_in, fps)
        src_out = src_in + (end - start)
        filters = []
        if abs(c.speed - 1) > 1e-6:
            src_out = _frames(c.source_out, fps)
            filters.append(
                "<filter><effect><name>Time Remap</name><effectid>timeremap</effectid>"
                "<effectcategory>motion</effectcategory><effecttype>motion</effecttype>"
                f"<mediatype>{kind}</mediatype><parameter><parameterid>variablespeed</parameterid>"
                "<name>variablespeed</name><value>0</value></parameter>"
                f"<parameter><parameterid>speed</parameterid><name>speed</name><value>{c.speed * 100:.2f}</value>"
                "</parameter></effect></filter>")
        if kind == "audio" and abs(c.volume - 1) > 1e-6:
            filters.append(
                "<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid>"
                "<effectcategory>audiolevels</effectcategory><effecttype>audiolevels</effecttype>"
                "<mediatype>audio</mediatype><parameter><parameterid>level</parameterid><name>Level</name>"
                f"<valuemin>0</valuemin><valuemax>3.98109</valuemax><value>{c.volume:.4f}</value>"
                "</parameter></effect></filter>")
        source_track = (f"<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>"
                        if kind == "audio" else "")
        enabled = "TRUE" if (kind == "audio" or c.visible) else "FALSE"
        return (f'<clipitem id="clipitem-{counter[0]}"><name>{escape(c.label or c.asset_id)}</name>'
                f"<enabled>{enabled}</enabled><duration>{_frames(a.duration, fps)}</duration>{rate}"
                f"<start>{start}</start><end>{end}</end><in>{src_in}</in><out>{src_out}</out>"
                f"{file_elem(c.asset_id)}{source_track}{''.join(filters)}</clipitem>")

    video_tracks = []
    for i, t in enumerate(tl.video_tracks, start=1):
        items = "".join(clipitem(c, "video", i) for c in t.clips)
        video_tracks.append(f"<track><enabled>{'FALSE' if t.hidden else 'TRUE'}</enabled>{items}</track>")
    audio_tracks = []
    for i, t in enumerate(tl.audio_tracks, start=1):
        items = "".join(clipitem(c, "audio", i) for c in t.clips)
        audio_tracks.append(f"<track><enabled>{'FALSE' if t.muted else 'TRUE'}</enabled>{items}</track>")
    linked = _audible_video_clips(tl, assets)
    if linked:
        items = "".join(clipitem(c, "audio", 1) for _, c in linked)
        audio_tracks.append(f"<track><enabled>TRUE</enabled>{items}</track>")
    cv = tl.canvas
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
        f'<sequence id="sequence-1"><name>{escape(project.data.name)}</name><duration>{total}</duration>{rate}'
        "<media><video><format><samplecharacteristics>"
        f"{rate}<width>{cv.width}</width><height>{cv.height}</height>"
        "<pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>"
        f"{''.join(video_tracks)}</video>"
        f"<audio><numOutputChannels>2</numOutputChannels>{''.join(audio_tracks)}</audio></media>"
        "</sequence></xmeml>\n"
    )


# ---------------------------------------------------------------------- otio --

def _rt(value: float, rate: float) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": float(value)}


def _range(start_frames: float, dur_frames: float, rate: float) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start_frames, rate), "duration": _rt(dur_frames, rate)}


def to_otio(tl: Timeline, project: "Project") -> dict:
    fps = tl.canvas.fps
    assets = project.assets

    def clip_obj(c: Clip) -> dict:
        a = assets[c.asset_id]
        dur = _frames(c.end, fps) - _frames(c.start, fps)
        effects = []
        if abs(c.speed - 1) > 1e-6:
            effects.append({"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "speed", "effect_name": "LinearTimeWarp",
                            "time_scalar": c.speed, "metadata": {}})
        return {
            "OTIO_SCHEMA": "Clip.2", "name": c.label or c.asset_id, "enabled": True,
            "source_range": _range(_frames(c.source_in, fps), dur, fps),
            "media_references": {"DEFAULT_MEDIA": {
                "OTIO_SCHEMA": "ExternalReference.1", "name": Path(a.path).name,
                "target_url": Path(a.path).resolve().as_uri(),
                "available_range": _range(0, _frames(a.duration, fps), fps), "metadata": {}}},
            "active_media_reference_key": "DEFAULT_MEDIA",
            "effects": effects, "markers": [],
            "metadata": {"aivideoeditor": {"clip_id": c.id, "volume": c.volume, "visible": c.visible,
                                           "transform": c.transform.model_dump()}},
        }

    def track_obj(name: str, kind: str, clips: list[Clip]) -> dict:
        children = []
        cursor = 0
        for c in sorted(clips, key=lambda c: c.start):
            s = _frames(c.start, fps)
            if s > cursor:
                children.append({"OTIO_SCHEMA": "Gap.1", "name": "", "source_range": _range(0, s - cursor, fps),
                                 "effects": [], "markers": [], "metadata": {}, "enabled": True})
            children.append(clip_obj(c))
            cursor = _frames(c.end, fps)
        return {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "children": children,
                "source_range": None, "effects": [], "markers": [], "metadata": {}, "enabled": True}

    tracks = [track_obj(t.name or t.id, "Video", t.clips) for t in tl.video_tracks]
    tracks += [track_obj(t.name or t.id, "Audio", t.clips) for t in tl.audio_tracks]
    linked = _audible_video_clips(tl, assets)
    if linked:
        tracks.append(track_obj("video audio", "Audio", [c for _, c in linked]))
    markers = [{"OTIO_SCHEMA": "Marker.2", "name": m.note, "color": "RED", "comment": m.note,
                "marked_range": _range(_frames(m.time, fps), 0, fps), "metadata": {}} for m in tl.markers]
    return {
        "OTIO_SCHEMA": "Timeline.1", "name": project.data.name, "global_start_time": None,
        "metadata": {"aivideoeditor": {"canvas": tl.canvas.model_dump()}},
        "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": tracks, "source_range": None,
                   "effects": [], "markers": markers, "metadata": {}, "enabled": True},
    }


# ----------------------------------------------------------------------- edl --

def _tc(frames: int, fps: float) -> str:
    tb, _ = _timebase(fps)
    f = frames % tb
    s = frames // tb
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}:{f:02d}"


def to_edl(tl: Timeline, project: "Project") -> str:
    fps = tl.canvas.fps
    lines = [f"TITLE: {project.data.name[:60]}", "FCM: NON-DROP FRAME", ""]
    n = 0
    tracks: list[tuple[str, Track]] = []
    if tl.video_tracks:
        tracks.append(("V", tl.video_tracks[0]))
    if tl.audio_tracks:
        tracks.append(("A", tl.audio_tracks[0]))
    for kind, t in tracks:
        for c in t.clips:
            n += 1
            rs, re_ = _frames(c.start, fps), _frames(c.end, fps)
            si = _frames(c.source_in, fps)
            so = si + int(round((re_ - rs) * c.speed))
            lines.append(f"{n:03d}  AX       {kind:<5} C        {_tc(si, fps)} {_tc(so, fps)} "
                         f"{_tc(rs, fps)} {_tc(re_, fps)}")
            if abs(c.speed - 1) > 1e-6:
                lines.append(f"M2   AX       {c.speed * fps:05.1f}    {_tc(si, fps)}")
            lines.append(f"* FROM CLIP NAME: {Path(project.assets[c.asset_id].path).name}")
            lines.append("")
    return "\n".join(lines)


def _srt_ts(t: float) -> str:
    ms = int(round(max(t, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(project: "Project") -> str:
    """Subtitles of the edited timeline (for uploading as a separate caption track)."""
    from .render.captions import group_words
    from .session import EditorSession
    from .timeline.model import Captions
    session = EditorSession(project)
    cfg = project.timeline.captions
    words = session.caption_words() if cfg.enabled else None
    if words is None:
        cfg = Captions(enabled=True, source_asset=cfg.source_asset)
        project.timeline.captions, original = cfg, project.timeline.captions
        try:
            words = session.caption_words() or []
        finally:
            project.timeline.captions = original
    lines = group_words(words, cfg.model_copy(update={"max_words": max(cfg.max_words, 8)}))
    out = []
    for i, ln in enumerate(lines, start=1):
        text = " ".join(w.text for w in ln.words)
        out.append(f"{i}\n{_srt_ts(ln.start)} --> {_srt_ts(ln.end)}\n{text}\n")
    return "\n".join(out)


def export_timeline(project: "Project", fmt: str, output: str | None = None) -> Path:
    tl = project.timeline
    ext = {"xml": ".xml", "otio": ".otio", "edl": ".edl", "json": ".json", "srt": ".srt"}[fmt]
    out = Path(output) if output else project.root / "exports" / f"{project.data.name}{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "xml":
        out.write_text(to_xmeml(tl, project), encoding="utf-8")
    elif fmt == "otio":
        out.write_text(json.dumps(to_otio(tl, project), indent=2), encoding="utf-8")
    elif fmt == "edl":
        out.write_text(to_edl(tl, project), encoding="utf-8")
    elif fmt == "srt":
        out.write_text(to_srt(project), encoding="utf-8")
    else:
        out.write_text(tl.model_dump_json(indent=2), encoding="utf-8")
    return out.resolve()
