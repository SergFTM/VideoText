"""Real (LLM-hitting) tests for editor tools. Skipped when no keys are set."""

import asyncio
import os
import pytest


LLM_KEYS_PRESENT = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))


@pytest.mark.skipif(not LLM_KEYS_PRESENT, reason="No LLM key — skip real tool tests")
@pytest.mark.asyncio
async def test_improve_headline_real(db):
    stream = await db.livestream.create(data={
        "url": "https://example.com", "channelName": "news channel",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "Нефть марки Брент выросла на два процента на открытии торгов",
        "quote": "Brent crude rose 2% at market open",
        "offsetSec": 120, "confidence": 0.95,
        "attribution": "news channel | 2026-04-20T10:00",
    })

    from assistant.tools.editor_tools import improve_headline
    # improve_headline is sync and calls store.get_news_item which uses asyncio.run().
    # We must run it in a thread to avoid "asyncio.run() cannot be called from a
    # running event loop" error (we're already inside an async test body).
    result = await asyncio.to_thread(improve_headline, item_id=item.id, style="острее, короче")
    assert result["ok"] is True, f"unexpected: {result}"
    assert result["old"] == item.headline
    assert result["new"] != item.headline, "LLM produced identical headline"
    assert result["new"], "new headline is empty"
    assert result["item_id"] == item.id
    assert result["field"] == "headline"
    assert "tool_call_id" in result
    assert "diff" in result


@pytest.mark.skipif(not LLM_KEYS_PRESENT, reason="No LLM key")
@pytest.mark.asyncio
async def test_rewrite_quote_real(db):
    import asyncio
    stream = await db.livestream.create(data={
        "url": "https://ex.com", "channelName": "x",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "h",
        "quote": "Цена нефти растёт из-за напряжения на ближнем востоке.",
        "offsetSec": 0, "confidence": 0.9, "attribution": "x|1",
    })
    from assistant.tools.editor_tools import rewrite_quote
    r = await asyncio.to_thread(rewrite_quote, item_id=item.id, tone="деловой")
    assert r["ok"] is True, f"unexpected: {r}"
    assert r["old"] == item.quote
    assert r["new"] != r["old"]
    assert len(r["new"]) > 20
    assert r["field"] == "quote"
    assert r["item_id"] == item.id
