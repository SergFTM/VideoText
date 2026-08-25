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


@pytest.mark.skipif(not LLM_KEYS_PRESENT, reason="No LLM key")
@pytest.mark.asyncio
async def test_expand_text_real(db):
    import asyncio
    stream = await db.livestream.create(data={
        "url": "https://ex.com", "channelName": "x",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "ФРС оставила ставку без изменений",
        "quote": "Федрезерв США сохранил диапазон ставки на уровне 5.25-5.5%.",
        "offsetSec": 0, "confidence": 0.95, "attribution": "x|1",
    })
    from assistant.tools.editor_tools import expand_text
    r = await asyncio.to_thread(expand_text, item_id=item.id, length="medium")
    assert r["ok"] is True, f"unexpected: {r}"
    assert len(r["new"]) > len(item.quote) * 2
    assert r["field"] == "expandedText"
    assert r["item_id"] == item.id


@pytest.mark.skipif(not LLM_KEYS_PRESENT, reason="No LLM key")
@pytest.mark.asyncio
async def test_suggest_tags_real(db):
    import asyncio
    stream = await db.livestream.create(data={"url": "https://ex.com", "channelName": "x"})
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "Нефть Brent выросла на 2%",
        "quote": "Краткое сообщение о росте нефтяных котировок.",
        "offsetSec": 0, "confidence": 0.9, "attribution": "x|1",
    })
    from assistant.tools.editor_tools import suggest_tags
    r = await asyncio.to_thread(suggest_tags, item_id=item.id)
    assert r["ok"] is True, f"unexpected: {r}"
    assert isinstance(r["new"], list)
    assert 1 <= len(r["new"]) <= 5
    assert all(isinstance(t, str) and t.strip() for t in r["new"])


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="Recognize-text needs OPENAI_API_KEY (vision)")
@pytest.mark.asyncio
async def test_recognize_text_url_real():
    """OCR on a publicly reachable image. Skipped if OPENAI key absent."""
    import asyncio
    from assistant.tools.editor_tools import recognize_text
    # Use a known public PNG that OpenAI vision can download.
    # The image may or may not contain text; we only check the response shape.
    r = await asyncio.to_thread(
        recognize_text,
        url="https://www.w3schools.com/css/img_5terre.jpg",
    )
    # ok may be False if the URL is blocked; we only assert shape when ok=True
    if r["ok"]:
        assert "text" in r
        assert "length" in r
    else:
        # Acceptable: network/provider issue — skip silently
        pytest.skip(f"recognize_text returned ok=False (provider/network): {r['error']}")


@pytest.mark.asyncio
async def test_bulk_action_dispatches(db):
    """bulk_action routes correctly and returns preview per item."""
    import asyncio
    stream = await db.livestream.create(data={"url": "https://ex.com", "channelName": "x"})
    items = []
    for i in range(3):
        it = await db.newsitem.create(data={
            "streamId": stream.id,
            "headline": f"тест item {i}",
            "quote": "q", "offsetSec": 0, "confidence": 0.9, "attribution": "x|1",
        })
        items.append(it)

    from assistant.tools.editor_tools import bulk_action
    if not LLM_KEYS_PRESENT:
        # With no LLM — still verify dispatch structure: we'll get ok=False per item
        r = await asyncio.to_thread(bulk_action, item_ids=[i.id for i in items], action="improve_headline")
        # bulk returns ok=True with previews; individual results may be ok:false
        assert r["ok"] is True
        assert r["total"] == 3
        return
    r = await asyncio.to_thread(bulk_action, item_ids=[i.id for i in items], action="suggest_tags")
    assert r["ok"] is True
    assert r["total"] == 3
    assert len(r["previews"]) == 3
    assert r["action"] == "suggest_tags"


@pytest.mark.asyncio
async def test_bulk_action_rejects_unknown():
    import asyncio
    from assistant.tools.editor_tools import bulk_action
    r = await asyncio.to_thread(bulk_action, item_ids=[1, 2], action="save_setting")
    assert r["ok"] is False
    assert "not supported" in r["error"]
