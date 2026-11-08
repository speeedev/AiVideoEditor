"""The Claude editing agent: a streaming tool-use loop over the editing tools."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..analysis.frames import format_tc
from ..session import EditorSession
from ..timeline.describe import describe_timeline
from .prompts import SYSTEM_PROMPT, initial_user_message
from .tools import run_tool, tool_definitions

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Display:
    """Receives progress events; the CLI prints them, tests collect them."""

    def text(self, delta: str) -> None: ...
    def thinking(self, delta: str) -> None: ...
    def turn_end(self) -> None: ...
    def tool_call(self, name: str, args: Any) -> None: ...
    def tool_result(self, name: str, content: Any, is_error: bool) -> None: ...
    def info(self, message: str) -> None: ...


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    requests: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        self.requests += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            setattr(self, k, getattr(self, k) + int(getattr(usage, k, 0) or 0))

    def summary(self) -> str:
        return (f"{self.requests} requests · input {self.input_tokens:,} (+{self.cache_read_input_tokens:,} cached, "
                f"{self.cache_creation_input_tokens:,} cache writes) · output {self.output_tokens:,}")


@dataclass
class AgentResult:
    text: str
    stop_reason: str
    turns: int
    usage: Usage
    last_render: Path | None = None
    tool_calls: list[str] = field(default_factory=list)


class AgentContext:
    """State shared with tool handlers."""

    def __init__(self, session: EditorSession, lock: Any = None):
        self.session = session
        self.lock = lock  # optional; held while a tool changes the timeline
        self.last_render: Path | None = None

    def overview(self) -> str:
        proj = self.session.project
        d = proj.data
        cv = d.canvas
        lines = [f"Project: {d.name}"]
        if d.brief:
            lines.append(f"Brief: {d.brief}")
        lines.append(f"Output canvas: {cv.width}x{cv.height} @ {cv.fps:g} fps")
        primary = self.session.primary_audio()
        lines.append(f"Primary speech recording (transcript/captions source): {primary or 'none'}")
        lines.append("Assets:")
        for a in proj.assets.values():
            info = a.info
            desc = [f"[{a.role}]", format_tc(a.duration)]
            if a.has_video:
                desc.append(f"{info.get('width')}x{info.get('height')}"
                            + (f" {info['fps']:.3g}fps" if info.get("fps") else ""))
            desc.append("audio" if a.has_audio else "no audio")
            if a.sync:
                if a.sync.method == "reference":
                    desc.append("sync reference")
                else:
                    desc.append(f"sync offset {a.sync.offset:+.3f}s ({a.sync.method}, confidence "
                                f"{a.sync.confidence:.1f}"
                                + (f", drift {a.sync.drift_ppm:+.0f}ppm" if a.sync.drift_ppm else "") + ")")
            if a.notes:
                desc.append(f"note: {a.notes}")
            lines.append(f"- {a.id}: " + ", ".join(desc))
        lines.append("Analysis available:")
        for a in proj.assets.values():
            have = []
            tr = proj.transcript(a.id)
            if tr is not None:
                n = sum(1 for _ in tr.words())
                have.append(f"transcript ({n} words, language {tr.language or '?'})")
            sp = proj.speech(a.id)
            if sp is not None:
                talk = sum(b - x for x, b in sp.speech)
                have.append(f"pauses (speech {100 * talk / max(sp.duration, 1e-6):.0f}% of the recording)")
            vis = proj.visual(a.id)
            if vis is not None:
                have.append(f"visual activity ({len(vis.active)} active spans, {len(vis.scene_cuts)} hard cuts)")
            if have:
                lines.append(f"- {a.id}: " + "; ".join(have))
        return "\n".join(lines)


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "to_dict"):
        return block.to_dict(mode="json")
    return block.model_dump(mode="json", exclude_none=True)


class EditAgent:
    def __init__(
        self,
        session: EditorSession,
        *,
        client: Any = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_turns: int = 80,
        display: Display | None = None,
        use_fallbacks: bool = True,
        max_tokens: int = 64000,
        lock: Any = None,
    ):
        self.session = session
        self.ctx = AgentContext(session, lock)
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.display = display or Display()
        self.use_fallbacks = use_fallbacks
        self.max_tokens = max_tokens
        self.usage = Usage()
        self.tools = tool_definitions(eager=True)
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.agent_dir = session.project.root / "agent"
        self.agent_dir.mkdir(exist_ok=True)
        self.messages: list[dict[str, Any]] = []

    # -- conversation persistence -------------------------------------------
    @property
    def conversation_file(self) -> Path:
        return self.agent_dir / "conversation.json"

    def load_conversation(self) -> bool:
        if not self.conversation_file.exists():
            return False
        self.messages = json.loads(self.conversation_file.read_text(encoding="utf-8"))
        self._repair_dangling_tool_use()
        return bool(self.messages)

    def reset_conversation(self) -> None:
        self.messages = []
        if self.conversation_file.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.conversation_file.rename(self.agent_dir / f"conversation-{stamp}.json")

    def save_conversation(self) -> None:
        tmp = self.conversation_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.messages, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.conversation_file)

    def _repair_dangling_tool_use(self) -> None:
        """A previous run may have stopped between a tool call and its result.

        Answer the open calls (appending, never editing history) so the
        conversation is valid again.
        """
        if not self.messages or self.messages[-1].get("role") != "assistant":
            return
        content = self.messages[-1].get("content") or []
        uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if uses:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                 "content": "Interrupted before this tool ran; it did not change anything."} for b in uses]})

    def _log(self, record: dict[str, Any]) -> None:
        record = {"ts": time.time(), **record}
        with (self.agent_dir / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # -- main loop --------------------------------------------------------------
    def _request(self) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            tools=self.tools,
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        with self.client.beta.messages.stream(**kwargs) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "text":
                    self.display.text(getattr(event, "text", ""))
                elif etype == "thinking":
                    self.display.thinking(getattr(event, "thinking", ""))
            message = stream.get_final_message()
        self.display.turn_end()
        return message

    def run(self, instruction: str, *, new_conversation: bool = False) -> AgentResult:
        session = self.session
        if new_conversation:
            self.reset_conversation()
        else:
            self.load_conversation()

        if not session.timeline.tracks and session.project.assets:
            self.display.info("Timeline is empty: assembling the synced recordings first.")
            session.assemble()

        summary = describe_timeline(session.timeline, max_clips_per_track=25)
        if not self.messages:
            text = initial_user_message(self.ctx.overview(), instruction, summary)
        else:
            text = ("<current_timeline>\n" + summary + "\n</current_timeline>\n\n<request>\n"
                    + instruction.strip() + "\n</request>")
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        self.save_conversation()
        self._log({"event": "request", "instruction": instruction})

        final_text = ""
        stop_reason = "max_turns"
        tool_names: list[str] = []
        json_retries = 0
        turns = 0
        finished = False
        while turns < self.max_turns:
            turns += 1
            try:
                response = self._request()
                json_retries = 0
            except ValueError as exc:
                # The SDK could not parse a streamed tool input at all; nothing was
                # appended, so simply re-issue the request (bounded).
                json_retries += 1
                self.display.info(f"Malformed tool input from the model ({exc}); retrying.")
                if json_retries > 2:
                    raise
                continue
            self.usage.add(getattr(response, "usage", None))
            content = [_block_to_dict(b) for b in response.content]
            self.messages.append({"role": "assistant", "content": content})
            self.save_conversation()
            stop_reason = response.stop_reason or "end_turn"
            self._log({"event": "response", "stop_reason": stop_reason,
                       "usage": getattr(response, "usage", None)})

            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                final_text = "The model declined this request" + (
                    f": {getattr(details, 'explanation', '')}" if details else ".")
                finished = True
                break
            if stop_reason == "pause_turn":
                continue

            uses = [b for b in content if b.get("type") == "tool_use"]
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if not uses:
                final_text = "\n".join(t for t in texts if t).strip()
                finished = True
                break

            results = []
            for b in uses:
                name, args = b["name"], b.get("input")
                tool_names.append(name)
                self.display.tool_call(name, args)
                if stop_reason == "max_tokens":
                    out, is_error = ("The response hit max_tokens and this tool input may be truncated; it was not "
                                     "run. Retry with smaller steps.", True)
                else:
                    out, is_error = run_tool(self.ctx, name, args)
                self.display.tool_result(name, out, is_error)
                self._log({"event": "tool", "name": name, "input": args, "is_error": is_error,
                           "result": out if isinstance(out, str) else "[content blocks]"})
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": b["id"], "content": out}
                if is_error:
                    block["is_error"] = True
                results.append(block)
            self.messages.append({"role": "user", "content": results})
            self.save_conversation()

        if not finished:
            stop_reason = "max_turns"
            final_text = (f"Stopped after {self.max_turns} turns. The edit so far is saved; run `ave edit` again "
                          "to let the agent continue.")
        self._log({"event": "done", "stop_reason": stop_reason, "usage": self.usage.__dict__})
        return AgentResult(final_text, stop_reason, turns, self.usage, self.ctx.last_render, tool_names)
