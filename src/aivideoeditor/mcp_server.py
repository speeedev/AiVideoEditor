"""MCP server exposing the editing tools to Claude Code.

This lets the editor run on a Claude subscription: the user's own, logged-in
Claude Code is the agent, and this server executes the editing tools on the
project. Nothing here reads or stores any Claude credentials.

Usage (normally started by Claude Code, see ``ave claude-setup``)::

    python -m aivideoeditor.mcp_server /path/to/project

The stdio transport is newline-delimited JSON-RPC 2.0 (Model Context Protocol).
Only stdout carries protocol messages; anything else goes to stderr.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from . import __version__
from .agent.prompts import SYSTEM_PROMPT
from .agent.tools import REGISTRY, run_tool
from .project import Project
from .session import EditorSession

SERVER_NAME = "ave"

INSTRUCTIONS = """\
Video editing tools for an AI Video Editor project (raw screen/camera/mic recordings already analysed, \
synced and laid out on a timeline). Before editing, call get_editing_guide once and follow it. Times are \
seconds on the current timeline; every edit can be undone with undo. Typical flow: get_project_overview, \
get_transcript (or scan_video / get_visual_activity for footage without speech), plan, edit with \
ripple_delete / apply_layout / zoom / speed_up / add_text / set_captions / add_music, verify with \
view_timeline, then summarise in the user's language. render_preview makes a preview video for the user."""


class _Context:
    def __init__(self, session: EditorSession):
        self.session = session
        self.lock = None
        self.last_render = None


def _mcp_content(out: Any) -> list[dict[str, Any]]:
    if isinstance(out, str):
        return [{"type": "text", "text": out}]
    blocks = []
    for b in out:
        if b.get("type") == "text":
            blocks.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "image":
            src = b["source"]
            blocks.append({"type": "image", "data": src["data"], "mimeType": src.get("media_type", "image/jpeg")})
    return blocks


def tool_list() -> list[dict[str, Any]]:
    tools = [{"name": "get_editing_guide",
              "description": "The editing guidelines for this editor (how the timeline works, editing craft, "
                             "working method). Read once at the start of a session.",
              "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
    for spec in REGISTRY.values():
        d = spec.definition(eager=False)
        tools.append({"name": d["name"], "description": d["description"], "inputSchema": d["input_schema"]})
    return tools


class MCPServer:
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.project = Project.open(project_path)
        self.session = EditorSession(self.project)
        self.ctx = _Context(self.session)

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:          # notification (e.g. notifications/initialized)
            return None
        try:
            result = self._dispatch(method, msg.get("params") or {})
        except _RPCError as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_list()}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "get_editing_guide":
                return {"content": [{"type": "text", "text": SYSTEM_PROMPT}], "isError": False}
            if name not in REGISTRY:
                raise _RPCError(-32602, f"unknown tool {name!r}")
            # Another process (the web UI, a previous run) may have changed the project on disk.
            self.project.load()
            out, is_error = run_tool(self.ctx, name, args)
            return {"content": _mcp_content(out), "isError": bool(is_error)}
        if method in ("resources/list", "prompts/list"):
            return {"resources": []} if method == "resources/list" else {"prompts": []}
        raise _RPCError(-32601, f"method not found: {method}")


class _RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def serve_stdio(project_path: str) -> None:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr          # stray prints must never corrupt the protocol stream
    server = MCPServer(project_path)
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            out.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m aivideoeditor.mcp_server PROJECT_DIR", file=sys.stderr)
        return 2
    serve_stdio(args[0])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
