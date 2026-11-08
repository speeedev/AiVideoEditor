"""Separate speech from music and background sound (Demucs, runs locally).

Produces two WAV files on the same clock as the original recording (sample 0
is file time 0), so the renderer can swap the original sound for the voice
without touching the edit. Optional dependency: ``pip install 'aivideoeditor[separate]'``.
"""

from __future__ import annotations

import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..media.ffmpeg import run_ffmpeg

MODEL = "htdemucs"


def _decode_stereo(path: str | Path, sr: int, stream: int) -> np.ndarray:
    proc = run_ffmpeg(["-i", str(path), "-vn", "-map", f"0:a:{stream}",
                       "-af", f"aresample={sr}:async=1:first_pts=0", "-ac", "2", "-ar", str(sr),
                       "-f", "f32le", "pipe:1"], capture_stdout=True)
    return np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2).T.copy()   # (2, n)


def _write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    pcm = (np.clip(audio.T, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(audio.shape[0])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def separate_voice(path: str | Path, out_dir: str | Path, name: str, *, stream: int = 0,
                   log: Callable[[str], None] = print) -> tuple[Path, Path]:
    """Write ``<name>_voice.wav`` and ``<name>_music.wav``; returns their paths."""
    try:
        import torch
        from demucs.apply import apply_model
        from demucs.pretrained import get_model
    except ImportError as exc:  # pragma: no cover - optional extra
        raise RuntimeError("Voice separation needs Demucs: pip install 'aivideoeditor[separate]'") from exc

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log("loading the separation model (downloaded once, about 80 MB)…")
    model = get_model(MODEL)
    model.eval()
    sr = model.samplerate
    audio = _decode_stereo(path, sr, stream)
    wav = torch.from_numpy(audio)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std().clamp_min(1e-8)
    wav = (wav - mean) / std
    devices = ["mps", "cpu"] if torch.backends.mps.is_available() else ["cuda", "cpu"] if torch.cuda.is_available() \
        else ["cpu"]
    sources = None
    for device in devices:
        try:
            log(f"separating speech from music ({audio.shape[1] / sr:.0f}s of audio, {device})…")
            with torch.no_grad():
                sources = apply_model(model, wav[None], device=device, shifts=0, split=True, overlap=0.25,
                                      progress=False)[0]
            break
        except Exception as exc:  # noqa: BLE001 - fall back to the CPU (e.g. an op missing on MPS)
            if device == devices[-1]:
                raise
            log(f"{device} failed ({type(exc).__name__}); retrying on the CPU")
    assert sources is not None
    sources = sources * std + mean
    names = list(model.sources)
    vocals = sources[names.index("vocals")].cpu().numpy()
    rest = sum(sources[i] for i, n in enumerate(names) if n != "vocals").cpu().numpy()
    voice_path, music_path = out / f"{name}_voice.wav", out / f"{name}_music.wav"
    _write_wav(voice_path, vocals, sr)
    _write_wav(music_path, rest, sr)
    return voice_path, music_path
