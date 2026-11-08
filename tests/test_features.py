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


def test_captions_skip_muted_parts_and_srt(session, tmp_path):
    from aivideoeditor.export import export_timeline
    from aivideoeditor.timeline import ops
    tl = session.timeline
    all_words = session.words()
    ops.speed_range(tl, 2.0, 6.0, 4)          # muted fast-forward
    ops.set_captions(tl, enabled=True, source_asset="mic")
    session.commit()
    heard = session.caption_words()
    assert 0 < len(heard) < len(all_words)
    assert not any(2.0 <= w.start < 3.0 for w in heard)
    srt = export_timeline(session.project, "srt", tmp_path / "x.srt").read_text()
    assert srt.startswith("1\n00:00:") and "-->" in srt


def test_save_text_tool(session):
    ctx = NS(session=session)
    out, err = run_tool(ctx, "save_text", {"filename": "../../evil chapters", "content": "00:00 Giriş"})
    assert not err
    saved = session.project.root / "exports" / "evil_chapters.txt"
    assert saved.read_text(encoding="utf-8") == "00:00 Giriş"


def test_proxies_for_preview(tmp_path):
    from aivideoeditor.demo import make_demo
    from aivideoeditor.media.proxy import load_proxies
    from aivideoeditor.pipeline import analyze_project
    from aivideoeditor.project import Project
    from aivideoeditor.session import EditorSession
    media = make_demo(tmp_path / "m", duration=4.0, size=(1280, 720), cam_size=(1280, 720))
    p = Project.create(tmp_path / "p", width=1280, height=720)
    p.add_asset(media.screen, "screen")
    p.add_asset(media.mic, "mic")
    analyze_project(p, do_transcribe=False, log=lambda m: None)
    proxies = load_proxies(p)
    assert set(proxies) == {"screen"} and proxies["screen"].height == 360
    s = EditorSession(p)
    s.assemble()
    a = np.asarray(Image.open(s.still(1.0, width=640)).convert("L"), dtype=float)
    p_no = Project.open(p.root)
    (p.root / "cache" / "proxies" / "manifest.json").unlink()
    b = np.asarray(Image.open(EditorSession(p_no).still(1.0, width=640)).convert("L"), dtype=float)
    assert a.shape == b.shape and np.abs(a - b).mean() < 6   # same picture, just from the proxy
    out = s.render(preview=True)
    assert _frames(out) == round(s.timeline.duration * 30)


def _flash_and_beep(path):
    """(time of the first bright frame, time of the first loud audio sample) of a file."""
    from aivideoeditor.analysis.visual import decode_thumbs
    frames = decode_thumbs(path, 30)
    bright = int(np.argmax(frames.mean(axis=(1, 2)) > 0.5))
    audio = decode_audio(path, 48000)
    loud = int(np.argmax(np.abs(audio) > 0.2))
    return bright / 30, loud / 48000


def test_audio_stream_starting_late_stays_in_sync(tmp_path):
    """A camera file whose audio stream starts 0.28 s after its video (common on phones)."""
    from aivideoeditor.project import Project
    from aivideoeditor.session import EditorSession
    from aivideoeditor.timeline import ops
    v = tmp_path / "v.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=5",
                "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,2,2.1)'",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", str(v)])
    a = tmp_path / "a.wav"   # beep at 1.72 s of the audio stream = 2.0 s of the file after the offset
    run_ffmpeg(["-f", "lavfi", "-i", "sine=f=1000:d=4.7:sample_rate=48000",
                "-af", "volume='if(between(t,1.72,1.82),6,0)':eval=frame", "-y", str(a)])
    cam = tmp_path / "cam.mp4"
    run_ffmpeg(["-i", str(v), "-itsoffset", "0.28", "-i", str(a), "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-y", str(cam)])
    assert probe(cam).audio_start > 0.2
    flash, beep = _flash_and_beep(cam)
    assert abs(flash - 2.0) < 0.04 and abs(beep - 2.0) < 0.03     # analysis sees file time

    p = Project.create(tmp_path / "p", width=320, height=180)
    p.add_asset(cam, "camera")
    s = EditorSession(p)
    s.assemble()
    s.timeline.audio.normalize = False
    out = s.render(preview=False)
    flash, beep = _flash_and_beep(out)
    assert abs(flash - beep) < 0.035, (flash, beep)
    # Same after a cut that makes the clip start mid-file.
    ops.ripple_delete(s.timeline, [(0.0, 1.0)])
    s.commit()
    out = s.render(preview=False)
    flash, beep = _flash_and_beep(out)
    assert abs(flash - 1.0) < 0.04 and abs(flash - beep) < 0.035, (flash, beep)


