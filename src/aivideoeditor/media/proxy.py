"""Low-resolution editing proxies.

Like a desktop NLE, previews and still frames are rendered from small,
short-GOP copies of the video recordings, so seeking to hundreds of cut
points stays fast. Final renders always use the original files. Proxies
keep the original timestamps, and audio always comes from the original.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .ffmpeg import run_ffmpeg
from .probe import probe

if TYPE_CHECKING:  # pragma: no cover
    from ..project import Asset, Project

PROXY_HEIGHT = 360


@dataclass
class Proxy:
    path: str
    width: int
    height: int
    source_size: int
    source_mtime: float


def _manifest_path(project: "Project") -> Path:
    return project.cache_dir("proxies") / "manifest.json"


def _load_manifest(project: "Project") -> dict[str, dict]:
    p = _manifest_path(project)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _fresh(asset: "Asset", entry: dict | None) -> bool:
    if not entry or not Path(entry.get("path", "")).exists():
        return False
    try:
        st = Path(asset.path).stat()
    except OSError:
        return False
    return entry.get("source_size") == st.st_size and abs(entry.get("source_mtime", 0) - st.st_mtime) < 1e-3


def needs_proxy(asset: "Asset") -> bool:
    h = asset.info.get("height") or 0
    return asset.has_video and not asset.is_image and h > PROXY_HEIGHT * 1.2


def ensure_proxy(project: "Project", asset: "Asset", *, log=print) -> Proxy | None:
    if not needs_proxy(asset):
        return None
    manifest = _load_manifest(project)
    entry = manifest.get(asset.id)
    if _fresh(asset, entry):
        return Proxy(**entry)
    out = project.cache_dir("proxies") / f"{asset.id}.mp4"
    log(f"[{asset.id}] creating editing proxy…")
    run_ffmpeg([
        "-i", asset.path, "-map", "0:v:0", "-an",
        "-vf", f"scale=-2:{PROXY_HEIGHT}:flags=area,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
        "-fps_mode", "passthrough", "-movflags", "+faststart", "-y", str(out),
    ])
    info = probe(out)
    st = Path(asset.path).stat()
    proxy = Proxy(str(out), int(info.width or 0), int(info.height or 0), st.st_size, st.st_mtime)
    manifest[asset.id] = asdict(proxy)
    _manifest_path(project).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return proxy


def load_proxies(project: "Project") -> dict[str, Proxy]:
    """Existing, up-to-date proxies by asset id (never creates any)."""
    manifest = _load_manifest(project)
    out: dict[str, Proxy] = {}
    for aid, asset in project.assets.items():
        entry = manifest.get(aid)
        if _fresh(asset, entry):
            out[aid] = Proxy(**entry)
    return out
