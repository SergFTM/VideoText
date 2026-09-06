"""Brief generator.

Design:
- System prompt is the cacheable stable prefix (~2300 tokens with few-shot).
- User message is the variable transcript — comes after the breakpoint.
- Cache hits on 2nd+ run with same prompt+language+format → 90% cheaper input.
- JSON output mode uses output_config.format for guaranteed schema-valid JSON.
- Model selection: explicit arg > CLAUDE_MODEL env > default (sonnet 4.6).
"""
import json
import os

import anthropic

# ─── Model shortcuts ──────────────────────────────────────────────
_MODEL_ALIASES = {
    # Strong tier — engineering-grade briefs / ТЗ. "opus" = chosen strong default.
    "opus": "claude-opus-4-8",
    "opus5": "claude-opus-5",
    "opus48": "claude-opus-4-8",
    "opus47": "claude-opus-4-7",
    "opus46": "claude-opus-4-6",
    # Balanced tier.
    "sonnet": "claude-sonnet-4-6",
    "sonnet5": "claude-sonnet-5",
    "sonnet46": "claude-sonnet-4-6",
    # Cheap/fast tier — simple briefs.
    "haiku": "claude-haiku-4-5",
    "haiku45": "claude-haiku-4-5",
}


class TruncatedResponse(RuntimeError):
    """The model ran out of output budget — the text is cut off, usually mid-word."""


def raise_if_truncated(message, what: str) -> None:
    """Fail loudly when a response hit `max_tokens`.

    A truncated artifact is worse here than no artifact: it gets persisted,
    cached, and then handed to every downstream stage as source material. Five
    of forty-nine briefs in the production database were stored this way before
    this check existed, ending mid-sentence.
    """
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise TruncatedResponse(
            f"{what}: модель упёрлась в max_tokens и ответ обрезан. "
            "Подними лимит или сократи вход — сохранять обрезанный результат нельзя."
        )


def resolve_model(model: str | None) -> str:
    if model:
        return _MODEL_ALIASES.get(model, model)
    env_model = os.getenv("CLAUDE_MODEL")
    if env_model:
        return _MODEL_ALIASES.get(env_model, env_model)
    return "claude-sonnet-4-6"


# ─── System prompt ────────────────────────────────────────────────
# Stable across runs (no timestamps, no user IDs). The few-shot example
# both (a) teaches the model the expected style and (b) pushes the prompt
# past the 2048-token minimum cacheable prefix for Sonnet 4.6.

_FEW_SHOT_EXAMPLE_RU = """<example>
<input>
Заголовок: Как мы подняли $2M seed без PowerPoint — история Finch
Длительность: 2400 сек
Язык: ru (manual)

Транскрипт: В первый год после запуска мы совершили все классические ошибки. \
Пытались продавать предприятиям через холодные email, тратили деньги на конференции, \
делали pitch deck на 40 слайдов. Ничего не работало. Переломным моментом стал \
разговор с одним VC из a16z, который сказал: "Ваш продукт не нужен 20 людям, \
которых вы хотите видеть клиентами. Он нужен одному, который уже у вас есть". \
Мы вернулись и пересмотрели воронку. Оказалось, что все наши платящие клиенты \
пришли через один канал — Twitter DMs нашего CTO, где он отвечал на технические \
вопросы. Мы убрали всё остальное. Сфокусировались на контенте в Twitter, \
техническом блоге и open source релизах. За 6 месяцев выручка выросла с $8K MRR \
до $85K MRR. Pitch deck на seed-раунде был 8 слайдов и один график — график \
органического роста. Закрыли $2M за 3 недели с 4 инвесторами. Главный урок: \
большинство founders оптимизируют не те метрики.
</input>
<output>
## Суть
Кейс Finch: как B2B SaaS вырос с $8K до $85K MRR за 6 месяцев, отказавшись \
от классических growth-тактик (холодные email, конференции, длинный pitch deck) \
в пользу одного канала — технического контента CTO в Twitter. На основе этого \
роста подняли $2M seed за 3 недели.

## Ключевые идеи
- **Фокус на один рабочий канал** важнее диверсификации — найти «что уже работает» и удвоить.
- Совет a16z: продавать не будущим клиентам, а текущему — масштабировать то, что уже купили.
- **Технический контент CTO в Twitter** оказался главным source платящих клиентов.
- Pitch deck в 8 слайдов + один график органического роста сработал лучше 40-слайдной презентации.
- Большинство founders оптимизируют не те метрики — начинать нужно с атрибуции выручки.
- Органический рост — самый сильный сигнал для seed-инвестора.

## Факты и цифры
- MRR: $8K → $85K за 6 месяцев (10.6×)
- Seed: $2M, закрыт за 3 недели, 4 инвестора
- Pitch deck: 8 слайдов
- Источник роста: 1 канал (Twitter CTO) вместо 4+ до этого

## Возможное применение
- **Источник для RAG** в ассистенте для founders на ранней стадии.
- **Материал для статьи** «Почему 40-слайдный pitch deck мёртв».
- **Референс для продукта** — система атрибуции выручки B2B SaaS по каналам.
- **Тема для разбора** — growth-воронка без outbound.

## Черновик ТЗ
**Проблема:** ранние B2B SaaS-founders распыляют ресурсы на 5+ каналов, не зная, какой реально приносит выручку.
**Пользователи:** технические founders, solo-основатели, early-stage teams (до $100K MRR).
**Ключевые функции:**
1. Авто-атрибуция: подтягивание источников клиентов из CRM/биллинга (Stripe, Pipedrive, HubSpot).
2. Визуализация «funnel by source» с контрибуцией в MRR/LTV.
3. Еженедельный дайджест: где органический рост, где outbound, где возврат денег.
4. Интеграция с Twitter / Substack / GitHub для автоматической привязки «контент → лид».
**Стек/ограничения:** Python/FastAPI, Stripe API, Twitter API v2, Postgres, простой дашборд; MVP без ML — просто чистая атрибуция last-touch + first-touch.
</output>
</example>"""

