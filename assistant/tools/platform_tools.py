"""Callable tools the assistant invokes via function-calling.

All tools follow the same contract:
  - `name`: stable string used in the function-call schema
  - `description`: natural language — the model reads this to decide when to call
  - `parameters`: JSON Schema (OpenAI / Anthropic compatible)
  - `execute(**kwargs) -> dict`: the actual implementation

Writes require `confirm=True`. The chat layer sets this only after the user
clicks approve in the UI — prevents the model from silently mutating state.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from prisma import Prisma

import store


# ─── Helpers ──────────────────────────────────────────────────────────


def _run_async(coro):
    """Bridge async DB calls to the sync tool API."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Called from within FastAPI — use a dedicated loop on a thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(coro)).result()
    except RuntimeError:
        pass
    return asyncio.run(coro)


def _ok(data: Any, msg: str = "") -> dict:
    return {"ok": True, "data": data, "msg": msg}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


# ─── Tool implementations ─────────────────────────────────────────────


def get_settings_snapshot() -> dict:
    """Return current settings + integration key presence (keys masked)."""
    settings = store.get_all_settings()
    key_env = {
        "supadata": bool(os.getenv("SUPADATA_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "pexels": bool(os.getenv("PEXELS_API_KEY")),
    }
    return _ok({"settings": settings, "keys_configured": key_env})


def test_integration(provider: str) -> dict:
    """Ping a connector to check if it works. Same logic as /config/test."""
    valid = {"supadata", "anthropic", "openai", "fastembed", "ollama", "pexels"}
    if provider not in valid:
        return _err(f"unknown provider: {provider}. valid: {sorted(valid)}")

    # Avoid circular import: call via requests against own server would loop.
    # Instead, inline the same logic from server.config_test.
    try:
        if provider == "supadata":
            key = os.getenv("SUPADATA_API_KEY")
            if not key:
                return _err("SUPADATA_API_KEY не задан")
            from supadata import Supadata
            client = Supadata(api_key=key)
            resp = client.youtube.transcript(video_id="jNQXAC9IVRw", text=True)
            sample = (getattr(resp, "content", "") or "")[:60]
            return _ok({"sample": sample}, f"Supadata OK: \"{sample}…\"")

        if provider == "anthropic":
            key = os.getenv("ANTHROPIC_API_KEY")
            if not key:
                return _err("ANTHROPIC_API_KEY не задан")
            import anthropic
            c = anthropic.Anthropic()
            msg = c.messages.create(
                model="claude-haiku-4-5", max_tokens=10,
                messages=[{"role": "user", "content": "say ok"}],
            )
            text = next((b.text for b in msg.content if b.type == "text"), "")
            return _ok({"reply": text.strip()}, f"Claude OK: \"{text.strip()}\"")

        if provider == "openai":
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                return _err("OPENAI_API_KEY не задан")
            from dedup import _openai_compat_embed
            vec = _openai_compat_embed("smoke test", "")
            return _ok({"dim": len(vec)}, f"OpenAI OK: {len(vec)}-dim embedding")

        if provider == "fastembed":
            try:
                from dedup import _fastembed_embed
                vec = _fastembed_embed("smoke test", "")
                return _ok({"dim": len(vec)}, f"fastembed OK: {len(vec)}-dim")
            except ImportError:
                return _err("fastembed не установлен. pip install fastembed")

        if provider == "ollama":
            import urllib.request
            try:
                with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
                    data = json.loads(r.read())
                models = [m["name"] for m in data.get("models", [])]
                if not models:
                    return _ok({"models": []}, "Ollama работает, но моделей нет. ollama pull nomic-embed-text")
                return _ok({"models": models}, f"Ollama OK: {', '.join(models[:5])}")
            except Exception as e:
                return _err(f"Ollama не отвечает: {e}")

        if provider == "pexels":
            key = os.getenv("PEXELS_API_KEY")
            if not key:
                return _err("PEXELS_API_KEY не задан")
            from enrich import fetch_pexels_photo
            result = fetch_pexels_photo("oil barrel")
            if not result:
                return _err("Pexels вернул пусто (проверь ключ/квоту)")
            img_bytes, source_url = result
            return _ok({"bytes": len(img_bytes), "source": source_url},
                       f"Pexels OK: {len(img_bytes)//1024} KB")

        return _err(f"провайдер {provider} не реализован")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def get_recent_errors(minutes: int = 60, limit: int = 20) -> dict:
    """Recent errors from Run + Chunk tables (last N minutes)."""
    since = datetime.utcnow() - timedelta(minutes=minutes)

    async def _fetch():
        db = Prisma()
        await db.connect()
        try:
            runs = await db.run.find_many(
                where={"status": "error", "startedAt": {"gte": since}},
                order={"startedAt": "desc"},
                take=limit,
            )
            chunks = await db.chunk.find_many(
                where={"status": "failed", "createdAt": {"gte": since}},
                order={"createdAt": "desc"},
                take=limit,
            )
            return {
                "runs": [
                    {"url": r.url, "error": r.error, "at": r.startedAt.isoformat()}
                    for r in runs
                ],
                "chunks": [
                    {"stream_id": c.streamId, "index": c.index, "error": c.error,
                     "at": c.createdAt.isoformat()}
                    for c in chunks
                ],
            }
        finally:
            await db.disconnect()

    return _ok(_run_async(_fetch()))


def list_active_streams() -> dict:
    """Currently active/paused live streams with chunk counts."""
    # store.list_streams returns Prisma LiveStream models, not dicts — camelCase
    # attributes, and `chunks` is the included relation rather than a count field.
    streams = store.list_streams(status="active") + store.list_streams(status="paused")
    return _ok([
        {
            "id": s.id, "url": s.url, "channel": s.channelName,
            "status": s.status, "chunks": len(s.chunks or []),
        }
        for s in streams
    ])


def list_news_items(status: str = "draft", limit: int = 30) -> dict:
    """Recent news items. Default: drafts (awaiting moderation)."""
    items = store.list_news_items(status=status, limit=limit)
    return _ok([
        {
            "id": i.id, "headline": i.headline, "category": i.category,
            "confidence": i.confidence, "status": i.status,
            "created_at": i.createdAt.isoformat() if i.createdAt else None,
            "stream_id": i.streamId,
        }
        for i in items
    ])


def get_news_item(item_id: int) -> dict:
    """Full news item by ID."""
    item = store.get_news_item(item_id)
    if not item:
        return _err(f"news item {item_id} not found")
    return _ok(item)


def save_setting(key: str, value: Any, confirm: bool = False) -> dict:
    """Update a single AppSetting. Requires confirm=True."""
    if not confirm:
        return _err("save_setting requires confirm=True — ask the user first")
    updated = store.set_settings({key: value})
    return _ok({key: updated.get(key)}, f"Setting {key} updated")


def save_api_key(provider: str, key: str, confirm: bool = False) -> dict:
    """Persist an API key to .env file. Requires confirm=True."""
    if not confirm:
        return _err("save_api_key requires confirm=True")

    env_var_map = {
        "supadata": "SUPADATA_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "pexels": "PEXELS_API_KEY",
    }
    env_var = env_var_map.get(provider)
    if not env_var:
        return _err(f"no env var mapped for provider: {provider}")

    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    env_path = os.path.abspath(env_path)

    lines: list[str] = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(f"{env_var}="):
                    lines.append(f"{env_var}={key}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{env_var}={key}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    os.environ[env_var] = key  # make effective without restart for most clients
    return _ok({"env_var": env_var}, f"{env_var} сохранён в .env (сервер подхватит без рестарта)")


def approve_news_item(item_id: int, confirm: bool = False) -> dict:
    """Mark a news item as approved. Requires confirm=True."""
    if not confirm:
        return _err("approve_news_item requires confirm=True")
    store.update_news_item_status(item_id, "approved")
    return _ok({"id": item_id}, f"item {item_id} approved")


def reject_news_item(item_id: int, confirm: bool = False) -> dict:
    """Mark a news item as rejected. Requires confirm=True."""
    if not confirm:
        return _err("reject_news_item requires confirm=True")
    store.update_news_item_status(item_id, "rejected")
    return _ok({"id": item_id}, f"item {item_id} rejected")


# ─── Registry (ToolDef-based) ──────────────────────────────────────────

from .base import ToolDef

TOOLS: dict[str, ToolDef] = {
    "get_settings_snapshot": ToolDef(
        name="get_settings_snapshot",
        description="Get current settings and integration key presence.",
        parameters={"type": "object", "properties": {}},
        execute=get_settings_snapshot,
    ),
    "test_integration": ToolDef(
        name="test_integration",
        description="Ping a connector (supadata|anthropic|openai|fastembed|ollama|pexels).",
        parameters={
            "type": "object",
            "properties": {"provider": {"type": "string"}},
            "required": ["provider"],
        },
        execute=test_integration,
    ),
    "get_recent_errors": ToolDef(
        name="get_recent_errors",
        description="Recent Run+Chunk failures (default last 60 min, top 20).",
        parameters={
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "default": 60},
                "limit": {"type": "integer", "default": 20},
            },
        },
        execute=get_recent_errors,
    ),
    "list_active_streams": ToolDef(
        name="list_active_streams",
        description="Active live streams with chunk counts.",
        parameters={"type": "object", "properties": {}},
        execute=list_active_streams,
    ),
    "save_setting": ToolDef(
        name="save_setting",
        description="Change an AppSetting. Returns preview; apply via /chat/platform/apply.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["key", "value"],
        },
        execute=save_setting,
        is_write=True,
    ),
    "save_api_key": ToolDef(
        name="save_api_key",
        description="Persist API key to .env. Returns preview; apply via /chat/platform/apply.",
        parameters={
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "key": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["provider", "key"],
        },
        execute=save_api_key,
        is_write=True,
    ),
    "storage_stats": ToolDef(
        name="storage_stats",
        description="Disk usage, row counts, retention policy snapshot.",
        parameters={"type": "object", "properties": {}},
        execute=lambda: {"ok": True, "data": __import__("cleanup").get_storage_stats(__import__("store").get_all_settings())},
    ),
}
