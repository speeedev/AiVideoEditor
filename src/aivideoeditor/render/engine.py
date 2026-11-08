"""Render a timeline with FFmpeg.

The timeline is rendered in time chunks. Each chunk is one ffmpeg run that
reads every clip segment in that window as its own (accurately seeked)
input, composites video with ``overlay`` and mixes audio with ``amix``,
writing an H.264 video file and a PCM WAV. Chunks are then joined
losslessly (stream copy for video, PCM concat for audio) and muxed.

Chunking keeps the number of simultaneously open decoders bounded, so a
heavily cut 1-hour recording renders as reliably as a 30-second clip.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from ..media.ffmpeg import run_ffmpeg
from ..timeline.mapping import TimelineWord
from ..timeline.model import AudioMaster, Clip, Timeline, Track
from .captions import build_ass

AUDIO_RATE = 48000


class AssetLike(Protocol):  # pragma: no cover - typing helper
    path: str
    info: dict


@dataclass
class RenderSettings:
    width: int
    height: int
    fps: float
    preview: bool = False
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "192k"
    max_inputs: int = 24
    keep_work: bool = False
    duck_amount: float = 6.0  # compressor ratio used for ducking

    @classmethod
    def for_timeline(cls, tl: Timeline, *, preview: bool = False, preview_width: int = 640) -> "RenderSettings":
        w, h = tl.canvas.width, tl.canvas.height
        if preview and w > preview_width:
            scale = preview_width / w
            w, h = _even(w * scale), _even(h * scale)
        return cls(width=w, height=h, fps=tl.canvas.fps, preview=preview,
                   crf=30 if preview else 18, preset="ultrafast" if preview else "medium")


@dataclass
class Segment:
    track: Track
    clip: Clip
    f0: int           # first frame (timeline)
    f1: int           # end frame (exclusive)
    fps: float
    video: bool
    audio: bool
    starts_clip: bool
    ends_clip: bool
    input_index: int = -1

    @property
    def t0(self) -> float:
        return self.f0 / self.fps

    @property
    def duration(self) -> float:
        return (self.f1 - self.f0) / self.fps

    @property
    def src_in(self) -> float:
        return max(0.0, self.clip.source_time(self.t0))

    @property
    def src_dur(self) -> float:
        return self.duration * self.clip.speed


@dataclass
class ChunkPlan:
    f0: int
    f1: int
    segments: list[Segment] = field(default_factory=list)


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def fps_string(fps: float) -> str:
    for ntsc in (24000 / 1001, 30000 / 1001, 60000 / 1001):
        if abs(fps - ntsc) < 0.005:
            return f"{round(ntsc * 1001)}/1001"
    frac = Fraction(fps).limit_denominator(1000)
    return f"{frac.numerator}/{frac.denominator}" if frac.denominator != 1 else str(frac.numerator)


def _audible(track: Track, clip: Clip, assets: Mapping[str, AssetLike]) -> bool:
    return (not track.muted and track.volume > 0 and clip.volume > 0
            and bool(assets[clip.asset_id].info.get("has_audio")))


def _visible(track: Track, clip: Clip, assets: Mapping[str, AssetLike]) -> bool:
    return (track.kind == "video" and not track.hidden and clip.visible
            and clip.transform.opacity > 0 and bool(assets[clip.asset_id].info.get("has_video")))


def collect_segments(tl: Timeline, assets: Mapping[str, AssetLike], f0: int, f1: int,
                     fps: float, *, with_audio: bool = True) -> list[Segment]:
    segs: list[Segment] = []
    for track in tl.tracks:
        for clip in track.clips:
            cf0, cf1 = round(clip.start * fps), round(clip.end * fps)
            s0, s1 = max(cf0, f0), min(cf1, f1)
            if s1 <= s0:
                continue
            video = _visible(track, clip, assets)
            audio = with_audio and _audible(track, clip, assets)
            if not (video or audio):
                continue
            segs.append(Segment(track, clip, s0, s1, fps, video, audio,
                                starts_clip=s0 == cf0, ends_clip=s1 == cf1))
    return segs


def plan_chunks(tl: Timeline, assets: Mapping[str, AssetLike], fps: float, max_inputs: int) -> list[ChunkPlan]:
    total = max(1, round(tl.duration * fps))
    # Candidate chunk boundaries: every clip edge.
    edges = sorted({0, total} | {min(max(round(c.start * fps), 0), total) for _, c in tl.all_clips()}
                   | {min(max(round(c.end * fps), 0), total) for _, c in tl.all_clips()})
    chunks: list[ChunkPlan] = []
    start = 0
    while start < total:
        best_end = None
        for e in edges:
            if e <= start:
                continue
            n = len(collect_segments(tl, assets, start, e, fps))
            if n > max_inputs and best_end is not None:
                break
            best_end = e
            if n > max_inputs:
                break  # a single edge-to-edge window already exceeds the limit
        end = best_end or total
        chunks.append(ChunkPlan(start, end, collect_segments(tl, assets, start, end, fps)))
        start = end
    return chunks


def _atempo_chain(speed: float) -> list[str]:
    out = []
    while speed > 100:
        out.append("atempo=100")
        speed /= 100
    while speed < 0.5:
        out.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1) > 1e-6:
        out.append(f"atempo={speed:.6f}")
    return out


def _video_chain(seg: Segment, asset: AssetLike, st: RenderSettings, chunk_t0: float) -> tuple[str, int, int]:
    """Filters for one video segment; returns (chain, x, y) of the overlay position."""
    clip = seg.clip
    tr = clip.transform
    W, H = st.width, st.height
    fps = fps_string(st.fps)
    filters = [f"setpts=(PTS-STARTPTS)/{clip.speed:.6f}", f"fps={fps}",
               f"tpad=stop_mode=clone:stop_duration={seg.duration + 1:.3f}",
               f"trim=duration={seg.duration:.6f}"]
    sw = int(asset.info.get("width") or W)
    sh = int(asset.info.get("height") or H)
    animated = _animated_zoom(clip, sw, sh)
    if tr.crop and not animated:
        cw, ch = _even(sw * tr.crop.w), _even(sh * tr.crop.h)
        cx, cy = int(sw * tr.crop.x), int(sh * tr.crop.y)
        cx, cy = min(cx, max(0, sw - cw)), min(cy, max(0, sh - ch))
        filters.append(f"crop={cw}:{ch}:{cx}:{cy}")
        sw, sh = cw, ch
    bx, by = int(round(tr.x * W)), int(round(tr.y * H))
    bw, bh = _even(tr.w * W), _even(tr.h * H)
    x, y = bx, by

    def resize(ow: int, oh: int) -> str:
        return _zoompan(seg, ow, oh, fps) if animated else f"scale={ow}:{oh}"

    if tr.fit == "stretch":
        filters.append(resize(bw, bh))
    elif tr.fit == "cover":
        scale = max(bw / sw, bh / sh)
        ow, oh = max(bw, _even(sw * scale)), max(bh, _even(sh * scale))
        filters.append(resize(ow, oh))
        filters.append(f"crop={bw}:{bh}:{(ow - bw) // 2}:{(oh - bh) // 2}")
    else:  # contain
        scale = min(bw / sw, bh / sh)
        ow, oh = min(bw, _even(sw * scale)), min(bh, _even(sh * scale))
        filters.append(resize(ow, oh))
        x, y = bx + (bw - ow) // 2, by + (bh - oh) // 2
    filters.append("setsar=1")
    needs_alpha = tr.opacity < 1 or (seg.starts_clip and clip.fade_in > 0) or (seg.ends_clip and clip.fade_out > 0)
    if needs_alpha:
        filters.append("format=yuva420p")
        if seg.starts_clip and clip.fade_in > 0:
            filters.append(f"fade=t=in:st=0:d={min(clip.fade_in, seg.duration):.3f}:alpha=1")
        if seg.ends_clip and clip.fade_out > 0:
            d = min(clip.fade_out, seg.duration)
            filters.append(f"fade=t=out:st={seg.duration - d:.3f}:d={d:.3f}:alpha=1")
        if tr.opacity < 1:
            filters.append(f"colorchannelmixer=aa={tr.opacity:.3f}")
    filters.append(f"setpts=PTS+{seg.t0 - chunk_t0:.6f}/TB")
    return ",".join(filters), x, y


def _animated_zoom(clip: Clip, sw: int, sh: int) -> bool:
    """Animated zooms need a crop with the source's aspect ratio (uniform zoom)."""
    crop = clip.transform.crop
    if crop is None or (clip.zoom_in <= 0 and clip.zoom_out <= 0):
        return False
    src_aspect = sw / sh
    crop_aspect = (crop.w * sw) / (crop.h * sh)
    return abs(crop_aspect - src_aspect) / src_aspect < 0.03