_SYSTEM_PROMPT_RU_BASE = """Ты — ассистент по анализу видео. На вход дан расшифрованный \
текст YouTube-видео (обычно транскрипт с субтитров). Составь КОРОТКИЙ структурированный \
бриф на русском языке.

Структура всегда одинаковая, все 5 секций ОБЯЗАТЕЛЬНЫ:

## Суть
2–3 предложения: о чём видео и главная мысль.

## Ключевые идеи
5–8 пунктов маркированным списком. Каждый пункт — законченная мысль, а не цитата.

## Факты и цифры
Конкретные числа, даты, имена, названия компаний/продуктов/технологий. \
Если ничего значимого нет — напиши "—".

## Возможное применение
Как этот контент можно использовать: идея продукта, материал для статьи, \
источник для RAG, тема для разбора, обучающий материал, референс.

## Черновик ТЗ
Если из видео вырисовывается идея программы/сервиса/инструмента — сформулируй \
черновик ТЗ в 4–6 пунктов (проблема → пользователи → ключевые функции → стек/ограничения). \
Если идеи для софта нет — напиши "—".

Стиль: деловой, конкретный, без воды. Не пересказывай видео — выжимай суть. \
Если транскрипт низкого качества (автосубтитры с ошибками) — пиши нормализованный текст, а не шум.

Вот два примера идеального брифа (разного стиля — бизнес-кейс и техническое интервью):

""" + _FEW_SHOT_EXAMPLE_RU + """

<example>
<input>
Заголовок: Почему pgvector заменил мне Pinecone — опыт за 6 месяцев
Длительность: 1200 сек
Язык: ru (manual)

Транскрипт: Мы полгода платили Pinecone $800 в месяц за 2 миллиона векторов. Решил \
попробовать pgvector в том же Postgres, где у нас уже лежали метаданные. Миграция \
заняла 3 дня. Результат: latency упала с 120мс до 40мс, потому что не стало сетевого \
round-trip, а главное — теперь можно джойнить эмбеддинги с реляционными таблицами \
в одном SQL-запросе. Pinecone не умеет в фильтры по сложным условиям, приходилось \
тащить ID в приложение и делать второй запрос. С pgvector у нас WHERE user_id = $1 \
AND created_at > NOW() - INTERVAL '30 days' ORDER BY embedding <-> $2 LIMIT 10 — \
один запрос, один план, 40мс. Минус — индексы HNSW строятся медленнее и жрут RAM. \
Но для 2М векторов это 6 ГБ — в нашу Neon-инстанцию влезает.
</input>
<output>
## Суть
Опыт миграции с Pinecone на pgvector для 2М векторов: снижение latency со 120 до 40мс, \
экономия $800/мес, возможность JOIN'ов эмбеддингов с реляционными данными в одном SQL-запросе.

## Ключевые идеи
- **pgvector устраняет сетевой round-trip** — эмбеддинги живут в той же БД, где и метаданные.
- Главный выигрыш — не цена, а возможность фильтровать по сложным условиям в SQL напрямую.
- Pinecone заставляет делать 2 запроса для метаданных, что усложняет код и добавляет latency.
- HNSW-индексы в pgvector жрут RAM (~3 ГБ на миллион векторов), планировать ёмкость заранее.
- Миграция за 3 дня — реально для среднего проекта, не многомесячное предприятие.

## Факты и цифры
- Было: Pinecone, $800/мес, latency 120мс
- Стало: pgvector на Neon, latency 40мс
- Объём: 2М векторов, ~6 ГБ памяти под HNSW-индекс
- Время миграции: 3 дня

## Возможное применение
- **Материал для статьи**: «Когда вам не нужна векторная БД».
- **Референс для продукта** — калькулятор TCO vector DB vs pgvector.
- **Источник для RAG** по выбору инфраструктуры AI-приложений.

## Черновик ТЗ
**Проблема:** команды переплачивают за отдельный vector DB, когда у них уже есть Postgres.
**Пользователи:** ML/backend-инженеры, CTO небольших продуктов на $10-100K MRR.
**Ключевые функции:**
1. Анализатор схемы: сканирует Postgres и предлагает план миграции векторов.
2. Калькулятор TCO: сравнивает Pinecone/Weaviate/pgvector для заданного объёма.
3. Генератор миграционных скриптов с HNSW-индексами.
**Стек/ограничения:** Python CLI; read-only доступ к Postgres; цель — помочь принять решение, а не автоматически мигрировать.
</output>
</example>

Теперь составь такой же бриф для видео, которое даст пользователь. Отвечай ТОЛЬКО брифом \
в markdown. Без преамбул, извинений и комментариев о транскрипте."""

