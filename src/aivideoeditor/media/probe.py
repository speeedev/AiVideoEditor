"""Read basic stream information with ffprobe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .ffmpeg import FFmpegError, ffprobe_bin


IMAGE_CODECS = {"png", "mjpeg", "webp", "bmp", "tiff", "gif", "jpegls", "jpeg2000"}
STILL_DURATION = 3600.0  # a still image can be held for up to an hour


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
    audio_streams: int = 0
    is_image: bool = False
    video_start: float = 0.0   # stream start relative to the file start (seconds)
    audio_start: float = 0.0
    created: str | None = None  # recording time from the file's tags (ISO 8601), if any
    has_alpha: bool = False     # video with a transparency channel (e.g. a rendered HTML animation)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MediaInfo":
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in known})


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
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    audio = audios[0] if audios else None

    duration = _float(fmt.get("duration"))
    if duration is None:
        duration = max(
            (_float(s.get("duration")) or 0.0 for s in streams), default=0.0
        )

    info = MediaInfo(path=path, duration=duration or 0.0,
                     has_video=video is not None, has_audio=audio is not None, audio_streams=len(audios))
    if video is not None and audio is None:
        frames = video.get("nb_frames")
        still_codec = video.get("codec_name") in IMAGE_CODECS
        if still_codec and (not duration or duration < 0.05 or frames in ("1", 1)):
            info.is_image = True
            info.duration = STILL_DURATION
    info.created = _creation_time(fmt, video, audio)
    fmt_start = _float(fmt.get("start_time")) or 0.0
    if video is not None and _float(video.get("start_time")) is not None:
        info.video_start = max(0.0, _float(video.get("start_time")) - fmt_start)
    if audio is not None and _float(audio.get("start_time")) is not None:
        info.audio_start = max(0.0, _float(audio.get("start_time")) - fmt_start)
    if video is not None:
        tags = {k.lower(): v for k, v in (video.get("tags") or {}).items()}
        pix = str(video.get("pix_fmt") or "")
        info.has_alpha = str(tags.get("alpha_mode", "")) == "1" or pix.startswith(("yuva", "rgba", "argb", "bgra",
                                                                                     "abgr", "ya", "gbrap"))
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


def _creation_time(fmt: dict, *streams: dict | None) -> str | None:
    for tags in [fmt.get("tags") or {}] + [(st or {}).get("tags") or {} for st in streams]:
        for key in ("creation_time", "com.apple.quicktime.creationdate", "date"):
            for k, v in tags.items():
                if k.lower() == key and v:
                    return str(v)
    return None


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
