"""Editing session: timeline + undo + rendering helpers shared by CLI and agent."""

from __future__ import annotations

import time
from pathlib import Path

from .analysis.frames import extract_frame, format_tc, make_sheet
from .media.proxy import load_proxies
from .project import Project
from .render.engine import RenderSettings, render_still, render_timeline
from .timeline.assemble import assemble, primary_audio_asset
from .timeline.mapping import TimelineWord, words_on_timeline
from .timeline.model import Timeline
from .timeline.ops import EditError, validate


class EditorSession:
    def __init__(self, project: Project):
        self.project = project
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

    def checkpoint(self, label: str = "") -> None:
        """Save the current timeline so the next change can be undone (also across runs)."""
        self.project.snapshot(label)
        self._redo.clear()

    def _pop_snapshot(self, *, check_assets: bool = False) -> Timeline | None:
        hist = self.project.history()
        if not hist:
            return None
        last = hist[-1]
        tl = Timeline.model_validate_json(last.read_text(encoding="utf-8"))
        if check_assets:
            missing = sorted({c.asset_id for _, c in tl.all_clips()} - set(self.project.assets))
            if missing:
                raise EditError(f"cannot undo: that version uses removed asset(s) {', '.join(missing)}")
        last.unlink()
        return tl

    def rollback(self) -> None:
        """Discard changes made since the last checkpoint (used after a failed edit)."""
        tl = self._pop_snapshot()
        if tl is not None:
            self.project.timeline = tl

    def undo(self) -> bool:
        tl = self._pop_snapshot(check_assets=True)
        if tl is None:
            return False
        self._redo.append(self.timeline.model_dump_json())
        self.project.timeline = tl
        self.project.save()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        data = self._redo.pop()
        self.project.snapshot("redo")
        self.project.timeline = Timeline.model_validate_json(data)
        self.project.save()
        return True

    def commit(self) -> None:
        problems = validate(self.timeline, self.asset_durations)
        if problems:
            raise EditError("edit left the timeline inconsistent: " + "; ".join(problems[:5]))
        self.project.save()

    def assemble(self, layout: str | None = None) -> Timeline:
        self.checkpoint("assemble")
        self.project.timeline = assemble(self.project, layout=layout)
        self.commit()
        return self.timeline

    # -- transcript ---------------------------------------------------------
    def speech_sources(self) -> list[str]:
        """One speech recording per scene (falls back to the primary audio)."""
        from .scenes import speech_sources
        sources = speech_sources(self.project)
        if not sources and self.primary_audio():
            sources = [self.primary_audio()]
        return sources

    def words(self, asset_id: str | None = None) -> list[TimelineWord]:
        """Transcript words on the timeline: of one recording, or of every scene's speech recording."""
        aid = asset_id or self.timeline.captions.source_asset
        sources = [aid] if aid else self.speech_sources()
        out: list[TimelineWord] = []
        for sid in sources:
            tr = self.project.transcript(sid)
            if tr is not None:
                out += words_on_timeline(self.timeline, sid, tr)
        out.sort(key=lambda w: w.start)
        return out

    def caption_words(self) -> list[TimelineWord] | None:
        """Words to caption: only those that are actually heard (skips muted / sped-up parts)."""
        cap = self.timeline.captions
        if not cap.enabled:
            return None
        audible: dict[str, bool] = {}
        for t, c in self.timeline.all_clips():
            audible[c.id] = not t.muted and t.volume > 0 and c.volume > 0
        return [w for w in self.words(cap.source_asset) if audible.get(w.clip_id, True)]

    # -- rendering ----------------------------------------------------------
    def render(self, *, preview: bool = True, name: str | None = None, progress=None,
               preview_width: int = 640) -> Path:
        st = RenderSettings.for_timeline(self.timeline, preview=preview, preview_width=preview_width)
        if preview:
            st.proxies = load_proxies(self.project)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = self.project.renders_dir() / (name or f"{'preview' if preview else 'final'}-{stamp}.mp4")
        return render_timeline(self.timeline, self.project.assets, out, st,
                               caption_words=self.caption_words(), progress=progress)

    def still(self, t: float, *, width: int = 768) -> Path:
        st = RenderSettings.for_timeline(self.timeline, preview=True, preview_width=width)
        if width <= 800:
            st.proxies = load_proxies(self.project)
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
