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

# Anthropic server-side web search. Only the research stage gets it: the other
# five stages reason over upstream artifacts and have nothing to look up.
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"


def web_search_tools(max_uses: int) -> list[dict] | None:
    """Tool block for the research stage, or None when search is disabled."""
    if max_uses is None or max_uses < 1:
        return None
    return [{"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": int(max_uses)}]


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
    tools: list[dict] | None = None,
) -> Iterator[str]:
    """Stream content deltas from Ollama /api/chat as plain strings.

    Uses urllib (stdlib) on purpose — no aiohttp/httpx dependency, no event loop
    entanglement. Caller decides how to ship deltas downstream (SSE, websocket,
    join-and-return, etc).

    `tools` (Anthropic server-side tools, e.g. web search) is forwarded only on
    the Claude branch. Ollama has no server-side tools — it is deliberately
    ignored below rather than leaked into the request payload.
    """
    # Claude fallback: a model id like "claude-sonnet-4-6" routes to Anthropic
    # via the dispatcher already in transcript_edit.py. Lazy import avoids the
    # circular dependency (transcript_edit imports local_llm).
    import transcript_edit
    if transcript_edit._is_claude(model):
        yield from transcript_edit._stream_claude(system, user, model, tools=tools)
        return
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
        "Ты делаешь РЕСЕРЧ: не пересказать материал, а ПРОВЕРИТЬ его. Твоя работа —"
        " отделить проверяемые факты от оценок автора и допущений, найти расхождения"
        " и сказать, что подтверждается, а что нет.\n\n"
        "СЕКЦИИ: Задача / Контекст / Проверка утверждений / Арифметическая и"
        " логическая сверка / Ангажированность источника / Варианты решения (минимум 2) /"
        " Ограничения / Риски / Источники / Открытые вопросы.\n\n"
        "ПРОВЕРКА УТВЕРЖДЕНИЙ — таблица со столбцами: Утверждение | Где в источнике |"
        " Тип (факт / оценка автора / допущение) | Внешняя проверка | Статус"
        " (подтверждено / противоречит / не найдено / не проверено). Бери все ключевые"
        " утверждения, а не два примера.\n\n"
        "АРИФМЕТИЧЕСКАЯ И ЛОГИЧЕСКАЯ СВЕРКА — пересчитай КАЖДОЕ производное число"
        " (проценты, доходности, степени, доли, суммы) и назови расхождения явно."
        " Проверь, не противоречат ли утверждения друг другу.\n\n"
        "АНГАЖИРОВАННОСТЬ ИСТОЧНИКА — коммерческий интерес автора, самоцитирование,"
        " отсутствие независимого подтверждения.\n\n"
        "ЗАПРЕТЫ: транскрипт НЕ засчитывается как подтверждение самого себя — ссылка"
        " на исходное видео не является внешней проверкой; в 'Источники' попадают"
        " только внешние источники. Не делай выводов и не рекомендуй решение (это"
        " репорт); не пиши ТЗ; не придумывай реализацию. Нет данных — '—' или 'TBD'."
        " markdown-заголовки, таблицы и списки, без преамбул."
    ),
    "report": (
        "Ты составляешь РЕПОРТ: сверить источники между собой, сделать выводы и вынести"
        " вердикт по проблематике.\n\n"
        "СЕКЦИИ: Краткое резюме (2-3 предложения) / Что исследовали / Сверка /"
        " Основные выводы / Варианты решений + плюсы-минусы каждого / Риски /"
        " Вердикт по проблематике / Рекомендация + обоснование / Следующие действия.\n\n"
        "СВЕРКА — таблица со столбцами: Заявлено в брифе | Подтверждает ли расшифровка |"
        " Что добавил или опроверг ресерч | Статус. Именно здесь видно, где бриф"
        " опережает источник, а где ресерч нашёл противоречие.\n\n"
        "ВЕРДИКТ ПО ПРОБЛЕМАТИКЕ — 'подтверждена' / 'подтверждена частично' /"
        " 'не подтверждена', с обоснованием на 2-4 предложения. Затем ПОСЛЕДНЕЙ СТРОКОЙ"
        " всего документа выведи ровно один маркер, без кавычек и без блока кода:\n"
        "<!-- verdict: confirmed -->  — если проблематика подтверждена\n"
        "<!-- verdict: partial -->    — если подтверждена частично\n"
        "<!-- verdict: refuted -->    — если не подтверждена\n"
        "Маркер обязателен: без него следующий этап не сможет принять решение.\n\n"
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
        "После всех алгоритмов — три общие секции:\n"
        "ВЫЧИСЛИТЕЛЬНОЕ ЯДРО: какие расчёты выполняются, какая нужна точность, где"
        " узкое место по времени и памяти, что считается онлайн, а что заранее.\n"
        "МОДУЛЬНАЯ ДЕКОМПОЗИЦИЯ: таблица Модуль | Ответственность | Интерфейс"
        " (вход → выход) | Зависимости. Модуль — одна ответственность.\n"
        "НУЖЕН ЛИ ML: ответь честно. Если задача решается правилами и порогами —"
        " пиши 'нет, правил достаточно' и объясни почему. Если ML нужен — назови"
        " задачу, признаки, целевую метрику и baseline без ML. Не предлагай модель,"
        " не назвав признаки и метрику.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не пишешь ТЗ (не фиксируешь требования); не пишешь AI-скилл (не"
        " повторяемая инструкция). Не выдумывай пороги/формулы — помечай '[TBD: ...]'."
        " markdown и нумерованные списки, без преамбул."
    ),
    "ai_skills": (
        "Ты проектируешь AI-СКИЛЛЫ — повторяемые инструкции для агента (Claude Code /"
        " Cursor). Скилл оправдан, если действие повторяется 3+ раза. Выдели 2-5 скиллов.\n\n"
        "ФОРМАТ СТРОГИЙ — по нему собирается пакет файлов, отклонения ломают сборку.\n"
        "Заголовок каждого скилла — ровно: '## Скилл N. <Название>'\n"
        "Сразу под заголовком — строка: '**Slug:** <имя-в-kebab-case>' (латиница,"
        " строчные, дефисы; это имя каталога).\n"
        "Затем строка: '**Описание:** <одно предложение, когда применять>'\n\n"
        "Далее секции скилла: Когда использовать / Когда НЕ использовать / Входные"
        " данные (обязательные / опциональные) / Что делать (шаги) / Что не делать /"
        " Формат результата / Критерии качества / Примеры (Вход / Выход) /"
        " Инструменты MCP.\n\n"
        "ИНСТРУМЕНТЫ MCP — для каждого тула: имя (snake_case), назначение, JSON-схема"
        " входа, JSON-схема выхода. Если скилл не требует тула — пиши 'не требуется'.\n\n"
        "ЧТО НЕ ДЕЛАЕШЬ: не заменяешь алгоритм; не пишешь ТЗ; не создаёшь скилл из сырой"
        " идеи без предварительных этапов. Не выдумывай API/данные — 'TBD: уточнить'."
        " markdown, без преамбул."
    ),
}


