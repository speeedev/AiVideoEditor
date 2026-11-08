"""Layout presets for screen-recording + webcam videos."""

from __future__ import annotations

from collections.abc import Sequence

from .model import EPS, Canvas, Rect, Timeline, Transform
from .ops import EditError, split_at

LAYOUTS = ("screen_only", "camera_only", "pip", "side_by_side", "stack")
CORNERS = ("bottom_right", "bottom_left", "top_right", "top_left")


def layout_transforms(
    layout: str,
    canvas: Canvas,
    *,
    camera_aspect: float = 16 / 9,
    pip_corner: str = "bottom_right",
    pip_size: float = 0.26,
    margin: float = 0.035,
    split: float | None = None,
) -> dict[str, Transform | None]:
    """Transforms for the ``screen`` and ``camera`` roles (None = hidden)."""
    if layout not in LAYOUTS:
        raise EditError(f"unknown layout '{layout}'. Use one of: {', '.join(LAYOUTS)}")
    full = Transform(fit="contain")
    if layout == "screen_only":
        return {"screen": full, "camera": None}
    if layout == "camera_only":
        return {"screen": None, "camera": Transform(fit="cover")}
    if layout == "pip":
        if pip_corner not in CORNERS:
            raise EditError(f"pip_corner must be one of: {', '.join(CORNERS)}")
        if not 0.05 <= pip_size <= 0.6:
            raise EditError("pip_size must be within 0.05..0.6 (fraction of canvas width)")
        w = pip_size
        h = min(0.9, w * canvas.aspect / camera_aspect)
        mx, my = margin / canvas.aspect, margin
        x = 1 - w - mx if pip_corner.endswith("right") else mx
        y = 1 - h - my if pip_corner.startswith("bottom") else my
        return {"screen": full, "camera": Transform(x=x, y=y, w=w, h=h, fit="cover")}
    if layout == "side_by_side":
        s = min(max(split if split is not None else 0.68, 0.3), 0.85)
        return {
            "screen": Transform(x=0, y=0, w=s, h=1, fit="contain"),
            "camera": Transform(x=s, y=0, w=1 - s, h=1, fit="cover"),
        }
    # stack: screen on top, camera below (best on vertical 9:16 canvases)
    s = min(max(split if split is not None else 0.5, 0.25), 0.75)
    return {
        "screen": Transform(x=0, y=0, w=1, h=s, fit="contain"),
        "camera": Transform(x=0, y=s, w=1, h=1 - s, fit="cover"),
    }


def apply_layout(
    tl: Timeline,
    start: float,
    end: float,
    layout: str,
    *,
    camera_aspect: float = 16 / 9,
    **opts,
) -> list[str]:
    """Apply a layout preset to all screen/camera video tracks in [start, end)."""
    if end <= start + EPS:
        raise EditError("layout end must be after start")
    transforms = layout_transforms(layout, tl.canvas, camera_aspect=camera_aspect, **opts)
    tracks = [t for t in tl.video_tracks if t.role in transforms]
    if not tracks:
        raise EditError("no video tracks with role 'screen' or 'camera' to lay out")
    ids = [t.id for t in tracks]
    split_at(tl, start, ids)
    split_at(tl, end, ids)
    touched: list[str] = []
    for t in tracks:
        tr = transforms[t.role or ""]
        for c in t.clips:
            if c.start >= start - EPS and c.end <= end + EPS:
                if tr is None:
                    c.visible = False
                else:
                    keep_crop = c.transform.crop
                    c.transform = tr.model_copy(deep=True)
                    c.transform.crop = keep_crop
                    c.visible = True
                touched.append(c.id)
    return touched


def fit_rect_to_aspect(rect: Rect, aspect: float, source_aspect: float) -> Rect:
    """Grow ``rect`` (normalised to the source) so it has the given pixel aspect.

    Keeps the centre where possible and stays inside the frame, so a rough
    region from the agent becomes a distortion-free zoom window.
    """
    # Work in pixel-proportional units: x scaled by source_aspect.
    w_px, h_px = rect.w * source_aspect, rect.h
    if w_px / h_px < aspect:
        w_px = h_px * aspect
    else:
        h_px = w_px / aspect
    w, h = w_px / source_aspect, h_px
    if w > 1 or h > 1:
        scale = max(w, h)
        w, h = w / scale, h / scale
    cx, cy = rect.x + rect.w / 2, rect.y + rect.h / 2
    x = min(max(cx - w / 2, 0.0), 1 - w)
    y = min(max(cy - h / 2, 0.0), 1 - h)
    return Rect(x=x, y=y, w=w, h=h)


def zoom_range(
    tl: Timeline,
    track_id: str,
    start: float,
    end: float,
    region: Rect | None,
    *,
    source_aspect: float,
) -> list[str]:
    """Punch in on ``region`` of the source in [start, end) (None resets the zoom)."""
    if end <= start + EPS:
        raise EditError("zoom end must be after start")
    track = tl.track(track_id)
    if track.kind != "video":
        raise EditError("zoom works on video tracks only")
    split_at(tl, start, [track_id])
    split_at(tl, end, [track_id])
    touched = []
    for c in track.clips:
        if c.start >= start - EPS and c.end <= end + EPS:
            if region is None:
                c.transform.crop = None
            else:
                box_aspect = (c.transform.w * tl.canvas.width) / (c.transform.h * tl.canvas.height)
                c.transform.crop = fit_rect_to_aspect(region, box_aspect, source_aspect)
            touched.append(c.id)
    if not touched:
        raise EditError(f"no clips on {track_id} between {start:.2f} and {end:.2f}")
    return touched


def roles_present(tl: Timeline) -> Sequence[str]:
    return sorted({t.role for t in tl.video_tracks if t.role})