_FEW_SHOT_EXAMPLE_EN = """<example>
<input>
Title: Why We Stopped Using Vector Databases for RAG
Duration: 1800 sec
Language: en (auto)

Transcript: Twelve months ago we were all-in on Pinecone. Embedded everything, tuned \
cosine similarity, built a whole evaluation harness. Then we noticed our customers \
were getting irrelevant results on any query that needed multi-hop reasoning or \
temporal filtering. The vector score was high but the chunks were wrong. We tried \
hybrid search with BM25 — it helped for exact-match queries but didn't solve the \
core issue. The real fix was replacing the single "retrieve top-k and stuff context" \
pattern with an agent that can issue multiple targeted SQL queries against structured \
metadata, and only falls back to vector search when the query is genuinely fuzzy. \
Result: a 47% improvement in answer quality on our eval set, and our monthly \
vector-DB bill dropped from $2400 to $180. Most teams are using vector DBs for \
problems that are actually metadata-filter problems in disguise.
</input>
<output>
## TL;DR
Case study on replacing vector-first RAG with agent-driven SQL retrieval. After 12 \
months on Pinecone, the team found that most failing queries needed metadata filtering, \
not semantic search. Switching to an SQL-first agent with vector fallback improved \
answer quality 47% and cut DB cost from $2400 to $180/month.

## Key ideas
- Most RAG failures are **metadata-filter problems**, not semantic similarity problems.
- Top-k + stuff-context is a crutch that hides poor retrieval strategy.
- Hybrid BM25 + vector helps exact-match queries but doesn't fix multi-hop reasoning.
- Agent-driven SQL retrieval outperforms pure vector DB for most business queries.
- Vector DB is best reserved for genuinely fuzzy, semantic queries — as a fallback tier.
- Eval harness revealed the problem; without one the team would have kept scaling the wrong thing.

## Facts & figures
- Timeframe: 12 months on Pinecone before the switch
- Answer-quality improvement: 47% on eval set
- Monthly vector-DB cost: $2400 → $180 (-92.5%)
- Provider: Pinecone → custom agent with SQL + vector fallback

## Possible uses
- Reference architecture for teams currently on vector-first RAG.
- Article material: "When to not use a vector DB".
- Evaluation-harness pattern for RAG quality debugging.
- Source for RAG best-practices training content.

## Software brief
**Problem:** Teams default to vector DBs for any LLM retrieval use case, paying for \
semantic search even when queries are really metadata filters.
**Users:** ML engineers and backend developers building RAG systems; B2B SaaS with \
structured business data (orders, tickets, CRM).
**Features:**
1. Query router: classifier sends query to SQL, vector, or hybrid path based on type.
2. Metadata filter extraction: parse user query for entities/dates/categories before retrieval.
3. Agent tier: multi-step planner that can issue 3-5 targeted SQL queries per question.
4. Vector fallback: only engages when router classifies the query as semantic/fuzzy.
5. Eval harness integration: every production query logged with its route and answer quality.
**Stack / constraints:** Postgres + pgvector for unified store; Claude / GPT-4 as router \
and agent; FastAPI; separate eval service. MVP targets single-DB customers; sharding out-of-scope.
</output>
</example>"""

