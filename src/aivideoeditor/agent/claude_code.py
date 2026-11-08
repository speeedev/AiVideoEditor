"""Run the editing agent on the user's Claude subscription through Claude Code.

The user's own Claude Code CLI (installed and logged in with ``claude`` →
``/login``) is started in print mode with this project's MCP server attached.
Claude Code does the reasoning on the user's subscription; the MCP server
runs the editing tools. This module never reads, stores or forwards Claude
credentials. API-key variables are removed from the child environment so
Claude Code uses the subscription login rather than API billing.

Intended for personal use on your own machine with the unmodified Claude Code
binary. An app distributed to other people must use API keys instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from ..session import EditorSession
from ..timeline.describe import describe_timeline
from .agent import AgentContext, AgentResult, Display, Usage
from .prompts import SYSTEM_PROMPT, initial_user_message

SERVER = "ave"
PREFIX = f"mcp__{SERVER}__"
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def claude_binary() -> str | None:
    return os.environ.get("AVE_CLAUDE_BIN") or shutil.which("claude")


def mcp_config(project_root: Path) -> dict[str, Any]:
    return {"mcpServers": {SERVER: {"command": sys.executable,
                                    "args": ["-m", "aivideoeditor.mcp_server", str(project_root)]}}}


class ClaudeCodeAgent:
    """Same interface as :class:`EditAgent`, backed by the Claude Code CLI."""

    backend = "claude-code"

    def __init__(self, session: EditorSession, *, model: str | None = None, max_turns: int = 80,
                 display: Display | None = None, on_tool_result=None, **_ignored: Any):
        self.session = session
        self.ctx = AgentContext(session)
        self.model = model
        self.max_turns = max_turns
        self.display = display or Display()
        self.on_tool_result = on_tool_result     # e.g. the web UI reloading the project
        self.agent_dir = session.project.root / "agent"
        self.agent_dir.mkdir(exist_ok=True)
        self.binary = claude_binary()
        self.proc: subprocess.Popen | None = None
        self._streamed_text = False
        self._streamed_thinking = False

    @property
    def session_file(self) -> Path:
        return self.agent_dir / "claude_code_session.txt"

    def _session_id(self) -> str | None:
        if self.session_file.exists():
            sid = self.session_file.read_text(encoding="utf-8").strip()
            return sid or None
        return None

    def command(self, prompt: str, resume: str | None) -> list[str]:
        assert self.binary
        cmd = [self.binary, "-p", prompt, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--mcp-config", json.dumps(mcp_config(self.session.project.root)), "--strict-mcp-config",
               "--tools", "", "--allowedTools", f"{PREFIX}*",
               "--append-system-prompt", SYSTEM_PROMPT, "--max-turns", str(self.max_turns)]
        if self.model:
            cmd += ["--model", self.model]
        if resume:
            cmd += ["--resume", resume]
        return cmd

    def run(self, instruction: str, *, new_conversation: bool = False) -> AgentResult:
        if not self.binary:
            raise RuntimeError("Claude Code was not found. Install it (https://code.claude.com), run `claude` once "
                               "and log in with /login, or choose the API backend.")
        session = self.session
        if new_conversation and self.session_file.exists():
            self.session_file.unlink()
        if not session.timeline.tracks and session.project.assets:
            self.display.info("Timeline is empty: assembling the synced recordings first.")
            session.assemble()
        resume = self._session_id()
        summary = describe_timeline(session.timeline, max_clips_per_track=25)
        if resume:
            prompt = ("<current_timeline>\n" + summary + "\n</current_timeline>\n\n<request>\n"
                      + instruction.strip() + "\n</request>")
        else:
            prompt = initial_user_message(self.ctx.overview(), instruction, summary)

        env = dict(os.environ)
        removed = [v for v in CREDENTIAL_VARS if env.pop(v, None)]
        if removed:
            self.display.info(f"Ignoring {', '.join(removed)} so Claude Code uses your subscription login.")

        self.proc = subprocess.Popen(self.command(prompt, resume), cwd=str(self.agent_dir), env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        stderr_lines: list[str] = []
        drain = threading.Thread(target=lambda: stderr_lines.extend(self.proc.stderr), daemon=True)  # type: ignore[union-attr]
        drain.start()

        names: dict[str, str] = {}
        tool_calls: list[str] = []
        texts: list[str] = []
        final: dict[str, Any] | None = None
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                final = self._handle(ev, names, tool_calls, texts) or final
        except BaseException:
            self.stop()
            raise
        finally:
            self.proc.wait()
            drain.join(timeout=2)
            session.project.load()        # pick up everything the tools changed on disk

        if final is None:
            err = "".join(stderr_lines).strip()[-1500:]
            raise RuntimeError(f"Claude Code exited with code {self.proc.returncode} without a result. "
                               f"{err or 'Is it installed and logged in (run `claude` and /login)?'}")
        usage = Usage()
        u = final.get("usage") or {}
        usage.requests = int(final.get("num_turns") or 0)
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            setattr(usage, k, int(u.get(k) or 0))
        subtype = final.get("subtype") or "success"
        text = final.get("result") or "\n".join(texts).strip()
        if final.get("is_error") and not final.get("result"):
            errors = final.get("errors") or []
            text = f"Claude Code stopped ({subtype}). " + " ".join(str(e) for e in errors)
        return AgentResult(text, "end_turn" if subtype == "success" else subtype,
                           int(final.get("num_turns") or 0), usage, self.model or "claude-code",
                           None, tool_calls, note="Claude subscription via Claude Code")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _handle(self, ev: dict[str, Any], names: dict[str, str], tool_calls: list[str],
                texts: list[str]) -> dict[str, Any] | None:
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            if ev.get("session_id"):
                self.session_file.write_text(ev["session_id"], encoding="utf-8")
            for srv in ev.get("mcp_servers") or []:
                if srv.get("name") == SERVER and srv.get("status") not in (None, "connected"):
                    self.display.info(f"Editing tools server status: {srv.get('status')}")
            return None
        if etype == "stream_event":
            # --include-partial-messages: raw API stream events, so text appears while it is written.
            raw = ev.get("event") or {}
            if raw.get("type") == "content_block_delta":
                delta = raw.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self._streamed_text = True
                    self.display.text(delta["text"])
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    self._streamed_thinking = True
                    self.display.thinking(delta["thinking"])
            elif raw.get("type") == "content_block_stop" and (self._streamed_text or self._streamed_thinking):
                self.display.turn_end()
            return None
        if etype == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    texts.append(b["text"])
                    if not self._streamed_text:       # already shown piece by piece
                        self.display.text(b["text"])
                        self.display.turn_end()
                elif bt == "thinking" and b.get("thinking"):
                    if not self._streamed_thinking:
                        self.display.thinking(b["thinking"])
                        self.display.turn_end()
                elif bt == "tool_use":
                    name = str(b.get("name", ""))
                    short = name[len(PREFIX):] if name.startswith(PREFIX) else name
                    names[b.get("id", "")] = short
                    tool_calls.append(short)
                    self.display.tool_call(short, b.get("input"))
            self._streamed_text = self._streamed_thinking = False
            return None
        if etype == "user":
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if b.get("type") != "tool_result":
                        continue
                    name = names.get(b.get("tool_use_id", ""), "tool")
                    self.display.tool_result(name, _normalise_result(b.get("content")), bool(b.get("is_error")))
                    if self.on_tool_result:
                        self.on_tool_result(name)
            return None
        if etype == "result":
            if ev.get("session_id"):
                self.session_file.write_text(ev["session_id"], encoding="utf-8")
            return ev
        return None


def _normalise_result(content: Any) -> Any:
    """Tool result content from stream-json -> our display format (str or text/image blocks)."""
    if isinstance(content, str) or content is None:
        return content or ""
    out = []
    for c in content:
        if c.get("type") == "text":
            out.append({"type": "text", "text": c.get("text", "")})
        elif c.get("type") == "image":
            src = c.get("source") or {}
            data = src.get("data") or c.get("data")
            if data:
                out.append({"type": "image", "source": {"type": "base64",
                                                        "media_type": src.get("media_type") or c.get("mimeType")
                                                        or "image/jpeg", "data": data}})
    return out
