"""Extract news items from a chunk's transcript via Claude with structured JSON output.

Input: ~5 minutes of transcript from a news stream.
Output: list of {headline, quote, category, offset_sec, confidence} items,
        possibly empty if nothing newsworthy was said.
"""
import json
import os
from dataclasses import dataclass

import anthropic

import brief
from brief import resolve_model


@dataclass
class ExtractedItem:
    headline: str
    quote: str
    category: str | None
    offset_sec: float
    confidence: float
    tags: list[str]


_NEWS_ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "List of news-worthy statements extracted from the transcript. Empty if nothing qualifies.",
            "items": {
                "type": "object",
                "properties": {
                    "headline":   {"type": "string", "description": "One-line publishable summary. No clickbait, no editorial spin."},
                    "quote":      {"type": "string", "description": "Direct quote or close paraphrase from the transcript."},
                    "category":   {"type": "string", "description": "politics | business | tech | markets | macro | sports | culture | science | other"},
                    "offset_sec": {"type": "number", "description": "Approximate seconds from chunk start where the item appears."},
                    "confidence": {"type": "number", "description": "0.0 to 1.0 — how confident this is genuinely news vs filler."},
                    "tags":       {"type": "array", "items": {"type": "string"}, "description": "2-5 short topical tags."},
                },
                "required": ["headline", "quote", "category", "offset_sec", "confidence", "tags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


_NEWS_EXTRACT_PROMPT_TEMPLATE = """Ты — ассистент для извлечения новостных фактов из прямых эфиров. \
На вход дан короткий отрезок расшифрованной речи ведущего(их) новостного канала.

⚠ ЯЗЫК ВЫВОДА: **{TARGET_LANGUAGE_LABEL}**
Всё в полях headline, quote, category, tags должно быть на {TARGET_LANGUAGE_LABEL}, \
НЕЗАВИСИМО от языка исходного транскрипта. Если транскрипт на другом языке \
(украинский, английский и т.д.) — переведи цитату смысл-в-смысл, сохранив суть и \
числа/имена/названия как есть.

Твоя задача: выдать МАССИВ отдельных новостных утверждений (items).

Критерии «news-worthy»:
- Конкретное утверждение о событии / цифре / решении / заявлении / факте.
- Есть смысл опубликовать это отдельной новостью.
- НЕ: введения, приветствия, переходы между темами, общие фразы без фактов.

Структура каждого item (все текстовые поля — на {TARGET_LANGUAGE_LABEL}):
- **headline** — одна строка, publish-ready. Без кликбейта, без «сенсации». Конкретно что произошло.
- **quote** — прямая цитата или близкий пересказ из транскрипта, переведённая на {TARGET_LANGUAGE_LABEL} если нужно. Должна подтверждать headline.
- **category** — одна из: politics | business | tech | markets | macro | sports | culture | science | other (оставляй латиницей, не переводи).
- **offset_sec** — приблизительно сколько секунд от начала отрезка.
- **confidence** — 0.0–1.0. Низкая, если факт спорный/неточный/перевод сомнительный.
- **tags** — 2-5 коротких тегов на {TARGET_LANGUAGE_LABEL} (компании, люди, страны, инструменты).

КРИТИЧНО:
- Пустой массив — валидный ответ, если в отрезке нет news-worthy контента.
- Не выдумывай фактов. Если цифра или имя не в транскрипте — не включай в quote.
- Если транскрипт низкого качества (автосубы с ошибками) — нормализуй, но не додумывай.
- На каждой «волне повторений» (ведущий повторил ту же мысль) — один item, не несколько.
- Имена собственные на нелатинских алфавитах при переводе на en транслитерируй стандартно.

Ответь JSON согласно схеме. Массив items может быть пустым."""

_LANG_LABELS = {
    "ru": "русском языке",
    "en": "English",
}


def _build_news_prompt(target_language: str) -> str:
    label = _LANG_LABELS.get(target_language, target_language)
    return _NEWS_EXTRACT_PROMPT_TEMPLATE.replace("{TARGET_LANGUAGE_LABEL}", label)


def extract_news_items(
    transcript_text: str,
    channel_name: str,
    chunk_started_at_iso: str,
    language_hint: str = "auto",
    model: str | None = None,
    target_language: str = "ru",
) -> tuple[list[ExtractedItem], dict]:
    """Returns (items_list, usage_dict). Raises on Claude API errors.

    target_language — 'ru' | 'en' | etc. Claude will translate headlines/quotes
    into this language even if the source transcript is in a different one.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    if not transcript_text.strip():
        return [], {"input_tokens": 0, "output_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

    resolved_model = resolve_model(model)
    system_text = _build_news_prompt(target_language)
    client = anthropic.Anthropic()
    message = client.messages.create(
        model=resolved_model,
        max_tokens=3000,
        system=[
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Канал: {channel_name}\n"
                    f"Начало отрезка: {chunk_started_at_iso}\n"
                    f"Язык транскрипта: {language_hint}\n\n"
                    f"--- ТРАНСКРИПТ ---\n{transcript_text}"
                ),
            }
        ],
        output_config={
            "format": {"type": "json_schema", "schema": _NEWS_ITEMS_SCHEMA}
        },
    )

    brief.raise_if_truncated(message, "Извлечение новостей")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude returned non-JSON: {e}\n{text[:300]}")

    items = [
        ExtractedItem(
            headline=it["headline"].strip(),
            quote=it["quote"].strip(),
            category=it.get("category"),
            offset_sec=float(it.get("offset_sec", 0)),
            confidence=float(it.get("confidence", 0.5)),
            tags=list(it.get("tags", [])),
        )
        for it in parsed.get("items", [])
        if it.get("headline", "").strip()
    ]

    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens":     getattr(message.usage, "cache_read_input_tokens", 0) or 0,
    }
    return items, usage


def build_attribution(
    channel_name: str,
    chunk_started_at_iso: str,
    speaker_default: str | None,
) -> str:
    """Compose the attribution string that will accompany every published item.

    Format: "Источник: <канал>, <YYYY-MM-DD HH:MM UTC>[, <спикер>]"
    """
    parts = [f"Источник: {channel_name}", chunk_started_at_iso]
    if speaker_default:
        parts.append(speaker_default)
    return ", ".join(parts)