_SYSTEM_PROMPT_EN = _FEW_SHOT_EXAMPLE_EN + """

You are a video-analysis assistant. The input is a YouTube transcript.
Produce a SHORT structured brief in English with all 5 sections.

## TL;DR
2–3 sentences — what it's about and the central thesis.

## Key ideas
5–8 bullets, each a complete thought.

## Facts & figures
Numbers, dates, names of companies/products/technologies. "—" if none.

## Possible uses
How this content could be reused: product idea, article fodder, RAG source, training material, reference.

## Software brief
If the video suggests an idea for a product or service — draft a 4–6-point spec
(problem → users → features → stack/constraints). Otherwise write "—".

Style: business-tone, concrete, no filler. Don't retell — distill.

Respond with the brief in markdown only. No preamble."""


# ─── JSON schema for structured output ───────────────────────────
_BRIEF_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr":            {"type": "string", "description": "2-3 sentence summary"},
        "key_ideas":       {"type": "array",  "items": {"type": "string"}, "description": "5-8 main points"},
        "facts_figures":   {"type": "array",  "items": {"type": "string"}, "description": "Concrete numbers/dates/names; empty if none"},
        "possible_uses":   {"type": "array",  "items": {"type": "string"}, "description": "Ways to reuse this content"},
        "software_brief": {
            "type": ["object", "null"],
            "description": "Spec draft if a software idea is suggested; null otherwise",
            "properties": {
                "problem":     {"type": "string"},
                "users":       {"type": "string"},
                "features":    {"type": "array", "items": {"type": "string"}},
                "stack_notes": {"type": "string"},
            },
            "required": ["problem", "users", "features", "stack_notes"],
            "additionalProperties": False,
        },
    },
    "required": ["tldr", "key_ideas", "facts_figures", "possible_uses", "software_brief"],
    "additionalProperties": False,
}


def _system_prompt(language: str, template: str = "full") -> str:
    base = _SYSTEM_PROMPT_RU_BASE if language == "ru" else _SYSTEM_PROMPT_EN
    if template == "news":
        note_ru = (
            "\n\n⚠ TEMPLATE=NEWS: пропусти секцию «Черновик ТЗ» целиком. "
            "Выдай только 4 секции — Суть, Ключевые идеи, Факты и цифры, Возможное применение. "
            "В примерах выше секция «Черновик ТЗ» присутствует — в этом ответе её НЕ включай."
        )
        note_en = (
            "\n\n⚠ TEMPLATE=NEWS: skip the «Software brief» section entirely. "
            "Emit only 4 sections — TL;DR, Key ideas, Facts & figures, Possible uses. "
            "The few-shot examples include a software brief; do NOT include it in this response."
        )
        return base + (note_ru if language == "ru" else note_en)
    return base


def generate_brief(
    transcript,
    language: str = "ru",
    fmt: str = "markdown",
    model: str | None = None,
    template: str = "full",
) -> dict:
    """Generate a brief.

    Returns: {
        "content_md": str,          # always set
        "content_json": str | None, # populated when fmt='json'
        "usage": dict,              # token counts
        "model": str,               # resolved model ID
        "format": str,              # 'markdown' | 'json'
    }
    """
    resolved_model = resolve_model(model)
    system_text = _system_prompt(language, template=template)

    client = anthropic.Anthropic()
    request: dict = {
        "model": resolved_model,
        # 2000 truncated 5 of 49 real briefs mid-sentence; the "Черновик ТЗ"
        # section is the tail, and it is exactly what the six artifact stages read.
        "max_tokens": 8000,
        "system": [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Заголовок: {transcript.title or '(без названия)'}\n"
                    f"Длительность: {transcript.duration} сек\n"
                    f"Язык транскрипта: {transcript.language} ({transcript.source})\n\n"
                    f"--- ТРАНСКРИПТ ---\n{transcript.text}"
                ),
            }
        ],
    }

    if fmt == "json":
        request["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": _BRIEF_JSON_SCHEMA,
            }
        }

    message = client.messages.create(**request)
    raise_if_truncated(message, "Бриф")
    text = next((b.text for b in message.content if b.type == "text"), "")

    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens":     getattr(message.usage, "cache_read_input_tokens", 0) or 0,
    }

    content_json: str | None = None
    content_md: str = text
    if fmt == "json":
        try:
            parsed = json.loads(text)
            content_json = json.dumps(parsed, ensure_ascii=False, indent=2)
            content_md = _render_json_as_md(parsed, language)
        except json.JSONDecodeError:
            # Shouldn't happen with json_schema output, but guard anyway
            content_json = None

    return {
        "content_md": content_md,
        "content_json": content_json,
        "usage": usage,
        "model": resolved_model,
        "format": fmt,
    }


