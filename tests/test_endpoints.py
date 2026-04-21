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
