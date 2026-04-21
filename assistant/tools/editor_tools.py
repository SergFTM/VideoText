"""Editor tools — work with news item content.

All mutating tools return a preview dict: {ok, old, new, diff, tool_call_id}.
Actual DB writes happen in /chat/editor/apply after user clicks "apply".
"""

from __future__ import annotations
import difflib
import os
import uuid

import store
from .base import ToolDef


def _stub_rewrite(item_id: int, field: str, marker: str) -> dict:
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": field,
        "old": f"(stub: current {field} of item {item_id})",
        "new": f"(stub: improved {field} of item {item_id}) {marker}",
        "diff": "--- stub diff ---",
    }


def _llm_rewrite(system: str, user: str, max_tokens: int = 200) -> str:
    """Try Claude Haiku first, fall back to gpt-4o-mini. Returns raw text."""
    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    if os.getenv("OPENAI_API_KEY"):
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()
    raise RuntimeError("no LLM key available (need ANTHROPIC_API_KEY or OPENAI_API_KEY)")


def _diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(
        (old or "").splitlines() or [""],
        (new or "").splitlines() or [""],
        fromfile="before", tofile="after", lineterm="",
    ))


def recognize_text(image_id: int | None = None, url: str | None = None) -> dict:
    return {"ok": True, "tool_call_id": str(uuid.uuid4()), "text": "(stub OCR output)"}


def improve_headline(item_id: int, style: str = "", confirm: bool = False) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    old = item.headline or ""
    system = (
        "Ты — редактор новостных заголовков на русском языке. "
        "Перепиши данный заголовок, сохраняя фактическую суть. "
        "Не меняй имена, цифры, названия. Отдай ТОЛЬКО новый заголовок, "
        "без кавычек, без пояснений. Одна строка."
    )
    user = f"Текущий заголовок: {old}\n"
    if style:
        user += f"Желаемый стиль: {style}\n"
    user += "Новый заголовок:"

    try:
        new = _llm_rewrite(system, user)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Strip surrounding quotes the model sometimes adds
    new = new.strip().strip('"').strip("«»").strip()

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "headline",
        "old": old,
        "new": new,
        "diff": _diff(old, new),
    }


def rewrite_quote(item_id: int, tone: str = "", confirm: bool = False) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    old = item.quote or ""
    system = (
        "Ты — редактор новостных цитат. Перепиши цитату, СТРОГО сохраняя смысл "
        "и факты (имена, числа, названия). Можешь менять формулировку и тон. "
        "Не добавляй информацию, которой нет в оригинале. "
        "Отдай только новый текст цитаты, без пояснений."
    )
    user = f"Цитата: {old}\n"
    if tone:
        user += f"Желаемый тон: {tone}\n"
    user += "Переписанная цитата:"

    try:
        new = _llm_rewrite(system, user, max_tokens=300)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    new = new.strip().strip('"').strip("«»").strip()

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "quote",
        "old": old,
        "new": new,
        "diff": _diff(old, new),
    }


_LENGTH_TARGETS = {"short": (150, 300), "medium": (400, 700), "long": (900, 1400)}


def expand_text(item_id: int, length: str = "medium", confirm: bool = False) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    min_chars, max_chars = _LENGTH_TARGETS.get(length, (400, 700))
    old = item.expandedText or ""
    system = (
        f"Ты пишешь расширенную версию новостной заметки на русском. "
        f"Длина: {min_chars}-{max_chars} символов. Не выдумывай факты — "
        f"используй только данные из заголовка и цитаты. Объясни контекст, "
        f"добавь фон, избегай клише. Markdown НЕ использовать — простой текст."
    )
    user = (
        f"Заголовок: {item.headline}\n"
        f"Цитата: {item.quote}\n"
        f"Категория: {item.category or '—'}\n"
        f"Атрибуция: {item.attribution}\n"
        f"Расширенный текст:"
    )
    try:
        new = _llm_rewrite(system, user, max_tokens=max(800, max_chars // 2))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    new = new.strip()

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "expandedText",
        "old": old,
        "new": new,
        "diff": _diff(old, new),
    }


def regenerate_image(item_id: int, concept: str = "", confirm: bool = False) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id, "field": "imageId",
        "old": None, "new": f"(stub image for concept={concept})",
        "diff": "(stub)",
    }


def suggest_tags(item_id: int) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id, "suggestions": ["stub-tag-1", "stub-tag-2"],
    }


def bulk_action(item_ids: list[int], action: str, confirm: bool = False) -> dict:
    return {
        "ok": True, "tool_call_id": str(uuid.uuid4()),
        "action": action, "preview": [{"item_id": i, "change": "stub"} for i in item_ids],
    }


TOOLS: dict[str, ToolDef] = {
    "recognize_text": ToolDef(
        name="recognize_text",
        description="OCR on an image (by image_id or url). Returns recognized text.",
        parameters={
            "type": "object",
            "properties": {
                "image_id": {"type": "integer"},
                "url": {"type": "string"},
            },
        },
        execute=recognize_text,
    ),
    "improve_headline": ToolDef(
        name="improve_headline",
        description="Rewrite a news item headline. Returns preview (old/new/diff).",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "style": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=improve_headline,
        is_write=True,
    ),
    "rewrite_quote": ToolDef(
        name="rewrite_quote",
        description="Rewrite a news item quote preserving meaning.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "tone": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=rewrite_quote,
        is_write=True,
    ),
    "expand_text": ToolDef(
        name="expand_text",
        description="Generate or update the expanded text of a news item.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "length": {"type": "string", "enum": ["short", "medium", "long"]},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=expand_text,
        is_write=True,
    ),
    "regenerate_image": ToolDef(
        name="regenerate_image",
        description="Generate a new illustration for a news item.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "concept": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_id"],
        },
        execute=regenerate_image,
        is_write=True,
    ),
    "suggest_tags": ToolDef(
        name="suggest_tags",
        description="Suggest tags for a news item. Write happens via /apply.",
        parameters={
            "type": "object",
            "properties": {"item_id": {"type": "integer"}},
            "required": ["item_id"],
        },
        execute=suggest_tags,
    ),
    "bulk_action": ToolDef(
        name="bulk_action",
        description="Apply the same action to multiple items. Returns preview list.",
        parameters={
            "type": "object",
            "properties": {
                "item_ids": {"type": "array", "items": {"type": "integer"}},
                "action": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["item_ids", "action"],
        },
        execute=bulk_action,
        is_write=True,
    ),
}
