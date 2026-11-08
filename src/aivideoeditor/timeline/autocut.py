"""Deterministic helpers that propose cuts; the agent decides what to apply."""

from __future__ import annotations

import re

from ..analysis.speech import SpeechAnalysis
from ..analysis.transcribe import Transcript
from .mapping import silences_on_timeline, words_on_timeline
from .model import Timeline

FILLERS = {
    "tr": {"ee", "eee", "eeee", "ı", "ıı", "ııı", "ıııı", "hı", "hmm", "hım", "mmm", "aa", "aaa"},
    "en": {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "mm", "mmm", "ah"},
}


def pause_ranges(tl: Timeline, asset_id: str, speech: SpeechAnalysis, *,
                 min_pause: float = 0.7, keep: float = 0.2) -> list[tuple[float, float]]:
    """Ranges that shorten every pause longer than ``min_pause`` to ``2*keep``.

    Leading silence before the first word and trailing silence after the
    last one are removed down to ``keep``.
    """
    out: list[tuple[float, float]] = []
    pauses = silences_on_timeline(tl, asset_id, speech)
    if not pauses:
        return out
    end = tl.duration
    for a, b in pauses:
        if b - a < min_pause:
            continue
        lo = a + keep if a > 1e-3 else a
        hi = b - keep if b < end - 1e-3 else b
        if hi - lo > 0.05:
            out.append((lo, hi))
    return out


def _norm(word: str) -> str:
    return re.sub(r"[^\wçğıöşü]", "", word.lower())


def filler_ranges(tl: Timeline, asset_id: str, transcript: Transcript, *,
                  language: str | None = None, extra: set[str] | None = None,
                  pad: float = 0.03) -> list[tuple[float, float, str]]:
    lang = (language or transcript.language or "en")[:2]
    vocab = set(FILLERS.get(lang, set())) | FILLERS["en"] | (extra or set())
    out = []
    for w in words_on_timeline(tl, asset_id, transcript):
        n = _norm(w.text)
        # Collapse elongated fillers: "eeeee" -> "eee"
        n = re.sub(r"(.)\1{3,}", r"\1\1\1", n)
        if n and n in vocab:
            out.append((max(0.0, w.start - pad), w.end + pad, w.text))
    return out


def repeated_phrases(tl: Timeline, asset_id: str, transcript: Transcript, *,
                     min_words: int = 3, window: float = 30.0) -> list[dict]:
    """Find likely retakes: the same word sequence spoken twice close together.

    Returns candidates with the earlier take's range; usually the last take
    is the one to keep.
    """
    words = words_on_timeline(tl, asset_id, transcript)
    norm = [_norm(w.text) for w in words]
    found: list[dict] = []
    i = 0
    while i < len(words) - min_words:
        key = norm[i:i + min_words]
        if not all(key):
            i += 1
            continue
        for j in range(i + min_words, len(words) - min_words + 1):
            if words[j].start - words[i].start > window:
                break
            if norm[j:j + min_words] == key:
                found.append({
                    "first_take": (round(words[i].start, 2), round(words[j].start, 2)),
                    "retake_starts": round(words[j].start, 2),
                    "phrase": " ".join(w.text for w in words[i:i + min_words + 2]),
                })
                i = j - 1
                break
        i += 1
    return found
