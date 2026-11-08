"""Cheap visual analysis: hard cuts and on-screen activity.

Frames are decoded at a low rate and tiny resolution (grayscale) and
compared with their predecessor. Large differences are scene cuts;
small but non-zero differences on a screen recording mean something is
happening (typing, scrolling, a window opening), which is a strong hint
for where to keep footage, zoom in, or speed up idle parts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..media.ffmpeg import FFmpegError, run_ffmpeg

THUMB_W, THUMB_H = 64, 36


@dataclass
class VisualAnalysis:
    sample_fps: float
    scene_cuts: list[float] = field(default_factory=list)
    active: list[tuple[float, float]] = field(default_factory=list)
    activity: list[float] = field(default_factory=list)  # one value per sample, 0..1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scene_cuts"] = [round(t, 3) for t in self.scene_cuts]
        d["active"] = [[round(a, 3), round(b, 3)] for a, b in self.active]
        d["activity"] = [round(v, 4) for v in self.activity]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "VisualAnalysis":
        return cls(
            sample_fps=d["sample_fps"],
            scene_cuts=list(d.get("scene_cuts", [])),
            active=[(float(a), float(b)) for a, b in d.get("active", [])],
            activity=list(d.get("activity", [])),
        )

    def activity_between(self, start: float, end: float) -> float:
        if not self.activity:
            return 0.0
        i0 = max(0, int(start * self.sample_fps))
        i1 = max(i0 + 1, int(np.ceil(end * self.sample_fps)))
        window = self.activity[i0:i1]
        return float(np.mean(window)) if window else 0.0


def decode_thumbs(path: str | Path, sample_fps: float) -> np.ndarray:
    args = ["-i", str(path), "-map", "0:v:0", "-an",
            "-vf", f"fps={sample_fps},scale={THUMB_W}:{THUMB_H}:flags=area,format=gray",
            "-f", "rawvideo", "pipe:1"]
    try:
        # Hardware decoding makes long 4K/60 fps recordings much faster where available.
        proc = run_ffmpeg(["-hwaccel", "auto", *args], capture_stdout=True)
    except FFmpegError:
        proc = run_ffmpeg(args, capture_stdout=True)
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = data.size // (THUMB_W * THUMB_H)
    return data[: n * THUMB_W * THUMB_H].reshape(n, THUMB_H, THUMB_W).astype(np.float32) / 255.0


def analyze_visual(
    path: str | Path,
    *,
    sample_fps: float = 2.0,
    cut_threshold: float = 0.12,
    activity_threshold: float = 0.004,
    min_gap: float = 1.5,
) -> VisualAnalysis:
    frames = decode_thumbs(path, sample_fps)
    return analyze_frames(frames, sample_fps, cut_threshold=cut_threshold,
                          activity_threshold=activity_threshold, min_gap=min_gap)


def analyze_frames(
    frames: np.ndarray,
    sample_fps: float,
    *,
    cut_threshold: float = 0.12,
    activity_threshold: float = 0.004,
    min_gap: float = 1.5,
) -> VisualAnalysis:
    if frames.shape[0] < 2:
        return VisualAnalysis(sample_fps, [], [], [0.0] * int(frames.shape[0]))
    diffs = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    activity = np.concatenate([[0.0], diffs])
    times = np.arange(activity.size) / sample_fps

    cuts = [float(times[i]) for i in np.flatnonzero(activity > cut_threshold)]

    active_mask = activity > activity_threshold
    # Merge active samples separated by less than ``min_gap`` seconds.
    gap = int(round(min_gap * sample_fps))
    spans: list[tuple[float, float]] = []
    idx = np.flatnonzero(active_mask)
    if idx.size:
        start = prev = int(idx[0])
        for i in idx[1:]:
            i = int(i)
            if i - prev > gap:
                spans.append((max(0.0, (start - 1) / sample_fps), (prev + 1) / sample_fps))
                start = i
            prev = i
        spans.append((max(0.0, (start - 1) / sample_fps), (prev + 1) / sample_fps))
    return VisualAnalysis(sample_fps, cuts, spans, activity.astype(float).tolist())
