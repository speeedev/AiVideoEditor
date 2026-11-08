"""Energy-based speech / silence segmentation.

Deliberately simple and deterministic: it is used to snap cut points to
pauses and to offer an automatic "remove long pauses" pass. Whisper word
timestamps complement it for content-aware editing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .audio import frame_rms_db

HOP = 0.01  # seconds per analysis frame


@dataclass
class SpeechAnalysis:
    duration: float
    threshold_db: float
    noise_floor_db: float
    peak_db: float
    speech: list[tuple[float, float]] = field(default_factory=list)
    silences: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["speech"] = [[round(a, 3), round(b, 3)] for a, b in self.speech]
        d["silences"] = [[round(a, 3), round(b, 3)] for a, b in self.silences]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SpeechAnalysis":
        return cls(
            duration=d["duration"],
            threshold_db=d["threshold_db"],
            noise_floor_db=d["noise_floor_db"],
            peak_db=d["peak_db"],
            speech=[(float(a), float(b)) for a, b in d.get("speech", [])],
            silences=[(float(a), float(b)) for a, b in d.get("silences", [])],
        )


def analyze_speech(
    samples: np.ndarray,
    sample_rate: int,
    *,
    min_silence: float = 0.25,
    min_speech: float = 0.08,
    threshold_db: float | None = None,
) -> SpeechAnalysis:
    """Split audio into speech and silence regions.

    The threshold adapts to the recording: it sits between the noise floor
    (10th percentile level) and the typical speech level (90th percentile),
    so it works for both quiet lavalier mics and noisy laptop recordings.
    """
    duration = samples.size / float(sample_rate) if sample_rate else 0.0
    db = frame_rms_db(samples, sample_rate, hop=HOP)
    if db.size == 0:
        return SpeechAnalysis(duration, -60.0, -120.0, -120.0)

    noise_floor = float(np.percentile(db, 10))
    loud = float(np.percentile(db, 90))
    peak = float(db.max())
    if threshold_db is None:
        if loud - noise_floor < 6.0:
            # Almost no dynamics: either all silence or all speech.
            threshold_db = noise_floor + 3.0 if loud < -50 else noise_floor - 1.0
        else:
            threshold_db = max(noise_floor + 0.35 * (loud - noise_floor), noise_floor + 6.0)
            threshold_db = min(threshold_db, loud - 6.0)

    active = db > threshold_db
    min_sil_frames = int(round(min_silence / HOP))
    min_sp_frames = int(round(min_speech / HOP))

    active = _fill_short_runs(active, value=False, max_len=min_sil_frames)   # bridge short pauses
    active = _fill_short_runs(active, value=True, max_len=min_sp_frames)     # drop clicks

    speech = [(s * HOP, min(e * HOP, duration)) for s, e in _runs(active, True)]
    silences = [(s * HOP, min(e * HOP, duration)) for s, e in _runs(active, False)]
    silences = [(a, b) for a, b in silences if b - a >= min_silence - 1e-9]
    return SpeechAnalysis(duration, float(threshold_db), noise_floor, peak, speech, silences)


def _runs(mask: np.ndarray, value: bool) -> list[tuple[int, int]]:
    """Return [start, end) index pairs of consecutive runs equal to ``value``."""
    m = (mask == value).astype(np.int8)
    if m.size == 0:
        return []
    diff = np.diff(np.concatenate([[0], m, [0]]))
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def _fill_short_runs(mask: np.ndarray, *, value: bool, max_len: int) -> np.ndarray:
    """Flip interior runs of ``value`` shorter than ``max_len`` frames."""
    out = mask.copy()
    for s, e in _runs(mask, value):
        if s == 0 or e == mask.size:
            continue  # keep leading/trailing runs as-is
        if e - s < max_len:
            out[s:e] = not value
    return out
