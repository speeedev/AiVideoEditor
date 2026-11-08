"""Read basic stream information with ffprobe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .ffmpeg import FFmpegError, ffprobe_bin


@dataclass
class MediaInfo:
    path: str
    duration: float
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    rotation: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MediaInfo":
        return cls(**data)


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            n, d = float(num), float(den)
        except ValueError:
            return None
        return n / d if d else None
    try:
        return float(rate)
    except ValueError:
        return None


def probe(path: str | Path) -> MediaInfo:
    path = str(path)
    cmd = [
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    data = json.loads(proc.stdout or b"{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _float(fmt.get("duration"))
    if duration is None:
        duration = max(
            (_float(s.get("duration")) or 0.0 for s in streams), default=0.0
        )

    info = MediaInfo(path=path, duration=duration or 0.0,
                     has_video=video is not None, has_audio=audio is not None)
    if video is not None:
        info.width = video.get("width")
        info.height = video.get("height")
        info.fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
        info.video_codec = video.get("codec_name")
        info.rotation = _rotation(video)
        if info.rotation in (90, 270) and info.width and info.height:
            info.width, info.height = info.height, info.width
    if audio is not None:
        info.sample_rate = int(audio["sample_rate"]) if audio.get("sample_rate") else None
        info.channels = audio.get("channels")
        info.audio_codec = audio.get("codec_name")
    return info


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rotation(stream: dict) -> int:
    tags = stream.get("tags", {}) or {}
    if "rotate" in tags:
        try:
            return int(tags["rotate"]) % 360
        except ValueError:
            pass
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                return int(-float(side["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    return 0
