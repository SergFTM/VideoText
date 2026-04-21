"""Editor tools — work with news item content.

All mutating tools return a preview dict: {ok, old, new, diff, tool_call_id}.
Actual DB writes happen in /chat/editor/apply after user clicks "apply".
"""

from __future__ import annotations
import difflib
import json as _json
import os
import re
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
    if not os.getenv("OPENAI_API_KEY"):
        return {"ok": False, "error": "OCR requires OPENAI_API_KEY"}

    image_payload: dict
    if image_id is not None:
        img = store.get_news_image(image_id)
        if not img:
            return {"ok": False, "error": f"NewsImage {image_id} not found"}
        import base64
        from pathlib import Path
        try:
            data = Path(img.filePath).read_bytes()
        except (FileNotFoundError, OSError) as e:
            return {"ok": False, "error": f"image file read failed: {e}"}
        b64 = base64.b64encode(data).decode()
        image_payload = {"url": f"data:image/png;base64,{b64}"}
    elif url:
        image_payload = {"url": url}
    else:
        return {"ok": False, "error": "Provide either image_id or url"}

    from openai import OpenAI
    client = OpenAI()
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Распознай весь видимый текст на изображении. Верни только текст, без комментариев. Если текста нет — верни пустую строку."},
                    {"type": "image_url", "image_url": image_payload},
                ],
            }],
            max_tokens=2000,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    text = (resp.choices[0].message.content or "").strip()
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "text": text,
        "length": len(text),
    }


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
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    if not concept:
        system = (
            "Ты — арт-директор. Дай КОРОТКУЮ концепт-фразу для иллюстрации "
            "новости (2-5 слов, на английском, пригодное для DALL-E). "
            "Только фраза, без пояснений."
        )
        user = f"Headline: {item.headline}\nQuote: {item.quote}"
        try:
            concept = _llm_rewrite(system, user, max_tokens=40).strip().strip('"').strip()[:80]
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    prompt = f"Editorial illustration: {concept}. Clean composition, news-style."
    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "imageId",
        "old": item.imageId,
        "new_concept": concept,
        "new_prompt": prompt,
        # Frontend passes `new_concept` to /apply when field==imageId; apply generates image.
        "new": concept,  # simplified single-field preview for UI
        "diff": f"concept: {concept}",
    }


def suggest_tags(item_id: int) -> dict:
    item = store.get_news_item(item_id)
    if not item:
        return {"ok": False, "error": f"NewsItem {item_id} not found"}

    system = (
        "Ты — теггер новостей. Предложи 1-5 коротких релевантных тегов "
        "на русском (одно-два слова каждый). Отвечай ТОЛЬКО JSON-массивом "
        'строк, например: ["энергетика","нефть","brent"]. '
        "Никаких пояснений до или после."
    )
    user = f"Заголовок: {item.headline}\nЦитата: {item.quote}"
    try:
        raw = _llm_rewrite(system, user, max_tokens=100).strip()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Parse JSON array. If LLM added prose, extract the first bracketed section.
    tags: list[str] = []
    try:
        parsed = _json.loads(raw)
        if isinstance(parsed, list):
            tags = parsed
    except _json.JSONDecodeError:
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if m:
            try:
                tags = _json.loads(m.group(0))
            except _json.JSONDecodeError:
                tags = []
    tags = [str(t).strip() for t in tags if str(t).strip()][:5]

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "item_id": item_id,
        "field": "tags",
        "old": _json.loads(item.tags) if item.tags else [],
        "new": tags,
        "diff": f"{len(tags)} tags suggested",
        "suggestions": tags,  # back-compat with stub shape
    }


def bulk_action(item_ids: list[int], action: str, confirm: bool = False) -> dict:
    BULK_ALLOWED = {
        "improve_headline": improve_headline,
        "rewrite_quote": rewrite_quote,
        "suggest_tags": suggest_tags,
    }
    if action not in BULK_ALLOWED:
        return {
            "ok": False,
            "error": f"bulk action {action!r} not supported. allowed: {sorted(BULK_ALLOWED)}",
        }

    func = BULK_ALLOWED[action]
    previews = []
    for iid in item_ids[:50]:  # hard cap to avoid runaway cost
        previews.append({"item_id": iid, "result": func(item_id=iid)})

    return {
        "ok": True,
        "tool_call_id": str(uuid.uuid4()),
        "action": action,
        "previews": previews,
        "total": len(previews),
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
