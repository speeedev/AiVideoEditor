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
from typing import TYPE_CHECKING, Protocol

from ..media.ffmpeg import run_ffmpeg
from ..timeline.mapping import TimelineWord
from ..timeline.model import AudioMaster, Clip, TextOverlay, Timeline, Track
from .captions import build_ass
from .titles import animation_duration, render_text_image

if TYPE_CHECKING:  # pragma: no cover
    from .textlayer import TextLayer

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
    proxies: dict = field(default_factory=dict)  # asset id -> Proxy (low-res video used for previews)

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
    cont_prev: bool = False   # the previous clip on the track continues the same audio seamlessly
    cont_next: bool = False
    tail: bool = False        # the continuing end of the previous clip under a crossfade
    video_input: int = -1
    audio_input: int = -1
    music_input: int = -1
    audio_stream: int | None = None

    @property
    def t0(self) -> float:
        return self.f0 / self.fps

    @property
    def lead(self) -> float:
        """Timeline seconds between the segment's first frame and the clip's first source sample.

        Non-zero only when a clip starts between frame boundaries (e.g. at a sync offset)."""
        return max(0.0, -self.clip.source_time(self.t0)) / self.clip.speed

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


def _continues(a: Clip, b: Clip) -> bool:
    """True when clip ``b`` picks up exactly where ``a`` left off (a split, not a cut)."""
    return (a.asset_id == b.asset_id and abs(a.end - b.start) < 1e-3
            and abs(a.source_out - b.source_in) < 1e-3 and abs(a.speed - b.speed) < 1e-9)


def collect_segments(tl: Timeline, assets: Mapping[str, AssetLike], f0: int, f1: int,
                     fps: float, *, with_audio: bool = True) -> list[Segment]:
    segs: list[Segment] = []
    for track in tl.tracks:
        clips = sorted(track.clips, key=lambda c: c.start)
        for i, clip in enumerate(clips):
            prev = clips[i - 1] if i > 0 and abs(clips[i - 1].end - clip.start) < 1.5 / fps else None
            xd = crossfade_length(clip, prev)
            if xd:
                # A crossfade fades this clip in over the continuing tail of the previous one.
                tf0, tf1 = round(clip.start * fps), round((clip.start + xd) * fps)
                t0, t1 = max(tf0, f0), min(tf1, f1)
                tail = prev.model_copy(update={"source_out": prev.source_out + xd * prev.speed, "fade_in": 0.0,
                                               "fade_out": xd, "transition": "none"})
                tv = _visible(track, prev, assets)
                ta = with_audio and _audible(track, prev, assets)
                if t1 > t0 and (tv or ta):
                    segs.append(Segment(track, tail, t0, t1, fps, tv, ta, starts_clip=False, ends_clip=False,
                                        tail=True))
                clip = clip.model_copy(update={"fade_in": max(clip.fade_in, xd)})
            cf0, cf1 = round(clip.start * fps), round(clip.end * fps)
            s0, s1 = max(cf0, f0), min(cf1, f1)
            if s1 <= s0:
                continue
            video = _visible(track, clip, assets)
            audio = with_audio and _audible(track, clip, assets)
            if not (video or audio):
                continue
            segs.append(Segment(track, clip, s0, s1, fps, video, audio,
                                starts_clip=s0 == cf0, ends_clip=s1 == cf1,
                                cont_prev=i > 0 and _continues(clips[i - 1], clip),
                                cont_next=i + 1 < len(clips) and _continues(clip, clips[i + 1])))
    return segs


def crossfade_length(clip: Clip, prev: Clip | None) -> float:
    """Length of the crossfade into ``clip`` (0 when there is none)."""
    if clip.transition != "crossfade" or prev is None:
        return 0.0
    return max(0.0, min(clip.transition_duration, clip.duration / 2))


