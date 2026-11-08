"""Drive the agent through the real Anthropic SDK against a local stub of the Messages API.

Verifies that the SDK accepts our request parameters, that streamed tool
inputs and thinking blocks are parsed and sent back correctly, and that
SDK content objects survive persistence.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

anthropic = pytest.importorskip("anthropic")

from aivideoeditor.agent import EditAgent  # noqa: E402


def sse(events):
    out = []
    for ev in events:
        out.append(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n")
    return "".join(out).encode()


def turn_with_tool():
    return sse([
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                              "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None,
                                              "usage": {"input_tokens": 50, "output_tokens": 1,
                                                        "cache_read_input_tokens": 0,
                                                        "cache_creation_input_tokens": 40}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Önce bakayım."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "c2lnbmF0dXJl"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Timeline'a bakıyorum."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu_01", "name": "ripple_delete", "input": {}}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '{"ranges": [{"start": 0, '}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '"end": 0.5}], "snap_to_pauses": false}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    ])


def final_turn():
    return sse([
        {"type": "message_start", "message": {"id": "msg_2", "type": "message", "role": "assistant",
                                              "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None,
                                              "usage": {"input_tokens": 5, "output_tokens": 1,
                                                        "cache_read_input_tokens": 90,
                                                        "cache_creation_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Girişteki sessizliği kestim."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ])


@pytest.fixture
def stub_api(monkeypatch):
    requests = []
    responses = [turn_with_tool(), final_turn()]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            data = responses.pop(0)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    yield f"http://127.0.0.1:{httpd.server_address[1]}", requests
    httpd.shutdown()
    httpd.server_close()


def test_agent_with_real_sdk(session, stub_api):
    base_url, requests = stub_api
    client = anthropic.Anthropic(api_key="test-key", base_url=base_url, max_retries=0)
    before = session.timeline.duration
    agent = EditAgent(session, client=client)
    result = agent.run("Girişi kısalt", new_conversation=True)

    assert result.text == "Girişteki sessizliği kestim."
    assert session.timeline.duration == pytest.approx(before - 0.5)
    assert result.usage.cache_read_input_tokens == 90 and result.usage.cost("claude-opus-5-5") > 0

    first = requests[0]
    assert first["path"].startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in first["headers"].get("anthropic-beta", "")
    body = first["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert all(t["eager_input_streaming"] is True for t in body["tools"])

    # The second request replays the assistant turn unchanged (thinking signature included)
    # followed by the tool result.
    msgs = requests[1]["body"]["messages"]
    assistant = msgs[-2]
    assert assistant["role"] == "assistant"
    kinds = [b["type"] for b in assistant["content"]]
    assert kinds == ["thinking", "text", "tool_use"]
    assert assistant["content"][0]["signature"] == "c2lnbmF0dXJl"
    assert assistant["content"][2]["input"] == {"ranges": [{"start": 0, "end": 0.5}], "snap_to_pauses": False}
    tool_result = msgs[-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_01"
    assert "Removed 0.50s" in tool_result["content"]
