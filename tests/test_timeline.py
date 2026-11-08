import pytest

from aivideoeditor.analysis.speech import SpeechAnalysis
from aivideoeditor.analysis.transcribe import Segment, Transcript, Word
from aivideoeditor.timeline import Rect, Timeline, layouts, ops
from aivideoeditor.timeline.autocut import filler_ranges, pause_ranges, repeated_phrases
from aivideoeditor.timeline.describe import describe_timeline, format_words
from aivideoeditor.timeline.mapping import snap_to_pauses, words_on_timeline
from aivideoeditor.timeline.ops import EditError, validate

DUR = {"screen": 60.0, "camera": 62.0, "mic": 61.0}


def make_tl() -> Timeline:
    tl = Timeline()
    v1 = ops.add_track(tl, "video", role="screen")
    v2 = ops.add_track(tl, "video", role="camera")
    a1 = ops.add_track(tl, "audio", role="mic")
    ops.place_clip(tl, v1.id, "screen", 0, 60, 0, volume=0)
    ops.place_clip(tl, v2.id, "camera", 2, 62, 0, volume=0)   # camera started 2 s before the screen
    ops.place_clip(tl, a1.id, "mic", 1, 61, 0)
    return tl


def offsets_consistent(tl: Timeline) -> bool:
    """At every clip boundary the three sources must keep their sync relationship."""
    for t in [x + 0.01 for x in tl.cut_points()[:-1]]:
        s = tl.track("V1").clip_at(t)
        c = tl.track("V2").clip_at(t)
        m = tl.track("A1").clip_at(t)
        if not (s and c and m):
            continue
        st, ct, mt = s.source_time(t), c.source_time(t), m.source_time(t)
        if abs((ct - st) - 2) > 1e-6 or abs((mt - st) - 1) > 1e-6:
            return False
    return True


def test_ripple_delete_multiple_ranges_keeps_sync():
    tl = make_tl()
    removed = ops.ripple_delete(tl, [(10, 12), (30, 35), (11, 13), (0, 1)])
    assert removed == pytest.approx(1 + 3 + 5)
    assert tl.duration == pytest.approx(51)
    assert offsets_consistent(tl)
    assert validate(tl, DUR) == []
    # Material at original time 13 now sits at 13 - 1 - 3 = 9.
    assert tl.track("V1").clip_at(9.0).source_time(9.0) == pytest.approx(13.0)


def test_lift_split_trim_move():
    tl = make_tl()
    ops.lift(tl, 5, 7, ["V2"])
    assert tl.track("V2").clip_at(6) is None
    assert tl.duration == 60
    new = ops.split_at(tl, 20)
    assert len(new) == 3
    left = tl.track("V1").clip_at(19.9)
    ops.trim_clip(tl, left.id, end=18)
    assert tl.track("V1").clip_at(19) is None
    with pytest.raises(EditError):
        ops.trim_clip(tl, left.id, end=25)  # would overlap the right piece
    with pytest.raises(EditError):
        ops.move_clip(tl, left.id, 30)       # overlaps without overwrite
    ops.move_clip(tl, left.id, 30, overwrite=True)
    assert validate(tl, DUR) == []


def test_speed_range():
    tl = make_tl()
    new = ops.speed_range(tl, 10, 20, 4)
    assert new == pytest.approx(2.5)
    assert tl.duration == pytest.approx(52.5)
    sped = tl.track("A1").clip_at(11)
    assert sped.speed == 4 and sped.volume == 0
    after = tl.track("V1").clip_at(13)
    assert after.source_time(13) == pytest.approx(20.5)
    assert validate(tl, DUR) == []


def test_layouts_and_zoom():
    tl = make_tl()
    layouts.apply_layout(tl, 0, 60, "pip", pip_corner="top_left")
    cam = tl.track("V2").clip_at(1)
    assert cam.transform.x < 0.1 and cam.transform.y < 0.1 and cam.transform.w == pytest.approx(0.26)
    layouts.apply_layout(tl, 10, 20, "camera_only")
    assert not tl.track("V1").clip_at(15).visible
    assert tl.track("V2").clip_at(15).transform.w == 1
    assert tl.track("V1").clip_at(25).visible
    layouts.zoom_range(tl, "V1", 30, 40, Rect(x=0.6, y=0.6, w=0.2, h=0.1), source_aspect=16 / 9)
    crop = tl.track("V1").clip_at(35).transform.crop
    # widened to 16:9 and kept inside the frame
    assert crop.w * 16 / 9 / crop.h == pytest.approx(16 / 9, rel=1e-3)
    assert crop.x + crop.w <= 1 + 1e-9 and crop.y + crop.h <= 1 + 1e-9
    with pytest.raises(EditError):
        layouts.apply_layout(tl, 5, 6, "nonsense")


