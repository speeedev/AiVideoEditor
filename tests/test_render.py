import json
import subprocess

import numpy as np
import pytest

from aivideoeditor.analysis.audio import decode_audio
from aivideoeditor.render.captions import ass_color, build_ass
from aivideoeditor.render.engine import RenderSettings, fps_string, plan_chunks, render_timeline
from aivideoeditor.timeline import Rect, layouts, ops
from aivideoeditor.timeline.model import Captions


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,nb_read_frames,width,height", "-of", "json", str(path)],
                         capture_output=True, check=True)
    return {s["codec_type"]: s for s in json.loads(out.stdout)["streams"]}


def test_fps_string():
    assert fps_string(30) == "30"
    assert fps_string(29.97) == "30000/1001"
    assert fps_string(23.976) == "24000/1001"
    assert fps_string(25.0) == "25"


def test_assemble_places_synced_recordings(session):
    tl = session.timeline
    cam = tl.track("V2").clips[0]
    assert cam.start == pytest.approx(1.3, abs=0.002)
    assert tl.track("V2").role == "camera"
    assert cam.volume == 0  # external mic present
    assert tl.track("A1").clips[0].volume == 1


def test_chunked_render_is_sample_accurate(session, tmp_path):
    tl = session.timeline
    sp = session.project.speech("mic")
    from aivideoeditor.timeline.autocut import pause_ranges
    ops.ripple_delete(tl, pause_ranges(tl, "mic", sp, min_pause=0.4, keep=0.1))
    layouts.apply_layout(tl, 2, 3, "camera_only")
    layouts.zoom_range(tl, "V1", 4, 5, Rect(x=0, y=0, w=0.5, h=0.5), source_aspect=16 / 9)
    ops.add_text(tl, 0.2, 1.5, "Başlık: 'test'")
    ops.set_captions(tl, enabled=True, source_asset="mic", highlight_words=True)
    session.commit()

    st = RenderSettings.for_timeline(tl, preview=True, preview_width=320)
    st.max_inputs = 4  # force many chunks
    assert len(plan_chunks(tl, session.project.assets, st.fps, st.max_inputs)) > 2
    out = render_timeline(tl, session.project.assets, tmp_path / "out.mp4", st,
                          caption_words=session.caption_words(), workdir=tmp_path / "work")
    info = _probe(out)
    total = round(tl.duration * 30)
    assert int(info["video"]["nb_read_frames"]) == total
    assert info["video"]["width"] == 320

    # Rebuild the expected mic signal from the timeline and compare sample by sample.
    mic = decode_audio(session.project.asset("mic").path, 48000)
    expected = np.zeros(total * 1600, dtype=np.float32)
    for c in tl.track("A1").clips:
        f0, f1 = round(c.start * 30), round(c.end * 30)
        s0 = int(round(c.source_time(f0 / 30) * 48000))
        seg = mic[s0:s0 + (f1 - f0) * 1600]
        expected[f0 * 1600:f0 * 1600 + seg.size] = seg
    got = decode_audio(tmp_path / "work" / "audio.wav", 48000)
    assert got.size == expected.size
    err = np.abs(expected - got)
    assert err.max() < 0.02          # only micro-fades at cuts + 16-bit rounding differ
    assert np.sqrt((err ** 2).mean()) < 0.002


def test_still_frame(session):
    p = session.still(2.0, width=320)
    assert p.exists() and p.stat().st_size > 1000


def test_music_ducking_render(session, tmp_path, demo_media):
    from aivideoeditor.agent.tools import AddMusicIn, add_music

    class Ctx:
        pass
    ctx = Ctx()
    ctx.session = session
    music = session.project.add_asset(demo_media.mic, "music", "bed")
    add_music(ctx, AddMusicIn(asset_id=music.id, volume=0.5))
    session.commit()
    track = session.timeline.audio_tracks[-1]
    assert track.duck and track.locked and track.role == "music"
    st = RenderSettings.for_timeline(session.timeline, preview=True, preview_width=320)
    out = render_timeline(session.timeline, session.project.assets, tmp_path / "m.mp4", st)
    assert "audio" in _probe(out)


