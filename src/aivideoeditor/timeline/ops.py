"""Edit operations on a :class:`Timeline`.

Every function mutates the timeline in place and raises :class:`EditError`
with an actionable message when the request does not make sense, so the
agent can read the error and correct itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .model import EPS, Captions, Clip, Marker, TextOverlay, Timeline, Track, Transform

MIN_CLIP = 1.0 / 120  # clips shorter than this are dropped after edits


class EditError(ValueError):
    pass


# ---------------------------------------------------------------- helpers --

def _check_range(a: float, b: float) -> None:
    if a < -EPS:
        raise EditError(f"range start {a:.3f} is negative")
    if b <= a + EPS:
        raise EditError(f"range end {b:.3f} must be after start {a:.3f}")


def _merge_ranges(ranges: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    rs = sorted((float(a), float(b)) for a, b in ranges)
    for a, b in rs:
        _check_range(a, b)
    merged: list[tuple[float, float]] = []
    for a, b in rs:
        if merged and a <= merged[-1][1] + EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _tracks(tl: Timeline, track_ids: Sequence[str] | None, *, include_locked: bool) -> list[Track]:
    if track_ids:
        return [tl.track(t) for t in track_ids]
    return [t for t in tl.tracks if include_locked or not t.locked]


def _trim_start(c: Clip, new_start: float) -> None:
    c.source_in = c.source_time(new_start)
    c.start = new_start
    c.fade_in = 0.0
    c.zoom_in = 0.0
    c.transition = "none"


def _trim_end(c: Clip, new_end: float) -> None:
    c.source_out = c.source_time(new_end)
    c.fade_out = 0.0
    c.zoom_out = 0.0


def _cleanup(track: Track) -> None:
    track.clips = [c for c in track.clips if c.duration >= MIN_CLIP]
    track.sort()


def validate(tl: Timeline, asset_durations: dict[str, float] | None = None) -> list[str]:
    """Return a list of problems (empty when the timeline is consistent)."""
    problems: list[str] = []
    for t in tl.tracks:
        t.sort()
        prev: Clip | None = None
        for c in t.clips:
            if c.source_out <= c.source_in:
                problems.append(f"{c.id}: source_out must be after source_in")
            if c.start < -EPS:
                problems.append(f"{c.id}: starts before 0")
            if not 0.05 <= c.speed <= 32:
                problems.append(f"{c.id}: speed {c.speed} out of range 0.05..32")
            if asset_durations is not None:
                dur = asset_durations.get(c.asset_id)
                if dur is None:
                    problems.append(f"{c.id}: unknown asset {c.asset_id}")
                elif c.source_in < -1e-3 or c.source_out > dur + 0.05:
                    problems.append(
                        f"{c.id}: source range {c.source_in:.3f}-{c.source_out:.3f} "
                        f"is outside asset {c.asset_id} (0-{dur:.3f})")
            if prev is not None and c.start < prev.end - 1e-4:
                problems.append(f"{t.id}: clips {prev.id} and {c.id} overlap")
            prev = c
    return problems


# ------------------------------------------------------------------ tracks --

def add_track(tl: Timeline, kind: str, name: str = "", role: str | None = None,
              *, position: int | None = None) -> Track:
    if kind not in ("video", "audio"):
        raise EditError("track kind must be 'video' or 'audio'")
    track = Track(id=tl.new_track_id(kind), kind=kind, name=name or role or kind, role=role)  # type: ignore[arg-type]
    if position is None:
        tl.tracks.append(track)
    else:
        tl.tracks.insert(position, track)
    return track


def set_track(tl: Timeline, track_id: str, **props) -> Track:
    t = tl.track(track_id)
    allowed = {"name", "role", "muted", "hidden", "locked", "duck", "volume", "denoise", "enhance"}
    for k, v in props.items():
        if v is None:
            continue
        if k not in allowed:
            raise EditError(f"unknown track property '{k}'")
        setattr(t, k, v)
    return t


def remove_track(tl: Timeline, track_id: str) -> None:
    t = tl.track(track_id)
    tl.tracks.remove(t)


# ------------------------------------------------------------------- clips --

def lift_track(tl: Timeline, track: Track, a: float, b: float) -> None:
    """Remove content in [a, b) from one track without closing the gap."""
    out: list[Clip] = []
    for c in track.clips:
        if c.end <= a + EPS or c.start >= b - EPS:
            out.append(c)
            continue
        if c.start < a - EPS and c.end > b + EPS:
            right = c.model_copy(deep=True)
            right.id = tl.new_id("c")
            _trim_end(c, a)
            _trim_start(right, b)
            out.extend([c, right])
        elif c.start < a - EPS:
            _trim_end(c, a)
            out.append(c)
        elif c.end > b + EPS:
            _trim_start(c, b)
            out.append(c)
        # else: fully inside -> dropped
    track.clips = out
    _cleanup(track)


def place_clip(
    tl: Timeline,
    track_id: str,
    asset_id: str,
    source_in: float,
    source_out: float,
    start: float,
    *,
    speed: float = 1.0,
    volume: float = 1.0,
    transform: Transform | None = None,
    mode: str = "overwrite",
    label: str = "",
) -> Clip:
    """Put a piece of an asset on a track.

    ``overwrite`` replaces whatever is under the new clip on that track;
    ``insert`` pushes everything at/after ``start`` on all unlocked tracks
    later by the clip's duration (a ripple insert).
    """
    if source_out <= source_in + EPS:
        raise EditError("source_out must be after source_in")
    if start < -EPS:
        raise EditError("start must be >= 0")
    track = tl.track(track_id)
    clip = Clip(id=tl.new_id("c"), asset_id=asset_id, start=max(start, 0.0), source_in=source_in,
                source_out=source_out, speed=speed, volume=volume,
                transform=transform or Transform(), label=label)
    if mode == "insert":
        split_at(tl, start)
        shift_after(tl, start, clip.duration)
    elif mode == "overwrite":
        lift_track(tl, track, clip.start, clip.end)
    else:
        raise EditError("mode must be 'overwrite' or 'insert'")
    track.clips.append(clip)
    track.sort()
    return clip


def split_at(tl: Timeline, t: float, track_ids: Sequence[str] | None = None) -> list[str]:
    """Split clips that span time ``t``. Returns ids of the new right-hand pieces."""
    new_ids: list[str] = []
    for track in _tracks(tl, track_ids, include_locked=False):
        for c in list(track.clips):
            if c.start + EPS < t < c.end - EPS:
                right = c.model_copy(deep=True)
                right.id = tl.new_id("c")
                _trim_end(c, t)
                _trim_start(right, t)
                track.clips.append(right)
                new_ids.append(right.id)
        track.sort()
    return new_ids


def shift_after(tl: Timeline, t: float, delta: float, track_ids: Sequence[str] | None = None) -> None:
    """Move every clip/text/marker starting at or after ``t`` by ``delta`` seconds."""
    for track in _tracks(tl, track_ids, include_locked=False):
        for c in track.clips:
            if c.start >= t - EPS:
                c.start = max(0.0, c.start + delta)
        track.sort()
    if track_ids is None:
        for x in tl.texts:
            if x.start >= t - EPS:
                x.start += delta
                x.end += delta
        for m in tl.markers:
            if m.time >= t - EPS:
                m.time = max(0.0, m.time + delta)


def ripple_delete(tl: Timeline, ranges: Iterable[Sequence[float]],
                  track_ids: Sequence[str] | None = None) -> float:
    """Remove time ranges and close the gaps. Returns seconds removed.

    Ranges are in *current* timeline time; they are merged and applied
    from the latest to the earliest so all of them refer to the same
    (pre-edit) timeline.
    """
    merged = _merge_ranges(ranges)
    removed = 0.0
    for a, b in reversed(merged):
        end = tl.duration
        if a >= end - EPS:
            continue
        for track in _tracks(tl, track_ids, include_locked=False):
            lift_track(tl, track, a, b)
            for c in track.clips:
                if c.start >= b - EPS:
                    c.start -= (b - a)
            track.sort()
        if track_ids is None:
            _ripple_overlays(tl, a, b)
        removed += min(b, end) - a
    fit_music(tl)
    return removed


def content_end(tl: Timeline) -> float:
    """End of the edit ignoring music beds (which are locked and do not follow cuts)."""
    ends = [t.end for t in tl.tracks if t.role != "music"] + [x.end for x in tl.texts]
    return max(ends, default=0.0)


def fit_music(tl: Timeline, *, fade: float = 2.0) -> None:
    """Trim music beds that run past the end of the content, keeping a fade-out."""
    end = content_end(tl)
    if end <= 0:
        return
    for t in tl.tracks:
        if t.role != "music":
            continue
        kept = []
        for c in t.clips:
            if c.start >= end - MIN_CLIP:
                continue
            if c.end > end + EPS:
                fo = max(c.fade_out, fade)
                _trim_end(c, end)
                c.fade_out = min(fo, c.duration / 2)
            kept.append(c)
        t.clips = kept


def _ripple_overlays(tl: Timeline, a: float, b: float) -> None:
    d = b - a
    texts: list[TextOverlay] = []
    for x in tl.texts:
        if x.end <= a + EPS:
            texts.append(x)
        elif x.start >= b - EPS:
            x.start -= d
            x.end -= d
            texts.append(x)
        else:
            # Overlaps the removed range: keep the parts outside it.
            x.start, x.end = min(x.start, a), (x.end - d if x.end > b else a)
            if x.end - x.start > 0.2:
                texts.append(x)
    tl.texts = texts
    for m in tl.markers:
        if m.time >= b:
            m.time -= d
        elif m.time > a:
            m.time = a


def lift(tl: Timeline, a: float, b: float, track_ids: Sequence[str] | None = None) -> None:
    _check_range(a, b)
    for track in _tracks(tl, track_ids, include_locked=False):
        lift_track(tl, track, a, b)


def delete_clips(tl: Timeline, clip_ids: Sequence[str], *, ripple: bool = False) -> None:
    found = [tl.find_clip(cid) for cid in dict.fromkeys(clip_ids)]   # validate all ids first
    ranges = [(clip.start, clip.end) for _, clip in found]
    for track, clip in found:
        track.clips.remove(clip)
    if ripple:
        ripple_delete(tl, ranges)


def trim_clip(tl: Timeline, clip_id: str, *, start: float | None = None, end: float | None = None) -> Clip:
    """Move a clip's edges (timeline time) without moving its content in time."""
    track, c = tl.find_clip(clip_id)
    new_start = c.start if start is None else start
    new_end = c.end if end is None else end
    if new_end <= new_start + EPS:
        raise EditError("trim would make the clip empty")
    # Extending the edges reveals more source material.
    src_in = c.source_time(new_start)
    src_out = c.source_time(new_end)
    if src_in < -1e-3:
        raise EditError(f"cannot extend {clip_id} before the start of its source")
    others = [o for o in track.clips if o is not c]
    for o in others:
        if o.start < new_end - 1e-4 and o.end > new_start + 1e-4:
            raise EditError(f"trim would overlap clip {o.id} on track {track.id}; lift it first")
    c.start, c.source_in, c.source_out = new_start, max(src_in, 0.0), src_out
    track.sort()
    return c


