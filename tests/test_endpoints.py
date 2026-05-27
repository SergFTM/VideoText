import pytest
from httpx import ASGITransport, AsyncClient

from server import app


@pytest.mark.asyncio
async def test_legacy_assistant_chat_removed():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/assistant/chat", json={"question": "x"})
        assert r.status_code == 404, f"legacy alias should be gone, got {r.status_code}"


@pytest.mark.asyncio
async def test_chat_platform_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/platform", json={"question": "x"})
        assert r.status_code != 404, f"/chat/platform missing: {r.status_code}"


@pytest.mark.asyncio
async def test_chat_editor_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/editor", json={"question": "x", "item_id": 1})
        assert r.status_code != 404, f"/chat/editor missing: {r.status_code}"


@pytest.mark.asyncio
async def test_chat_editor_apply_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/chat/editor/apply", json={
            "item_id": 1, "field": "headline", "value": "x", "tool_call_id": "abc",
        })
        assert r.status_code != 404, f"/chat/editor/apply missing: {r.status_code}"


@pytest.mark.asyncio
async def test_chat_editor_sessions_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/chat/editor/sessions")
        assert r.status_code == 200, f"/chat/editor/sessions missing: {r.status_code}"
        assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_chat_platform_sessions_registered():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/chat/platform/sessions")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_news_items_search_by_q(db):
    """GET /news-items?q=TEXT filters items whose headline or quote contains TEXT (case-insensitive)."""
    from server import app

    stream = await db.livestream.create(data={"url": "https://u", "channelName": "c"})
    await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "UNIQUE_HEADLINE_MARKER alpha",
        "quote": "somebody said alpha",
        "offsetSec": 0, "confidence": 0.9, "attribution": "c|1",
    })
    await db.newsitem.create(data={
        "streamId": stream.id,
        "headline": "unrelated bravo",
        "quote": "nothing special",
        "offsetSec": 0, "confidence": 0.9, "attribution": "c|2",
    })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/news-items?q=unique_headline_marker")
        assert r.status_code == 200
        hits = r.json()
        assert len(hits) == 1, f"expected 1, got {len(hits)}: {[h['headline'] for h in hits]}"
        assert "UNIQUE_HEADLINE_MARKER" in hits[0]["headline"]


def test_transcript_routes_registered():
    paths = {r.path for r in app.routes}
    assert "/videos/{video_id}/transcript" in paths
    assert "/videos/{video_id}/transcript/edits" in paths
    assert "/videos/{video_id}/transcript/edits/{version}" in paths
    assert "/videos/{video_id}/transcript/edit" in paths
    assert "/videos/{video_id}/transcript/edits/apply" in paths


def test_stage_gate_routes_registered():
    paths = {r.path for r in app.routes}
    assert "/videos/{video_id}/stage-gates" in paths
    assert "/videos/{video_id}/stage-gates/{stage}" in paths
    assert "/videos/{video_id}/stage-assess/{stage}" in paths
