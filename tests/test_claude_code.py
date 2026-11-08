"""Subscription mode: the MCP server and the Claude Code backend (with a fake `claude` CLI)."""

import json
import os
import stat
import subprocess
import sys
import textwrap

import pytest

from aivideoeditor.agent import ClaudeCodeAgent, Display, make_agent, resolve_backend
from aivideoeditor.claude_setup import write_claude_files


def rpc(proc, msg):
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    if "id" not in msg:
        return None
    return json.loads(proc.stdout.readline())


def test_mcp_server_protocol(session):
    root = session.project.root
    proc = subprocess.Popen([sys.executable, "-m", "aivideoeditor.mcp_server", str(root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        init = rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}}})
        assert init["result"]["serverInfo"]["name"] == "ave"
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert len(init["result"]["instructions"]) <= 4096
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"get_editing_guide", "ripple_delete", "scan_video", "view_timeline"} <= names
        guide = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "get_editing_guide", "arguments": {}}})
        assert "senior video editor" in guide["result"]["content"][0]["text"]
        before = session.timeline.duration
        cut = rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "ripple_delete",
                                    "arguments": {"ranges": [{"start": 0, "end": 1}], "snap_to_pauses": False}}})
        assert not cut["result"]["isError"]
        session.project.load()
        assert session.timeline.duration == pytest.approx(before - 1)      # saved to disk
        img = rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": "view_timeline", "arguments": {"times": [1.0]}}})
        kinds = [c["type"] for c in img["result"]["content"]]
        assert kinds == ["text", "image"] and img["result"]["content"][1]["mimeType"] == "image/jpeg"
        bad = rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                         "params": {"name": "zoom", "arguments": {"start": 1}}})
        assert bad["result"]["isError"]
        unknown = rpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "nope"})
        assert unknown["error"]["code"] == -32601
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    """Stand-in for the Claude Code CLI: drives the MCP server and prints stream-json."""
    import json, os, subprocess, sys
    argv = sys.argv[1:]
    log = {{"argv": argv, "has_api_key": "ANTHROPIC_API_KEY" in os.environ, "cwd": os.getcwd()}}
    with open(os.environ["FAKE_CLAUDE_LOG"], "w") as f:
        json.dump(log, f)
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["ave"]
    srv = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    def call(i, method, params=None):
        srv.stdin.write((json.dumps({{"jsonrpc": "2.0", "id": i, "method": method, "params": params or {{}}}}) + "\\n").encode())
        srv.stdin.flush()
        return json.loads(srv.stdout.readline())["result"]
    call(1, "initialize", {{"protocolVersion": "2025-06-18"}})
    out = lambda ev: print(json.dumps(ev), flush=True)
    out({{"type": "system", "subtype": "init", "session_id": "sess-42",
          "mcp_servers": [{{"name": "ave", "status": "connected"}}]}})
    out({{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "Girişi kesiyorum."}},
        {{"type": "tool_use", "id": "tu1", "name": "mcp__ave__ripple_delete",
          "input": {{"ranges": [{{"start": 0, "end": 2}}], "snap_to_pauses": False}}}}]}}}})
    r = call(2, "tools/call", {{"name": "ripple_delete",
                                 "arguments": {{"ranges": [{{"start": 0, "end": 2}}], "snap_to_pauses": False}}}})
    out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu1",
          "content": r["content"], "is_error": r["isError"]}}]}}}})
    out({{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "id": "tu2", "name": "mcp__ave__view_timeline", "input": {{"times": [1.0]}}}}]}}}})
    r = call(3, "tools/call", {{"name": "view_timeline", "arguments": {{"times": [1.0]}}}})
    content = [c if c["type"] == "text" else {{"type": "image", "source": {{"type": "base64",
               "media_type": c["mimeType"], "data": c["data"]}}}} for c in r["content"]]
    out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu2",
          "content": content}}]}}}})
    out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Tamam, 2 sn kestim."}}]}}}})
    out({{"type": "result", "subtype": "success", "is_error": False, "result": "Tamam, 2 sn kestim.",
          "num_turns": 3, "session_id": "sess-42", "total_cost_usd": 0.05,
          "usage": {{"input_tokens": 1200, "output_tokens": 300}}}})
    srv.stdin.close()
    srv.wait()
''')


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude_log.json"
    monkeypatch.setenv("AVE_CLAUDE_BIN", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-passed")
    return log


class Rec(Display):
    def __init__(self):
        self.events = []

    def text(self, delta):
        self.events.append(("text", delta))

    def tool_call(self, name, args):
        self.events.append(("call", name))

    def tool_result(self, name, content, is_error):
        kinds = "str" if isinstance(content, str) else [b["type"] for b in content]
        self.events.append(("result", name, is_error, kinds))

    def info(self, message):
        self.events.append(("info", message))


@pytest.mark.skipif(os.name == "nt", reason="shebang-based fake CLI")
def test_claude_code_backend(session, fake_claude):
    assert resolve_backend("auto") == "claude-code"
    rec = Rec()
    reloads = []
    agent = make_agent(session, backend="auto", display=rec, on_tool_result=reloads.append)
    assert isinstance(agent, ClaudeCodeAgent)
    before = session.timeline.duration
    result = agent.run("Girişi kısalt", new_conversation=True)

    assert result.text == "Tamam, 2 sn kestim."
    assert result.stop_reason == "end_turn" and result.turns == 3
    assert "subscription" in result.summary()
    assert session.timeline.duration == pytest.approx(before - 2)     # project reloaded from disk
    assert ("call", "ripple_delete") in rec.events and ("call", "view_timeline") in rec.events
    assert ("result", "view_timeline", False, ["text", "image"]) in rec.events
    assert reloads == ["ripple_delete", "view_timeline"]
    assert any(e[0] == "info" and "ANTHROPIC_API_KEY" in e[1] for e in rec.events)

    log = json.loads(fake_claude.read_text())
    argv = log["argv"]
    assert log["has_api_key"] is False                    # never passed to Claude Code
    assert argv[0] == "-p" and "<request>" in argv[1]
    for flag in ("--output-format", "--verbose", "--strict-mcp-config", "--append-system-prompt"):
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__ave__*"
    assert "--resume" not in argv

    # The next instruction resumes the same Claude Code session.
    agent2 = make_agent(session, backend="claude-code", display=Rec())
    agent2.run("Biraz daha kısalt")
    argv2 = json.loads(fake_claude.read_text())["argv"]
    assert argv2[argv2.index("--resume") + 1] == "sess-42"


def test_claude_setup_files(session):
    files = write_claude_files(session.project)
    cfg = json.loads((session.project.root / ".mcp.json").read_text())
    server = cfg["mcpServers"]["ave"]
    assert server["args"][-1] == str(session.project.root) and "aivideoeditor.mcp_server" in server["args"]
    assert "ave" in (session.project.root / "CLAUDE.md").read_text()
    assert len(files) == 2


def test_missing_claude_falls_back_to_api(session, monkeypatch):
    monkeypatch.setenv("AVE_CLAUDE_BIN", "")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert resolve_backend("auto") == "api"
    with pytest.raises(RuntimeError, match="Claude Code was not found"):
        ClaudeCodeAgent(session).run("x")