def move_clip(tl: Timeline, clip_id: str, start: float, track_id: str | None = None,
              *, overwrite: bool = False) -> Clip:
    src_track, c = tl.find_clip(clip_id)
    dst = tl.track(track_id) if track_id else src_track
    if dst.kind != src_track.kind:
        raise EditError("can only move a clip between tracks of the same kind")
    if start < -EPS:
        raise EditError("start must be >= 0")
    src_track.clips.remove(c)
    c.start = max(0.0, start)
    blocking = [o for o in dst.clips if o.start < c.end - 1e-4 and o.end > c.start + 1e-4]
    if blocking and not overwrite:
        src_track.clips.append(c)
        src_track.sort()
        ids = ", ".join(o.id for o in blocking)
        raise EditError(f"destination overlaps {ids}; pass overwrite=true to replace them")
    lift_track(tl, dst, c.start, c.end)
    dst.clips.append(c)
    dst.sort()
    return c


def set_clips(tl: Timeline, clip_ids: Sequence[str], **props) -> list[Clip]:
    allowed = {"volume", "fade_in", "fade_out", "visible", "transform", "label", "speed"}
    out = []
    for cid in clip_ids:
        track, c = tl.find_clip(cid)
        for k, v in props.items():
            if v is None:
                continue
            if k not in allowed:
                raise EditError(f"unknown clip property '{k}'")
            if k == "speed":
                old_end = c.end
                c.speed = float(v)
                nxt = next((o for o in track.clips if o.start >= old_end - EPS and o is not c), None)
                if nxt and c.end > nxt.start + 1e-4:
                    raise EditError(f"new speed makes {cid} overlap {nxt.id}; use speed_range instead")
            elif k == "transform":
                c.transform = v if isinstance(v, Transform) else Transform.model_validate(v)
            else:
                setattr(c, k, v)
        out.append(c)
    return out