def summarize_stream(
    stream_id: str,
    template: str = "news",
    language: str = "ru",
    fmt: str = "markdown",
    model: str | None = None,
) -> dict:
    """Collect all transcribed chunks of a stream, concatenate, and run generate_brief.

    Returns a dict compatible with generate_brief's return, plus `chunks_covered`.
    """
    # Local imports to avoid circulars
    from extractor import Segment, Transcript
    from store import get_stream

    stream = get_stream(stream_id, with_chunks=True)
    if not stream:
        raise ValueError(f"Stream {stream_id} not found")

    chunks_with_text = [
        c for c in (stream.chunks or [])
        if c.transcriptText and c.transcriptText.strip()
    ]
    if not chunks_with_text:
        raise RuntimeError("No transcribed chunks yet — nothing to summarize")

    parts: list[str] = []
    segments: list[Segment] = []
    total_duration = 0
    offset = 0.0
    for c in chunks_with_text:
        parts.append(
            f"[chunk {c.index} @ {c.startedAt.isoformat(timespec='seconds')}]\n"
            f"{c.transcriptText.strip()}"
        )
        segments.append(Segment(
            start=offset, end=offset + c.durationSec,
            text=c.transcriptText.strip(),
        ))
        offset += c.durationSec
        total_duration += int(c.durationSec)
    combined_text = "\n\n".join(parts)

    synthetic = Transcript(
        video_id=stream.id,
        title=f"{stream.channelName} (live stream)",
        duration=total_duration,
        language=language,
        source="live-stream",
        text=combined_text,
        segments=segments,
    )

    result = generate_brief(
        synthetic, language=language, fmt=fmt, model=model, template=template,
    )
    result["chunks_covered"] = len(chunks_with_text)
    return result


def _render_json_as_md(data: dict, language: str) -> str:
    """Render the structured JSON brief back to human-readable markdown."""
    is_ru = language == "ru"
    h = {
        "tldr": "Суть" if is_ru else "TL;DR",
        "ideas": "Ключевые идеи" if is_ru else "Key ideas",
        "facts": "Факты и цифры" if is_ru else "Facts & figures",
        "uses": "Возможное применение" if is_ru else "Possible uses",
        "spec": "Черновик ТЗ" if is_ru else "Software brief",
        "problem": "Проблема" if is_ru else "Problem",
        "users": "Пользователи" if is_ru else "Users",
        "features": "Ключевые функции" if is_ru else "Features",
        "stack": "Стек/ограничения" if is_ru else "Stack / constraints",
        "none": "—",
    }
    parts = [f"## {h['tldr']}\n{data.get('tldr', '').strip()}"]
    parts.append("## " + h["ideas"] + "\n" + "\n".join(f"- {i}" for i in data.get("key_ideas") or []))
    facts = data.get("facts_figures") or []
    parts.append("## " + h["facts"] + "\n" + ("\n".join(f"- {i}" for i in facts) if facts else h["none"]))
    parts.append("## " + h["uses"] + "\n" + "\n".join(f"- {i}" for i in data.get("possible_uses") or []))
    sb = data.get("software_brief")
    if sb:
        spec_lines = [
            f"**{h['problem']}:** {sb.get('problem', '')}",
            f"**{h['users']}:** {sb.get('users', '')}",
            f"**{h['features']}:**",
            *(f"{i+1}. {f}" for i, f in enumerate(sb.get("features") or [])),
            f"**{h['stack']}:** {sb.get('stack_notes', '')}",
        ]
        parts.append("## " + h["spec"] + "\n" + "\n".join(spec_lines))
    else:
        parts.append("## " + h["spec"] + "\n" + h["none"])
    return "\n\n".join(parts)
