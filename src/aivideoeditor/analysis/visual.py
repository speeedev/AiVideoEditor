"""Visual analysis: hard cuts, on-screen activity and *where* it happens.

Frames are decoded at a low rate as small grayscale images and compared
with their predecessor. Two measures are kept per sample:

* the mean difference, which spikes at hard cuts / full-screen changes;
* the number of pixels that clearly changed, which catches small events on
  screen recordings (typing, a cursor, a menu opening) that a mean would miss.

For every stretch of activity the bounding box of the changes is stored, so
the editor knows not only *when* something happens on screen but *where*
(useful for zooms, and essential for videos without narration).
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..media.ffmpeg import FFmpegError, ffmpeg_bin

THUMB_W, THUMB_H = 256, 144
PIXEL_THRESHOLD = 0.06       # a pixel "changed" when it moved by more than this (0..1)
MIN_CHANGED = 3              # pixels needed for a sample to count as active
CUT_THRESHOLD = 0.12         # mean difference that marks a hard cut


@dataclass
class VisualAnalysis:
    sample_fps: float
    scene_cuts: list[float] = field(default_factory=list)
    active: list[tuple[float, float]] = field(default_factory=list)
    activity: list[float] = field(default_factory=list)   # fraction of changed pixels per sample
    regions: list[list[float] | None] = field(default_factory=list)  # [x, y, w, h] per active span

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scene_cuts"] = [round(t, 3) for t in self.scene_cuts]
        d["active"] = [[round(a, 3), round(b, 3)] for a, b in self.active]
        d["activity"] = [round(v, 5) for v in self.activity]
        d["regions"] = [[round(v, 3) for v in r] if r else None for r in self.regions]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "VisualAnalysis":
        active = [(float(a), float(b)) for a, b in d.get("active", [])]
        regions = list(d.get("regions") or [])
        regions += [None] * (len(active) - len(regions))
        return cls(sample_fps=d["sample_fps"], scene_cuts=list(d.get("scene_cuts", [])), active=active,
                   activity=list(d.get("activity", [])), regions=regions[: len(active)])

    def activity_between(self, start: float, end: float) -> float:
        if not self.activity:
            return 0.0
        i0 = max(0, int(start * self.sample_fps))
        i1 = max(i0 + 1, int(np.ceil(end * self.sample_fps)))
        window = self.activity[i0:i1]
        return float(np.mean(window)) if window else 0.0


@dataclass
class _Sample:
    mean_diff: float
    changed: int
    bbox: tuple[float, float, float, float] | None


def _compare(prev: np.ndarray | None, cur: np.ndarray) -> _Sample:
    if prev is None:
        return _Sample(0.0, 0, None)
    d = np.abs(cur.astype(np.int16) - prev.astype(np.int16))
    mask = d > int(PIXEL_THRESHOLD * 255)
    n = int(mask.sum())
    bbox = None
    if n >= MIN_CHANGED:
        rows = np.flatnonzero(mask.any(axis=1))
        cols = np.flatnonzero(mask.any(axis=0))
        h, w = cur.shape
        bbox = (cols[0] / w, rows[0] / h, (cols[-1] + 1 - cols[0]) / w, (rows[-1] + 1 - rows[0]) / h)
    return _Sample(float(d.mean()) / 255.0, n, bbox)


def _ffmpeg_thumbs_cmd(path: str | Path, sample_fps: float, hwaccel: bool) -> list[str]:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if hwaccel:
        cmd += ["-hwaccel", "auto"]
    return cmd + ["-i", str(path), "-map", "0:v:0", "-an",
                  "-vf", f"fps={sample_fps},scale={THUMB_W}:{THUMB_H}:flags=area,format=gray",
                  "-f", "rawvideo", "pipe:1"]


def _stream_samples(path: str | Path, sample_fps: float, hwaccel: bool) -> list[_Sample]:
    """Decode and compare frame by frame (constant memory, even for hour-long recordings)."""
    cmd = _ffmpeg_thumbs_cmd(path, sample_fps, hwaccel)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size = THUMB_W * THUMB_H
    samples: list[_Sample] = []
    prev = None
    assert proc.stdout is not None and proc.stderr is not None
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        cur = np.frombuffer(buf, dtype=np.uint8).reshape(THUMB_H, THUMB_W)
        samples.append(_compare(prev, cur))
        prev = cur
    stderr = proc.stderr.read().decode("utf-8", "replace")
    proc.wait()
    if proc.returncode != 0 and not samples:
        raise FFmpegError(cmd, proc.returncode, stderr)
    return samples


def decode_thumbs(path: str | Path, sample_fps: float) -> np.ndarray:
    """All sampled frames as an array in 0..1 (small files / tests); analysis streams instead."""
    cmd = _ffmpeg_thumbs_cmd(path, sample_fps, hwaccel=False)
    out = subprocess.run(cmd, capture_output=True)
    if out.returncode != 0:
        raise FFmpegError(cmd, out.returncode, out.stderr.decode("utf-8", "replace"))
    data = np.frombuffer(out.stdout, dtype=np.uint8)
    n = data.size // (THUMB_W * THUMB_H)
    return data[: n * THUMB_W * THUMB_H].reshape(n, THUMB_H, THUMB_W).astype(np.float32) / 255.0


def analyze_visual(path: str | Path, *, sample_fps: float = 2.0, min_gap: float = 1.5) -> VisualAnalysis:
    try:
        # Hardware decoding makes long 4K/60 fps recordings much faster where available.
        samples = _stream_samples(path, sample_fps, hwaccel=True)
    except FFmpegError:
        samples = _stream_samples(path, sample_fps, hwaccel=False)
    return _summarize(samples, sample_fps, min_gap=min_gap, total=THUMB_W * THUMB_H)


def analyze_frames(frames: np.ndarray, sample_fps: float, *, min_gap: float = 1.5) -> VisualAnalysis:
    """Same analysis on an array of grayscale frames in 0..1 (used by tests)."""
    samples: list[_Sample] = []
    prev = None
    for f in frames:
        cur = np.clip(np.asarray(f) * 255.0, 0, 255).astype(np.uint8)
        samples.append(_compare(prev, cur))
        prev = cur
    total = int(frames[0].size) if len(frames) else THUMB_W * THUMB_H
    return _summarize(samples, sample_fps, min_gap=min_gap, total=total)


def _summarize(samples: list[_Sample], fps: float, *, min_gap: float, total: int) -> VisualAnalysis:
    if not samples:
        return VisualAnalysis(fps)
    cuts = [i / fps for i, s in enumerate(samples) if s.mean_diff > CUT_THRESHOLD]
    active_idx = [i for i, s in enumerate(samples) if s.changed >= MIN_CHANGED]
    gap = max(1, int(round(min_gap * fps)))
    spans: list[tuple[int, int]] = []
    for i in active_idx:
        if spans and i - spans[-1][1] <= gap:
            spans[-1] = (spans[-1][0], i)
        else:
            spans.append((i, i))
    active: list[tuple[float, float]] = []
    regions: list[list[float] | None] = []
    for a, b in spans:
        active.append((max(0.0, (a - 1) / fps), (b + 1) / fps))
        boxes = [samples[i].bbox for i in range(a, b + 1)
                 if samples[i].bbox and samples[i].mean_diff <= CUT_THRESHOLD]
        if boxes:
            x0 = min(bx[0] for bx in boxes)
            y0 = min(bx[1] for bx in boxes)
            x1 = max(bx[0] + bx[2] for bx in boxes)
            y1 = max(bx[1] + bx[3] for bx in boxes)
            regions.append([x0, y0, x1 - x0, y1 - y0])
        else:
            regions.append(None)
    activity = [s.changed / total for s in samples]
    return VisualAnalysis(fps, cuts, active, activity, regions)
