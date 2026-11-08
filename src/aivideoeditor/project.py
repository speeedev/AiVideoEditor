"""Project workspace: assets, analysis results, sync data and the timeline.

Layout on disk::

    my_project/
      project.json            assets, canvas settings, sync offsets
      timeline.json           current edit
      history/                timeline snapshots (undo)
      analysis/<asset_id>/    transcript.json, speech.json, visual.json
      cache/                  extracted frames and contact sheets
      renders/                previews and final exports
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .analysis.speech import SpeechAnalysis
from .analysis.transcribe import Transcript
from .analysis.visual import VisualAnalysis
from .media.probe import MediaInfo, probe
from .timeline.model import Canvas, Timeline

AssetRole = Literal["screen", "camera", "mic", "music", "broll", "sfx", "other"]
ROLES: tuple[str, ...] = ("screen", "camera", "mic", "music", "broll", "sfx", "other")


class SyncInfo(BaseModel):
    """Placement of an asset on the shared recording clock."""

    reference: str
    offset: float = 0.0          # seconds on the reference clock where this asset starts
    drift_ppm: float = 0.0
    confidence: float = 0.0
    method: str = "audio"        # reference | audio | assumed | time | manual
    group: int = 0               # recordings of the same moment share a group; separate scenes differ


class Asset(BaseModel):
    id: str
    path: str
    role: AssetRole = "other"
    info: dict = Field(default_factory=dict)
    sync: SyncInfo | None = None
    notes: str = ""
    audio_stream: int = 0     # which audio stream of the file to use (e.g. OBS multi-track recordings)
    stabilized: str | None = None   # stabilised copy of the picture (cache/stabilized/<id>.mp4)
    voice_path: str | None = None   # speech separated from music/noise (cache/separated/<id>_voice.wav)
    music_path: str | None = None   # everything except the speech
    audio_mode: Literal["original", "voice"] = "original"
    music_level: float = 0.0        # with audio_mode "voice": how much of the separated music to keep

    @property
    def media(self) -> MediaInfo:
        return MediaInfo.from_dict(self.info)

    @property
    def duration(self) -> float:
        return float(self.info.get("duration", 0.0))

    @property
    def has_video(self) -> bool:
        return bool(self.info.get("has_video"))

    @property
    def has_audio(self) -> bool:
        return bool(self.info.get("has_audio"))

    @property
    def is_image(self) -> bool:
        return bool(self.info.get("is_image"))

    @property
    def uses_voice(self) -> bool:
        return self.audio_mode == "voice" and bool(self.voice_path) and Path(self.voice_path).exists()

    @property
    def video_path(self) -> str:
        """File to take the picture from (a stabilised copy when there is one)."""
        return self.stabilized if self.stabilized and Path(self.stabilized).exists() else self.path


class ProjectFile(BaseModel):
    version: int = 1
    name: str
    canvas: Canvas = Field(default_factory=Canvas)
    assets: dict[str, Asset] = Field(default_factory=dict)
    sync_reference: str | None = None
    brief: str = ""  # free-form description of the video's purpose / style


class Project:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._data: ProjectFile | None = None
        self._timeline: Timeline | None = None

    # -- creation / loading -------------------------------------------------
    @classmethod
    def create(cls, root: str | Path, name: str | None = None, *, width: int = 1920,
               height: int = 1080, fps: float = 30.0) -> "Project":
        proj = cls(root)
        if proj.project_file.exists():
            raise FileExistsError(f"a project already exists at {proj.root}")
        proj.root.mkdir(parents=True, exist_ok=True)
        for sub in ("analysis", "cache", "renders", "history"):
            (proj.root / sub).mkdir(exist_ok=True)
        proj._data = ProjectFile(name=name or proj.root.name,
                                 canvas=Canvas(width=width, height=height, fps=fps))
        proj._timeline = Timeline(canvas=proj._data.canvas)
        proj.save()
        return proj

    @classmethod
    def open(cls, root: str | Path) -> "Project":
        proj = cls(root)
        if not proj.project_file.exists():
            raise FileNotFoundError(f"no project.json in {proj.root} (run `ave init` first)")
        proj.load()
        return proj

    @property
    def project_file(self) -> Path:
        return self.root / "project.json"

    @property
    def timeline_file(self) -> Path:
        return self.root / "timeline.json"

    def load(self) -> None:
        self._data = ProjectFile.model_validate_json(self.project_file.read_text(encoding="utf-8"))
        if self.timeline_file.exists():
            self._timeline = Timeline.model_validate_json(self.timeline_file.read_text(encoding="utf-8"))
        else:
            self._timeline = Timeline(canvas=self._data.canvas)

    def save(self) -> None:
        _atomic_write(self.project_file, self.data.model_dump_json(indent=2))
        _atomic_write(self.timeline_file, self.timeline.model_dump_json(indent=2))

    @property
    def data(self) -> ProjectFile:
        assert self._data is not None
        return self._data

    @property
    def timeline(self) -> Timeline:
        assert self._timeline is not None
        return self._timeline

    @timeline.setter
    def timeline(self, value: Timeline) -> None:
        self._timeline = value

    # -- assets ---------------------------------------------------------------
    @property
    def assets(self) -> dict[str, Asset]:
        return self.data.assets

    def asset(self, asset_id: str) -> Asset:
        try:
            return self.data.assets[asset_id]
        except KeyError:
            known = ", ".join(sorted(self.data.assets)) or "(none)"
            raise KeyError(f"unknown asset '{asset_id}'. Known assets: {known}") from None

    def add_asset(self, path: str | Path, role: str = "auto", asset_id: str | None = None,
                  notes: str = "", audio_stream: int = 0) -> Asset:
        """Add a recording. ``role="auto"`` guesses it from the file's streams and name."""
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        if role != "auto" and role not in ROLES:
            raise ValueError(f"role must be auto or one of {', '.join(ROLES)}")
        info = probe(path)
        if not (info.has_video or info.has_audio):
            raise ValueError(f"{path} has neither audio nor video streams")
        if role == "auto":
            role = guess_role(path.name, info, [a.role for a in self.data.assets.values()])
        if audio_stream and audio_stream >= info.audio_streams:
            raise ValueError(f"{path.name} has {info.audio_streams} audio stream(s); "
                             f"audio_stream {audio_stream} does not exist")
        base = asset_id or (_slug(path.stem) + (f"_a{audio_stream}" if audio_stream else "")) or role
        aid = base
        i = 2
        while aid in self.data.assets:
            aid = f"{base}_{i}"
            i += 1
        asset = Asset(id=aid, path=str(path), role=role, info=info.to_dict(), notes=notes,  # type: ignore[arg-type]
                      audio_stream=audio_stream)
        self.data.assets[aid] = asset
        self.save()
        return asset

    def remove_asset(self, asset_id: str) -> None:
        self.asset(asset_id)
        del self.data.assets[asset_id]
        self.save()

    # -- analysis storage -----------------------------------------------------
    def analysis_dir(self, asset_id: str) -> Path:
        d = self.root / "analysis" / asset_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cache_dir(self, *parts: str) -> Path:
        d = self.root.joinpath("cache", *parts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def renders_dir(self) -> Path:
        d = self.root / "renders"
        d.mkdir(exist_ok=True)
        return d

    def _write_json(self, asset_id: str, name: str, data: dict) -> None:
        (self.analysis_dir(asset_id) / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def _read_json(self, asset_id: str, name: str) -> dict | None:
        p = self.root / "analysis" / asset_id / name
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save_transcript(self, asset_id: str, t: Transcript) -> None:
        self._write_json(asset_id, "transcript.json", t.to_dict())

    def transcript(self, asset_id: str) -> Transcript | None:
        d = self._read_json(asset_id, "transcript.json")
        return Transcript.from_dict(d) if d else None

    def save_speech(self, asset_id: str, s: SpeechAnalysis) -> None:
        self._write_json(asset_id, "speech.json", s.to_dict())

    def speech(self, asset_id: str) -> SpeechAnalysis | None:
        d = self._read_json(asset_id, "speech.json")
        return SpeechAnalysis.from_dict(d) if d else None

    def save_visual(self, asset_id: str, v: VisualAnalysis) -> None:
        self._write_json(asset_id, "visual.json", v.to_dict())

    def visual(self, asset_id: str) -> VisualAnalysis | None:
        d = self._read_json(asset_id, "visual.json")
        return VisualAnalysis.from_dict(d) if d else None

    # -- timeline history -----------------------------------------------------
    def snapshot(self, label: str = "") -> Path:
        hist = self.root / "history"
        hist.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        existing = self.history()
        n = _history_index(existing[-1]) + 1 if existing else 0
        name = f"{n:06d}-{stamp}{('-' + _slug(label)[:40]) if label else ''}.json"
        p = hist / name
        _atomic_write(p, self.timeline.model_dump_json(indent=1))
        return p

    def history(self) -> list[Path]:
        return sorted((self.root / "history").glob("*.json"), key=_history_index)


_SCREEN_HINT = re.compile(r"screen|ekran|display|capture|obs|desktop|masaüstü|masaustu|kayit|kayıt|recording")
_CAMERA_HINT = re.compile(r"cam|kamera|webcam|face|yuz|yüz|selfie|facecam")
_MUSIC_HINT = re.compile(r"music|müzik|muzik|song|bgm|track|soundtrack|şarkı|sarki")


def guess_role(name: str, info: MediaInfo, existing: list[str] | None = None) -> str:
    """Best guess of a recording's role from its streams and file name.

    A video file always lands on the timeline (screen or camera), so a single
    "talking" video with its own sound just works. ``existing`` is kept for callers;
    the guess depends only on the file.
    """
    n = name.lower()
    existing = existing or []
    if info.is_image:
        return "other"
    if not info.has_video:
        return "music" if _MUSIC_HINT.search(n) else "mic"
    if _CAMERA_HINT.search(n):
        return "camera"
    if _SCREEN_HINT.search(n):
        return "screen"
    # No name hint: screen recordings usually have no sound of their own, or a display's 16:10 shape;
    # anything else (phones, cameras, several takes of a scene) is camera footage.
    if not info.has_audio:
        return "screen"
    if info.width and info.height and abs(info.width / info.height - 1.6) < 0.02:
        return "screen"
    return "camera"


def _history_index(p: Path) -> int:
    head = p.name.split("-", 1)[0]
    return int(head) if head.isdigit() else -1


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _slug(s: str) -> str:
    s = s.lower()
    s = s.translate(str.maketrans("çğıöşüâîû", "cgiosuaiu"))
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s
