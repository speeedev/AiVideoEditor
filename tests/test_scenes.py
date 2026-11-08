"""Separate scenes/takes: grouping by moment, sequential layout, merged transcripts, reordering."""

from types import SimpleNamespace as NS

import pytest

from aivideoeditor.agent.tools import run_tool
from aivideoeditor.analysis.transcribe import Segment, Transcript, Word
from aivideoeditor.demo import SR, speech_like, write_wav
from aivideoeditor.media.ffmpeg import run_ffmpeg
from aivideoeditor.pipeline import analyze_project
from aivideoeditor.project import Project
from aivideoeditor.scenes import scene_groups, speech_sources
from aivideoeditor.session import EditorSession


def _video(path, voice, created, color):
    wav = path.with_suffix(".wav")
    write_wav(wav, voice)
    run_ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30:d={voice.size / SR:.3f}", "-i", str(wav),
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                "-metadata", f"creation_time={created}", "-y", str(path)])
    wav.unlink()


def _transcript(text, start=0.3, step=0.45):
    words = [Word(start + i * step, start + i * step + 0.35, w) for i, w in enumerate(text.split())]
    return Transcript("tr", [Segment(words[0].start, words[-1].end, text, words)])


@pytest.fixture(scope="module")
def scene_project(tmp_path_factory):
    d = tmp_path_factory.mktemp("scenes")
    va, _ = speech_like(5.0, seed=11)
    vb, _ = speech_like(5.8, seed=22)
    vc, _ = speech_like(5.0, seed=33)
    # Names do not match the recording order: b was recorded first, then a, then c.
    _video(d / "a.mp4", va, "2026-11-07T10:05:00Z", "red")
    _video(d / "b.mp4", vb[int(0.8 * SR):], "2026-11-07T10:00:00Z", "green")   # camera started 0.8 s late
    _video(d / "c.mp4", vc, "2026-11-07T10:10:00Z", "blue")
    write_wav(d / "mic.wav", vb)                                                 # mic of scene b
    p = Project.create(d / "proj", width=320, height=180)
    for name in ("a.mp4", "b.mp4", "c.mp4", "mic.wav"):
        p.add_asset(d / name)
    analyze_project(p, do_transcribe=False, log=lambda m: None)                 # also syncs
    return p.root


@pytest.fixture
def scenes(scene_project, tmp_path):
    import shutil
    dst = tmp_path / "proj"
    shutil.copytree(scene_project, dst)
    return Project.open(dst)


def test_grouping_and_order(scenes):
    p = scenes
    assert {a.id: a.role for a in p.assets.values()} == {"a": "camera", "b": "camera", "c": "camera", "mic": "mic"}
    groups = [[a.id for a in g] for g in scene_groups(p)]
    assert groups == [["mic", "b"], ["a"], ["c"]]
    assert p.asset("b").sync.reference == "mic"
    assert p.asset("b").sync.offset == pytest.approx(0.8, abs=0.03)
    assert speech_sources(p) == ["mic", "a", "c"]


def test_sequential_layout(scenes):
    s = EditorSession(scenes)
    tl = s.assemble()
    cams = [t for t in tl.video_tracks if t.role == "camera"]
    assert len(cams) == 1                                      # scenes share one camera track
    clips = sorted(cams[0].clips, key=lambda c: c.start)
    assert [c.asset_id for c in clips] == ["b", "a", "c"]
    assert clips[0].start == pytest.approx(0.8, abs=0.03) and clips[0].volume == 0   # the mic carries scene 1
    assert clips[1].start == pytest.approx(5.8, abs=0.05) and clips[1].volume == 1.0
    assert clips[2].start == pytest.approx(clips[1].end)
    assert all(c.transform.w == 1 and c.transform.fit == "cover" for c in clips)     # camera-only scenes
    assert len(tl.markers) == 3 and tl.markers[1].note.startswith("Sahne 2")
    assert tl.track("A1").clips[0].asset_id == "mic" and tl.track("A1").clips[0].start == 0


def test_words_scenes_and_reordering(scenes):
    p = scenes
    p.save_transcript("mic", _transcript("merhaba bugün size yeni bir özelliği anlatacağım"))
    p.save_transcript("a", _transcript("ikinci sahnede ayarları açıp kaydediyoruz"))
    p.save_transcript("c", _transcript("merhaba bugün size yeni bir özelliği anlatacağım tamam"))
    s = EditorSession(p)
    s.assemble()
    words = s.words()
    assert [w.text for w in words][:2] == ["merhaba", "bugün"]
    assert any(w.text == "ikinci" and 5.8 <= w.start < 7 for w in words)
    assert sum(w.text == "merhaba" for w in words) == 2       # every scene is in the transcript

    ctx = NS(session=s)
    out, err = run_tool(ctx, "get_scenes", {})
    assert not err and "3 scenes/takes" in out
    assert "Scene 3" in out and "similar to scene 1" in out and "probably another take" in out
    out, err = run_tool(ctx, "get_transcript", {})
    assert not err and "ikinci sahnede" in out and "anlatacağım tamam" in out

    third = next(c for c in s.timeline.track("V1").clips if c.asset_id == "c")
    start, end = third.start, third.end
    out, err = run_tool(ctx, "move_range", {"start": start, "end": end, "to": 0})
    assert not err, out
    first = min(s.timeline.track("V1").clips, key=lambda c: c.start)
    assert first.asset_id == "c" and first.start == pytest.approx(0)
    assert s.words()[-1].text == "kaydediyoruz"                # scene a is now last
