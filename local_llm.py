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
    "research": (
        "Ты делаешь РЕСЕРЧ по теме: собрать и первично структурировать информацию.\n\n"
        "СЕКЦИИ: Задача / Контекст (где и зачем это нужно) / Варианты решения (минимум 2) /"
        " Ограничения / Риски / Источники и наблюдения / Открытые вопросы.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не делаешь выводы и не рекомендуешь решение (это репорт); не"
        " пишешь ТЗ; не придумываешь реализацию. Если данных нет — пиши '—' / 'TBD'."
        " markdown-заголовки и списки, без преамбул."
    ),
    "report": (
        "Ты составляешь РЕПОРТ по результатам ресерча: сделать выводы и рекомендацию.\n\n"
        "СЕКЦИИ: Краткое резюме (2-3 предложения) / Что исследовали / Основные выводы /"
        " Варианты решений + плюсы-минусы каждого / Риски / Рекомендация + обоснование /"
        " Следующие действия.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не пишешь ТЗ; не описываешь реализацию; не перечисляешь всё"
        " найденное без выводов. markdown, без преамбул."
    ),
    "spec": (
        "Ты пишешь ТЗ: зафиксировать постановку задачи на реализацию. Деловой, конкретный.\n\n"
        "СЕКЦИИ: Название задачи / Цель / Пользователь-заказчик / Пользовательские сценарии /"
        " Функциональные требования / Нефункциональные требования / Ограничения /"
        " Используемые данные / Критерии приёмки / Что не входит в скоп.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не описываешь порядок реализации (это алгоритм); не рекомендуешь"
        " технологии (это репорт); не описываешь интерфейс детально (это UI/UX). Если"
        " данных нет — 'TBD'. markdown, без преамбул."
    ),
    "uiux": (
        "Ты описываешь UI/UX — интерфейсный сценарий взаимодействия пользователя с"
        " функцией. Нужен только если задача предполагает интерфейс.\n\n"
        "UI-СЛОЙ: какие экраны нужны; какие элементы на каждом экране; какие состояния"
        " (загрузка/успех/ошибка/пустое); какие сообщения видит пользователь.\n"
        "UX-СЛОЙ: что пользователь делает первым; что происходит после каждого действия;"
        " где нужна подсказка/подтверждение/предупреждение; где можно упростить путь.\n\n"
        "ФОРМАТ на каждый экран: Экран / Цель экрана / Элементы / Кнопки и действия /"
        " Состояния (загрузка/успех/ошибка/пусто) / Переходы (при успехе → / при ошибке →).\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не описываешь внутреннюю логику; не пишешь алгоритм обработки"
        " данных; не заменяешь ТЗ. markdown, без преамбул."
    ),
    "ai_algorithms": (
        "Ты описываешь АЛГОРИТМЫ действий — пошагово, что происходит в системе, у"
        " пользователя или в AI-процессе. Выдели 1-4 алгоритма. Для КАЖДОГО:\n\n"
        "Название алгоритма / Тип [пользовательский | системный | AI | технический] /"
        " Цель / Предусловия / Входные данные (что брать, откуда) / Шаги (нумерованно:"
        " что взять, что с чем сравнить, что проверить) / Правила и условия (если X → Y) /"
        " Критерии выхода / Результат / Граничные случаи / Ошибки и обработка.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не пишешь ТЗ (не фиксируешь требования); не пишешь AI-скилл (не"
        " повторяемая инструкция). Не выдумывай пороги/формулы — помечай '[TBD: ...]'."
        " markdown и нумерованные списки, без преамбул."
    ),
    "ai_skills": (
        "Ты проектируешь AI-СКИЛЛЫ — повторяемые инструкции для агента (Claude Code /"
        " Cursor). Скилл оправдан, если действие повторяется 3+ раза. Выдели 2-5 скиллов."
        " Для КАЖДОГО:\n\n"
        "Название скилла / Версия / Когда использовать / Когда НЕ использовать /"
        " Входные данные (обязательные / опциональные) / Что делать (шаги) / Что не делать /"
        " Формат результата / Критерии качества / Примеры (Вход / Выход).\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не заменяешь алгоритм; не пишешь ТЗ; не создаёшь скилл из сырой"
        " идеи без предварительных этапов. Не выдумывай API/данные — 'TBD: уточнить'."
        " markdown, без преамбул."
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
    """Returns (system_prompt, user_prompt) for any expand mode.

    `mode` ∈ {"spec", "research", "report", "ai_skills", "ai_algorithms"}.
    Unknown modes fall back to "spec".
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
        "research":      "Подготовь ресерч по структуре из системного промпта.",
        "report":        "Составь репорт по структуре из системного промпта.",
        "spec":          "Сформулируй ТЗ по структуре из системного промпта.",
        "uiux":          "Опиши UI/UX-сценарий по структуре из системного промпта.",
        "ai_algorithms": "Опиши алгоритмы действий по структуре из системного промпта.",
        "ai_skills":     "Спроектируй AI-скиллы по структуре из системного промпта.",
    }.get(mode, "Расширь секцию согласно системному промпту.")

    user = (
        f"Видео: {video_title}\n"
        f"Источники контекста: {sources_line}.\n\n"
        f"{body}\n\n"
        f"{instruction} Если данных недостаточно — пиши 'TBD' вместо домыслов."
    )
    return system, user
