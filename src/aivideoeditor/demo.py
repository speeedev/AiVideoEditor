"""Synthetic demo footage: a "screen recording", a "webcam" and a "mic".

The webcam and the mic hear the same (synthetic) speech, but the webcam
started recording later and has a worse microphone, so the pair exercises
audio sync. Used by the test-suite and by ``ave demo`` to try the
pipeline without real recordings.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .media.ffmpeg import run_ffmpeg

SR = 48000


@dataclass
class DemoFiles:
    screen: Path
    camera: Path
    mic: Path
    camera_offset: float  # seconds on the mic clock where the camera starts
    speech: list[tuple[float, float]]  # (start, end) of "syllable bursts" on the mic clock


def speech_like(duration: float, seed: int = 1, sr: int = SR) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Voiced bursts with pitch and formant-ish colour, separated by pauses."""
    rng = np.random.default_rng(seed)
    n = int(duration * sr)
    out = np.zeros(n, dtype=np.float64)
    bursts: list[tuple[float, float]] = []
    t = 0.6
    while t < duration - 0.6:
        # a "phrase" of a few syllables, then a pause (sometimes a long one)
        for _ in range(rng.integers(2, 7)):
            L = rng.uniform(0.12, 0.32)
            if t + L > duration - 0.5:
                break
            i0, i1 = int(t * sr), int((t + L) * sr)
            tt = np.arange(i1 - i0) / sr
            f0 = rng.uniform(110, 210)
            voiced = sum(np.sin(2 * np.pi * f0 * k * tt) / k for k in range(1, 8))
            noise = rng.standard_normal(tt.size) * 0.15
            env = np.hanning(tt.size) * rng.uniform(0.25, 0.6)
            out[i0:i1] += (voiced * 0.3 + noise) * env
            bursts.append((float(t), float(t + L)))
            t += L + rng.uniform(0.03, 0.12)
        t += float(rng.choice([rng.uniform(0.25, 0.5), rng.uniform(1.2, 2.2)], p=[0.7, 0.3]))
    out += rng.standard_normal(n) * 0.002  # room tone
    return out.astype(np.float32), bursts


def write_wav(path: Path, samples: np.ndarray, sr: int = SR) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _has_drawtext() -> bool:
    from .media.ffmpeg import ffmpeg_bin
    from .render.textlayer import _filters
    return "drawtext" in _filters(ffmpeg_bin())


def make_demo(out_dir: str | Path, *, duration: float = 30.0, camera_offset: float = 1.3,
              size: tuple[int, int] = (1280, 720), cam_size: tuple[int, int] = (640, 360),
              fps: int = 30, labels: bool | None = None) -> DemoFiles:
    """``labels`` burns "CAMERA/SCREEN hh:mm:ss" into the footage (needs FFmpeg's drawtext;
    by default only when this FFmpeg has it)."""
    if labels is None:
        labels = _has_drawtext()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    voice, bursts = speech_like(duration + 1.0)
    voice = voice[: int(duration * SR)]
    mic = out / "mic.wav"
    write_wav(mic, voice)

    # Camera: starts ``camera_offset`` s later, quieter, muffled, noisier.
    cam_audio = voice[int(camera_offset * SR):]
    cam_audio = np.convolve(cam_audio, np.ones(9) / 9, mode="same") * 0.5
    cam_audio = cam_audio + np.random.default_rng(7).standard_normal(cam_audio.size).astype(np.float32) * 0.01
    cam_wav = out / "_camera_audio.wav"
    write_wav(cam_wav, cam_audio)
    cam_dur = cam_audio.size / SR
    camera = out / "camera.mp4"
    w, h = cam_size
    cam_label = (["-vf", "drawtext=text='CAMERA %{pts\\:hms}':x=20:y=20:fontsize=28:fontcolor=white:"
                          "box=1:boxcolor=black@0.6"] if labels else [])
    run_ffmpeg([
        "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={cam_dur:.3f}",
        "-i", str(cam_wav), *cam_label,
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", "-y", str(camera),
    ])
    cam_wav.unlink(missing_ok=True)

    screen = out / "screen.mp4"
    w, h = size
    # A mostly static "desktop" with a moving element and occasional big changes.
    run_ffmpeg([
        "-f", "lavfi", "-i", f"color=c=0x1e1e2e:s={w}x{h}:r={fps}:d={duration:.3f}",
        "-vf", ",".join([
            "drawbox=x=40:y=40:w=iw-80:h=60:color=0x313244:t=fill",
            "drawbox=x='40+mod(t*120\\,iw-200)':y=200:w=120:h=40:color=0x89b4fa:t=fill",
            *(["drawtext=text='SCREEN %{pts\\:hms}':x=60:y=55:fontsize=30:fontcolor=white"] if labels else []),
            "drawbox=x=0:y=0:w=iw:h=ih:color=0xf38ba8@0.5:t=fill:enable='between(t,10,14)'",
        ]),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", "-y", str(screen),
    ])
    return DemoFiles(screen=screen, camera=camera, mic=mic, camera_offset=camera_offset, speech=bursts)
