import pytest
from httpx import ASGITransport, AsyncClient

from server import app


@pytest.mark.asyncio
async def test_legacy_assistant_chat_removed():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/assistant/chat", json={"question": "x"})
        assert r.status_code == 404, f"legacy alias should be gone, got {r.status_code}"


# These three assert route REGISTRATION, which is all their names ever claimed.
# They used to prove it by firing a real request without the `db` fixture, so
# they hit the database named in .env — the user's PRODUCTION one. The apply
# test wrote {"item_id": 1, "field": "headline", "value": "x"} and permanently
# destroyed the headline of a real news item; the other two appended a garbage
# "x" row to the live assistant cache on every single run. Checking app.routes
# proves the same thing with no side effects, and matches the pattern the rest
# of this file already uses (see test_transcript_routes_registered below).

def test_chat_platform_registered():
    assert "/chat/platform" in {r.path for r in app.routes}


def test_chat_editor_registered():
    assert "/chat/editor" in {r.path for r in app.routes}


def test_chat_editor_apply_registered():
    assert "/chat/editor/apply" in {r.path for r in app.routes}


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


def test_docs_routes_registered():
    paths = {r.path for r in app.routes}
    assert "/videos/{video_id}/docs/{kind}" in paths
    assert "/videos/{video_id}/docs/{kind}/edits" in paths
    assert "/videos/{video_id}/docs/{kind}/edit" in paths
    assert "/videos/{video_id}/docs/{kind}/edits/apply" in paths
    # legacy transcript aliases must still exist
    assert "/videos/{video_id}/transcript" in paths
    assert "/videos/{video_id}/transcript/edits/apply" in paths


# ─── Expansion markdown download ───────────────────────────────────
# The UI has always rendered a ".md" link next to ".pdf" for artifacts, but the
# route was never registered — the request fell through to the bare "{mode}"
# route, where mode="research.md" fails ExpandMode's Literal validation and the
# user got a 422 JSON body instead of a file.

class _DoneExpansion:
    contentMd = "# Ресерч\n\nтело артефакта"
    sourceTitle = "бриф"


def test_expansion_md_route_registered():
    paths = {r.path for r in app.routes}
    assert "/videos/{video_id}/expansions/{mode}.md" in paths
    assert "/videos/{video_id}/expansions/{mode}.pdf" in paths


def test_expansion_md_download_returns_markdown(monkeypatch):
    import server
    monkeypatch.setattr(server, "get_latest_done_expansion",
                        lambda v, m: _DoneExpansion())
    from fastapi.testclient import TestClient
    r = TestClient(server.app).get("/videos/vid1/expansions/research.md")
    assert r.status_code == 200, f"было 422 — маршрута .md не существовало: {r.text[:200]}"
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    assert "research-vid1.md" in r.headers["content-disposition"]
    assert r.text == _DoneExpansion.contentMd


def test_expansion_md_404_when_no_artifact(monkeypatch):
    import server
    monkeypatch.setattr(server, "get_latest_done_expansion", lambda v, m: None)
    from fastapi.testclient import TestClient
    r = TestClient(server.app).get("/videos/vid1/expansions/report.md")
    assert r.status_code == 404
