"""Command line interface: ``ave <command> ...``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

from . import __version__
from .analysis.frames import format_tc

DIM, BOLD, CYAN, RED, GREEN, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[31m", "\033[32m", "\033[0m"


def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{code}{text}{RESET}"


def _open(path: str):
    from .project import Project
    return Project.open(path)


# ------------------------------------------------------------------ commands --

def cmd_init(a: argparse.Namespace) -> None:
    from .project import Project
    from .claude_setup import write_claude_files
    p = Project.create(a.project, a.name, width=a.width, height=a.height, fps=a.fps)
    if a.brief:
        p.data.brief = a.brief
        p.save()
    write_claude_files(p)
    print(f"Created project {p.root} ({a.width}x{a.height} @ {a.fps:g} fps)")


def cmd_add(a: argparse.Namespace) -> None:
    p = _open(a.project)
    for f in a.files:
        asset = p.add_asset(f, a.role, a.id if len(a.files) == 1 else None, notes=a.notes or "",
                            audio_stream=a.audio_stream)
        info = asset.info
        kind = []
        if asset.has_video:
            kind.append(f"{info.get('width')}x{info.get('height')} {info.get('fps') or 0:.3g}fps")
        if asset.has_audio:
            kind.append(f"audio {info.get('sample_rate')}Hz")
        print(f"Added {asset.id} [{asset.role}] {format_tc(asset.duration)} {' '.join(kind)}")


def cmd_brief(a: argparse.Namespace) -> None:
    p = _open(a.project)
    if a.text:
        p.data.brief = " ".join(a.text)
        p.save()
    print(p.data.brief or "(no brief)")


def cmd_analyze(a: argparse.Namespace) -> None:
    from .pipeline import analyze_project
    p = _open(a.project)
    analyze_project(p, do_transcribe=not a.no_transcribe, whisper_model=a.model, language=a.language,
                    force=a.force, transcribe_only_primary=not a.transcribe_all)
    print("Analysis done.")


def cmd_sync(a: argparse.Namespace) -> None:
    from .pipeline import set_manual_offset, sync_project
    p = _open(a.project)
    if a.manual:
        asset_id, offset = a.manual[0], float(a.manual[1])
        set_manual_offset(p, asset_id, offset)
        print(f"{asset_id}: manual offset {offset:+.3f}s")
        return
    sync_project(p, reference=a.reference, max_offset=a.max_offset)


def cmd_assemble(a: argparse.Namespace) -> None:
    from .session import EditorSession
    from .timeline.describe import describe_timeline
    s = EditorSession(_open(a.project))
    s.assemble(layout=a.layout)
    print(describe_timeline(s.timeline))


def project_status(p) -> tuple[list[str], str | None]:
    """Human-readable checklist of a project's state and the suggested next command."""
    from .timeline.assemble import primary_audio_asset
    lines: list[str] = []
    nxt: str | None = None
    root = str(p.root)
    if not p.assets:
        return ["No recordings yet."], f"ave add {root} <file> --role screen|camera|mic|music"
    lines.append("Recordings:")
    need_analysis = False
    for asset in p.assets.values():
        marks = []
        if asset.has_audio and asset.role in ("mic", "camera", "screen"):
            marks.append("pauses ✓" if p.speech(asset.id) else "pauses ✗")
        if asset.has_video and asset.role in ("screen", "camera", "broll") and not asset.is_image:
            marks.append("visual ✓" if p.visual(asset.id) else "visual ✗")
        if asset.has_audio and asset.role in ("mic", "camera", "screen"):
            marks.append("transcript ✓" if p.transcript(asset.id) else "transcript –")
        if any("✗" in m for m in marks):
            need_analysis = True
        sync = ""
        if asset.sync:
            sync = "sync ref" if asset.sync.method == "reference" else f"sync {asset.sync.offset:+.3f}s ({asset.sync.method})"
        lines.append(f"  {asset.id:<16} {asset.role:<7} {format_tc(asset.duration):>9}  {'  '.join(marks)}  {sync}")
    primary = primary_audio_asset(p)
    if primary and not p.transcript(primary):
        lines.append(f"  ! no transcript for the primary speech recording '{primary}' (Claude edits much better with one)")
    synced = all(a.sync for a in p.assets.values() if a.role in ("screen", "camera", "mic"))
    tl = p.timeline
    lines.append(f"Timeline: {len(tl.tracks)} tracks, {sum(len(t.clips) for t in tl.tracks)} clips, "
                 f"{format_tc(tl.duration)}")
    conv = p.root / "agent" / "conversation.json"
    if conv.exists():
        n = sum(1 for m in json.loads(conv.read_text(encoding="utf-8")) if m.get("role") == "assistant")
        lines.append(f"Claude conversation: {n} assistant turns")
    renders = sorted(p.renders_dir().glob("*.mp4"))
    if renders:
        lines.append(f"Latest render: {renders[-1].name}")
    if need_analysis or (primary and not p.transcript(primary)):
        nxt = f"ave analyze {root} --language tr"
    elif not synced:
        nxt = f"ave sync {root}"
    elif not tl.tracks:
        nxt = f"ave assemble {root}"
    elif not conv.exists():
        nxt = f"ave edit {root} \"<what you want>\"   (or: ave ui {root})"
    else:
        nxt = f"ave render {root} --preview"
    return lines, nxt


