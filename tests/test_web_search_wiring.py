"""Wiring only: that the search tool reaches the Anthropic call for research and
nowhere else. The search itself runs server-side at Anthropic — nothing to mock."""
import local_llm
import transcript_edit


def test_stream_chat_forwards_tools_to_claude(monkeypatch):
    seen = {}

    def fake_stream_claude(system, user, model, tools=None):
        seen["tools"] = tools
        yield "ok"

    monkeypatch.setattr(transcript_edit, "_stream_claude", fake_stream_claude)
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    list(local_llm.stream_chat(system="s", user="u", model="claude-sonnet-4-6", tools=tools))
    assert seen["tools"] == tools


def test_stream_chat_ignores_tools_on_ollama(monkeypatch):
    """Ollama has no server-side tools; passing them must not crash or leak into the payload."""
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self): return iter([b'{"message":{"content":"hi"},"done":true}'])

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(local_llm.urllib.request, "urlopen", fake_urlopen)
    out = list(local_llm.stream_chat(
        system="s", user="u", model="qwen2.5:7b",
        tools=[{"type": "web_search_20250305", "name": "web_search"}]))
    assert out == ["hi"]
    assert "web_search" not in captured["body"]


def test_research_tools_builder_respects_max_uses():
    tools = local_llm.web_search_tools(max_uses=3)
    assert tools == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]


def test_web_search_tools_clamps_nonsense():
    assert local_llm.web_search_tools(max_uses=0) is None
    assert local_llm.web_search_tools(max_uses=-2) is None
