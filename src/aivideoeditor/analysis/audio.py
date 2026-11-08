"""Decode audio to numpy arrays via ffmpeg."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..media.ffmpeg import run_ffmpeg


def decode_audio(
    path: str | Path,
    sample_rate: int = 16000,
    start: float | None = None,
    duration: float | None = None,
) -> np.ndarray:
    """Decode the first audio stream as mono float32 in [-1, 1]."""
    args: list[str] = []
    if start is not None and start > 0:
        args += ["-ss", f"{start:.6f}"]
    args += ["-i", str(path)]
    if duration is not None:
        args += ["-t", f"{max(duration, 0.0):.6f}"]
    args += ["-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "pipe:1"]
    proc = run_ffmpeg(args, capture_stdout=True)
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def frame_rms_db(samples: np.ndarray, sample_rate: int, hop: float = 0.01, win: float = 0.03) -> np.ndarray:
    """RMS level in dBFS for overlapping windows centred every ``hop`` seconds."""
    hop_n = max(1, int(round(hop * sample_rate)))
    win_n = max(hop_n, int(round(win * sample_rate)))
    if samples.size == 0:
        return np.zeros(0, dtype=np.float32)
    n_frames = int(np.ceil(samples.size / hop_n))
    # Cumulative sum of squares lets us compute any window energy in O(1).
    sq = np.concatenate([[0.0], np.cumsum(samples.astype(np.float64) ** 2)])
    centers = np.arange(n_frames) * hop_n + hop_n // 2
    lo = np.clip(centers - win_n // 2, 0, samples.size)
    hi = np.clip(centers + win_n // 2, 0, samples.size)
    length = np.maximum(hi - lo, 1)
    rms = np.sqrt((sq[hi] - sq[lo]) / length)
    return (20.0 * np.log10(np.maximum(rms, 1e-7))).astype(np.float32)