def test_ass_generation():
    from aivideoeditor.timeline.mapping import TimelineWord
    words = [TimelineWord(i * 0.4, i * 0.4 + 0.3, f"kelime{i}", 1.0, "c1", 0, 0) for i in range(10)]
    cfg = Captions(enabled=True, max_words=4, highlight_words=True)
    ass = build_ass(words, cfg, 1920, 1080)
    assert "PlayResX: 1920" in ass
    assert ass.count("Dialogue:") == 10      # one event per highlighted word
    assert ass_color("#FFD400") in ass
    assert ass_color("#102030") == "&H00302010"


def test_pillow_text_layer_when_ffmpeg_cannot_draw_text(session, tmp_path, monkeypatch):
    """Homebrew's slim ffmpeg has no drawtext/libass: titles and captions are drawn with Pillow."""
    from PIL import Image
    from aivideoeditor.render.textlayer import TextLayer, ffmpeg_can_draw_text
    assert isinstance(ffmpeg_can_draw_text(), bool)
    tl = session.timeline
    ops.add_text(tl, 0.5, 3.0, "BAŞLIK", y=0.15, size=0.12, color="#FF0000", box=False)
    ops.set_captions(tl, enabled=True, source_asset="mic", highlight_words=True)
    session.commit()
    layer = TextLayer(tl, session.caption_words(), 320, 180, tmp_path)
    assert layer.visible and layer.spans[0][0] == 0.0
    assert abs(layer.spans[-1][1] - tl.duration) < 1e-6
    assert all(a < b for a, b, _ in layer.spans)

    monkeypatch.setenv("AVE_TEXT_RENDERER", "pillow")
    st = RenderSettings.for_timeline(tl, preview=True, preview_width=320)
    st.max_inputs = 3
    out = render_timeline(tl, session.project.assets, tmp_path / "t.mp4", st,
                          caption_words=session.caption_words(), workdir=tmp_path / "w")
    assert int(_probe(out)["video"]["nb_read_frames"]) == round(tl.duration * 30)
    assert list((tmp_path / "w").glob("txt_*.png"))            # the fallback was used
    still = Image.open(session.still(1.0, width=320)).convert("RGB")
    w, h = still.size
    reds = sum(1 for x in range(w) for y in range(int(h * 0.05), int(h * 0.25))
               if (lambda p: p[0] > 180 and p[1] < 90 and p[2] < 90)(still.getpixel((x, y))))
    assert reds > 50                                            # the red title is on the frame


def test_title_templates_render_and_animate(session, tmp_path):
    from PIL import Image
    from aivideoeditor.render.titles import render_text_image
    from aivideoeditor.timeline.model import TextOverlay
    for style in ("plain", "title", "lower_third", "badge", "end_card"):
        img = render_text_image(TextOverlay(id="t", start=0, end=3, text="Başlık", subtitle="alt satır",
                                            style=style, x=0.3, y=0.5, size=0.08), 320, 180)
        assert img.size == (320, 180) and img.getbbox() is not None, style
    tl = session.timeline
    ops.add_text(tl, 1.0, 4.0, "SLIDE", style="badge", accent="#FF0000", animation="slide_up", x=0.5, y=0.5,
                 size=0.12, color="#FFFFFF")
    session.commit()
    st = RenderSettings.for_timeline(tl, preview=True, preview_width=320)
    st.max_inputs = 3
    out = render_timeline(tl, session.project.assets, tmp_path / "t.mp4", st)
    assert int(_probe(out)["video"]["nb_read_frames"]) == round(tl.duration * 30)

    def red_rows(t):
        im = Image.open(session.still(t, width=320)).convert("RGB")
        w, h = im.size
        rows = [y for y in range(h) if (lambda p: p[0] > 170 and p[1] < 80 and p[2] < 80)(im.getpixel((w // 2 - 30, y)))]
        return rows
    settled, early = red_rows(2.5), red_rows(1.1)
    assert settled and early and min(early) > min(settled)          # still sliding up at the start
