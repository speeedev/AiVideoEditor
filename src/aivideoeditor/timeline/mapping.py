"""Map source-time analysis (transcripts, pauses) onto the current timeline.

The agent always reasons in timeline time. After every cut the same
transcript is re-projected through the clips, so word times stay correct
no matter how much has been removed, moved or sped up.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from ..analysis.speech import SpeechAnalysis
from ..analysis.transcribe import Transcript
from .model import EPS, Clip, Timeline, Track


@dataclass
class TimelineWord:
    start: float
    end: float
    text: str
    prob: float
    clip_id: str
    source_start: float
    source_end: float


def asset_clips(tl: Timeline, asset_id: str) -> list[tuple[Track, Clip]]:
    """Clips of an asset sorted by timeline start, preferring audible tracks."""
    pairs = tl.clips_of_asset(asset_id)
    pairs.sort(key=lambda tc: (tc[0].muted, tc[1].start))
    chosen: list[tuple[Track, Clip]] = []
    for t, c in pairs:
        # Drop clips that duplicate an already chosen clip at the same time.
        if any(o.start < c.end - EPS and o.end > c.start + EPS for _, o in chosen):
            continue
        chosen.append((t, c))
    chosen.sort(key=lambda tc: tc[1].start)
    return chosen


def source_to_timeline(tl: Timeline, asset_id: str, s: float) -> float | None:
    for _, c in asset_clips(tl, asset_id):
        if c.source_in - EPS <= s < c.source_out - EPS:
            return c.timeline_time(s)
    return None


def words_on_timeline(tl: Timeline, asset_id: str, transcript: Transcript) -> list[TimelineWord]:
    words = sorted(transcript.words(), key=lambda w: w.start)
    mids = [(w.start + w.end) / 2 for w in words]
    out: list[TimelineWord] = []
    for _, c in asset_clips(tl, asset_id):
        lo = bisect.bisect_left(mids, c.source_in)
        hi = bisect.bisect_left(mids, c.source_out)
        for w in words[lo:hi]:
            s0 = max(w.start, c.source_in)
            s1 = min(w.end, c.source_out)
            out.append(TimelineWord(c.timeline_time(s0), c.timeline_time(max(s1, s0)), w.text,
                                    w.prob, c.id, w.start, w.end))
    out.sort(key=lambda w: w.start)
    return out


def silences_on_timeline(tl: Timeline, asset_id: str, speech: SpeechAnalysis) -> list[tuple[float, float]]:
    """Silent stretches of an asset, projected onto the timeline."""
    out: list[tuple[float, float]] = []
    for _, c in asset_clips(tl, asset_id):
        for a, b in speech.silences:
            lo, hi = max(a, c.source_in), min(b, c.source_out)
            if hi - lo > EPS:
                out.append((c.timeline_time(lo), c.timeline_time(hi)))
    out.sort()
    return out


def snap_to_pauses(t: float, pauses: list[tuple[float, float]], tolerance: float = 0.25,
                   *, prefer: str = "nearest") -> float:
    """Move a cut point into the nearest pause if one is within ``tolerance``."""
    best = t
    best_d = tolerance + 1
    for a, b in pauses:
        if a - tolerance > t:
            break
        if a <= t <= b:
            return t
        inset = min(0.03, (b - a) / 2)
        for edge in (a + inset, b - inset):
            d = abs(edge - t)
            if d <= tolerance and d < best_d:
                if prefer == "before" and edge > t:
                    continue
                if prefer == "after" and edge < t:
                    continue
                best, best_d = edge, d
    return best
