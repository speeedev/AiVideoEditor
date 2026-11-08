"""Local web editor: ``ave ui PROJECT``.

A dependency-free HTTP server (stdlib) that serves a single-page editor:
assets, a visual timeline, a frame viewer / preview player and a chat
panel where Claude's work streams in live (Server-Sent Events).
Binds to localhost by default; it is meant for the person on this machine.
"""

from __future__ import annotations

import json
import mimetypes
import queue
import re
import tempfile
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .project import ROLES, Project

STATIC = Path(__file__).parent / "webui"


# Browsers play H.264/AAC QuickTime files fine when told they are MP4.
_MEDIA_TYPES = {".mov": "video/mp4", ".m4v": "video/mp4", ".mp4": "video/mp4", ".wav": "audio/wav",
                ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac", ".png": "image/png",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

_NO_PROJECT_OK = ("/", "/index.html", "/api/state", "/api/events", "/api/settings")


def _needs_project(path: str) -> bool:
    return not (path in _NO_PROJECT_OK or path.startswith("/static/") or path.startswith("/api/projects"))


def _claude_binary() -> str | None:
    from .agent.claude_code import claude_binary
    return claude_binary()


class EventBus:
    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


class Busy(RuntimeError):
    pass


class NoProject(RuntimeError):
    pass


class App:
    def __init__(self, project_path: str | None = None, *, model: str | None = None, effort: str = "high",
                 backend: str = "auto", workspace: str | None = None):
        from .agent import DEFAULT_MODEL, resolve_backend
        from .workspace import default_workspace
        self.project: Project | None = None
        self.session = None
        self.workspace = Path(workspace).expanduser() if workspace else default_workspace()
        self.lock = threading.RLock()
        self.bus = EventBus()
        self.backend = resolve_backend(backend)
        self.model = model or (DEFAULT_MODEL if self.backend == "api" else "")
        self.effort = effort
        self.job: str | None = None
        self._job_lock = threading.Lock()
        self.agent = None
        if project_path:
            self.open_project(project_path)

    # -- projects -------------------------------------------------------------
    def open_project(self, path: str) -> dict:
        from .session import EditorSession
        from .workspace import remember
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        project = Project.open(path)
        with self.lock:
            self.project = project
            self.session = EditorSession(project)
        remember(project.root)
        self.bus.publish({"type": "project"})
        return {"path": str(project.root), "name": project.data.name}

    def new_project(self, name: str, fmt: str, brief: str = "") -> dict:
        from .workspace import create_project
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        if not name.strip():
            raise ValueError("project name is empty")
        project = create_project(name, fmt, workspace=self.workspace, brief=brief)
        return self.open_project(str(project.root))

    def save(self) -> dict:
        """Everything is saved on every change; this is the explicit 'Save' for peace of mind."""
        with self.lock:
            self.project.save()
        return {"saved_at": time.strftime("%H:%M:%S")}

    def save_as(self, name: str) -> dict:
        from .workspace import duplicate_project
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        if not name.strip():
            raise ValueError("name is empty")
        with self.lock:
            self.project.save()
            copy = duplicate_project(self.project, name)
        return self.open_project(str(copy.root))

    def close_project(self) -> None:
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        with self.lock:
            self.project = None
            self.session = None
        self.bus.publish({"type": "project"})

    def launcher_state(self) -> dict[str, Any]:
        from .workspace import FORMATS, list_projects
        return {
            "project": None,
            "projects": list_projects(self.workspace),
            "workspace": str(self.workspace),
            "formats": [{"id": k, "label": v[0]} for k, v in FORMATS.items()],
            "job": self.job,
            "model": self.model,
            "effort": self.effort,
            "backend": self.backend,
            "claude_code_available": bool(_claude_binary()),
        }

    # -- jobs -----------------------------------------------------------------
    def start_job(self, name: str, fn) -> None:
        with self._job_lock:
            if self.job is not None:
                raise Busy(f"'{self.job}' is still running")
            self.job = name

        def runner():
            self.bus.publish({"type": "job", "name": name, "status": "running"})
            try:
                result = fn()
                self.bus.publish({"type": "job", "name": name, "status": "done", "result": result})
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self.bus.publish({"type": "job", "name": name, "status": "error",
                                  "error": f"{type(exc).__name__}: {exc}"})
            finally:
                with self._job_lock:
                    self.job = None
                self.bus.publish({"type": "state"})

        threading.Thread(target=runner, daemon=True).start()

    def log(self, message: str) -> None:
        self.bus.publish({"type": "log", "message": message})

    # -- state ----------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        if self.project is None:
            return self.launcher_state()
        from .scenes import scene_groups
        with self.lock:
            p = self.project
            groups = scene_groups(p)
            scene_of = {a.id: (i + 1, len(g)) for i, g in enumerate(groups) for a in g} if len(groups) > 1 else {}
            assets = []
            for a in p.assets.values():
                tr = p.transcript(a.id)
                assets.append({
                    "id": a.id, "role": a.role, "path": a.path, "name": Path(a.path).name,
                    "duration": a.duration, "has_video": a.has_video, "has_audio": a.has_audio,
                    "is_image": a.is_image, "audio_stream": a.audio_stream,
                    "scene": scene_of.get(a.id, (None, 0))[0],
                    "stabilized": bool(a.stabilized), "voice": a.uses_voice, "music_level": a.music_level,
                    "width": a.info.get("width"), "height": a.info.get("height"),
                    "sync": a.sync.model_dump() if a.sync else None,
                    "analysis": {"transcript": tr is not None, "speech": p.speech(a.id) is not None,
                                 "visual": p.visual(a.id) is not None,
                                 "words": sum(1 for _ in tr.words()) if tr else 0},
                })
            renders = sorted((f.name for f in p.renders_dir().glob("*.mp4")), reverse=True)
            words = [{"s": round(w.start, 3), "e": round(w.end, 3), "t": w.text}
                     for w in self.session.words()] if self.session.primary_audio() else []
            return {
                "project": {"name": p.data.name, "brief": p.data.brief, "canvas": p.data.canvas.model_dump(),
                            "root": str(p.root)},
                "assets": assets,
                "roles": list(ROLES),
                "timeline": json.loads(p.timeline.model_dump_json()),
                "duration": p.timeline.duration,
                "renders": renders,
                "current_render": self.current_render(),
                "caption_events": self._caption_events(),
                "words": words,
                "job": self.job,
                "history": len(p.history()),
                "model": self.model,
                "effort": self.effort,
                "backend": self.backend,
                "claude_code_available": bool(_claude_binary()),
            }

    # -- actions ----------------------------------------------------------------
    def add_asset(self, path: str, role: str, audio_stream: int = 0) -> dict:
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        if role not in ROLES:
            role = "auto"
        with self.lock:
            a = self.project.add_asset(path, role, audio_stream=int(audio_stream or 0))
        self.bus.publish({"type": "state"})
        return {"id": a.id}

    def analyze(self, opts: dict) -> None:
        from .pipeline import analyze_project

        def run():
            analyze_project(self.project, do_transcribe=bool(opts.get("transcribe", True)),
                            whisper_model=opts.get("model") or "small", language=opts.get("language") or None,
                            force=bool(opts.get("force")), log=self.log)
        self.start_job("analyze", run)

    def sync(self) -> None:
        from .pipeline import sync_project

        def run():
            with self.lock:
                sync_project(self.project, log=self.log)
        self.start_job("sync", run)

    def assemble(self, layout: str | None) -> None:
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        with self.lock:
            self.session.assemble(layout=layout or None)
        self.bus.publish({"type": "state"})

    def render(self, preview: bool) -> None:
        def progress(i: int, n: int) -> None:
            self.bus.publish({"type": "progress", "i": i, "n": n})

        def run():
            with self.lock:
                tl_copy = self.project.timeline.model_copy(deep=True)
                key = self._timeline_key()
            from .render.engine import RenderSettings, render_timeline
            st = RenderSettings.for_timeline(tl_copy, preview=preview)
            if preview:
                from .media.proxy import load_proxies
                st.proxies = load_proxies(self.project)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out = self.project.renders_dir() / f"{'preview' if preview else 'final'}-{stamp}.mp4"
            words = self.session.caption_words()
            render_timeline(tl_copy, self.project.assets, out, st, caption_words=words, progress=progress)
            # Remember which version of the edit this file shows, so "Play" knows when it is stale.
            out.with_suffix(".json").write_text(json.dumps({"timeline": key, "preview": preview}), encoding="utf-8")
            return {"file": out.name}
        self.start_job("render", run)

    def _caption_events(self) -> list:
        """Caption timing for the live player (the same grouping as the renderer)."""
        if not self.project.timeline.captions.enabled:
            return []
        from .render.captions import caption_events
        words = self.session.caption_words() or []
        return [[round(e.start, 3), round(e.end, 3), list(e.texts), e.highlight]
                for e in caption_events(words, self.project.timeline.captions)]

    def text_image(self, text_id: str, width: int, height: int) -> bytes:
        """A title exactly as the renderer draws it (for the live player)."""
        import io

        from .render.titles import render_text_image
        width, height = max(16, min(width, 3840)), max(16, min(height, 3840))
        x = next((t for t in self.project.timeline.texts if t.id == text_id), None)
        if x is None:
            raise KeyError(f"unknown text '{text_id}'")
        buf = io.BytesIO()
        render_text_image(x, width, height).save(buf, format="PNG", compress_level=1)
        return buf.getvalue()

    def media_file(self, asset_id: str, video: bool) -> Path:
        """File the live player streams: the editing proxy for picture when there is one, else the original."""
        asset = self.project.asset(asset_id)
        if video and not asset.is_image:
            from .media.proxy import load_proxies
            proxy = load_proxies(self.project).get(asset_id)
            if proxy:
                return Path(proxy.path)
            return Path(asset.video_path)
        if asset.uses_voice:
            return Path(asset.voice_path)
        return Path(asset.path)

    def _timeline_key(self) -> str:
        import hashlib
        return hashlib.sha1(self.project.timeline.model_dump_json().encode("utf-8")).hexdigest()

    def current_render(self) -> str | None:
        """Newest render that shows exactly the current edit (None when the edit changed since)."""
        key = self._timeline_key()
        for f in sorted(self.project.renders_dir().glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = f.with_suffix(".json")
            try:
                if json.loads(meta.read_text(encoding="utf-8")).get("timeline") == key:
                    return f.name
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def still(self, t: float, width: int) -> bytes:
        from .render.engine import RenderSettings, render_still
        with self.lock:
            tl = self.project.timeline.model_copy(deep=True)
            words = self.session.caption_words()
        if tl.duration <= 0:
            raise ValueError("the timeline is empty")
        st = RenderSettings.for_timeline(tl, preview=True, preview_width=width)
        if width <= 800:
            from .media.proxy import load_proxies
            st.proxies = load_proxies(self.project)
        with tempfile.TemporaryDirectory() as tmp:
            out = render_still(tl, self.project.assets, t, Path(tmp) / "f.jpg", st, caption_words=words)
            return out.read_bytes()

    def run_agent(self, instruction: str, new: bool) -> None:
        from .agent import DEFAULT_MODEL, Display, make_agent
        bus = self.bus
        app = self

        chat_log = self.project.root / "agent" / "ui_chat.jsonl"
        chat_log.parent.mkdir(exist_ok=True)

        def log_chat(role: str, text: str) -> None:
            if text.strip():
                with chat_log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"role": role, "text": text}, ensure_ascii=False) + "\n")

        log_chat("user", instruction)

        class WebDisplay(Display):
            def __init__(self) -> None:
                self.buffer = ""

            def text(self, delta: str) -> None:
                if delta:
                    self.buffer += delta
                    bus.publish({"type": "agent_text", "delta": delta})

            def thinking(self, delta: str) -> None:
                if delta:
                    bus.publish({"type": "agent_thinking", "delta": delta})

            def turn_end(self) -> None:
                log_chat("assistant", self.buffer)
                self.buffer = ""
                bus.publish({"type": "agent_turn_end"})

            def tool_call(self, name: str, args: Any) -> None:
                log_chat("assistant", self.buffer)
                self.buffer = ""
                log_chat("tool", name)
                bus.publish({"type": "tool_call", "name": name, "args": args})

            def tool_result(self, name: str, content: Any, is_error: bool) -> None:
                images, texts = [], []
                if isinstance(content, str):
                    texts.append(content)
                else:
                    for b in content:
                        if b.get("type") == "text":
                            texts.append(b["text"])
                        elif b.get("type") == "image":
                            images.append("data:image/jpeg;base64," + b["source"]["data"])
                text = "\n".join(texts)
                bus.publish({"type": "tool_result", "name": name, "is_error": is_error,
                             "text": text[:4000], "images": images})
                bus.publish({"type": "state"})

            def info(self, message: str) -> None:
                bus.publish({"type": "log", "message": message})

        def run():
            def reload_project(_name: str) -> None:
                # Claude Code runs the tools in its own MCP server process, which saves to disk.
                with app.lock:
                    app.project.load()

            if self.backend == "claude-code":
                agent = make_agent(self.session, backend="claude-code", model=self.model or None,
                                   display=WebDisplay(), on_tool_result=reload_project)
            else:
                agent = make_agent(self.session, backend="api", model=self.model or DEFAULT_MODEL,
                                   effort=self.effort, display=WebDisplay(), lock=self.lock)
            try:
                result = agent.run(instruction, new_conversation=new)
            except TypeError as exc:
                if "authentication" in str(exc).lower():
                    raise RuntimeError("Anthropic API credentials not found. Set ANTHROPIC_API_KEY, or switch the "
                                       "backend to your Claude subscription (Claude Code).") from exc
                raise
            bus.publish({"type": "agent_done", "text": result.text, "stop_reason": result.stop_reason,
                         "usage": result.summary()})
            return {"stop_reason": result.stop_reason}
        self.start_job("agent", run)

    def edit(self, tool: str, args: dict) -> dict:
        """Apply one editing tool directly (manual edits from the UI), with the same validation and undo."""
        from types import SimpleNamespace

        from .agent.tools import run_tool
        allowed = {"ripple_delete", "apply_layout", "zoom", "speed_up", "add_text", "add_marker", "set_captions",
                   "split", "delete_clips", "move_range", "color_correct", "add_transition", "move_clip",
                   "trim_clip", "update_text"}
        if tool not in allowed:
            raise ValueError(f"tool '{tool}' is not available from the UI")
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        ctx = SimpleNamespace(session=self.session, lock=self.lock, last_render=None)
        out, is_error = run_tool(ctx, tool, args)
        self.bus.publish({"type": "state"})
        text = out if isinstance(out, str) else " ".join(b.get("text", "") for b in out if b.get("type") == "text")
        if is_error:
            raise ValueError(text)
        return {"result": text}

    def drop_asset(self, asset_id: str, start: float, track_id: str | None) -> dict:
        """A recording dragged from the asset list onto the timeline."""
        from .analysis.frames import format_tc
        from .timeline.assemble import add_asset_to_timeline
        if self.job:
            raise Busy(f"'{self.job}' is still running")
        with self.lock:
            self.session.checkpoint("add to timeline")
            try:
                clip = add_asset_to_timeline(self.project, self.session.timeline, asset_id, start, track_id)
                self.session.commit()
            except Exception:
                self.session.rollback()
                raise
        self.bus.publish({"type": "state"})
        return {"clip": clip.id, "result": f"{asset_id} → {format_tc(clip.start)}–{format_tc(clip.end)}"}

    def add_animation(self, html: str, name: str, duration: float | None) -> None:
        """An HTML animation (e.g. downloaded from a Claude artifact) rendered into a clip, as a job."""
        from .pipeline import create_animation

        def run():
            asset, _ = create_animation(self.project, name, html, duration=duration, log=self.log)
            return {"asset": asset.id}
        self.start_job("animation", run)

    def asset_action(self, asset_id: str, action: str, value=None) -> None:
        """Slow per-recording processing, run as a background job."""
        from .pipeline import isolate_voice, stabilize_asset
        self.project.asset(asset_id)
        jobs = {
            "stabilize": lambda: stabilize_asset(self.project, asset_id, value or "medium", log=self.log),
            "unstabilize": lambda: stabilize_asset(self.project, asset_id, off=True, log=self.log),
            "isolate": lambda: isolate_voice(self.project, asset_id, float(value or 0.0), log=self.log),
            "original": lambda: isolate_voice(self.project, asset_id, off=True, log=self.log),
        }
        if action not in jobs:
            raise ValueError(f"unknown action '{action}'")
        self.start_job(action, lambda: (jobs[action](), None)[1])

    def reveal(self, name: str) -> None:
        """Show a render in Finder / Explorer (this app runs on the user's own machine)."""
        import subprocess
        import sys
        base = self.project.renders_dir().resolve()
        path = (base / Path(name).name).resolve()
        if not path.is_file() or not path.is_relative_to(base):
            raise FileNotFoundError(name)
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(base)])

    def conversation(self) -> list[dict]:
        """Chat history (text only) for the panel."""
        log = self.project.root / "agent" / "ui_chat.jsonl"
        if log.exists():
            out = []
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return out
        f = self.project.root / "agent" / "conversation.json"
        if not f.exists():
            return []
        out = []
        for m in json.loads(f.read_text(encoding="utf-8")):
            content = m.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for b in content or []:
                if b.get("type") == "text" and b.get("text"):
                    text = b["text"]
                    if m["role"] == "user":
                        mm = re.search(r"<request>\n?(.*?)\n?</request>", text, re.S)
                        if not mm:
                            continue
                        text = mm.group(1)
                    out.append({"role": m["role"], "text": text})
                elif b.get("type") == "tool_use":
                    out.append({"role": "tool", "text": b["name"]})
        return out


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AVE/0.1"

        def log_message(self, fmt, *args):  # quiet
            pass

        # -- helpers ------------------------------------------------------------
        def _json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, msg: str, status: int = 400) -> None:
            self._json({"error": msg}, status)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def _file(self, path: Path, content_type: str | None = None, download_name: str | None = None) -> None:
            if not path.is_file():
                self._error("not found", 404)
                return
            ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            size = path.stat().st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            status = 200
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    elif m.group(2):
                        start = max(0, size - int(m.group(2)))
                    end = min(end, size - 1)
                    status = 206
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if download_name:
                from urllib.parse import quote
                safe = re.sub(r'[^\w .\-]+', "_", download_name.encode("ascii", "replace").decode())
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(download_name)}")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)

        def _safe_child(self, base: Path, name: str) -> Path | None:
            p = (base / unquote(name)).resolve()
            return p if p.is_relative_to(base.resolve()) else None

        # -- GET ------------------------------------------------------------------
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            qs = parse_qs(url.query)
            if app.project is None and _needs_project(path):
                return self._error("no project is open", 409)
            try:
                if path in ("/", "/index.html"):
                    return self._file(STATIC / "index.html", "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    p = self._safe_child(STATIC, path[len("/static/"):])
                    return self._file(p) if p else self._error("not found", 404)
                if path == "/api/state":
                    return self._json(app.state())
                if path == "/api/conversation":
                    return self._json(app.conversation())
                if path == "/api/textimg":
                    data = app.text_image(qs.get("id", [""])[0], int(qs.get("w", ["1280"])[0]),
                                          int(qs.get("h", ["720"])[0]))
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "max-age=3600")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == "/api/still":
                    t = float(qs.get("t", ["0"])[0])
                    w = int(qs.get("w", ["960"])[0])
                    data = app.still(t, min(max(w, 160), 1920))
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                m = re.fullmatch(r"/media/([^/]+)", path)
                if m:
                    f = app.media_file(unquote(m.group(1)), video=bool(qs.get("v")))
                    ctype = _MEDIA_TYPES.get(f.suffix.lower())
                    return self._file(f, ctype)
                if path.startswith("/renders/"):
                    p = self._safe_child(app.project.renders_dir(), path[len("/renders/"):])
                    if p and qs.get("download"):
                        return self._file(p, download_name=f"{app.project.data.name} - {p.name}")
                    return self._file(p) if p else self._error("not found", 404)
                if path.startswith("/exports/"):
                    p = self._safe_child(app.project.root / "exports", path[len("/exports/"):])
                    return self._file(p) if p else self._error("not found", 404)
                if path == "/api/events":
                    return self._sse()
                return self._error("not found", 404)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:  # noqa: BLE001
                return self._error(f"{type(exc).__name__}: {exc}", 500)

        def _sse(self) -> None:
            q = app.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        data = json.dumps(ev, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.bus.unsubscribe(q)

        # -- POST -----------------------------------------------------------------
        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            qs = parse_qs(url.query)
            if app.project is None and _needs_project(path):
                return self._error("no project is open", 409)
            try:
                if path == "/api/upload":
                    return self._upload(qs)
                body = self._body()
                if path == "/api/projects/new":
                    return self._json(app.new_project(body.get("name", ""), body.get("format", "landscape_1080p"),
                                                      body.get("brief", "")))
                if path == "/api/projects/open":
                    return self._json(app.open_project(body.get("path", "")))
                if path == "/api/projects/close":
                    app.close_project()
                    return self._json({"ok": True})
                if path == "/api/save":
                    return self._json(app.save())
                if path == "/api/save_as":
                    return self._json(app.save_as(body.get("name", "")))
                if path == "/api/assets":
                    return self._json(app.add_asset(body["path"], body.get("role", "other"),
                                                    body.get("audio_stream", 0)))
                m = re.fullmatch(r"/api/assets/([^/]+)", path)
                if m:
                    aid = unquote(m.group(1))
                    if app.job:
                        raise Busy(f"'{app.job}' is still running")
                    with app.lock:
                        asset = app.project.asset(aid)
                        if body.get("delete") and app.project.timeline.clips_of_asset(aid):
                            raise ValueError(f"'{aid}' is used by the timeline; remove its clips or rebuild "
                                             "the timeline first")
                        if "role" in body:
                            if body["role"] not in ROLES:
                                return self._error("invalid role")
                            asset.role = body["role"]
                        if "notes" in body:
                            asset.notes = body["notes"]
                        if body.get("delete"):
                            app.project.remove_asset(aid)
                        app.project.save()
                    app.bus.publish({"type": "state"})
                    return self._json({"ok": True})
                if path == "/api/brief":
                    with app.lock:
                        app.project.data.brief = body.get("brief", "")
                        app.project.save()
                    return self._json({"ok": True})
                if path == "/api/analyze":
                    app.analyze(body)
                    return self._json({"ok": True})
                if path == "/api/sync":
                    app.sync()
                    return self._json({"ok": True})
                if path == "/api/assemble":
                    app.assemble(body.get("layout"))
                    return self._json({"ok": True})
                if path == "/api/render":
                    app.render(bool(body.get("preview", True)))
                    return self._json({"ok": True})
                if path == "/api/agent":
                    text = (body.get("instruction") or "").strip()
                    if not text:
                        return self._error("instruction is empty")
                    app.run_agent(text, bool(body.get("new")))
                    return self._json({"ok": True})
                if path in ("/api/undo", "/api/redo"):
                    if app.job:
                        raise Busy(f"'{app.job}' is still running")
                    with app.lock:
                        ok = app.session.undo() if path == "/api/undo" else app.session.redo()
                    app.bus.publish({"type": "state"})
                    return self._json({"ok": ok})
                if path == "/api/timeline/add":
                    return self._json(app.drop_asset(body.get("asset_id", ""), float(body.get("start") or 0.0),
                                                     body.get("track_id") or None))
                if path == "/api/asset_action":
                    app.asset_action(body.get("asset_id", ""), body.get("action", ""), body.get("value"))
                    return self._json({"ok": True})
                if path == "/api/reveal":
                    app.reveal(body.get("file", ""))
                    return self._json({"ok": True})
                if path == "/api/edit":
                    return self._json(app.edit(body.get("tool", ""), body.get("args") or {}))
                if path == "/api/export":
                    from .export import export_timeline
                    with app.lock:
                        out = export_timeline(app.project, body.get("format", "xml"))
                    return self._json({"file": out.name, "url": f"/exports/{out.name}"})
                if path == "/api/settings":
                    if "model" in body:
                        app.model = body.get("model") or ""
                    if body.get("effort") in ("low", "medium", "high", "xhigh", "max"):
                        app.effort = body["effort"]
                    if body.get("backend") in ("claude-code", "api"):
                        app.backend = body["backend"]
                    return self._json({"model": app.model, "effort": app.effort, "backend": app.backend})
                return self._error("not found", 404)
            except Busy as exc:
                return self._error(str(exc), 409)
            except (KeyError, ValueError, FileNotFoundError, FileExistsError) as exc:
                msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
                return self._error(str(msg))
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                return self._error(f"{type(exc).__name__}: {exc}", 500)

        def _upload(self, qs: dict) -> None:
            name = Path(unquote(qs.get("name", ["upload.bin"])[0])).name
            name = re.sub(r"[^\w.\-]+", "_", name) or "upload.bin"
            role = qs.get("role", ["other"])[0]
            n = int(self.headers.get("Content-Length") or 0)
            if name.lower().endswith((".html", ".htm")):
                if n > 20_000_000:
                    raise ValueError("the HTML file is too large")
                html = self.rfile.read(n).decode("utf-8", "replace")
                dur = qs.get("duration", [""])[0]
                app.add_animation(html, Path(name).stem, float(dur) if dur else None)
                return self._json({"job": "animation"})
            media = app.project.root / "media"
            media.mkdir(exist_ok=True)
            dest = media / name
            i = 2
            while dest.exists():
                dest = media / f"{Path(name).stem}_{i}{Path(name).suffix}"
                i += 1
            with dest.open("wb") as f:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            try:
                result = app.add_asset(str(dest), role if role in ROLES else "auto")
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            self._json(result)

    return Handler


def serve(project_path: str | None = None, *, host: str = "127.0.0.1", port: int = 8765, model: str | None = None,
          effort: str = "high", backend: str = "auto", open_browser: bool = True) -> None:
    app = App(project_path, model=model, effort=effort, backend=backend)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    mode = "Claude subscription (Claude Code)" if app.backend == "claude-code" else "Anthropic API key"
    where = app.project.root if app.project else f"projects in {app.workspace}"
    print(f"AI Video Editor UI ({where})\n  {url}\n  Claude: {mode}\n(Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


__all__ = ["serve", "App", "EventBus"]