def test_split_does_not_dip_continuous_audio(session, tmp_path):
    from aivideoeditor.timeline import ops
    tl = session.timeline
    tl.audio.normalize = False
    before = decode_audio(session.project.asset("mic").path, 48000)
    ops.split_at(tl, 3.0)
    session.commit()
    st = RenderSettings.for_timeline(tl, preview=True, preview_width=320)
    render_timeline(tl, session.project.assets, tmp_path / "s.mp4", st, workdir=tmp_path / "w")
    got = decode_audio(tmp_path / "w" / "audio.wav", 48000)
    i = 3 * 48000
    assert np.abs(got[i - 480:i + 480] - before[i - 480:i + 480]).max() < 0.01


def test_long_audio_fade_survives_chunk_boundaries(session, tmp_path):
    from aivideoeditor.timeline import ops
    tl = session.timeline
    tl.audio.normalize = False
    mic = tl.track("A1").clips[0]
    mic.fade_out = 3.0
    ops.split_at(tl, 5.0, ["V1"])          # extra clip edges -> chunk boundaries
    ops.split_at(tl, 10.0, ["V1"])
    session.commit()
    st = RenderSettings.for_timeline(tl, preview=True, preview_width=320)
    st.max_inputs = 2
    render_timeline(tl, session.project.assets, tmp_path / "f.mp4", st, workdir=tmp_path / "w")
    got = decode_audio(tmp_path / "w" / "audio.wav", 48000)
    src = decode_audio(session.project.asset("mic").path, 48000)
    end = mic.end
    for t in (end - 2.5, end - 1.5, end - 0.5):       # gain follows the 3 s ramp across boundaries
        i0 = int(t * 48000)
        seg_src = src[i0:i0 + 2400]
        if np.abs(seg_src).max() < 0.05:
            continue
        ratio = np.abs(got[i0:i0 + 2400]).sum() / np.abs(seg_src).sum()
        assert abs(ratio - (end - t) / 3.0) < 0.1, (t, ratio)


def test_silent_screen_recording_project(tmp_path):
    """Only a screen recording, no audio at all: analysis, sync, editing tools and MP4 output still work."""
    from aivideoeditor.pipeline import analyze_project
    from aivideoeditor.project import Project
    from aivideoeditor.session import EditorSession
    screen = tmp_path / "screen.mp4"
    # A static "editor" where characters are "typed" only between 4 s and 7 s at the bottom right
    # (drawbox, so it also works on FFmpeg builds without drawtext).
    typing = ",".join(f"drawbox=x={1080 + i * 16}:y=600:w=12:h=20:color=white:t=fill:"
                      f"enable='between(t,{4 + 0.5 * i},7)'" for i in range(6))
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=0x1e1e2e:s=1280x720:r=30:d=12", "-vf", typing,
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", "-y", str(screen)])
    p = Project.create(tmp_path / "p", width=1280, height=720)
    p.add_asset(screen, "screen")
    analyze_project(p, log=lambda m: None)
    vis = p.visual("screen")
    assert len(vis.active) == 1
    a, b = vis.active[0]
    assert 3.0 <= a <= 4.5 and 6.5 <= b <= 8.0
    x, y, w, h = vis.regions[0]
    assert x > 0.75 and y > 0.75 and w < 0.15 and h < 0.1    # the change is localised at the bottom right
    res = sync_project(p, log=lambda m: None)                  # nothing to sync, no error
    assert res["screen"].method == "assumed"

    s = EditorSession(p)
    s.assemble()
    ctx = NS(session=s)
    out, err = run_tool(ctx, "get_transcript", {})
    assert not err and "no recording with audio" in out
    out, err = run_tool(ctx, "get_visual_activity", {})
    assert not err and "[x0.8" in out
    out, err = run_tool(ctx, "scan_video", {"frames": 6})
    assert not err and out[1]["type"] == "image"
    out, err = run_tool(ctx, "ripple_delete", {"ranges": [{"start": 8, "end": 12}]})
    assert not err
    out_file = s.render(preview=False)
    info = probe(out_file)
    assert info.has_video and info.has_audio and abs(info.duration - 8.0) < 0.1