def _fade_windows(tl: Timeline, fps: float) -> list[tuple[int, int]]:
    """Frame ranges a chunk boundary must not fall inside (video fades are per segment)."""
    out: list[tuple[int, int]] = []
    for t in tl.video_tracks:
        for c in t.clips:
            cf0, cf1 = round(c.start * fps), round(c.end * fps)
            if c.fade_in > 0:
                out.append((cf0, cf0 + math.ceil(c.fade_in * fps)))
            if c.fade_out > 0:
                out.append((cf1 - math.ceil(c.fade_out * fps), cf1))
    for t in tl.tracks:
        clips = sorted(t.clips, key=lambda c: c.start)
        for prev, c in zip(clips, clips[1:]):
            xd = crossfade_length(c, prev if abs(prev.end - c.start) < 1.5 / fps else None)
            if xd:
                out.append((round(c.start * fps), round(c.start * fps) + math.ceil(xd * fps)))
    for x in tl.texts:
        d = animation_duration(x)
        if d > 0:
            out.append((round(x.start * fps), round(x.start * fps) + math.ceil(d * fps)))
            out.append((round(x.end * fps) - math.ceil(d * fps), round(x.end * fps)))
    return out


def plan_chunks(tl: Timeline, assets: Mapping[str, AssetLike], fps: float, max_inputs: int) -> list[ChunkPlan]:
    total = max(1, round(tl.duration * fps))
    # Candidate chunk boundaries: every clip edge, except inside video fades.
    edges = sorted({0, total} | {min(max(round(c.start * fps), 0), total) for _, c in tl.all_clips()}
                   | {min(max(round(c.end * fps), 0), total) for _, c in tl.all_clips()})
    windows = _fade_windows(tl, fps)
    edges = [e for e in edges if e in (0, total) or not any(a < e < b for a, b in windows)]
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


def _video_chain(seg: Segment, asset: AssetLike, st: RenderSettings, chunk_t0: float,
                 dims: tuple[int, int] | None = None) -> tuple[str, int, int]:
    """Filters for one video segment; returns (chain, x, y) of the overlay position.

    ``dims`` is the pixel size of the decoded input (a proxy may be smaller
    than the original asset).
    """
    clip = seg.clip
    tr = clip.transform
    W, H = st.width, st.height
    fps = fps_string(st.fps)
    # Timestamps after input seeking are relative to the seek point and consistent across the
    # file's streams; keep them (instead of resetting each stream to 0) so audio/video offsets
    # inside the file and sub-frame positions are honoured. fps=start_time=0 pads the start.
    lead = f"+{seg.lead:.6f}/TB" if seg.lead > 1e-6 else ""
    filters = [f"setpts=PTS/{clip.speed:.6f}{lead}", f"fps={fps}:start_time=0",
               f"tpad=stop_mode=clone:stop_duration={seg.duration + 1:.3f}",
               f"trim=duration={seg.duration:.6f}"]
    if dims:
        sw, sh = dims
    else:
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
    filters += color_filters(clip)
    filters.append("setsar=1")
    # Fades are evaluated in clip time, so a segment that starts inside a fade (a render chunk
    # boundary, or a single still frame) gets the right opacity.
    off = seg.t0 - clip.start
    fade_in = clip.fade_in > 0 and not seg.tail and off < clip.fade_in
    fade_out = clip.fade_out > 0 and not seg.tail and off + seg.duration > clip.duration - clip.fade_out
    if tr.opacity < 1 or fade_in or fade_out:
        filters.append("format=yuva420p")
        if fade_in or fade_out:
            filters.append(f"setpts=PTS+{off:.6f}/TB")
            if fade_in:
                filters.append(f"fade=t=in:st=0:d={clip.fade_in:.4f}:alpha=1")
            if fade_out:
                filters.append(f"fade=t=out:st={max(0.0, clip.duration - clip.fade_out):.4f}:"
                               f"d={clip.fade_out:.4f}:alpha=1")
            filters.append(f"setpts=PTS-{off:.6f}/TB")
        if tr.opacity < 1:
            filters.append(f"colorchannelmixer=aa={tr.opacity:.3f}")
    filters.append(f"setpts=PTS+{seg.t0 - chunk_t0:.6f}/TB")
    return ",".join(filters), x, y


