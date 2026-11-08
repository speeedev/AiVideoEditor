import json
from types import SimpleNamespace as NS

import pytest

from aivideoeditor.agent import Display, EditAgent
from aivideoeditor.agent.tools import REGISTRY, run_tool, tool_definitions


class FakeStream:
    def __init__(self, content, stop_reason=None):
        self.content = content
        self.stop_reason = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter([NS(type="text", text="")])

    def get_final_message(self):
        stop = self.stop_reason or ("tool_use" if any(b["type"] == "tool_use" for b in self.content) else "end_turn")
        return NS(content=self.content, stop_reason=stop, stop_details=None,
                  usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50,
                           cache_creation_input_tokens=0))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        item = self.script.pop(0)
        if isinstance(item, tuple):
            return FakeStream(*item)
        return FakeStream(item)


def use(i, name, **inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}


class Recorder(Display):
    def __init__(self):
        self.events = []

    def tool_call(self, name, args):
        self.events.append(("call", name))

    def tool_result(self, name, content, is_error):
        self.events.append(("result", name, is_error))


def test_tool_definitions_are_valid():
    defs = tool_definitions()
    names = [d["name"] for d in defs]
    assert len(names) == len(set(names)) >= 25
    for d in defs:
        assert d["input_schema"]["type"] == "object"
        assert d["input_schema"]["additionalProperties"] is False
        assert d["eager_input_streaming"] is True
        assert d["description"]
        assert "$ref" not in json.dumps(d)


def test_agent_loop_edits_and_verifies(session):
    script = [
        [{"type": "text", "text": "Önce bakıyorum."}, use(1, "get_project_overview"),
         use(2, "get_transcript", mode="phrases")],
        [use(3, "find_edit_candidates", kind="pauses", min_pause=0.6)],
        [use(4, "ripple_delete", ranges=[{"start": 0, "end": 0.4}])],
        [use(5, "view_timeline", times=[1.0, 3.0]), use(6, "apply_layout", start=1, end=2, layout="camera_only")],
        [use(7, "ripple_delete", ranges=[{"start": 5, "end": 1}]), use(8, "nonexistent_tool")],
        [{"type": "text", "text": "Tamamlandı."}],
    ]
    client = FakeClient(script)
    rec = Recorder()
    before = session.timeline.duration
    agent = EditAgent(session, client=client, display=rec)
    result = agent.run("Sessizlikleri kısalt", new_conversation=True)
    assert result.text == "Tamamlandı."
    assert result.stop_reason == "end_turn"
    assert session.timeline.duration == pytest.approx(before - 0.4, abs=0.05)
    errors = [e for e in rec.events if e[0] == "result" and e[2]]
    assert [e[1] for e in errors] == ["ripple_delete", "nonexistent_tool"]

    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["output_config"] == {"effort": "high"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert "<request>" in first["messages"][0]["content"][0]["text"]

    # The view_timeline result carried an image back to the model.
    msgs = client.calls[4]["messages"]
    tool_results = [b for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                    for b in m["content"] if b.get("type") == "tool_result"]
    images = [c for r in tool_results if isinstance(r["content"], list) for c in r["content"]
              if c.get("type") == "image"]
    assert images and images[0]["source"]["media_type"] == "image/jpeg"

    # Conversation was persisted and can be continued.
    saved = json.loads((session.project.root / "agent" / "conversation.json").read_text())
    assert saved[-1]["role"] == "assistant"
    client2 = FakeClient([[{"type": "text", "text": "Devam."}]])
    agent2 = EditAgent(session, client=client2, display=rec)
    res2 = agent2.run("Biraz daha")
    assert res2.text == "Devam."
    assert len(client2.calls[0]["messages"]) == len(saved) + 1


def test_dangling_tool_use_is_repaired(session):
    client = FakeClient([[use(1, "get_timeline")], [{"type": "text", "text": "ok"}]])
    agent = EditAgent(session, client=client, max_turns=1)
    res = agent.run("x", new_conversation=True)
    assert res.stop_reason == "max_turns"
    # Simulate a crash right after the assistant turn: drop the tool results.
    conv = session.project.root / "agent" / "conversation.json"
    msgs = json.loads(conv.read_text())
    conv.write_text(json.dumps(msgs[:-1]))
    agent2 = EditAgent(session, client=client)
    agent2.run("continue")
    sent = client.calls[-1]["messages"]
    assert sent[-2]["content"][0]["type"] == "tool_result"
    assert sent[-2]["content"][0]["is_error"] is True


def test_refusal_and_max_tokens(session):
    client = FakeClient([([use(1, "ripple_delete", ranges=[{"start": 0, "end": 1}])], "max_tokens"),
                         ([], "refusal")])
    agent = EditAgent(session, client=client)
    before = session.timeline.duration
    res = agent.run("x", new_conversation=True)
    assert res.stop_reason == "refusal"
    assert session.timeline.duration == before  # truncated tool call was not executed
    # The refused turn is not part of the saved history.
    saved = json.loads((session.project.root / "agent" / "conversation.json").read_text())
    assert saved[-1]["role"] == "user" and saved[-1]["content"][0]["type"] == "tool_result"


def test_failed_mutation_rolls_back(session):
    ctx = NS(session=session)
    before = session.timeline.model_dump_json()
    out, err = run_tool(ctx, "zoom", {"start": 1, "end": 2, "region": {"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}})
    assert err and "inside the frame" in out
    assert session.timeline.model_dump_json() == before
    out, err = run_tool(ctx, "speed_up", {"start": 1, "end": 3, "factor": "fast"})
    assert err and "Invalid input" in out
    out, err = run_tool(ctx, "speed_up", {"start": 1, "end": 3, "factor": 2})
    assert not err
    out, err = run_tool(ctx, "undo", {})
    assert not err
    assert session.timeline.model_dump_json() == before


def test_every_tool_has_a_handler():
    for name, spec in REGISTRY.items():
        assert callable(spec.handler), name


def test_interrupted_batch_records_completed_calls(session, monkeypatch):
    from aivideoeditor.agent import tools as tools_mod

    def boom(ctx, args):
        raise KeyboardInterrupt
    monkeypatch.setattr(tools_mod.REGISTRY["render_preview"], "handler", boom)
    client = FakeClient([[use(1, "ripple_delete", ranges=[{"start": 0, "end": 1}], snap_to_pauses=False),
                          use(2, "render_preview")]])
    agent = EditAgent(session, client=client)
    with pytest.raises(KeyboardInterrupt):
        agent.run("x", new_conversation=True)
    saved = json.loads((session.project.root / "agent" / "conversation.json").read_text())
    results = saved[-1]["content"]
    assert results[0]["tool_use_id"] == "toolu_1" and not results[0].get("is_error")
    assert results[1]["tool_use_id"] == "toolu_2" and results[1]["is_error"]
    assert "may or may not" in results[1]["content"]


def test_canvas_change_is_undoable(session):
    ctx = NS(session=session)
    original = session.project.data.canvas.model_copy()
    out, err = run_tool(ctx, "set_canvas", {"preset": "vertical_1080x1920"})
    assert not err and session.timeline.canvas.height == 1920
    assert session.project.data.canvas == original
    run_tool(ctx, "undo", {})
    assert session.timeline.canvas.width == original.width
