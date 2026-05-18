"""Local LLM integration (Ollama) for spec expansion.

Reuses the same Ollama runtime that powers `dedup.py` (embeddings) and
`assistant/core/chat.py` (chat). This module is the **generation** entry point
for non-streaming + streaming text completions used outside the assistant loop.

Why a separate module: the assistant's `_run_ollama` is tightly coupled to the
chat session abstraction (messages list, tool steering, SSE event format used
by `/assistant/chat`). For spec expansion we want a leaner sync-friendly stream
that any FastAPI endpoint can consume without dragging in chat session state.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_NUM_CTX = 32768  # qwen2.5 native cap; Ollama silently truncates to 2048 without this.
MAX_TRANSCRIPT_CHARS = 60000  # ≈ 20-22K tokens on Russian — leaves headroom for brief + answer.


def _endpoint() -> str:
    return os.getenv("OLLAMA_URL", DEFAULT_ENDPOINT).rstrip("/")


def list_installed_models() -> list[dict[str, Any]]:
    """GET /api/tags. Returns [] if Ollama is not running."""
    try:
        with urllib.request.urlopen(f"{_endpoint()}/api/tags", timeout=2) as r:
            data = json.loads(r.read())
            return data.get("models") or []
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return []


def stream_chat(
    *,
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    temperature: float = 0.3,
) -> Iterator[str]:
    """Stream content deltas from Ollama /api/chat as plain strings.

    Uses urllib (stdlib) on purpose — no aiohttp/httpx dependency, no event loop
    entanglement. Caller decides how to ship deltas downstream (SSE, websocket,
    join-and-return, etc).
    """
    payload = json.dumps({
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "options": {
            "num_ctx": num_ctx,
            "temperature": temperature,
            # -1 = let model generate until EOS. Ollama default is 128 tokens —
            # which truncates a long, structured spec mid-section.
            "num_predict": -1,
            "top_p": 0.9,
            "repeat_penalty": 1.05,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{_endpoint()}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            piece = (obj.get("message") or {}).get("content", "")
            if piece:
                yield piece
            if obj.get("done"):
                break


# ─── Mode-specific system prompts ──────────────────────────────────────
# Each mode expands a brief section into a different deliverable. Edit these
# to taste — they're the highest-leverage knobs for output quality.

SYSTEM_PROMPTS: dict[str, str] = {
    "spec": (
        "Ты пишешь расширенное техническое задание (ТЗ) на русском языке для self-prompt"
        " в Cursor/Claude Code. Стиль: деловой, конкретный, без воды.\n\n"
        "ОБЯЗАТЕЛЬНЫЕ СЕКЦИИ (в этом порядке):\n"
        "1. Проблема — 2-3 предложения, чем она болит\n"
        "2. Целевая аудитория — кто пользователь, его технический уровень\n"
        "3. User Stories — 5-10 штук в формате 'Как X, я хочу Y, чтобы Z'\n"
        "4. Функциональные требования — нумерованный список с приоритетами P0/P1/P2\n"
        "5. Архитектура — компоненты, потоки данных, выбор стека с обоснованием\n"
        "6. API контракт / точки интеграции — endpoints, входы, выходы, форматы\n"
        "7. Модель данных — сущности, поля, связи (если применимо)\n"
        "8. Acceptance Criteria — измеримые критерии готовности по каждой User Story\n"
        "9. Риски и допущения — что может пойти не так, что мы предполагаем\n\n"
        "ЗАПРЕТЫ: не пересказывай видео, не пиши маркетинговые слоганы, не выдумывай"
        " цифры/API/имена библиотек — если данных нет, пиши 'TBD' с пометкой что нужно"
        " уточнить. Используй markdown-заголовки и списки."
    ),
    "research": (
        "Ты делаешь глубокое аналитическое исследование на русском языке по теме видео."
        " Стиль: аналитический, с фактами и цифрами, нейтральный тон, без журналистики.\n\n"
        "ОБЯЗАТЕЛЬНЫЕ СЕКЦИИ:\n"
        "1. Контекст и постановка вопроса — что исследуем, зачем\n"
        "2. Ключевые тезисы автора — 5-10 пунктов, с прямыми цитатами в кавычках"
        " (используй транскрипт как источник)\n"
        "3. Факты и цифры — таблица или нумерованный список всех количественных"
        " утверждений, с пометкой [verifiable] / [needs check] / [opinion]\n"
        "4. Сильные стороны позиции автора — что аргументировано убедительно\n"
        "5. Слабые места и пробелы — что не доказано, что упущено, контраргументы\n"
        "6. Связи с предметной областью — параллели с известными концепциями,"
        " методологиями, конкурентами, исследованиями\n"
        "7. Открытые вопросы — что осталось неясным, что стоит копнуть глубже\n"
        "8. Источники для дальнейшего изучения — конкретные книги, статьи, доклады\n\n"
        "ЗАПРЕТЫ: не пересказывай видео по хронологии, не повторяй автора без анализа,"
        " не выдумывай ссылки и статистику. Если факт не из материалов — помечай"
        " '[вне материалов: ...]'."
    ),
    "report": (
        "Ты составляешь executive-report на русском языке по материалам видео."
        " Стиль: ёмкий, структурированный, для занятого читателя — топ-менеджера или"
        " инвестора, у которого есть 3-5 минут.\n\n"
        "ОБЯЗАТЕЛЬНЫЕ СЕКЦИИ:\n"
        "1. TL;DR — 3-5 предложений, главная мысль и почему это важно\n"
        "2. Ключевые выводы (Bottom Line) — 5-7 буллетов, начинающихся с глагола"
        " действия или утверждения, без предисловий\n"
        "3. Что нового / неочевидного — что отличает этот материал от 'обычного"
        " контента в нише'\n"
        "4. Цифры и метрики — таблица: метрика | значение | источник/контекст\n"
        "5. Применимость — кому это полезно, в каких сценариях, что делать дальше\n"
        "6. Действия (Action Items) — 3-5 конкретных шагов, которые можно сделать"
        " на этой неделе на основе материала\n"
        "7. Риски использования — где идеи автора могут сломаться, оговорки\n\n"
        "ЗАПРЕТЫ: никакой воды, повторов, восторгов; не использовать слова"
        " 'революция', 'переворот', 'изменит мир'; не пересказывать видео — делать"
        " выводы. Каждый буллет должен нести самостоятельную ценность."
    ),
}


def _format_context(
    *,
    section_title: str,
    section_md: str,
    software_brief_json: dict | None,
    full_brief_md: str,
    transcript_excerpt: str,
) -> tuple[str, list[str]]:
    """Builds the user-message body shared across all modes.

    Returns (user_body, sources_list) so the caller can adjust the framing.
    """
    sb_block = ""
    if software_brief_json:
        sb_block = "\n\n## Структурированный software_brief (JSON)\n```json\n" \
                   + json.dumps(software_brief_json, ensure_ascii=False, indent=2) \
                   + "\n```"

    transcript_block = ""
    if transcript_excerpt:
        truncated = transcript_excerpt[:MAX_TRANSCRIPT_CHARS]
        suffix = "" if len(transcript_excerpt) <= MAX_TRANSCRIPT_CHARS else \
                 f"\n\n[... транскрипт обрезан после {MAX_TRANSCRIPT_CHARS} симв. — " \
                 f"исходно {len(transcript_excerpt)} симв.]"
        transcript_block = (
            "\n\n## Полный транскрипт видео (источник истины — цитируй и проверяй факты против него)\n"
            + truncated + suffix
        )

    brief_block = ""
    if full_brief_md.strip():
        brief_block = f"\n\n## Полный бриф (структурированная выжимка от Claude)\n{full_brief_md}"

    sources = []
    if full_brief_md.strip(): sources.append("брифа")
    if software_brief_json:   sources.append("software_brief JSON")
    if transcript_excerpt:    sources.append("транскрипта видео")

    body = (
        f"## Исходная секция: {section_title}\n{section_md}"
        f"{brief_block}{sb_block}{transcript_block}"
    )
    return body, sources


def build_expand_prompt(
    *,
    mode: str,
    video_title: str,
    section_title: str,
    section_md: str,
    software_brief_json: dict | None,
    full_brief_md: str,
    transcript_excerpt: str,
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for any of the three expand modes.

    `mode` ∈ {"spec", "research", "report"}. Unknown modes fall back to "spec".
    """
    system = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["spec"])

    body, sources = _format_context(
        section_title=section_title,
        section_md=section_md,
        software_brief_json=software_brief_json,
        full_brief_md=full_brief_md,
        transcript_excerpt=transcript_excerpt,
    )
    sources_line = ", ".join(sources) if sources else "только исходной секции"

    instruction = {
        "spec":     "Расширь секцию в полноценное ТЗ согласно структуре в системном промпте.",
        "research": "Подготовь глубокое исследование по теме согласно структуре в системном промпте.",
        "report":   "Составь executive-report согласно структуре в системном промпте.",
    }.get(mode, "Расширь секцию согласно системному промпту.")

    user = (
        f"Видео: {video_title}\n"
        f"Источники контекста: {sources_line}.\n\n"
        f"{body}\n\n"
        f"{instruction} Если данных недостаточно — пиши 'TBD' вместо домыслов."
    )
    return system, user
