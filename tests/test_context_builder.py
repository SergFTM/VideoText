import pytest

from assistant.personas.platform import PlatformPersona
from assistant.personas.editor import EditorPersona


@pytest.mark.asyncio
async def test_platform_context_excludes_news_content(db):
    """Platform persona should not see news item headlines in its context."""
    await db.appsetting.create(data={"key": "dedup_enabled", "value": "true"})
    stream = await db.livestream.create(data={"url": "u", "channelName": "c"})
    await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "SECRET_HEADLINE_XYZ_PLATFORM_MUSTNT_SEE_THIS",
        "quote": "q", "offsetSec": 0, "confidence": 0.9, "attribution": "c|1",
    })

    from assistant.core.context_builder import build_context
    ctx = await build_context(db, question="how to configure dedup", persona=PlatformPersona())
    assert "SECRET_HEADLINE_XYZ" not in str(ctx).upper()


@pytest.mark.asyncio
async def test_editor_context_includes_recent_items(db):
    """Editor persona should see recent news items in its context (same stream)."""
    stream = await db.livestream.create(data={"url": "u", "channelName": "c"})
    await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "UNIQUE_EDITOR_MARKER_MUST_APPEAR",
        "quote": "q", "offsetSec": 0, "confidence": 0.9, "attribution": "c|1",
    })

    from assistant.core.context_builder import build_context
    ctx = await build_context(
        db, question="improve this headline",
        persona=EditorPersona(),
        ui_context={"stream_id": stream.id},
    )
    assert "UNIQUE_EDITOR_MARKER" in str(ctx).upper()


@pytest.mark.asyncio
async def test_editor_context_excludes_settings_snapshot(db):
    """Editor should not see platform settings / app config."""
    await db.appsetting.create(data={"key": "UNIQUE_SETTING_KEY", "value": "42"})

    from assistant.core.context_builder import build_context
    ctx = await build_context(db, question="rewrite headline", persona=EditorPersona())
    assert "UNIQUE_SETTING_KEY" not in str(ctx)
