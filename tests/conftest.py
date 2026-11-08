import shutil
from pathlib import Path

import pytest

from aivideoeditor.demo import make_demo
from aivideoeditor.pipeline import analyze_project, sync_project
from aivideoeditor.project import Project
from aivideoeditor.session import EditorSession

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def demo_media(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("media")
    return make_demo(out, duration=12.0, size=(640, 360), cam_size=(320, 180), camera_offset=1.3)


@pytest.fixture(scope="session")
def analyzed_template(demo_media, tmp_path_factory):
    root = tmp_path_factory.mktemp("tpl") / "proj"
    p = Project.create(root, "test", width=640, height=360, fps=30)
    p.add_asset(demo_media.screen, "screen")
    p.add_asset(demo_media.camera, "camera")
    p.add_asset(demo_media.mic, "mic")
    analyze_project(p, do_transcribe=False, log=lambda m: None)
    sync_project(p, log=lambda m: None)
    return root


@pytest.fixture
def project(analyzed_template, tmp_path):
    dst = tmp_path / "proj"
    shutil.copytree(analyzed_template, dst)
    return Project.open(dst)


@pytest.fixture
def session(project, demo_media):
    from aivideoeditor.analysis.transcribe import Segment, Transcript, Word
    words = [Word(a, b, f"w{i}") for i, (a, b) in enumerate(demo_media.speech) if b < 12.0]
    project.save_transcript("mic", Transcript("en", [Segment(words[0].start, words[-1].end, "", words)]))
    s = EditorSession(project)
    s.assemble()
    return s
