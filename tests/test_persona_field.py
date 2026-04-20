"""Verify the persona column exists on all three tables and defaults correctly."""
import pytest

@pytest.mark.asyncio
async def test_persona_default_on_session(db):
    s = await db.assistantsession.create(data={})
    assert s.persona == "platform"

@pytest.mark.asyncio
async def test_persona_default_on_message(db):
    s = await db.assistantsession.create(data={})
    m = await db.assistantmessage.create(data={
        "sessionId": s.id, "role": "user", "content": "hi",
    })
    assert m.persona == "platform"

@pytest.mark.asyncio
async def test_persona_default_on_cache(db):
    c = await db.assistantcache.create(data={
        "question": "why", "answer": "because",
    })
    assert c.persona == "platform"
