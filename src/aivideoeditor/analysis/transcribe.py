"""Speech-to-text with word timestamps.

Claude cannot listen to audio, so the transcript is its main window into
what was said and when. The default backend is faster-whisper (local,
optional dependency). An existing transcript can also be imported from
our JSON format or from SRT/VTT subtitles.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    language: str | None = None
    segments: list[Segment] = field(default_factory=list)

    def words(self) -> Iterator[Word]:
        for seg in self.segments:
            if seg.words:
                yield from seg.words
            else:
                # Segment-level transcript (e.g. imported SRT): spread words evenly.
                tokens = seg.text.split()
                if not tokens:
                    continue
                step = (seg.end - seg.start) / len(tokens)
                for i, tok in enumerate(tokens):
                    yield Word(seg.start + i * step, seg.start + (i + 1) * step, tok, 0.5)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        return cls(
            language=d.get("language"),
            segments=[
                Segment(
                    start=float(s["start"]), end=float(s["end"]), text=s.get("text", ""),
                    words=[Word(float(w["start"]), float(w["end"]), w["text"], float(w.get("prob", 1.0)))
                           for w in s.get("words", [])],
                )
                for s in d.get("segments", [])
            ],
        )


# Whisper tends to "clean up" disfluencies. Priming it with fillers makes it
# keep them, which is exactly what an editor needs to find and cut them.
DEFAULT_PROMPTS = {
    "tr": "Eee, ıı, şey, yani... Hmm, tamam. Şimdi, ee, bakalım.",
    "en": "Umm, uh, like, you know... So, uh, let's see.",
}


def transcribe(
    path: str | Path,
    *,
    model: str = "small",
    language: str | None = None,
    device: str = "auto",
    compute_type: str = "default",
    initial_prompt: str | None = None,
) -> Transcript:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "faster-whisper is not installed. Install it with "
            "`pip install 'aivideoeditor[transcribe]'` or import an existing "
            "transcript with `ave transcript import`."
        ) from exc

    whisper = WhisperModel(model, device=device, compute_type=compute_type)
    prompt = initial_prompt if initial_prompt is not None else DEFAULT_PROMPTS.get(language or "", None)
    segments, info = whisper.transcribe(
        str(path),
        language=language,
        word_timestamps=True,
        vad_filter=False,  # keep pauses; we detect them ourselves
        condition_on_previous_text=False,
        initial_prompt=prompt,
    )
    out = Transcript(language=getattr(info, "language", language))
    for seg in segments:
        words = [
            Word(float(w.start), float(w.end), w.word.strip(), float(getattr(w, "probability", 1.0)))
            for w in (seg.words or [])
            if w.word.strip()
        ]
        out.segments.append(Segment(float(seg.start), float(seg.end), seg.text.strip(), words))
    return out


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _parse_ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"bad timestamp: {s!r}")
    h, mnt, sec, frac = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(frac.ljust(3, "0")) / 1000.0


def load_transcript(path: str | Path) -> Transcript:
    """Load a transcript from our JSON format or from SRT / WebVTT."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return Transcript.from_dict(json.loads(text))
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is None:
            continue
        a, b = lines[timing].split("-->", 1)
        body = " ".join(re.sub(r"<[^>]+>", "", ln) for ln in lines[timing + 1:]).strip()
        if body:
            segments.append(Segment(_parse_ts(a), _parse_ts(b.split()[0] if b.split() else b), body))
    return Transcript(language=None, segments=segments)