def speed_range(tl: Timeline, a: float, b: float, factor: float, *,
                track_ids: Sequence[str] | None = None, mute: bool = True) -> float:
    """Play [a, b) ``factor`` times faster on all unlocked tracks and ripple.

    Returns the new duration of the range.
    """
    _check_range(a, b)
    if not 0.1 <= factor <= 16:
        raise EditError("speed factor must be within 0.1..16")
    tracks = _tracks(tl, track_ids, include_locked=False)
    ids = [t.id for t in tracks]
    split_at(tl, a, ids)
    split_at(tl, b, ids)
    old = b - a
    new = old / factor
    inside: list[Clip] = []
    for track in tracks:
        for c in track.clips:
            if c.start >= a - EPS and c.end <= b + EPS:
                inside.append(c)
    shift_after(tl, b, new - old, ids if track_ids else None)
    if track_ids is None:
        def remap(t: float) -> float:
            return a + (t - a) / factor if a < t < b else t
        for x in tl.texts:
            if x.start < b and x.end > a:
                x.start, x.end = remap(x.start), (remap(x.end) if x.end <= b else x.end + new - old)
        for m in tl.markers:
            m.time = remap(m.time)
    for c in inside:
        # Re-read the start: the clip was not shifted (it starts before b).
        c.start = a + (c.start - a) / factor
        c.speed *= factor
        if mute:
            c.volume = 0.0
    for track in tracks:
        track.sort()
    fit_music(tl)
    return new


