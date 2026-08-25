"""End-to-end smoke tests against real providers. Skipped by default.

Run with: RUN_SMOKE=1 .venv/Scripts/python.exe -m pytest tests/test_smoke.py -v
"""
import json
import os
import pytest
from httpx import ASGITransport, AsyncClient

# Import at module level — server.load_dotenv runs on import, so this must happen
# BEFORE the test body would read env overrides (lesson from Task 1.3).
from server import app


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SMOKE") != "1",
    reason="Set RUN_SMOKE=1 to run real-provider smoke tests",
)


@pytest.mark.asyncio
async def test_platform_chat_stream():
    """POST /chat/platform returns a valid SSE stream with text + done events."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=30.0,
    ) as ac:
        async with ac.stream("POST", "/chat/platform", json={
            "question": "скажи одно слово: ok",
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
        }) as r:
            assert r.status_code == 200, f"unexpected status: {r.status_code}"
            events: list[dict] = []
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            assert any(e["type"] == "text" for e in events), f"no text events: {events}"
            assert any(e["type"] == "done" for e in events), f"no done event: {events}"


@pytest.mark.asyncio
async def test_editor_chat_stream():
    """POST /chat/editor returns a valid SSE stream and reaches done."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=30.0,
    ) as ac:
        async with ac.stream("POST", "/chat/editor", json={
            "question": "скажи одно слово: ok",
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
        }) as r:
            assert r.status_code == 200, f"unexpected status: {r.status_code}"
            events: list[dict] = []
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            assert any(e["type"] == "done" for e in events), f"no done event: {events}"