def _zoompan(seg: Segment, ow: int, oh: int, fps: str) -> str:
    """zoompan filter easing from the full frame into the clip's crop (and back)."""
    clip = seg.clip
    crop = clip.transform.crop
    assert crop is not None
    off = seg.t0 - clip.start            # clip-local time of this segment's first frame
    dur = clip.duration
    target = 1.0 / max(crop.w, 1e-3)
    cx, cy = crop.x + crop.w / 2, crop.y + crop.h / 2

    def smooth(var: int, progress: str) -> str:
        return f"st({var},clip({progress},0,1));st({var},ld({var})*ld({var})*(3-2*ld({var})))"

    parts = [smooth(0, f"(it+{off:.6f})/{clip.zoom_in:.6f}") if clip.zoom_in > 0 else "st(0,1)",
             smooth(1, f"({dur:.6f}-it-{off:.6f})/{clip.zoom_out:.6f}") if clip.zoom_out > 0 else "st(1,1)",
             "st(2,min(ld(0),ld(1)))"]
    amount = ";".join(parts)
    z = f"{amount};1+ld(2)*{target - 1:.6f}"
    x = f"{amount};clip((0.5+ld(2)*({cx:.6f}-0.5))*iw-iw/zoom/2,0,iw-iw/zoom)"
    y = f"{amount};clip((0.5+ld(2)*({cy:.6f}-0.5))*ih-ih/zoom/2,0,ih-ih/zoom)"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={ow}x{oh}:fps={fps}"


