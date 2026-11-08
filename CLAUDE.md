# AI Video Editor — notes for Claude Code

Python 3.10+ package `aivideoeditor` (src layout), CLI `ave`, local web UI `ave ui`. FFmpeg/ffprobe must be on PATH.

## Commands
- Install: `pip install -e ".[dev]"` (add `,transcribe` for faster-whisper)
- Tests: `pytest -q` (~2-4 min; generates synthetic media with ffmpeg; no API key needed — the agent is tested with a fake client and with the real SDK against a local stub)
- Try it: `ave demo /tmp/demo && ave ui /tmp/demo`

## Layout
- `analysis/` audio decode (always via `aresample=first_pts=0`, so sample 0 = file time 0), speech/pause detection, visual activity, frames/contact sheets, Whisper transcription + hallucination cleanup, audio sync (`ref_time = other_time + offset`).
- `timeline/model.py` pydantic timeline: times in seconds; `start/source_in/source_out/speed`; transforms normalised 0..1; video tracks stacked bottom→top; a video clip also plays its own audio at `volume`.
- `timeline/ops.py` all edits (ripple delete across unlocked tracks keeps sync; music beds are locked and auto-trimmed by `fit_music`). Raise `EditError` with an actionable message.
- `render/engine.py` chunked render: one ffmpeg run per chunk (inputs = clip segments with accurate `-ss`, keep container timestamps, `fps=start_time=0`), PCM audio concat, two-pass loudnorm at mux. Proxies (`media/proxy.py`) are used for previews/stills only.
- `agent/` Claude tool loop (`claude-opus-5-5`, adaptive thinking, effort, top-level cache_control, `fallbacks="default"`, eager input streaming + pydantic validation). The conversation in `agent/conversation.json` is append-only (preserved thinking) — never edit earlier messages. Tools live in `agent/tools.py` (`@tool(name, InputModel, mutates=...)`; mutating tools are snapshotted and rolled back on failure).
- `web.py` + `webui/` stdlib HTTP server + vanilla JS SPA; SSE for live events; the agent holds `App.lock` while mutating.
- Subscription mode: `mcp_server.py` (dependency-free stdio MCP server over the same tool registry) and `agent/claude_code.py` (drives the user's own `claude -p --output-format stream-json` with `--mcp-config`, strips API-key env vars, resumes sessions, reloads the project after each tool result). Never read or store Claude credentials.

## Conventions
- Keep the system prompt (`agent/prompts.py`) free of per-run data so it caches.
- New edit features: implement in `timeline/ops.py` (pure), expose as a tool, render support in `engine.py`, add a test that renders and checks frames/audio.
- User-facing docs (README, UI strings) are Turkish; code, comments and tool descriptions are English.