def color_filters(clip: Clip) -> list[str]:
    g = clip.color
    if g is None or g.neutral:
        return []
    out = []
    if abs(g.brightness) > 1e-3 or abs(g.contrast - 1) > 1e-3 or abs(g.saturation - 1) > 1e-3:
        out.append(f"eq=brightness={g.brightness:.3f}:contrast={g.contrast:.3f}:saturation={g.saturation:.3f}")
    if abs(g.warmth) > 1e-3:
        out.append(f"colortemperature=temperature={6500 - g.warmth * 2500:.0f}:pl=0.5")
    return out


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


MICRO_FADE = 0.008  # seconds; removes clicks at hard audio cuts


def _audio_chain(seg: Segment, chunk_t0: float, gain: float = 1.0) -> str:
    clip = seg.clip
    # first_pts=0 pads with silence when the file's audio starts after the seek point
    # (e.g. camera files whose audio stream begins a few hundred ms after the video).
    filters = [f"aresample={AUDIO_RATE}:async=1:first_pts=0", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    filters += _atempo_chain(clip.speed)
    n = round(seg.duration * AUDIO_RATE)
    filters += [f"apad=whole_len={n}", f"atrim=end_sample={n}"]
    if seg.track.denoise:
        filters.append("afftdn=nr=14:nf=-45:tn=1")
    if seg.track.enhance:
        filters += ["highpass=f=80", "acompressor=threshold=-20dB:ratio=3:attack=8:release=160:makeup=2"]
    gain = gain * clip.volume * seg.track.volume
    # Requested fades are computed in clip time so they stay correct when a render chunk
    # boundary falls inside the fade.
    off = seg.t0 - clip.start
    terms = [f"{gain:.4f}"]
    if clip.fade_in > 0.05:
        terms.append(f"min(1,max(0,(t+{off:.6f})/{clip.fade_in:.4f}))")
    if clip.fade_out > 0.05:
        terms.append(f"min(1,max(0,({clip.duration:.6f}-t-{off:.6f})/{clip.fade_out:.4f}))")
    if len(terms) > 1:
        filters.append(f"volume='{'*'.join(terms)}':eval=frame")
    elif abs(gain - 1) > 1e-6:
        filters.append(f"volume={gain:.4f}")
    # Micro-fades only at real cuts (not where a split continues the same audio).
    if seg.starts_clip and not seg.cont_prev and clip.fade_in <= 0.05:
        d = min(max(clip.fade_in, MICRO_FADE), seg.duration / 2)
        filters.append(f"afade=t=in:st=0:d={d:.4f}")
    if seg.ends_clip and not seg.cont_next and clip.fade_out <= 0.05:
        d = min(max(clip.fade_out, MICRO_FADE), seg.duration / 2)
        filters.append(f"afade=t=out:st={seg.duration - d:.4f}:d={d:.4f}")
    delay = round((seg.t0 - chunk_t0 + seg.lead) * AUDIO_RATE)
    if delay > 0:
        filters.append(f"adelay={delay}S:all=1")
    return ",".join(filters)


def _text_key(x: TextOverlay) -> str:
    import hashlib
    return hashlib.sha1(x.model_dump_json().encode("utf-8")).hexdigest()[:10]


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
    text_layer: "TextLayer | None" = None,
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
    n_inputs = 0
    dims: dict[int, tuple[int, int]] = {}

    def add_input(seg: Segment, path: str, is_image: bool, shift: float = 0.0) -> int:
        """``shift``: the input file's time 0 is at this asset time (proxies of files whose
        video starts after their audio)."""
        nonlocal args, n_inputs
        if is_image:
            args += ["-loop", "1", "-framerate", fps, "-t", f"{seg.duration + 0.5:.6f}", "-i", path]
        else:
            info = assets[seg.clip.asset_id].info
            if info.get("has_alpha") and info.get("video_codec") in ("vp9", "vp8") and path.endswith(".webm"):
                # FFmpeg's built-in VP8/VP9 decoders drop the alpha channel; libvpx keeps it.
                args += ["-c:v", "libvpx-vp9" if info["video_codec"] == "vp9" else "libvpx"]
            pad = 0.25 * seg.clip.speed
            ss = seg.src_in - shift
            if ss < 0:
                args += ["-itsoffset", f"{-ss:.6f}"]
                ss = 0.0
            args += ["-ss", f"{ss:.6f}", "-t", f"{seg.src_dur + pad:.6f}", "-i", path]
        n_inputs += 1
        return n_inputs - 1

    want_video = video_out is not None or image_out is not None
    want_audio = audio_out is not None
    for seg in chunk.segments:
        asset = assets[seg.clip.asset_id]
        is_image = bool(asset.info.get("is_image"))
        proxy = st.proxies.get(seg.clip.asset_id) if want_video and seg.video else None
        video_path = getattr(asset, "video_path", asset.path)
        if want_video and seg.video:
            shift = float(asset.info.get("video_start") or 0.0) if proxy else 0.0
            seg.video_input = add_input(seg, proxy.path if proxy else video_path, is_image, shift)
            if proxy:
                dims[seg.video_input] = (proxy.width, proxy.height)
        if want_audio and seg.audio:
            if getattr(asset, "uses_voice", False):
                seg.audio_input = add_input(seg, asset.voice_path, False)
                seg.audio_stream = 0
                if asset.music_level > 0 and asset.music_path:
                    seg.music_input = add_input(seg, asset.music_path, False)
            elif seg.video_input >= 0 and proxy is None and video_path == asset.path:
                seg.audio_input = seg.video_input
            else:
                seg.audio_input = add_input(seg, asset.path, is_image)

    text_inputs: list[tuple[TextOverlay, int]] = []
    if want_video:
        for x in tl.texts:
            if x.end <= t0 or x.start >= t0 + dur:
                continue
            png = workdir / f"text_{x.id}_{st.width}x{st.height}_{_text_key(x)}.png"
            if not png.exists():
                render_text_image(x, st.width, st.height).save(png, compress_level=1)
            args += ["-loop", "1", "-framerate", fps, "-t", f"{dur + 0.5:.6f}", "-i", png.name]
            text_inputs.append((x, n_inputs))
            n_inputs += 1

    layer_input = None
    if text_layer is not None and want_video:
        name = text_layer.concat_list(t0, t0 + dur, f"textlayer_{chunk.f0}_{chunk.f1}.txt")
        if name:
            args += ["-f", "concat", "-safe", "0", "-i", name]
            layer_input = n_inputs
            n_inputs += 1

    graph: list[str] = []

    if want_video:
        graph.append(f"color=c={tl.canvas.background}:s={st.width}x{st.height}:r={fps}:d={dur + 1:.6f},"
                     f"format=yuv420p,trim=duration={dur:.6f}[base0]")
        cur = "base0"
        k = 0
        video_tracks = [t for t in tl.tracks if t.kind == "video"]
        for track in video_tracks:  # bottom layer first
            for seg in [s for s in chunk.segments if s.video and s.track is track]:
                chain, x, y = _video_chain(seg, assets[seg.clip.asset_id], st, t0, dims.get(seg.video_input))
                graph.append(f"[{seg.video_input}:v:0]{chain}[v{k}]")
                graph.append(f"[{cur}][v{k}]overlay=x={x}:y={y}:eof_action=pass:format=auto[o{k}]")
                cur = f"o{k}"
                k += 1
        for j, (x, idx) in enumerate(text_inputs):
            ts, te = x.start - t0, x.end - t0
            d = animation_duration(x)
            chain = ["format=rgba"]
            if d > 0 and ts >= 0:
                chain.append(f"fade=t=in:st={ts:.4f}:d={d:.4f}:alpha=1")
            if d > 0 and te <= dur + 1e-6:
                chain.append(f"fade=t=out:st={max(0.0, te - d):.4f}:d={d:.4f}:alpha=1")
            chain.append("setpts=PTS-STARTPTS")
            graph.append(f"[{idx}:v:0]{','.join(chain)}[t{j}]")
            ox, oy = "0", "0"
            if x.animation == "slide_up" and d > 0:
                oy = f"'if(lt(t,{ts + d:.4f}),max(0,1-(t-{ts:.4f})/{d:.4f})*main_h*0.05,0)'"
            elif x.animation == "slide_left" and d > 0:
                ox = f"'if(lt(t,{ts + d:.4f}),max(0,1-(t-{ts:.4f})/{d:.4f})*main_w*0.05,0)'"
            graph.append(f"[{cur}][t{j}]overlay=x={ox}:y={oy}:format=auto:"
                         f"enable='between(t,{ts:.4f},{te:.4f})'[ot{j}]")
            cur = f"ot{j}"
        if layer_input is not None:
            # Captions drawn by Pillow: one image per caption state, timed by the concat list.
            graph.append(f"[{layer_input}:v:0]format=rgba,setpts=PTS-STARTPTS[txt]")
            graph.append(f"[{cur}][txt]overlay=0:0:format=auto[otxt]")
            cur = "otxt"
        post = [f"setpts=PTS+{t0:.6f}/TB"]
        if captions_file:
            post.append(f"ass={captions_file}")
        post += ["setpts=PTS-STARTPTS", "format=yuv420p"]
        graph.append(f"[{cur}]{','.join(post)}[vout]")

    if want_audio:
        n = max(1, round(dur * AUDIO_RATE))
        voice, music = [], []
        for j, seg in enumerate(s for s in chunk.segments if s.audio):
            asset = assets[seg.clip.asset_id]
            stream = seg.audio_stream if seg.audio_stream is not None else getattr(asset, "audio_stream", 0)
            graph.append(f"[{seg.audio_input}:a:{stream}]{_audio_chain(seg, t0)}[a{j}]")
            bus = music if seg.track.duck else voice
            bus.append(f"[a{j}]")
            if seg.music_input >= 0:     # separated music kept at a lower level under the voice
                graph.append(f"[{seg.music_input}:a:0]{_audio_chain(seg, t0, gain=asset.music_level)}[a{j}m]")
                bus.append(f"[a{j}m]")

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
        layer = _text_layer(tl, caption_words, settings, tmp)
        captions_file = None if layer else _write_captions(tl, caption_words, settings, tmp)
        chunks = plan_chunks(tl, assets, settings.fps, settings.max_inputs)
        vlist, alist = [], []
        for i, chunk in enumerate(chunks):
            if progress:
                progress(i, len(chunks))
            v, a = f"chunk_{i:04d}.mp4", f"chunk_{i:04d}.wav"
            args = build_chunk(tl, assets, chunk, settings, tmp, video_out=v, audio_out=a,
                               captions_file=captions_file, text_layer=layer)
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


def _text_layer(tl: Timeline, words: list[TimelineWord] | None, st: RenderSettings,
                workdir: Path) -> "TextLayer | None":
    """A Pillow caption layer when this FFmpeg cannot draw subtitles itself (libass)."""
    if not (tl.captions.enabled and words):
        return None
    from .textlayer import TextLayer, use_pillow_text
    if not use_pillow_text():
        return None
    layer = TextLayer(tl, words, st.width, st.height, workdir)
    return layer if layer.visible else None


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
        layer = _text_layer(tl, caption_words, settings, tmpd)
        captions_file = None if layer else _write_captions(tl, caption_words, settings, tmpd)
        chunk = ChunkPlan(f, f + 1, collect_segments(tl, assets, f, f + 1, settings.fps, with_audio=False))
        args = build_chunk(tl, assets, chunk, settings, tmpd, video_out=None, audio_out=None,
                           image_out=str(out_path), captions_file=captions_file, text_layer=layer)
        run_ffmpeg(args, cwd=tmpd)
    return out_path


def estimate_render_seconds(tl: Timeline, preview: bool) -> float:
    """Very rough wall-clock estimate used for progress messages."""
    return tl.duration * (0.15 if preview else 0.8) + 2.0


__all__ = ["RenderSettings", "render_timeline", "render_still", "plan_chunks", "build_chunk",
           "estimate_render_seconds", "fps_string"]