# Appended to the research system prompt when no external search is available
# (Ollama branch, or the setting is off). Degrading silently would let the model
# invent sources; degrading loudly keeps the artifact honest.
_NO_SEARCH_NOTE = (
    "\n\nВНЕШНИЙ ПОИСК НЕДОСТУПЕН: в столбце 'Внешняя проверка' пиши '—', в"
    " столбце 'Статус' — 'не проверено'. Не выдумывай источники и ссылки."
    " Арифметическую и логическую сверку всё равно выполняй полностью."
)


def _format_context(
    *,
    section_title: str,
    section_md: str,
    software_brief_json: dict | None,
    full_brief_md: str,
    transcript_excerpt: str,
    upstream: dict[str, str] | None = None,
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

    upstream_block = ""
    for dep_mode, dep_text in (upstream or {}).items():
        if dep_text and dep_text.strip():
            upstream_block += (
                f"\n\n## Выход предыдущего этапа: {dep_mode} "
                "(используй как основной вход — конвейер)\n" + dep_text.strip()
            )

    sources = []
    if full_brief_md.strip(): sources.append("брифа")
    if software_brief_json:   sources.append("software_brief JSON")
    if transcript_excerpt:    sources.append("транскрипта видео")
    if upstream_block:        sources.append("предыдущих этапов")

    body = (
        f"## Исходная секция: {section_title}\n{section_md}"
        f"{brief_block}{sb_block}{upstream_block}{transcript_block}"
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
    upstream: dict[str, str] | None = None,
    web_search_available: bool = False,
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for any expand mode.

    `mode` ∈ {"spec", "research", "report", "uiux", "ai_skills", "ai_algorithms"}.
    Unknown modes fall back to "spec".
    """
    system = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["spec"])
    if mode == "research" and not web_search_available:
        system += _NO_SEARCH_NOTE

    body, sources = _format_context(
        section_title=section_title,
        section_md=section_md,
        software_brief_json=software_brief_json,
        full_brief_md=full_brief_md,
        transcript_excerpt=transcript_excerpt,
        upstream=upstream,
    )
    sources_line = ", ".join(sources) if sources else "только исходной секции"

    instruction = {
        "research":      "Подготовь ресерч по структуре из системного промпта. Проверяй, а не пересказывай.",
        "report":        "Составь репорт по структуре из системного промпта. Не забудь маркер вердикта последней строкой.",
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
