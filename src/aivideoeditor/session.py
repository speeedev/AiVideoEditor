"""Editing session: timeline + undo + rendering helpers shared by CLI and agent."""

from __future__ import annotations

import time
from pathlib import Path

from .analysis.frames import extract_frame, format_tc, make_sheet
from .project import Project
from .render.engine import RenderSettings, render_still, render_timeline
from .timeline.assemble import assemble, primary_audio_asset
from .timeline.mapping import TimelineWord, words_on_timeline
from .timeline.model import Timeline
from .timeline.ops import EditError, validate


class EditorSession:
    def __init__(self, project: Project):
        self.project = project
        self._undo: list[str] = []
        self._redo: list[str] = []

    # -- state --------------------------------------------------------------
    @property
    def timeline(self) -> Timeline:
        return self.project.timeline

    @property
    def asset_durations(self) -> dict[str, float]:
        return {aid: a.duration for aid, a in self.project.assets.items()}

    def primary_audio(self) -> str | None:
        return primary_audio_asset(self.project)

    def checkpoint(self) -> None:
        self._undo.append(self.timeline.model_dump_json())
        self._redo.clear()
        if len(self._undo) > 200:
            self._undo.pop(0)

    def rollback(self) -> None:
        """Discard changes made since the last checkpoint (used after a failed edit)."""
        if self._undo:
            self.project.timeline = Timeline.model_validate_json(self._undo.pop())

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.timeline.model_dump_json())
        self.project.timeline = Timeline.model_validate_json(self._undo.pop())
        self.project.save()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.timeline.model_dump_json())
        self.project.timeline = Timeline.model_validate_json(self._redo.pop())
        self.project.save()
        return True

    def commit(self) -> None:
        problems = validate(self.timeline, self.asset_durations)
        if problems:
            raise EditError("edit left the timeline inconsistent: " + "; ".join(problems[:5]))
        self.project.save()

    def assemble(self, layout: str | None = None) -> Timeline:
        self.checkpoint()
        self.project.timeline = assemble(self.project, layout=layout)
        self.commit()
        return self.timeline

    # -- transcript ---------------------------------------------------------
    def words(self, asset_id: str | None = None) -> list[TimelineWord]:
        aid = asset_id or self.timeline.captions.source_asset or self.primary_audio()
        if aid is None:
            return []
        tr = self.project.transcript(aid)
        if tr is None:
            return []
        return words_on_timeline(self.timeline, aid, tr)

    def caption_words(self) -> list[TimelineWord] | None:
        cap = self.timeline.captions
        if not cap.enabled:
            return None
        return self.words(cap.source_asset)

    # -- rendering ----------------------------------------------------------
    def render(self, *, preview: bool = True, name: str | None = None, progress=None,
               preview_width: int = 640) -> Path:
        st = RenderSettings.for_timeline(self.timeline, preview=preview, preview_width=preview_width)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = self.project.renders_dir() / (name or f"{'preview' if preview else 'final'}-{stamp}.mp4")
        return render_timeline(self.timeline, self.project.assets, out, st,
                               caption_words=self.caption_words(), progress=progress)

    def still(self, t: float, *, width: int = 768) -> Path:
        st = RenderSettings.for_timeline(self.timeline, preview=True, preview_width=width)
        out = self.project.cache_dir("stills") / f"t{t:010.3f}.jpg"
        return render_still(self.timeline, self.project.assets, t, out, st,
                            caption_words=self.caption_words())

    def timeline_sheet(self, times: list[float], *, thumb_width: int = 384, columns: int = 4) -> Path:
        stills = [self.still(t, width=thumb_width) for t in times]
        out = self.project.cache_dir("sheets") / f"timeline-{time.time_ns()}.jpg"
        return make_sheet(stills, [format_tc(t) for t in times], out, columns=columns)

    def source_sheet(self, asset_id: str, times: list[float], *, thumb_width: int = 384,
                     columns: int = 4) -> Path:
        asset = self.project.asset(asset_id)
        if not asset.has_video:
            raise EditError(f"asset '{asset_id}' has no video")
        d = self.project.cache_dir("frames", asset_id)
        frames = [extract_frame(asset.path, t, d / f"{t:010.3f}_{thumb_width}.jpg", max_width=thumb_width)
                  for t in times]
        out = self.project.cache_dir("sheets") / f"{asset_id}-{time.time_ns()}.jpg"
        return make_sheet(frames, [f"src {format_tc(t)}" for t in times], out, columns=columns)
