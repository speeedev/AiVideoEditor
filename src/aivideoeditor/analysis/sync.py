"""Align two recordings of the same event by their audio.

Works like PluralEyes: a coarse search on loudness envelopes over the whole
recordings, then a precise GCC-PHAT refinement on raw audio around a few
anchor points. Measuring at several anchors also reveals clock drift
between devices (common with separate camera / recorder clocks).

Convention: ``offset`` is the time in the *reference* recording at which
the *other* recording's t=0 happens, i.e. ``ref_time = other_time + offset``.
A positive offset means the other device started recording later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .audio import decode_audio

SYNC_SR = 8000
ENV_HOP = 0.01  # 100 Hz envelope


@dataclass
class SyncResult:
    offset: float
    confidence: float
    drift_ppm: float = 0.0
    measurements: list[tuple[float, float]] = field(default_factory=list)  # (ref_time, offset)
    overlap: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["measurements"] = [[round(a, 4), round(b, 6)] for a, b in self.measurements]
        return d

    @property
    def reliable(self) -> bool:
        # The precise (GCC-PHAT) check must have matched too: two takes of the same script can
        # have similar loudness envelopes, but only the same recording matches sample by sample.
        return self.confidence >= 6.0 and bool(self.measurements)

    def offset_at(self, ref_time: float) -> float:
        """Offset corrected for drift at a given reference time."""
        return self.offset + self.drift_ppm * 1e-6 * ref_time


def _envelope(x: np.ndarray, sr: int) -> np.ndarray:
    hop = int(sr * ENV_HOP)
    n = x.size // hop
    if n == 0:
        return np.zeros(0)
    frames = x[: n * hop].reshape(n, hop).astype(np.float64)
    env = np.log(np.sqrt((frames ** 2).mean(axis=1)) + 1e-4)
    # Remove slow level changes (different gains / mic distances): high-pass by
    # subtracting a ~1 s moving average, then whiten to unit variance.
    k = 101
    if env.size > k:
        kernel = np.ones(k) / k
        env = env - np.convolve(np.pad(env, k // 2, mode="edge"), kernel, mode="valid")
    std = env.std()
    return env / std if std > 0 else env


def _xcorr(a: np.ndarray, b: np.ndarray, *, phat: bool) -> np.ndarray:
    """Circular-free cross-correlation r[k] = sum a[n+k] b[n] for all lags.

    Returned array is ordered from lag -(len(b)-1) to +(len(a)-1).
    """
    n = a.size + b.size - 1
    nfft = 1 << (n - 1).bit_length()
    A = np.fft.rfft(a, nfft)
    B = np.fft.rfft(b, nfft)
    R = A * np.conj(B)
    if phat:
        R /= np.maximum(np.abs(R), 1e-12)
    r = np.fft.irfft(R, nfft)
    # Negative lags wrap to the end of the buffer.
    return np.concatenate([r[nfft - (b.size - 1):], r[: a.size]])


def _peak(r: np.ndarray, lag0: int, lo: int, hi: int) -> tuple[float, float]:
    """Best lag within [lo, hi] (inclusive) with sub-sample refinement + z-score."""
    i0, i1 = lo - lag0, hi - lag0 + 1
    i0, i1 = max(i0, 0), min(i1, r.size)
    seg = r[i0:i1]
    if seg.size == 0:
        return 0.0, 0.0
    j = int(np.argmax(seg))
    frac = 0.0
    if 0 < j < seg.size - 1:
        y0, y1, y2 = seg[j - 1], seg[j], seg[j + 1]
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            frac = 0.5 * (y0 - y2) / denom
    std = seg.std()
    z = float((seg[j] - seg.mean()) / std) if std > 0 else 0.0
    return float(i0 + j + frac + lag0), z


def coarse_offset(ref: np.ndarray, other: np.ndarray, sr: int,
                  max_offset: float | None = None) -> tuple[float, float]:
    ea, eb = _envelope(ref, sr), _envelope(other, sr)
    if ea.size < 10 or eb.size < 10:
        raise ValueError("recordings are too short to synchronise")
    r = _xcorr(ea, eb, phat=False)
    lag0 = -(eb.size - 1)
    lo, hi = lag0, ea.size - 1
    if max_offset is not None:
        m = int(max_offset / ENV_HOP)
        lo, hi = max(lo, -m), min(hi, m)
    lag, z = _peak(r, lag0, lo, hi)
    return lag * ENV_HOP, z


def fine_offset(ref: np.ndarray, other: np.ndarray, sr: int, guess: float,
                ref_time: float, window: float = 12.0, search: float = 0.08) -> tuple[float, float]:
    """Refine ``guess`` with GCC-PHAT on a window of the reference centred at ``ref_time``."""
    w = int(window * sr)
    a0 = int(ref_time * sr) - w // 2
    a0 = max(0, min(a0, ref.size - w))
    a = ref[a0: a0 + w]
    margin = int(search * sr)
    b0 = int(round((a0 / sr - guess) * sr)) - margin
    b_start = max(0, b0)
    b = other[b_start: b0 + w + margin]
    if a.size < sr or b.size < sr:
        return guess, 0.0
    r = _xcorr(a.astype(np.float64), b.astype(np.float64), phat=True)
    lag0 = -(b.size - 1)
    # The relative lag between windows that corresponds to ``guess``:
    expected = b_start - (a0 - int(round(guess * sr)))
    lag, z = _peak(r, lag0, expected - margin, expected + margin)
    # r[k] = sum a[n+k] b[n]  -> a at (n+k) matches b at n
    # ref_time(a0+n+k) == other_time(b_start+n) + offset
    offset = (a0 + lag - b_start) / sr
    return offset, z


def find_offset(
    ref_path: str | Path,
    other_path: str | Path,
    *,
    max_offset: float | None = None,
    anchors: int = 5,
) -> SyncResult:
    ref = decode_audio(ref_path, SYNC_SR)
    other = decode_audio(other_path, SYNC_SR)
    return find_offset_arrays(ref, other, SYNC_SR, max_offset=max_offset, anchors=anchors)


def find_offset_arrays(
    ref: np.ndarray, other: np.ndarray, sr: int, *,
    max_offset: float | None = None, anchors: int = 5,
) -> SyncResult:
    guess, z_coarse = coarse_offset(ref, other, sr, max_offset)

    ref_dur, other_dur = ref.size / sr, other.size / sr
    ov_start = max(0.0, guess)
    ov_end = min(ref_dur, guess + other_dur)
    overlap = max(0.0, ov_end - ov_start)
    if overlap < 2.0:
        return SyncResult(guess, z_coarse, 0.0, [], overlap)

    window = min(12.0, overlap)
    n = max(1, anchors if overlap > 4 * window else 1)
    centers = np.linspace(ov_start + window / 2, ov_end - window / 2, n) if n > 1 else [(ov_start + ov_end) / 2]
    measurements: list[tuple[float, float]] = []
    scores: list[float] = []
    for c in centers:
        off, z = fine_offset(ref, other, sr, guess, float(c), window=window)
        if z >= 5.0 and abs(off - guess) < 0.08:
            measurements.append((float(c), off))
            scores.append(z)
    if not measurements:
        return SyncResult(guess, z_coarse, 0.0, [], overlap)

    times = np.array([m[0] for m in measurements])
    offs = np.array([m[1] for m in measurements])
    drift = 0.0
    if len(measurements) >= 3 and np.ptp(times) > 30:
        slope, intercept = np.polyfit(times, offs, 1)
        drift = float(slope * 1e6)
        offset = float(intercept)
    else:
        offset = float(np.median(offs))
        slope = 0.0
    # Ignore drift that stays below ~one 60 fps frame over the overlap.
    if abs(slope) * overlap < 1 / 60:
        drift = 0.0
        offset = float(np.median(offs))
    confidence = float(min(z_coarse, np.median(scores)) if z_coarse > 0 else np.median(scores))
    return SyncResult(offset, confidence, drift, measurements, overlap)
