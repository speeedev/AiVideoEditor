"""HTML animations (Claude artifacts) rendered into transparent clips."""

from types import SimpleNamespace as NS

import numpy as np
import pytest
from PIL import Image

from aivideoeditor.agent.tools import run_tool
from aivideoeditor.media.probe import probe

HTML = """<!doctype html><html><head><meta name="duration" content="1.5"><style>
html,body{margin:0;background:transparent}
.box{position:absolute;left:5%;top:30%;width:25%;height:40%;background:#ff0000;animation:mv 1.5s linear forwards}
@keyframes mv{to{transform:translateX(200%)}}
</style></head><body><div class="box"></div></body></html>"""


@pytest.fixture(scope="module")
def chrome():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from aivideoeditor.render.animation import _launch
    try:
        with sync_playwright() as p:
            _launch(p).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no headless browser: {exc}")


def _red(im, x, y):
    r, g, b = im.getpixel((int(im.size[0] * x), int(im.size[1] * y)))[:3]
    return r > 180 and g < 80 and b < 80


def test_animation_clip_with_alpha(chrome, session):
    ctx = NS(session=session)
    out, err = run_tool(ctx, "create_animation", {"name": "Kutu", "html": HTML, "start": 1.0})
    assert not err, out
    assert out[1]["type"] == "image"                          # frames to check
    asset = session.project.asset("kutu")
    assert asset.role == "graphics" and asset.info["has_alpha"] and abs(asset.duration - 1.5) < 0.05
    track = next(t for t in session.timeline.video_tracks if t.role == "overlay")
    clip = track.clips[0]
    assert clip.asset_id == "kutu" and clip.start == pytest.approx(1.0)

    early = Image.open(session.still(1.05, width=320)).convert("RGB")
    late = Image.open(session.still(2.4, width=320)).convert("RGB")
    assert _red(early, 0.15, 0.5) and not _red(late, 0.15, 0.5)       # the box moved right
    assert _red(late, 0.65, 0.5)
    assert not _red(early, 0.9, 0.9)                                  # transparent elsewhere: footage shows

    # Re-render in place: clips that use it pick up the new version.
    first_path = asset.path
    out, err = run_tool(ctx, "create_animation", {"name": "Kutu", "html": HTML.replace("#ff0000", "#00ff00"),
                                                  "asset_id": "kutu"})
    assert not err, out
    asset = session.project.asset("kutu")
    assert asset.path != first_path and probe(asset.path).has_alpha
    im = Image.open(session.still(1.05, width=320)).convert("RGB")
    px = np.array(im.getpixel((int(im.size[0] * 0.15), int(im.size[1] * 0.5))))
    assert px[1] > 180 and px[0] < 80                                 # now green


def test_html_upload_in_the_ui(chrome, session):
    import json
    import threading
    import time
    import urllib.request
    from http.server import ThreadingHTTPServer

    from aivideoeditor.web import App, make_handler
    app = App(str(session.project.root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/upload?name=intro.html&role=auto&duration=1",
                                     data=HTML.encode(), method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            assert json.loads(r.read())["job"] == "animation"
        for _ in range(200):
            if app.job is None:
                break
            time.sleep(0.2)
        a = app.project.asset("intro")
        assert a.role == "graphics" and abs(a.duration - 1.0) < 0.05
    finally:
        httpd.shutdown()
        httpd.server_close()