# ---------------------------------------------------------------- overlays --

def add_text(tl: Timeline, start: float, end: float, text: str, **style) -> TextOverlay:
    _check_range(start, end)
    if not text.strip():
        raise EditError("text must not be empty")
    overlay = TextOverlay(id=tl.new_id("t"), start=start, end=end, text=text,
                          **{k: v for k, v in style.items() if v is not None})
    tl.texts.append(overlay)
    tl.texts.sort(key=lambda x: x.start)
    return overlay


def update_text(tl: Timeline, text_id: str, **props) -> TextOverlay:
    for x in tl.texts:
        if x.id == text_id:
            data = x.model_dump()
            data.update({k: v for k, v in props.items() if v is not None})
            new = TextOverlay.model_validate(data)
            if new.end <= new.start:
                raise EditError("text end must be after start")
            tl.texts[tl.texts.index(x)] = new
            return new
    raise EditError(f"unknown text overlay '{text_id}'")


def remove_text(tl: Timeline, text_id: str) -> None:
    before = len(tl.texts)
    tl.texts = [x for x in tl.texts if x.id != text_id]
    if len(tl.texts) == before:
        raise EditError(f"unknown text overlay '{text_id}'")


def set_captions(tl: Timeline, **props) -> Captions:
    data = tl.captions.model_dump()
    data.update({k: v for k, v in props.items() if v is not None})
    tl.captions = Captions.model_validate(data)
    return tl.captions


def add_marker(tl: Timeline, time: float, note: str) -> Marker:
    m = Marker(id=tl.new_id("m"), time=time, note=note)
    tl.markers.append(m)
    tl.markers.sort(key=lambda m: m.time)
    return m


def move_range(tl: Timeline, a: float, b: float, to: float) -> float:
    """Move [a, b) of the edit (all unlocked tracks, with its texts and markers) so it starts at ``to``.

    ``to`` is a time on the current timeline outside the range; the content is
    taken out (closing its gap) and inserted there (pushing later material on).
    Used to reorder scenes or sections. Returns the length moved.
    """
    _check_range(a, b)
    total = tl.duration
    if a >= total - EPS:
        raise EditError(f"range starts at {a:.2f}, after the end of the edit ({total:.2f})")
    b = min(b, total)
    to = min(max(to, 0.0), total)
    if a + EPS < to < b - EPS:
        raise EditError("the target time must be outside the range being moved")
    if abs(to - a) < EPS or abs(to - b) < EPS:
        return 0.0
    tracks = _tracks(tl, None, include_locked=False)
    ids = [t.id for t in tracks]
    split_at(tl, a, ids)
    split_at(tl, b, ids)
    moved: list[tuple[Track, Clip]] = []
    for t in tracks:
        for c in [c for c in t.clips if c.start >= a - EPS and c.end <= b + EPS]:
            t.clips.remove(c)
            moved.append((t, c))
    texts = [x for x in tl.texts if x.start >= a - EPS and x.end <= b + EPS]
    markers = [m for m in tl.markers if a - EPS <= m.time < b - EPS]
    tl.texts = [x for x in tl.texts if x not in texts]
    tl.markers = [m for m in tl.markers if m not in markers]
    length = b - a
    # Close the gap the range leaves behind.
    for t in tracks:
        for c in t.clips:
            if c.start >= b - EPS:
                c.start -= length
    _ripple_overlays(tl, a, b)
    # Open a gap at the destination (in the closed-up timeline) and put the content there.
    dest = to if to <= a else to - length
    split_at(tl, dest, ids)
    shift_after(tl, dest, length)
    for t, c in moved:
        c.start = dest + (c.start - a)
        t.clips.append(c)
    for x in texts:
        x.start, x.end = dest + (x.start - a), dest + (x.end - a)
        tl.texts.append(x)
    for m in markers:
        m.time = dest + (m.time - a)
        tl.markers.append(m)
    for t in tracks:
        t.sort()
    tl.texts.sort(key=lambda x: x.start)
    tl.markers.sort(key=lambda m: m.time)
    fit_music(tl)
    return length