def cmd_status(a: argparse.Namespace) -> None:
    p = _open(a.project)
    lines, nxt = project_status(p)
    print(_c(BOLD, f"{p.data.name}") + (f" — {p.data.brief}" if p.data.brief else ""))
    print("\n".join(lines))
    if nxt:
        print(_c(GREEN, f"\nNext: {nxt}"))


def cmd_show(a: argparse.Namespace) -> None:
    from .timeline.describe import describe_timeline
    p = _open(a.project)
    print(describe_timeline(p.timeline, start=a.start, end=a.end, max_clips_per_track=a.max_clips))


def cmd_transcript(a: argparse.Namespace) -> None:
    from .analysis.transcribe import load_transcript
    from .session import EditorSession
    from .timeline.describe import format_words
    p = _open(a.project)
    if a.action == "import":
        if not a.file:
            raise SystemExit("usage: ave transcript import PROJECT --asset ID --file transcript.(json|srt|vtt)")
        asset_id = a.asset or EditorSession(p).primary_audio()
        p.asset(asset_id)
        p.save_transcript(asset_id, load_transcript(a.file))
        print(f"Imported transcript for {asset_id}")
        return
    s = EditorSession(p)
    print(format_words(s.words(a.asset), mode="words" if a.words else "phrases"))


class CliDisplay:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._in_text = False

    def text(self, delta: str) -> None:
        sys.stdout.write(delta)
        sys.stdout.flush()
        self._in_text = True

    def thinking(self, delta: str) -> None:
        if self.verbose and delta:
            sys.stdout.write(_c(DIM, delta))
            sys.stdout.flush()
            self._in_text = True

    def turn_end(self) -> None:
        if self._in_text:
            print()
            self._in_text = False

    def tool_call(self, name: str, args: Any) -> None:
        s = json.dumps(args, ensure_ascii=False)
        if len(s) > 160 and not self.verbose:
            s = s[:157] + "…"
        print(_c(CYAN, f"→ {name}") + _c(DIM, f" {s}"))

    def tool_result(self, name: str, content: Any, is_error: bool) -> None:
        if isinstance(content, str):
            first = content.strip().splitlines()[0] if content.strip() else ""
            text = content if self.verbose else first[:200]
        else:
            text = " ".join(b.get("text", "[image]") if b.get("type") == "text" else "[image]" for b in content)
        print(("  " + _c(RED, "✗ " + text)) if is_error else ("  " + _c(DIM, text)))

    def info(self, message: str) -> None:
        print(_c(DIM, message))