def test_texts_follow_ripple():
    tl = make_tl()
    a = ops.add_text(tl, 20, 25, "after")
    b = ops.add_text(tl, 8, 14, "spanning")
    ops.ripple_delete(tl, [(10, 12)])
    assert (a.start, a.end) == (18, 23)
    assert (b.start, b.end) == (8, 12)
    m = ops.add_marker(tl, 30, "check")
    ops.ripple_delete(tl, [(0, 5)])
    assert m.time == 25


def test_words_map_through_cuts():
    tl = make_tl()
    tr = Transcript("tr", [Segment(0, 30, "", [Word(5.0, 5.5, "bir"), Word(12.0, 12.4, "iki"),
                                                 Word(20.0, 20.5, "üç")])])
    ops.ripple_delete(tl, [(9, 14)])  # removes mic source 10..15 (word "iki")
    words = words_on_timeline(tl, "mic", tr)
    assert [w.text for w in words] == ["bir", "üç"]
    # mic is offset by 1 s: source 20.0 -> timeline 19.0, minus 5 s cut -> 14.0
    assert words[1].start == pytest.approx(14.0)
    text = format_words(words)
    assert "✂" in text and "bir" in text


def test_snap_to_pauses():
    pauses = [(1.0, 1.4), (3.0, 3.5)]
    assert snap_to_pauses(1.2, pauses) == 1.2
    assert snap_to_pauses(2.9, pauses) == pytest.approx(3.03)
    assert snap_to_pauses(2.0, pauses) == 2.0


def test_autocut_helpers():
    tl = make_tl()
    speech = SpeechAnalysis(61, -40, -60, -10, speech=[], silences=[(0, 2), (10, 11), (30, 30.4)])
    ranges = pause_ranges(tl, "mic", speech, min_pause=0.7, keep=0.2)
    # mic offset 1: (0,2)->(-1..1) clipped to (0,1); (10,11)->(9,10)
    assert ranges[0] == pytest.approx((0, 0.8))
    assert ranges[1] == pytest.approx((9.2, 9.8))
    assert len(ranges) == 2
    words = [Word(i, i + 0.3, t) for i, t in enumerate(
        "şimdi bu ayarı eee şimdi bu ayarı açıyoruz ıı tamam".split(), start=2)]
    tr = Transcript("tr", [Segment(2, 12, "", words)])
    fill = filler_ranges(tl, "mic", tr)
    assert [w for _, _, w in fill] == ["eee", "ıı"]
    rep = repeated_phrases(tl, "mic", tr, min_words=3)
    assert len(rep) == 1
    # first take starts at source 2 (timeline 1), the restart at source 6 (timeline 5)
    assert rep[0]["first_take"] == (1.0, 5.0)


def test_describe_and_validate_overlap():
    tl = make_tl()
    text = describe_timeline(tl)
    assert "V1" in text and "camera" in text
    tl.track("V1").clips.append(tl.track("V1").clips[0].model_copy(update={"id": "dup", "start": 10}))
    assert any("overlap" in p for p in validate(tl, DUR))


def test_review_regressions():
    # delete_clips with ripple over several clips covering the same range
    tl = make_tl()
    ops.split_at(tl, 10)
    ops.split_at(tl, 20)
    ids = [tl.track("V1").clip_at(15).id, tl.track("V2").clip_at(15).id]
    ops.delete_clips(tl, ids, ripple=True)
    assert tl.duration == pytest.approx(50) and offsets_consistent(tl)
    # removed time is clamped to the timeline
    assert ops.ripple_delete(tl, [(45, 90)]) == pytest.approx(5)
    # speed_range remaps texts and markers inside the range
    tl = make_tl()
    x = ops.add_text(tl, 12, 18, "inside")
    y = ops.add_text(tl, 25, 35, "straddles")
    m = ops.add_marker(tl, 25, "m")
    ops.speed_range(tl, 10, 30, 4)
    assert (x.start, x.end) == pytest.approx((10.5, 12.0))
    assert (y.start, y.end) == pytest.approx((13.75, 20.0))
    assert m.time == pytest.approx(13.75)


def test_music_bed_follows_content_end():
    tl = make_tl()
    music = ops.add_track(tl, "audio", role="music")
    ops.place_clip(tl, music.id, "bed", 0, 60, 0)
    music.locked = True
    ops.ripple_delete(tl, [(10, 30)])
    assert tl.duration == pytest.approx(40)
    assert music.clips[0].end == pytest.approx(40) and music.clips[0].fade_out >= 2.0
