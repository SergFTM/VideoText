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


# ─── the setting → web_search_tools handoff ────────────────────────
# `int(settings.get(...) or 5)` used to turn a configured 0 — the finer of the two
# kill switches on the only per-call paid path in this app — back into 5, and 500
# the endpoint on a non-numeric value.

def _research_client(monkeypatch, settings_value):
    from fastapi.testclient import TestClient

    import server

    seen = {}

    class _Brief:
        contentJson = None

    class _Video:
        id, title, briefs, segments = "vidW", "T", [_Brief()], []

    settings = {
        "local_llm_model": "claude-sonnet-4-6",
        "research_web_search_enabled": "true",
    }
    if settings_value is not _MISSING:
        settings["research_web_search_max_uses"] = settings_value

    # `server.local_llm` IS the local_llm module — keep the original before patching.
    real_tools = local_llm.web_search_tools

    def _tools(max_uses):
        seen["max_uses"] = max_uses
        return real_tools(max_uses)

    monkeypatch.setattr(server, "get_video", lambda *a, **k: _Video())
    monkeypatch.setattr(server, "get_expansion", lambda v, m: None)
    monkeypatch.setattr(server, "get_latest_done_expansion", lambda v, m: None)
    monkeypatch.setattr(server, "get_all_settings", lambda: dict(settings))
    monkeypatch.setattr(server, "_current_doc_text", lambda v, k: "")
    monkeypatch.setattr(server, "start_expansion", lambda **kw: None)
    monkeypatch.setattr(server, "_run_expansion_job", lambda **kw: None)
    monkeypatch.setattr(server.local_llm, "web_search_tools", _tools)
    server._expansion_jobs.clear()
    return TestClient(server.app), seen


_MISSING = object()


def _run(monkeypatch, settings_value):
    client, seen = _research_client(monkeypatch, settings_value)
    r = client.post("/videos/vidW/expand-spec",
                    json={"mode": "research", "model": "claude-sonnet-4-6"})
    assert r.status_code == 200, r.text
    return seen


def test_configured_zero_disables_search(monkeypatch):
    seen = _run(monkeypatch, 0)
    assert seen["max_uses"] == 0
    assert local_llm.web_search_tools(seen["max_uses"]) is None


def test_configured_zero_as_string_disables_search(monkeypatch):
    """Settings round-trip through the DB as text, so "0" must count as 0 too."""
    assert _run(monkeypatch, "0")["max_uses"] == 0


def test_non_numeric_setting_falls_back_to_default(monkeypatch):
    assert _run(monkeypatch, "не число")["max_uses"] == 5


def test_absent_setting_falls_back_to_default(monkeypatch):
    assert _run(monkeypatch, _MISSING)["max_uses"] == 5


def test_configured_value_is_passed_through(monkeypatch):
    assert _run(monkeypatch, 2)["max_uses"] == 2
