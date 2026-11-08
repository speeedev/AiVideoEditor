import json
import subprocess
from types import SimpleNamespace as NS

import numpy as np
import pytest
from PIL import Image

from aivideoeditor.agent.tools import run_tool
from aivideoeditor.analysis.audio import decode_audio
from aivideoeditor.media.ffmpeg import run_ffmpeg
from aivideoeditor.media.probe import probe
from aivideoeditor.pipeline import sync_project
from aivideoeditor.render.engine import RenderSettings, _video_chain, collect_segments, render_timeline
from aivideoeditor.timeline import Rect, layouts


def _frames(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def _lufs(path):
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                          capture_output=True, text=True)
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip().startswith("I:")]
    return float(lines[-1].split()[1])


def test_animated_zoom_uses_zoompan_and_renders(session, tmp_path):
    tl = session.timeline
    layouts.zoom_range(tl, "V1", 2, 5, Rect(x=0.1, y=0.1, w=0.4, h=0.4), source_aspect=16 / 9, ease=0.5)
    session.commit()
    clip = tl.track("V1").clip_at(3)
    assert clip.zoom_in == 0.5 and clip.zoom_out == 0.5
    st = RenderSettings.for_timeline(tl, preview=True, preview_width=320)
    seg = next(s for s in collect_segments(tl, session.project.assets, 0, round(tl.duration * 30), 30)
               if s.clip.id == clip.id)
    chain, _, _ = _video_chain(seg, session.project.asset("screen"), st, 0.0)
    assert "zoompan=" in chain and "crop=" not in chain.split("zoompan")[0]
    st.max_inputs = 3
    out = render_timeline(tl, session.project.assets, tmp_path / "z.mp4", st)
    assert _frames(out) == round(tl.duration * 30)
    # A split inside the zoom keeps the ease only on the outer edges.
    from aivideoeditor.timeline import ops
    ops.split_at(tl, 3.5)
    left, right = tl.track("V1").clip_at(3), tl.track("V1").clip_at(4)
    assert (left.zoom_in, left.zoom_out, right.zoom_in, right.zoom_out) == (0.5, 0, 0, 0.5)


def test_loudness_normalisation(session, tmp_path):
    tl = session.timeline
    st = RenderSettings.for_timeline(tl, preview=True, preview_width=320)
    out = render_timeline(tl, session.project.assets, tmp_path / "loud.mp4", st)
    assert abs(_lufs(out) - (-14.0)) < 1.5
    tl.audio.normalize = False
    out2 = render_timeline(tl, session.project.assets, tmp_path / "raw.mp4", st)
    assert abs(_lufs(out2) - (-14.0)) > 2.0


def test_denoise_enhance_and_mastering_tools(session, tmp_path):
    ctx = NS(session=session)
    out, err = run_tool(ctx, "set_track", {"track_id": "A1", "denoise": True, "enhance": True})
    assert not err and "denoise=True" in out
    out, err = run_tool(ctx, "set_audio_mastering", {"normalize": True, "target_lufs": -16})
    assert not err
    st = RenderSettings.for_timeline(session.timeline, preview=True, preview_width=320)
    out = render_timeline(session.timeline, session.project.assets, tmp_path / "dn.mp4", st)
    assert abs(_lufs(out) - (-16.0)) < 1.5


def test_png_logo_overlay(session, tmp_path):
    logo = tmp_path / "logo.png"
    img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    for x in range(200):
        for y in range(100):
            if (x - 100) ** 2 / 90 ** 2 + (y - 50) ** 2 / 40 ** 2 < 1:
                img.putpixel((x, y), (255, 0, 0, 255))
    img.save(logo)
    info = probe(logo)
    assert info.is_image and info.width == 200
    asset = session.project.add_asset(logo, "other", "logo")
    ctx = NS(session=session)
    out, err = run_tool(ctx, "add_overlay", {"asset_id": asset.id, "start": 1, "end": 4, "position": "top_left",
                                             "size": 0.3, "fade": 0})
    assert not err, out
    track = session.timeline.video_tracks[-1]
    assert track.role == "overlay"
    still = session.still(2.0, width=640)
    px = Image.open(still).convert("RGB")
    w, h = px.size
    r, g, b = px.getpixel((int(w * 0.03 / (16 / 9) + w * 0.15), int(h * 0.03 + w * 0.3 * 0.5 / 2)))
    assert r > 180 and g < 80 and b < 80              # inside the red logo
    r2, g2, b2 = px.getpixel((int(w * 0.03 / (16 / 9)) + 2, int(h * 0.03) + 2))
    assert not (r2 > 180 and g2 < 80)                  # transparent corner shows the screen


def test_second_audio_stream(session, tmp_path, demo_media):
    # An OBS-style file: video + desktop audio (noise) on stream 0 + microphone on stream 1.
    mkv = tmp_path / "obs.mkv"
    run_ffmpeg(["-i", str(demo_media.screen), "-f", "lavfi", "-i", "anoisesrc=d=12:a=0.05",
                "-i", str(demo_media.mic), "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "copy", "-c:a", "aac", "-shortest", "-y", str(mkv)])
    info = probe(mkv)
    assert info.audio_streams == 2
    p = session.project
    a = p.add_asset(mkv, "camera", audio_stream=1)
    assert a.id.endswith("_a1")
    with pytest.raises(ValueError):
        p.add_asset(mkv, "camera", audio_stream=5)
    from aivideoeditor.analysis.sync import find_offset_arrays
    ref = decode_audio(demo_media.mic, 8000)
    on_mic = find_offset_arrays(ref, decode_audio(mkv, 8000, stream=1), 8000)
    on_noise = find_offset_arrays(ref, decode_audio(mkv, 8000, stream=0), 8000)
    assert on_mic.reliable and abs(on_mic.offset) < 0.03   # (AAC priming delay is a few ms)
    assert not on_noise.reliable
    res = sync_project(p, log=lambda m: None)
    assert abs(res[a.id].offset) < 0.03      # same clock as the mic
