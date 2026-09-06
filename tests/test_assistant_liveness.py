"""The assistant layer was dead in four independent ways. These pin each fix.

Evidence from the production DB before the fixes: AssistantCache held real
April clicks whose stored answer is the model apologising that it could not
reach the data, and a row `expand_text on item 57` with usedCount=5 — five
clicks on five different news items all served one another's answer.
"""
import asyncio

import pytest

from assistant.core import chat as chat_mod


# ─── A. Tools must not run asyncio.run() on the live event loop ────

def test_execute_off_loop_helper_exists():
    assert hasattr(chat_mod, "_execute_off_loop"), \
        "нет обёртки, уводящей синхронный тул с event loop"


async def test_store_backed_tool_works_from_inside_a_running_loop():
    """store.* wrappers call asyncio.run(); calling them straight from an async
    handler raised RuntimeError, which execute() swallowed into {"ok": false}."""
    import store

    def sync_tool():
        return store.get_all_settings()

    # Direct call from the loop is exactly what used to happen.
    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        sync_tool()

    settings = await chat_mod._execute_off_loop(sync_tool)
    assert isinstance(settings, dict) and settings, "тул не вернул настройки"


# ─── B. Item-scoped answers must never come from a shared cache ────

async def test_cache_is_skipped_for_item_scoped_questions(monkeypatch):
    looked_up = []

    async def spy(db, question, **kw):
        looked_up.append(question)
        return None

    monkeypatch.setattr(chat_mod, "find_cached_answer", spy)
    monkeypatch.setattr(chat_mod, "build_context", lambda *a, **k: "")

    s = chat_mod.Assistant(persona=None, provider="openai", use_cache=True)

    async def drain(**kw):
        async for _ in s.ask_stream(db=None, question="улучши заголовок", **kw):
            pass

    with pytest.raises(Exception):
        await drain(ui_context={"item_id": 42})
    assert looked_up == [], "кеш опрошен для вопроса, привязанного к item_id"

    with pytest.raises(Exception):
        await drain(ui_context=None)
    assert looked_up == ["улучши заголовок"], "кеш не опрошен для общего вопроса"


# ─── C. The Ollama provider must not import a package we do not ship ──

def test_ollama_path_uses_a_declared_dependency():
    src = open("assistant/core/chat.py", encoding="utf-8").read()
    assert "import aiohttp" not in src, "aiohttp не объявлен в requirements и не установлен"
    # Match the actual lookup, not prose about it: a comment naming the old
    # variable is fine, reading it is not.
    assert 'getenv("OLLAMA_ENDPOINT"' not in src, \
        "весь остальной проект читает OLLAMA_URL — расхождение имён"
    assert 'getenv("OLLAMA_URL"' in src


# ─── D. Tools must read Prisma models, not dicts ───────────────────
# Once the tools actually run (fix A), the next wall is here: store.* returns
# Prisma model objects with camelCase attributes, but these tools subscripted
# them like dicts with snake_case keys.

def test_list_news_items_tool_returns_real_rows():
    from assistant.tools import platform_tools

    r = platform_tools.list_news_items(status="draft", limit=3)
    assert r["ok"] is True, r
    for row in r["data"]:
        assert isinstance(row["id"], int)
        assert isinstance(row["headline"], str)
        assert "stream_id" in row


def test_list_active_streams_tool_does_not_subscript_models():
    from assistant.tools import platform_tools

    r = platform_tools.list_active_streams()
    assert r["ok"] is True, r
    for row in r["data"]:
        assert isinstance(row["id"], str)
        assert "channel" in row and "chunks" in row