def _make_agent(a: argparse.Namespace, session):
    from .agent import DEFAULT_MODEL, make_agent, resolve_backend
    backend = resolve_backend(a.backend)
    display = CliDisplay(verbose=a.verbose)
    if backend == "claude-code":
        print(_c(DIM, "Using your Claude subscription through Claude Code (switch with --backend api)."))
        return make_agent(session, backend=backend, model=a.model, max_turns=a.max_turns, display=display)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE")):
        config = Path.home() / ".config" / "anthropic"
        if not config.exists():
            print(_c(RED, "No Anthropic API credentials found. Set ANTHROPIC_API_KEY, or install Claude Code "
                          "and use --backend claude-code to edit on your Claude subscription."))
    return make_agent(session, backend=backend, model=a.model or DEFAULT_MODEL, effort=a.effort,
                      max_turns=a.max_turns, display=display, use_fallbacks=not a.no_fallbacks)


def cmd_claude_setup(a: argparse.Namespace) -> None:
    from .claude_setup import write_claude_files
    p = _open(a.project)
    files = write_claude_files(p)
    print("Wrote " + ", ".join(str(f) for f in files))
    print(f"Now run Claude Code in the project folder (logged in with your subscription):\n  cd {p.root}\n  claude\n"
          "and ask it to edit the video. The editing tools appear as the 'ave' MCP server.")


def cmd_mcp(a: argparse.Namespace) -> None:
    from .mcp_server import serve_stdio
    serve_stdio(a.project)


def cmd_edit(a: argparse.Namespace) -> None:
    from .session import EditorSession
    p = _open(a.project)
    session = EditorSession(p)
    agent = _make_agent(a, session)
    instruction = " ".join(a.instruction) if a.instruction else ""
    if not instruction:
        instruction = sys.stdin.read() if not sys.stdin.isatty() else input("Instruction> ")
    result = agent.run(instruction, new_conversation=a.new)
    print()
    print(_c(BOLD, result.text))
    print(_c(DIM, f"\n[{result.stop_reason}] {result.turns} turns · {result.summary()}"))
    print(_c(DIM, f"Timeline duration: {format_tc(session.timeline.duration)}"))
    if a.render:
        _render(session, preview=True)


