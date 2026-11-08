"""Stabilise shaky footage (handheld phone recordings).

Writes a stabilised copy of the picture next to the project; the original
file is never changed. Uses FFmpeg's vid.stab two-pass stabiliser when the
build has it, otherwise the built-in ``deshake`` filter. A slight zoom hides
the moving borders. Timestamps are kept, so the edit stays in sync.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from .ffmpeg import ffmpeg_bin, run_ffmpeg

STRENGTH = {  # (deshake search range px, vid.stab smoothing frames, zoom %)
    "low": (16, 10, 3),
    "medium": (32, 20, 5),
    "high": (64, 35, 8),
}


@functools.lru_cache(maxsize=4)
def _has(binary: str, name: str) -> bool:
    out = subprocess.run([binary, "-hide_banner", "-filters"], capture_output=True, text=True)
    return any(len(p := line.split()) >= 2 and p[1] == name for line in out.stdout.splitlines())


@functools.lru_cache(maxsize=4)
def _encoder(binary: str) -> list[str]:
    out = subprocess.run([binary, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "h264_videotoolbox" in out:
        return ["-c:v", "h264_videotoolbox", "-q:v", "70", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


def stabilize(src: str | Path, out: str | Path, strength: str = "medium") -> Path:
    if strength not in STRENGTH:
        raise ValueError(f"strength must be one of {', '.join(STRENGTH)}")
    rng, smoothing, zoom = STRENGTH[strength]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    binary = ffmpeg_bin()
    z = 1 + zoom / 100
    scale = f"scale=trunc(iw*{z:.3f}/2)*2:trunc(ih*{z:.3f}/2)*2,crop=trunc(iw/{z:.3f}/2)*2:trunc(ih/{z:.3f}/2)*2"
    if _has(binary, "vidstabdetect") and _has(binary, "vidstabtransform"):
        trf = out.with_suffix(".trf")
        run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-vf", f"vidstabdetect=shakiness=6:result={trf.name}",
                    "-f", "null", "-"], cwd=out.parent)
        vf = f"vidstabtransform=input={trf.name}:smoothing={smoothing}:zoom={zoom},unsharp=5:5:0.6:3:3:0.3"
    else:
        vf = f"deshake=rx={rng}:ry={rng}:edge=mirror,{scale}"
    run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-vf", vf, *_encoder(binary),
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-y", out.name], cwd=out.parent)
    return out