def _audio_chain(seg: Segment, chunk_t0: float) -> str:
    clip = seg.clip
    filters = ["asetpts=PTS-STARTPTS", f"aresample={AUDIO_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    filters += _atempo_chain(clip.speed)
    n = round(seg.duration * AUDIO_RATE)
    filters += [f"apad=whole_len={n}", f"atrim=end_sample={n}"]
    if seg.track.denoise:
        filters.append("afftdn=nr=14:nf=-45:tn=1")
    if seg.track.enhance:
        filters += ["highpass=f=80", "acompressor=threshold=-20dB:ratio=3:attack=8:release=160:makeup=2"]
    gain = clip.volume * seg.track.volume
    if abs(gain - 1) > 1e-6:
        filters.append(f"volume={gain:.4f}")
    # Short fades at real cuts avoid clicks; longer ones when requested.
    fi = max(clip.fade_in, 0.008) if seg.starts_clip else 0.0
    fo = max(clip.fade_out, 0.008) if seg.ends_clip else 0.0
    if fi:
        filters.append(f"afade=t=in:st=0:d={min(fi, seg.duration / 2):.4f}")
    if fo:
        d = min(fo, seg.duration / 2)
        filters.append(f"afade=t=out:st={seg.duration - d:.4f}:d={d:.4f}")
    delay = round((seg.t0 - chunk_t0) * AUDIO_RATE)
    if delay > 0:
        filters.append(f"adelay={delay}S:all=1")
    return ",".join(filters)


def _font_file() -> Path | None:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return Path(c)
    return None


def build_chunk(
    tl: Timeline,
    assets: Mapping[str, AssetLike],
    chunk: ChunkPlan,
    st: RenderSettings,
    workdir: Path,
    *,
    video_out: str | None,
    audio_out: str | None,
    image_out: str | None = None,
    captions_file: str | None = None,
) -> list[str]:
    """ffmpeg arguments (without the binary) for one chunk.

    Paths of helper files (text, fonts, captions) are relative to ``workdir``,
    which must be the working directory of the ffmpeg process.
    """
    fps = fps_string(st.fps)
    t0 = chunk.f0 / st.fps
    dur = (chunk.f1 - chunk.f0) / st.fps
    n_frames = chunk.f1 - chunk.f0
    args: list[str] = []
    for i, seg in enumerate(chunk.segments):
        seg.input_index = i
        asset = assets[seg.clip.asset_id]
        pad = 0.25 * seg.clip.speed
        if asset.info.get("is_image"):
            args += ["-loop", "1", "-framerate", fps, "-t", f"{seg.duration + 0.5:.6f}", "-i", asset.path]
        else:
            args += ["-ss", f"{seg.src_in:.6f}", "-t", f"{seg.src_dur + pad:.6f}", "-i", asset.path]

    graph: list[str] = []
    want_video = video_out is not None or image_out is not None
    want_audio = audio_out is not None

    if want_video:
        graph.append(f"color=c={tl.canvas.background}:s={st.width}x{st.height}:r={fps}:d={dur + 1:.6f},"
                     f"format=yuv420p,trim=duration={dur:.6f}[base0]")
        cur = "base0"
        k = 0
        video_tracks = [t for t in tl.tracks if t.kind == "video"]
        for track in video_tracks:  # bottom layer first
            for seg in [s for s in chunk.segments if s.video and s.track is track]:
                chain, x, y = _video_chain(seg, assets[seg.clip.asset_id], st, t0)
                graph.append(f"[{seg.input_index}:v:0]{chain}[v{k}]")
                graph.append(f"[{cur}][v{k}]overlay=x={x}:y={y}:eof_action=pass:format=auto[o{k}]")
                cur = f"o{k}"
                k += 1
        post = [f"setpts=PTS+{t0:.6f}/TB"]
        font = _font_file()
        if font is not None and tl.texts:
            local_font = workdir / ("font" + font.suffix)
            if not local_font.exists():
                shutil.copyfile(font, local_font)
        for x in tl.texts:
            if x.end <= t0 or x.start >= t0 + dur:
                continue
            (workdir / f"text_{x.id}.txt").write_text(x.text, encoding="utf-8")
            size = max(8, int(round(x.size * st.height)))
            opts = [f"textfile=text_{x.id}.txt", "expansion=none", f"fontsize={size}",
                    f"fontcolor={x.color}", f"x=w*{x.x:.4f}-text_w/2", f"y=h*{x.y:.4f}-text_h/2",
                    f"enable='between(t,{x.start:.3f},{x.end:.3f})'"]
            if font is not None:
                opts.insert(0, "fontfile=font" + font.suffix)
            if x.box:
                opts += ["box=1", f"boxcolor={x.box_color}", f"boxborderw={max(4, size // 3)}"]
            post.append("drawtext=" + ":".join(opts))
        if captions_file:
            post.append(f"ass={captions_file}")
        post += ["setpts=PTS-STARTPTS", "format=yuv420p"]
        graph.append(f"[{cur}]{','.join(post)}[vout]")

    if want_audio:
        n = max(1, round(dur * AUDIO_RATE))
        voice, music = [], []
        for j, seg in enumerate(s for s in chunk.segments if s.audio):
            stream = getattr(assets[seg.clip.asset_id], "audio_stream", 0)
            graph.append(f"[{seg.input_index}:a:{stream}]{_audio_chain(seg, t0)}[a{j}]")
            (music if seg.track.duck else voice).append(f"[a{j}]")

        def bus(labels: list[str], name: str) -> str:
            graph.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=end_sample={n}[{name}_sil]")
            ins = [f"[{name}_sil]"] + labels
            graph.append(f"{''.join(ins)}amix=inputs={len(ins)}:duration=first:normalize=0:dropout_transition=0[{name}]")
            return f"[{name}]"

        if voice and music:
            v = bus(voice, "voice")
            m = bus(music, "music")
            graph.append(f"{v}asplit=2[vmix][vkey]")
            graph.append(f"{m}[vkey]sidechaincompress=threshold=0.02:ratio={st.duck_amount}:attack=20:"
                         f"release=400:makeup=1[ducked]")
            graph.append("[vmix][ducked]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            out = bus(voice + music, "mix")
            graph.append(f"{out}anull[aout]")

    args += ["-filter_complex", ";".join(graph)]
    if image_out is not None:
        args += ["-map", "[vout]", "-frames:v", "1", "-q:v", "3", "-update", "1", "-y", image_out]
        return args
    if video_out is not None:
        args += ["-map", "[vout]", "-c:v", "libx264", "-preset", st.preset, "-crf", str(st.crf),
                 "-pix_fmt", "yuv420p", "-r", fps, "-frames:v", str(n_frames),
                 "-g", str(max(1, int(round(st.fps * 2)))), "-an", "-y", video_out]
    if audio_out is not None:
        args += ["-map", "[aout]", "-c:a", "pcm_s16le", "-ar", str(AUDIO_RATE), "-ac", "2", "-y", audio_out]
    return args


ProgressFn = Callable[[int, int], None]


def render_timeline(
    tl: Timeline,
    assets: Mapping[str, AssetLike],
    out_path: str | Path,
    settings: RenderSettings,
    *,
    caption_words: list[TimelineWord] | None = None,
    progress: ProgressFn | None = None,
    workdir: str | Path | None = None,
) -> Path:
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if tl.duration <= 0:
        raise ValueError("the timeline is empty")
    tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="ave_render_"))
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        captions_file = _write_captions(tl, caption_words, settings, tmp)
        chunks = plan_chunks(tl, assets, settings.fps, settings.max_inputs)
        vlist, alist = [], []
        for i, chunk in enumerate(chunks):
            if progress:
                progress(i, len(chunks))
            v, a = f"chunk_{i:04d}.mp4", f"chunk_{i:04d}.wav"
            args = build_chunk(tl, assets, chunk, settings, tmp, video_out=v, audio_out=a,
                               captions_file=captions_file)
            run_ffmpeg(args, cwd=tmp)
            vlist.append(v)
            alist.append(a)
        if progress:
            progress(len(chunks), len(chunks))
        (tmp / "video.txt").write_text("".join(f"file '{v}'\n" for v in vlist), encoding="utf-8")
        (tmp / "audio.txt").write_text("".join(f"file '{a}'\n" for a in alist), encoding="utf-8")
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", "video.txt", "-c", "copy", "-y", "video.mp4"], cwd=tmp)
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", "audio.txt", "-c", "copy", "-y", "audio.wav"], cwd=tmp)
        audio_filter = loudnorm_filter(tmp / "audio.wav", tl.audio) if tl.audio.normalize else None
        mux = ["-i", "video.mp4", "-i", "audio.wav", "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
        if audio_filter:
            mux += ["-af", audio_filter, "-ar", str(AUDIO_RATE)]
        mux += ["-c:a", "aac", "-b:a", settings.audio_bitrate, "-movflags", "+faststart", "-y", str(out_path)]
        run_ffmpeg(mux, cwd=tmp)
    finally:
        if not settings.keep_work and not workdir:
            shutil.rmtree(tmp, ignore_errors=True)
    return out_path


def measure_loudness(wav: Path, target: AudioMaster) -> dict | None:
    """First loudnorm pass: measure integrated loudness, true peak and range."""
    proc = run_ffmpeg(["-i", str(wav), "-af",
                       f"loudnorm=I={target.target_lufs}:TP={target.true_peak}:LRA=11:print_format=json",
                       "-f", "null", "-"], loglevel="info")
    text = proc.stderr.decode("utf-8", "replace")
    start, end = text.rfind("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def loudnorm_filter(wav: Path, target: AudioMaster) -> str | None:
    """Second-pass loudnorm filter (linear gain) or None for (near) silent mixes."""
    m = measure_loudness(wav, target)
    if not m:
        return None
    try:
        measured_i = float(m["input_i"])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(measured_i) or measured_i < -70:
        return None
    return (f"loudnorm=I={target.target_lufs}:TP={target.true_peak}:LRA=11:"
            f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
            f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true:print_format=none")


def _write_captions(tl: Timeline, words: list[TimelineWord] | None, st: RenderSettings,
                    workdir: Path) -> str | None:
    if not (tl.captions.enabled and words):
        return None
    (workdir / "captions.ass").write_text(build_ass(words, tl.captions, st.width, st.height), encoding="utf-8")
    return "captions.ass"


def render_still(
    tl: Timeline,
    assets: Mapping[str, AssetLike],
    t: float,
    out_path: str | Path,
    settings: RenderSettings,
    *,
    caption_words: list[TimelineWord] | None = None,
) -> Path:
    """Composite a single frame of the timeline at time ``t``."""
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = max(1, round(tl.duration * settings.fps))
    f = min(max(0, int(math.floor(t * settings.fps + 1e-6))), total - 1)
    with tempfile.TemporaryDirectory(prefix="ave_still_") as tmp:
        tmpd = Path(tmp)
        captions_file = _write_captions(tl, caption_words, settings, tmpd)
        chunk = ChunkPlan(f, f + 1, collect_segments(tl, assets, f, f + 1, settings.fps, with_audio=False))
        args = build_chunk(tl, assets, chunk, settings, tmpd, video_out=None, audio_out=None,
                           image_out=str(out_path), captions_file=captions_file)
        run_ffmpeg(args, cwd=tmpd)
    return out_path


def estimate_render_seconds(tl: Timeline, preview: bool) -> float:
    """Very rough wall-clock estimate used for progress messages."""
    return tl.duration * (0.15 if preview else 0.8) + 2.0


__all__ = ["RenderSettings", "render_timeline", "render_still", "plan_chunks", "build_chunk",
           "estimate_render_seconds", "fps_string"]