def cmd_chat(a: argparse.Namespace) -> None:
    from .session import EditorSession
    p = _open(a.project)
    session = EditorSession(p)
    agent = _make_agent(a, session)
    first = True
    print(_c(DIM, "Type an instruction, '/undo', '/render', '/show' or '/quit'."))
    while True:
        try:
            line = input(_c(GREEN, "\nyou> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/undo":
            print("Undone." if session.undo() else "Nothing to undo.")
            continue
        if line == "/render":
            _render(session, preview=True)
            continue
        if line == "/show":
            from .timeline.describe import describe_timeline
            print(describe_timeline(session.timeline))
            continue
        result = agent.run(line, new_conversation=a.new and first)
        first = False
        print("\n" + _c(BOLD, result.text))
        print(_c(DIM, f"[{result.summary()}] duration {format_tc(session.timeline.duration)}"))


def _render(session, *, preview: bool, output: str | None = None, width: int = 640) -> Path:
    def progress(i: int, n: int) -> None:
        sys.stdout.write(f"\rRendering chunk {min(i + 1, n)}/{n}…")
        sys.stdout.flush()
    path = session.render(preview=preview, name=None, progress=progress, preview_width=width)
    if output:
        out = Path(output).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(out))
        path = out
    print(f"\rRendered {path}                ")
    return path


def cmd_render(a: argparse.Namespace) -> None:
    from .session import EditorSession
    s = EditorSession(_open(a.project))
    _render(s, preview=a.preview, output=a.output, width=a.preview_width)


def cmd_still(a: argparse.Namespace) -> None:
    from .session import EditorSession
    s = EditorSession(_open(a.project))
    path = s.still(a.time, width=a.width)
    if a.output:
        out = Path(a.output).resolve()
        out.write_bytes(path.read_bytes())
        path = out
    print(path)


def cmd_undo(a: argparse.Namespace) -> None:
    from .session import EditorSession
    s = EditorSession(_open(a.project))
    n = 0
    for _ in range(a.steps):
        if not s.undo():
            break
        n += 1
    print(f"Undid {n} edit(s); timeline is {format_tc(s.timeline.duration)}.")


def cmd_export(a: argparse.Namespace) -> None:
    from .export import export_timeline
    p = _open(a.project)
    out = export_timeline(p, a.format, a.output)
    print(f"Exported {out}")


def cmd_demo(a: argparse.Namespace) -> None:
    from .demo import make_demo
    from .pipeline import analyze_project, sync_project
    from .project import Project
    from .session import EditorSession
    root = Path(a.project).resolve()
    media = root / "media"
    files = make_demo(media, duration=a.duration)
    p = Project.create(root, "demo", width=1280, height=720, fps=30)
    p.data.brief = "Demo: a short screen-recording tutorial with a webcam and an external mic."
    p.add_asset(files.screen, "screen")
    p.add_asset(files.camera, "camera")
    p.add_asset(files.mic, "mic")
    analyze_project(p, do_transcribe=False)
    sync_project(p)
    EditorSession(p).assemble()
    from .claude_setup import write_claude_files
    write_claude_files(p)
    print(f"\nDemo project ready at {root}. Try:\n  ave show {root}\n  ave render {root} --preview\n"
          f"  ave edit {root} \"Remove long pauses and add a title\"")


def cmd_ui(a: argparse.Namespace) -> None:
    from .web import serve
    serve(a.project, host=a.host, port=a.port, model=a.model, effort=a.effort, backend=a.backend,
          open_browser=not a.no_browser)


# -------------------------------------------------------------------- parser --

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ave",
        description="AI video editor: Claude edits a timeline built from your raw footage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            typical workflow:
              ave init myvideo --brief "YouTube tutorial about X"
              ave add myvideo screen.mp4 --role screen
              ave add myvideo webcam.mp4 --role camera
              ave add myvideo mic.wav --role mic
              ave analyze myvideo --language tr
              ave sync myvideo
              ave edit myvideo "Tighten the pacing, remove mistakes, add captions"
              ave render myvideo -o final.mp4
        """),
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    def agent_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--backend", default=os.environ.get("AVE_BACKEND", "auto"),
                       choices=["auto", "claude-code", "api"],
                       help="claude-code: your Claude subscription via the Claude Code CLI; api: ANTHROPIC_API_KEY; "
                            "auto (default): Claude Code when installed")
        p.add_argument("--model", default=os.environ.get("AVE_MODEL"),
                       help="model id (default: claude-opus-5-5 for api, Claude Code's default otherwise)")
        p.add_argument("--effort", default=os.environ.get("AVE_EFFORT", "high"),
                       choices=["low", "medium", "high", "xhigh", "max"])
        p.add_argument("--max-turns", type=int, default=80)
        p.add_argument("--new", action="store_true", help="start a new conversation instead of continuing")
        p.add_argument("--no-fallbacks", action="store_true", help="disable server-side refusal fallbacks")
        p.add_argument("-v", "--verbose", action="store_true", help="show reasoning summaries and full results")

    p = sub.add_parser("init", help="create a project")
    p.add_argument("project")
    p.add_argument("--name")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--brief", help="what the video is for, audience, style, target length")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="add recordings to a project")
    p.add_argument("project")
    p.add_argument("files", nargs="+")
    p.add_argument("--role", default="other",
                   choices=["screen", "camera", "mic", "music", "broll", "sfx", "other"])
    p.add_argument("--id", help="asset id (single file only)")
    p.add_argument("--notes", help="free-form notes for the editor about this asset")
    p.add_argument("--audio-stream", type=int, default=0,
                   help="use this audio stream of the file (e.g. 1 for the second track of an OBS recording)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("brief", help="show or set the project brief")
    p.add_argument("project")
    p.add_argument("text", nargs="*")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("analyze", help="transcribe and analyse the recordings")
    p.add_argument("project")
    p.add_argument("--no-transcribe", action="store_true")
    p.add_argument("--model", default="small", help="faster-whisper model (tiny, base, small, medium, large-v3)")
    p.add_argument("--language", help="speech language code, e.g. tr or en (auto-detect if omitted)")
    p.add_argument("--transcribe-all", action="store_true", help="transcribe every recording with speech")
    p.add_argument("--force", action="store_true", help="redo analysis that already exists")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("sync", help="align recordings by their audio")
    p.add_argument("project")
    p.add_argument("--reference", help="asset id to use as the master clock (default: mic > camera > screen)")
    p.add_argument("--max-offset", type=float, help="limit the search to ± this many seconds")
    p.add_argument("--manual", nargs=2, metavar=("ASSET", "OFFSET"),
                   help="set an offset by hand (seconds on the reference clock where ASSET starts)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("assemble", help="(re)build the timeline from the synced recordings")
    p.add_argument("project")
    p.add_argument("--layout", choices=["screen_only", "camera_only", "pip", "side_by_side", "stack"])
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("status", help="what has been done and what to do next")
    p.add_argument("project")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show", help="print the timeline")
    p.add_argument("project")
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--max-clips", type=int, default=60)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("transcript", help="show the transcript on the timeline, or import one")
    p.add_argument("action", choices=["show", "import"])
    p.add_argument("project")
    p.add_argument("--asset")
    p.add_argument("--file")
    p.add_argument("--words", action="store_true", help="show word timestamps")
    p.set_defaults(func=cmd_transcript)

    p = sub.add_parser("edit", help="let Claude edit the timeline according to an instruction")
    p.add_argument("project")
    p.add_argument("instruction", nargs="*")
    p.add_argument("--render", action="store_true", help="render a preview afterwards")
    agent_opts(p)
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("chat", help="interactive editing session with Claude")
    p.add_argument("project")
    agent_opts(p)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("render", help="render the timeline to a video file")
    p.add_argument("project")
    p.add_argument("-o", "--output")
    p.add_argument("--preview", action="store_true", help="fast low-resolution render")
    p.add_argument("--preview-width", type=int, default=640)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("still", help="render one frame of the timeline")
    p.add_argument("project")
    p.add_argument("time", type=float)
    p.add_argument("-o", "--output")
    p.add_argument("--width", type=int, default=1280)
    p.set_defaults(func=cmd_still)

    p = sub.add_parser("undo", help="undo the last timeline change(s)")
    p.add_argument("project")
    p.add_argument("--steps", type=int, default=1)
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("export", help="export the timeline (FCP7 XML, OTIO, EDL) or subtitles (SRT)")
    p.add_argument("project")
    p.add_argument("--format", choices=["otio", "xml", "edl", "json", "srt"], default="xml")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("demo", help="create a demo project with synthetic footage")
    p.add_argument("project")
    p.add_argument("--duration", type=float, default=30.0)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("claude-setup", help="write .mcp.json and CLAUDE.md so Claude Code can edit this project")
    p.add_argument("project")
    p.set_defaults(func=cmd_claude_setup)

    p = sub.add_parser("mcp", help="run the editing-tools MCP server on stdio (started by Claude Code)")
    p.add_argument("project")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ui", help="open the local web editor (timeline, preview, chat with Claude)")
    p.add_argument("project")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--backend", default=os.environ.get("AVE_BACKEND", "auto"), choices=["auto", "claude-code", "api"])
    p.add_argument("--model", default=os.environ.get("AVE_MODEL"))
    p.add_argument("--effort", default=os.environ.get("AVE_EFFORT", "high"),
                   choices=["low", "medium", "high", "xhigh", "max"])
    p.set_defaults(func=cmd_ui)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except (FileNotFoundError, FileExistsError, KeyError, ValueError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(_c(RED, f"error: {msg}"), file=sys.stderr)
        return 1
    except Exception as exc:  # anthropic API errors and ffmpeg failures
        name = type(exc).__name__
        print(_c(RED, f"error ({name}): {exc}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
