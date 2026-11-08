"""Turn an HTML animation (like a Claude artifact) into a video clip with transparency.

The page is opened in headless Chrome with a virtual clock: for every frame the
renderer sets the time, moves CSS animations / Web Animations / SVG (SMIL) /
GSAP to it, calls ``window.renderFrame(t)`` when the page defines one, runs the
pending ``requestAnimationFrame`` callbacks, and takes a screenshot with a
transparent background. The frames become a VP9 WebM with an alpha channel,
which the renderer, the live preview and other editors can use directly.

Page contract (also given to Claude in the tool description):
* the page is shown at the given width x height; leave html/body transparent
  for an overlay, or paint a background for a full-frame clip;
* animate with CSS animations, the Web Animations API, SVG animations, GSAP,
  or define ``window.renderFrame = (t) => {...}`` (t in seconds) and draw the
  state at time t (canvas, three.js, d3, ...);
* ``<meta name="duration" content="5">`` sets the default length.

Requires the ``playwright`` package and Google Chrome (or ``playwright install
chromium``).
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from ..media.ffmpeg import run_ffmpeg

VIRTUAL_CLOCK = r"""
(() => {
  let now = 0;
  let queue = [];
  const epoch = 1767225600000;
  performance.now = () => now;
  const RealDate = Date;
  Date.now = () => epoch + now;
  window.requestAnimationFrame = (cb) => { queue.push(cb); return queue.length; };
  window.cancelAnimationFrame = () => {};
  window.__aveSeek = async (t) => {
    now = t * 1000;
    for (const a of document.getAnimations()) { try { a.pause(); a.currentTime = now; } catch (e) {} }
    document.querySelectorAll("svg").forEach((s) => {
      try { s.pauseAnimations(); s.setCurrentTime(t); } catch (e) {}
    });
    if (window.gsap && window.gsap.globalTimeline) {
      try { window.gsap.globalTimeline.pause(); window.gsap.globalTimeline.seek(t, false); } catch (e) {}
    }
    if (window.lottie && window.lottie.getRegisteredAnimations) {
      for (const an of window.lottie.getRegisteredAnimations()) {
        try { an.goToAndStop(t * 1000, false); } catch (e) {}
      }
    }
    if (typeof window.renderFrame === "function") await window.renderFrame(t);
    const pending = queue; queue = [];
    for (const cb of pending) { try { cb(now); } catch (e) {} }
  };
})();
"""


def html_duration(html: str, default: float = 5.0) -> float:
    m = re.search(r'<meta\s+name=["\']duration["\']\s+content=["\']([\d.]+)', html, re.I)
    return float(m.group(1)) if m else default


def _launch(p):
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:  # noqa: BLE001 - no Google Chrome: Playwright's own Chromium
        return p.chromium.launch(headless=True)


def render_html_animation(
    html_path: str | Path,
    out: str | Path,
    *,
    duration: float,
    fps: float = 30.0,
    width: int = 1920,
    height: int = 1080,
    sheet: str | Path | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Render ``html_path`` to ``out`` (WebM, VP9 with alpha). Optionally write a contact sheet."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("HTML animations need Playwright: pip install playwright") from exc
    html_path = Path(html_path).resolve()
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    n = max(1, round(duration * fps))
    tmp = Path(tempfile.mkdtemp(prefix="ave_anim_"))
    try:
        with sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page(viewport={"width": int(width), "height": int(height)}, device_scale_factor=1)
            page.add_init_script(VIRTUAL_CLOCK)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30000)
            page.evaluate("document.fonts ? document.fonts.ready : null")
            log(f"rendering {n} frames of {html_path.name} ({width}x{height} @ {fps:g} fps)…")
            for i in range(n):
                page.evaluate("t => window.__aveSeek(t)", i / fps)
                page.screenshot(path=str(tmp / f"f{i:05d}.png"), omit_background=True)
            browser.close()
        if errors and not any(tmp.glob("f*.png")):
            raise RuntimeError("the animation page failed: " + "; ".join(errors[:3]))
        run_ffmpeg(["-framerate", f"{fps:g}", "-i", "f%05d.png", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                    "-b:v", "0", "-crf", "28", "-auto-alt-ref", "0", "-row-mt", "1", "-deadline", "good",
                    "-cpu-used", "4", "-metadata:s:v:0", "alpha_mode=1", "-y", str(out)], cwd=tmp)
        if sheet:
            _sheet(sorted(tmp.glob("f*.png")), Path(sheet), fps)
        if errors:
            log("page errors: " + "; ".join(errors[:3]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _sheet(frames: list[Path], out: Path, fps: float, count: int = 8) -> None:
    """Contact sheet of evenly spaced frames on a checkerboard (so transparency is visible)."""
    from ..analysis.frames import format_tc, make_sheet
    if not frames:
        return
    picks = [frames[min(len(frames) - 1, round(i * (len(frames) - 1) / max(1, count - 1)))] for i in range(count)]
    tiles, labels = [], []
    for f in picks:
        img = Image.open(f).convert("RGBA")
        img.thumbnail((384, 384))
        board = Image.new("RGBA", img.size, (90, 90, 90, 255))
        sq = 16
        dark = Image.new("RGBA", (sq, sq), (60, 60, 60, 255))
        for y in range(0, img.size[1], sq):
            for x in range((y // sq) % 2 * sq, img.size[0], sq * 2):
                board.paste(dark, (x, y))
        board.alpha_composite(img)
        path = f.with_suffix(".jpg")
        board.convert("RGB").save(path, quality=85)
        tiles.append(path)
        labels.append(format_tc(int(f.stem[1:]) / fps))
    out.parent.mkdir(parents=True, exist_ok=True)
    make_sheet(tiles, labels, out, columns=4)
