import pytest
from assistant.core.cache import save_qa, find_cached_answer

@pytest.mark.asyncio
async def test_same_question_different_persona_miss(db):
    await save_qa(db, "how to configure dedup", "do X", persona="platform")
    hit = await find_cached_answer(db, "how to configure dedup", persona="editor", threshold=0.01)
    assert hit is None, f"expected miss, got hit: {hit}"


@pytest.mark.asyncio
async def test_same_question_same_persona_hit(db):
    await save_qa(db, "how to configure dedup", "do X", persona="platform")
    hit = await find_cached_answer(db, "how to configure dedup", persona="platform", threshold=0.5)
    assert hit is not None
    assert hit["answer"] == "do X"


@pytest.mark.asyncio
async def test_default_persona_is_platform(db):
    # No persona arg → treated as "platform"
    await save_qa(db, "default call", "answer")
    hit = await find_cached_answer(db, "default call", threshold=0.5)
    assert hit is not None
    assert hit["answer"] == "answer"
