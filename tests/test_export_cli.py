import json
import xml.dom.minidom

from aivideoeditor.cli import main
from aivideoeditor.export import export_timeline
from aivideoeditor.timeline import ops


def test_exports(session, tmp_path):
    tl = session.timeline
    ops.ripple_delete(tl, [(2, 3)])
    ops.speed_range(tl, 5, 7, 2)
    session.commit()
    p = session.project
    x = export_timeline(p, "xml", tmp_path / "t.xml")
    dom = xml.dom.minidom.parse(str(x))
    assert dom.getElementsByTagName("clipitem")
    assert any(e.firstChild.nodeValue == "200.00" for e in dom.getElementsByTagName("value") if e.firstChild)
    o = json.loads(export_timeline(p, "otio", tmp_path / "t.otio").read_text())
    kinds = [t["kind"] for t in o["tracks"]["children"]]
    assert "Video" in kinds and "Audio" in kinds
    edl = export_timeline(p, "edl", tmp_path / "t.edl").read_text()
    assert edl.startswith("TITLE:") and "FROM CLIP NAME" in edl and "M2" in edl


def test_cli_flow(tmp_path, demo_media, capsys):
    root = tmp_path / "cli"
    assert main(["init", str(root), "--width", "640", "--height", "360", "--brief", "test"]) == 0
    assert main(["add", str(root), str(demo_media.mic), "--role", "mic"]) == 0
    assert main(["add", str(root), str(demo_media.camera), "--role", "camera"]) == 0
    assert main(["analyze", str(root), "--no-transcribe"]) == 0
    assert main(["sync", str(root)]) == 0
    assert main(["assemble", str(root)]) == 0
    out = capsys.readouterr().out
    assert "camera" in out and "offset +1.30" in out
    assert main(["show", str(root)]) == 0
    assert main(["undo", str(root)]) == 0
    assert main(["add", str(root), str(tmp_path / "missing.mp4")]) == 1
