import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from aivideoeditor.web import App, make_handler


@pytest.fixture
def server(session):
    app = App(str(session.project.root), model="claude-opus-5-5", effort="high")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_static_and_state(server):
    base, _ = server
    status, ctype, body = get(base + "/")
    assert status == 200 and b"AI Video Editor" in body
    status, ctype, body = get(base + "/static/app.js")
    assert status == 200 and b"handleEvent" in body
    status, _, body = get(base + "/api/state")
    state = json.loads(body)
    assert {a["id"] for a in state["assets"]} == {"screen", "camera", "mic"}
    assert state["timeline"]["tracks"]
    assert state["words"]


def test_still_undo_brief_export(server):
    base, app = server
    status, ctype, body = get(base + "/api/still?t=2&w=320")
    assert status == 200 and ctype == "image/jpeg" and len(body) > 1000
    assert post(base + "/api/brief", {"brief": "Kısa eğitim videosu"})[0] == 200
    assert app.project.data.brief == "Kısa eğitim videosu"
    assert post(base + "/api/undo", {}) == (200, {"ok": True})     # undo the assemble
    assert app.project.timeline.tracks == []
    assert post(base + "/api/redo", {}) == (200, {"ok": True})
    assert app.project.timeline.tracks
    status, data = post(base + "/api/export", {"format": "xml"})
    assert status == 200
    assert get(base + data["url"])[0] == 200
    assert post(base + "/api/assets/nope", {"role": "mic"})[0] == 400


def test_path_traversal_blocked(server):
    base, _ = server
    import http.client
    host, port = base.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("GET", "/renders/..%2F..%2Fproject.json")
    assert conn.getresponse().status == 404


def test_manual_edit_endpoint(server):
    base, app = server
    before = app.project.timeline.duration
    status, data = post(base + "/api/edit", {"tool": "ripple_delete",
                                             "args": {"ranges": [{"start": 1.0, "end": 2.0}], "snap_to_pauses": False}})
    assert status == 200 and "Removed 1.00s" in data["result"]
    assert app.project.timeline.duration == pytest.approx(before - 1.0)
    assert post(base + "/api/edit", {"tool": "apply_layout", "args": {"start": 0, "end": 1, "layout": "nope"}})[0] == 400
    assert post(base + "/api/edit", {"tool": "assemble_timeline", "args": {}})[0] == 400


def test_play_uses_current_render(server):
    import time as _time
    base, app = server
    assert json.loads(get(base + "/api/state")[2])["current_render"] is None
    assert post(base + "/api/render", {"preview": True})[0] == 200
    for _ in range(240):
        if app.job is None:
            break
        _time.sleep(0.25)
    state = json.loads(get(base + "/api/state")[2])
    assert state["current_render"] and state["current_render"] in state["renders"]
    # Any edit makes the preview stale, so Play renders again.
    post(base + "/api/edit", {"tool": "ripple_delete", "args": {"ranges": [{"start": 0, "end": 1}], "snap_to_pauses": False}})
    assert json.loads(get(base + "/api/state")[2])["current_render"] is None
