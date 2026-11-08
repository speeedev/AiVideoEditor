"""Where projects live: a default projects folder, a recent-projects list and project creation.

Lets the UI work like a desktop editor: start the app, create a new project
or pick a recent one, then add recordings — no command line needed.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .project import Project

FORMATS: dict[str, tuple[str, int, int]] = {
    "landscape_1080p": ("Yatay 1080p (YouTube)", 1920, 1080),
    "landscape_720p": ("Yatay 720p", 1280, 720),
    "landscape_4k": ("Yatay 4K", 3840, 2160),
    "vertical": ("Dikey 1080×1920 (Shorts / Reels / TikTok)", 1080, 1920),
    "square": ("Kare 1080×1080", 1080, 1080),
}


def ave_home() -> Path:
    d = Path(os.environ.get("AVE_HOME") or Path.home() / ".ave")
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_workspace() -> Path:
    env = os.environ.get("AVE_WORKSPACE")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    for media in ("Movies", "Videos"):
        if (home / media).is_dir():
            return home / media / "AVE Projeleri"
    return home / "AVE Projeleri"


def _recent_file() -> Path:
    return ave_home() / "recent.json"


def recent_paths() -> list[str]:
    try:
        data = json.loads(_recent_file().read_text(encoding="utf-8"))
        return [p for p in data if isinstance(p, str)]
    except (OSError, json.JSONDecodeError):
        return []


def remember(root: str | Path) -> None:
    root = str(Path(root).resolve())
    paths = [root] + [p for p in recent_paths() if p != root]
    _recent_file().write_text(json.dumps(paths[:30], ensure_ascii=False, indent=1), encoding="utf-8")


def _summary(root: Path) -> dict | None:
    pf = root / "project.json"
    if not pf.is_file():
        return None
    try:
        data = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    duration = 0.0
    tf = root / "timeline.json"
    try:
        tl = json.loads(tf.read_text(encoding="utf-8"))
        for t in tl.get("tracks", []):
            for c in t.get("clips", []):
                duration = max(duration, c["start"] + (c["source_out"] - c["source_in"]) / c.get("speed", 1.0))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ZeroDivisionError):
        pass
    mtime = max((p.stat().st_mtime for p in (pf, tf) if p.exists()), default=pf.stat().st_mtime)
    canvas = data.get("canvas", {})
    return {
        "name": data.get("name") or root.name,
        "path": str(root),
        "modified": mtime,
        "modified_text": time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime)),
        "assets": len(data.get("assets", {})),
        "duration": round(duration, 2),
        "canvas": f"{canvas.get('width', '?')}×{canvas.get('height', '?')}",
        "brief": data.get("brief", ""),
    }


def list_projects(workspace: Path | None = None) -> list[dict]:
    """Recent projects plus every project folder in the workspace, most recently changed first."""
    seen: dict[str, dict] = {}
    candidates = [Path(p) for p in recent_paths()]
    ws = workspace or default_workspace()
    if ws.is_dir():
        candidates += [d for d in ws.iterdir() if d.is_dir()]
    for root in candidates:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        info = _summary(root)
        if info:
            seen[key] = info
    return sorted(seen.values(), key=lambda p: p["modified"], reverse=True)


def _folder_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name).strip().strip(".")
    return re.sub(r"\s+", " ", cleaned)[:80] or "Yeni proje"


def create_project(name: str, fmt: str = "landscape_1080p", *, fps: float = 30.0,
                   workspace: Path | None = None, brief: str = "") -> Project:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format '{fmt}'")
    _, w, h = FORMATS[fmt]
    ws = workspace or default_workspace()
    ws.mkdir(parents=True, exist_ok=True)
    base = _folder_name(name)
    root = ws / base
    i = 2
    while root.exists():
        root = ws / f"{base} {i}"
        i += 1
    project = Project.create(root, name.strip() or base, width=w, height=h, fps=fps)
    if brief:
        project.data.brief = brief
        project.save()
    from .claude_setup import write_claude_files
    write_claude_files(project)
    remember(project.root)
    return project


def duplicate_project(src: Project, name: str, *, workspace: Path | None = None) -> Project:
    """'Save as': copy a project (recordings stay where they are) under a new name.

    Renders, exports and the Claude conversation are not copied, so the copy
    starts a fresh editing conversation; editing proxies are reused.
    """
    import shutil

    from .claude_setup import write_claude_files
    ws = workspace or src.root.parent
    ws.mkdir(parents=True, exist_ok=True)
    base = _folder_name(name)
    dst = ws / base
    i = 2
    while dst.exists():
        dst = ws / f"{base} {i}"
        i += 1
    skip = {"renders", "exports", "agent"}
    shutil.copytree(src.root, dst, ignore=lambda d, names: [n for n in names if Path(d) == src.root and n in skip])
    manifest = dst / "cache" / "proxies" / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for entry in data.values():
                entry["path"] = str(dst / "cache" / "proxies" / Path(entry["path"]).name)
            manifest.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            manifest.unlink(missing_ok=True)
    copy = Project.open(dst)
    copy.data.name = name.strip() or base
    copy.save()
    write_claude_files(copy)
    remember(copy.root)
    return copy
