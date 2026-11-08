"""Projects like a desktop editor: create, list, reopen, save and save-as."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from aivideoeditor.project import Project
from aivideoeditor.web import App, make_handler
from aivideoeditor.workspace import create_project, duplicate_project, list_projects, recent_paths


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("AVE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AVE_WORKSPACE", str(tmp_path / "projects"))
    return tmp_path / "projects"


def test_create_list_and_duplicate(ws, demo_media):
    p = create_project("Ürün: tanıtım/video", "vertical", brief="kısa tanıtım")
    assert p.root.parent == ws and p.root.name == "Ürün tanıtım video"
    assert (p.data.canvas.width, p.data.canvas.height) == (1080, 1920)
    assert (p.root / ".mcp.json").exists() and p.data.brief == "kısa tanıtım"
    assert recent_paths()[0] == str(p.root)
    p.add_asset(demo_media.camera)
    (p.root / "renders").mkdir(exist_ok=True)
    (p.root / "renders" / "x.mp4").write_bytes(b"0")
    (p.root / "agent").mkdir(exist_ok=True)
    (p.root / "agent" / "conversation.json").write_text("[]")

    second = create_project("Ürün: tanıtım/video")          # same name -> numbered folder
    assert second.root.name == "Ürün tanıtım video 2"

    copy = duplicate_project(Project.open(p.root), "Dikey sürüm")
    assert copy.data.name == "Dikey sürüm" and copy.root != p.root
    assert list(copy.assets) == list(p.assets)                # recordings referenced, not copied
    assert not (copy.root / "renders" / "x.mp4").exists() and not (copy.root / "agent").exists()
    cfg = json.loads((copy.root / ".mcp.json").read_text())
    assert cfg["mcpServers"]["ave"]["args"][-1] == str(copy.root)
    names = [x["name"] for x in list_projects()]
    assert {"Ürün: tanıtım/video", "Dikey sürüm"} <= set(names)


def call(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_web_launcher_flow(ws, demo_media):
    app = App(None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, state = call(base, "/api/state")
        assert status == 200 and state["project"] is None and state["formats"]
        assert call(base, "/api/still?t=1")[0] == 409                 # needs an open project
        status, opened = call(base, "/api/projects/new", {"name": "İlk video", "format": "landscape_720p"})
        assert status == 200 and opened["name"] == "İlk video"
        status, state = call(base, "/api/state")
        assert state["project"]["canvas"]["width"] == 1280
        assert call(base, "/api/assets", {"path": str(demo_media.camera), "role": "auto"})[0] == 200
        status, saved = call(base, "/api/save", {})
        assert status == 200 and saved["saved_at"]
        status, copy = call(base, "/api/save_as", {"name": "İlk video dikey"})
        assert status == 200 and copy["name"] == "İlk video dikey"
        status, state = call(base, "/api/state")
        assert state["project"]["name"] == "İlk video dikey" and len(state["assets"]) == 1
        assert call(base, "/api/projects/close", {})[0] == 200
        status, state = call(base, "/api/state")
        assert state["project"] is None
        assert {"İlk video", "İlk video dikey"} <= {p["name"] for p in state["projects"]}
        first = next(p for p in state["projects"] if p["name"] == "İlk video")
        assert call(base, "/api/projects/open", {"path": first["path"]})[0] == 200
        assert call(base, "/api/state")[1]["project"]["name"] == "İlk video"
        assert call(base, "/api/projects/open", {"path": "/nope"})[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
