"""Preview must not mutate DB. Apply must mutate. Proves the confirm-flow contract."""

# Import server at module level so load_dotenv(override=True) fires at collection
# time — before any test fixture sets DATABASE_URL. This ensures the db fixture's
# DATABASE_URL override is not clobbered by the server import inside the test body.
from server import app  # noqa: E402 — must be module-level for env-var ordering

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_preview_does_not_mutate_db(db):
    """Calling improve_headline stub directly returns preview but leaves NewsItem unchanged."""
    stream = await db.livestream.create(data={
        "url": "https://example.com", "channelName": "test",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "original headline",
        "quote": "q",
        "offsetSec": 0,
        "confidence": 0.9,
        "attribution": "test | 2026-01-01",
    })

    from assistant.tools.editor_tools import improve_headline
    preview = improve_headline(item_id=item.id, style="shorter")
    assert preview["ok"] is True
    assert "new" in preview

    fresh = await db.newsitem.find_unique(where={"id": item.id})
    assert fresh.headline == "original headline", (
        f"preview must not mutate DB — got headline={fresh.headline!r}"
    )


@pytest.mark.asyncio
async def test_apply_mutates_db(db):
    """POST /chat/editor/apply updates the NewsItem.headline field."""
    stream = await db.livestream.create(data={
        "url": "https://example.com/2", "channelName": "test2",
    })
    item = await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "before",
        "quote": "q",
        "offsetSec": 0,
        "confidence": 0.9,
        "attribution": "t|1",
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/editor/apply", json={
            "item_id": item.id,
            "field": "headline",
            "value": "AFTER",
            "tool_call_id": "test-abc",
        })
        assert r.status_code == 200, r.text

    # Re-read from DB (the fixture's db client is fine — apply_editor_change
    # uses its own Prisma client but writes to the same DATABASE_URL)
    updated = await db.newsitem.find_unique(where={"id": item.id})
    assert updated.headline == "AFTER"
