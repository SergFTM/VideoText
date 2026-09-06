"""A truncated model response must fail loudly, never be stored as content.

Measured on the production DB before this guard existed: 5 of 49 briefs had
outputTokens == exactly 2000 (the ceiling) and ended mid-word — "…job-queue на
Postgres с зависимост". Those briefs were cached, and then fed as source
material into all six downstream artifact stages.
"""
import pytest

import brief
import news_extractor
import pipeline


class _Msg:
    """Minimal stand-in for an Anthropic Message."""

    def __init__(self, text, stop_reason):
        self.stop_reason = stop_reason
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.usage = type("U", (), {
            "input_tokens": 10, "output_tokens": 20,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        })()


def test_helper_raises_only_on_max_tokens():
    brief.raise_if_truncated(_Msg("ok", "end_turn"), "бриф")
    brief.raise_if_truncated(_Msg("ok", "stop_sequence"), "бриф")
    brief.raise_if_truncated(_Msg("ok", None), "бриф")
    with pytest.raises(brief.TruncatedResponse) as e:
        brief.raise_if_truncated(_Msg("обрыв", "max_tokens"), "бриф")
    assert "бриф" in str(e.value)


def _stub_client(monkeypatch, _module, msg):
    """Patch the anthropic module itself — pipeline.py imports it inside the
    function, so there is no module attribute to patch there."""
    import anthropic

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                return msg

    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: _Client())


class _T:
    title, duration, language, source, text = "T", 60, "ru", "test", "текст"


def test_generate_brief_raises_on_truncation(monkeypatch):
    _stub_client(monkeypatch, brief, _Msg("обрезано на середине сло", "max_tokens"))
    with pytest.raises(brief.TruncatedResponse):
        brief.generate_brief(_T(), language="ru", fmt="markdown")


def test_generate_brief_passes_when_complete(monkeypatch):
    _stub_client(monkeypatch, brief, _Msg("# Полный бриф", "end_turn"))
    assert brief.generate_brief(_T(), language="ru", fmt="markdown")["content_md"] == "# Полный бриф"


def test_news_extraction_raises_on_truncation(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # guarded before the call
    _stub_client(monkeypatch, news_extractor, _Msg('{"items": [', "max_tokens"))
    with pytest.raises(brief.TruncatedResponse):
        news_extractor.extract_news_items(
            transcript_text="t", channel_name="c",
            chunk_started_at_iso="2026-01-01T00:00:00Z", language_hint="ru")


def test_checklist_assessment_raises_on_truncation(monkeypatch):
    """Truncated JSON used to be swallowed into {} — every criterion then read
    as failed, which looks identical to a genuinely incomplete artifact."""
    _stub_client(monkeypatch, pipeline, _Msg('{"domain": {"chec', "max_tokens"))
    with pytest.raises(brief.TruncatedResponse):
        pipeline.assess_checklist("research", "артефакт")


def test_brief_ceiling_is_not_2000():
    import re
    src = open("brief.py", encoding="utf-8").read()
    ceiling = int(re.search(r'"max_tokens":\s*(\d+)', src).group(1))
    assert ceiling > 2000, f"потолок брифа всё ещё {ceiling}"
