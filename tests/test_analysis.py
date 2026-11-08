import numpy as np
import pytest

from aivideoeditor.analysis.speech import analyze_speech
from aivideoeditor.analysis.sync import find_offset_arrays
from aivideoeditor.analysis.transcribe import Transcript, load_transcript
from aivideoeditor.analysis.visual import analyze_frames
from aivideoeditor.demo import speech_like


def _degrade(x, rng):
    y = np.convolve(x, np.ones(7) / 7, mode="same") * 0.4
    return (y + rng.standard_normal(y.size) * 0.01).astype(np.float32)


@pytest.mark.parametrize("offset", [2.371, -1.6043, 0.0])
def test_sync_offset_accuracy(offset):
    sr = 8000
    rng = np.random.default_rng(3)
    ref, _ = speech_like(60.0, seed=5, sr=sr)
    if offset >= 0:
        other = ref[int(offset * sr):]
    else:
        other = np.concatenate([rng.standard_normal(int(-offset * sr)).astype(np.float32) * 0.002, ref])
    other = _degrade(other[: 45 * sr], rng)
    res = find_offset_arrays(ref, other, sr)
    assert res.reliable
    assert abs(res.offset - offset) < 0.002
    assert res.drift_ppm == 0.0


def test_sync_detects_drift():
    sr = 8000
    ref, _ = speech_like(120.0, seed=9, sr=sr)
    ratio = 1 + 300e-6  # the other device's clock runs fast
    idx = np.arange(int(100 * sr)) * ratio + 2.0 * sr
    other = np.interp(idx, np.arange(ref.size), ref).astype(np.float32)
    res = find_offset_arrays(ref, other, sr)
    assert abs(res.offset - 2.0) < 0.003
    assert 200 < res.drift_ppm < 400


def test_speech_and_pauses():
    sr = 16000
    x = np.zeros(sr * 6, dtype=np.float32)
    rng = np.random.default_rng(0)
    for a, b in [(0.5, 1.5), (1.7, 2.5), (4.0, 5.2)]:
        x[int(a * sr):int(b * sr)] = rng.standard_normal(int((b - a) * sr)) * 0.2
    x += rng.standard_normal(x.size).astype(np.float32) * 0.001
    res = analyze_speech(x, sr, min_silence=0.3)
    # The 0.2 s gap is bridged; the 1.5 s gap is a pause.
    assert any(abs(a - 2.5) < 0.05 and abs(b - 4.0) < 0.05 for a, b in res.silences)
    assert not any(1.5 < a < 1.7 for a, _ in res.silences)
    assert abs(res.speech[0][0] - 0.5) < 0.05


def test_visual_activity_and_cuts():
    frames = np.zeros((40, 36, 64), dtype=np.float32)
    frames[10:, :, :] = 0.8          # hard cut at sample 10
    for i in range(20, 26):          # small motion between samples 20-25
        frames[i, 5:15, i:i + 5] = 0.2
    res = analyze_frames(frames, 2.0)
    assert res.scene_cuts == [5.0]
    assert any(a <= 10.5 and b >= 12.0 for a, b in res.active)


def test_load_srt(tmp_path):
    srt = tmp_path / "x.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,500\nMerhaba dünya\n\n2\n00:00:03,000 --> 00:00:04,000\n"
                   "<i>İkinci</i> satır\n", encoding="utf-8")
    tr = load_transcript(srt)
    assert [s.text for s in tr.segments] == ["Merhaba dünya", "İkinci satır"]
    words = list(tr.words())
    assert words[0].text == "Merhaba" and words[0].start == 1.0
    assert Transcript.from_dict(tr.to_dict()).segments[1].start == 3.0
