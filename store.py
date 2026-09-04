"""Thin sync wrapper around the async Prisma client.

All DB access goes through here. If you need to swap SQLite → Postgres later,
the only thing that changes is `prisma/schema.prisma`'s datasource.
"""
import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from prisma import Prisma
from prisma.errors import UniqueViolationError

# Per-1M-token pricing (USD). Keep in sync with CLAUDE.md + pricing pages.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-sonnet-5":   (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
    # claude-opus-5: rate unconfirmed — left unpriced so cost shows blank
    # rather than a fabricated number. Add (in, out) $/1M once verified.
}


def calculate_cost(model: str, usage: dict) -> float | None:
    """USD cost with cache-write 1.25× and cache-read 0.10× multipliers."""
    if model not in _PRICING:
        return None
    in_price, out_price = _PRICING[model]
    effective_in = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0) * 1.25
        + usage.get("cache_read_input_tokens", 0) * 0.10
    )
    return (effective_in * in_price + usage.get("output_tokens", 0) * out_price) / 1_000_000


async def _upsert_video_and_segments(transcript) -> None:
    db = Prisma()
    await db.connect()
    try:
        vid = transcript.video_id
        url = f"https://www.youtube.com/watch?v={vid}"
        await db.video.upsert(
            where={"id": vid},
            data={
                "create": {
                    "id": vid, "url": url,
                    "title": transcript.title or None,
                    "duration": transcript.duration or None,
                    "language": transcript.language,
                    "source": transcript.source,
                },
                "update": {
                    "title": transcript.title or None,
                    "duration": transcript.duration or None,
                    "language": transcript.language,
                    "source": transcript.source,
                },
            },
        )
        # Rewrite segments: simpler than diffing. Cascade already covers cleanup if Video is deleted.
        await db.segment.delete_many(where={"videoId": vid})
        if transcript.segments:
            await db.segment.create_many(
                data=[
                    {"videoId": vid, "index": i, "start": s.start, "end": s.end, "text": s.text}
                    for i, s in enumerate(transcript.segments)
                ]
            )
    finally:
        await db.disconnect()


async def _insert_brief(
    video_id: str,
    model: str,
    language: str,
    fmt: str,
    content_md: str,
    content_json: str | None,
    usage: dict,
    cost_usd: float | None,
) -> int:
    db = Prisma()
    await db.connect()
    try:
        brief = await db.brief.create(
            data={
                "videoId": video_id,
                "model": model,
                "language": language,
                "format": fmt,
                "contentMd": content_md,
                "contentJson": content_json,
                "inputTokens": usage.get("input_tokens", 0),
                "outputTokens": usage.get("output_tokens", 0),
                "cacheReadTokens": usage.get("cache_read_input_tokens", 0),
                "cacheWriteTokens": usage.get("cache_creation_input_tokens", 0),
                "costUsd": cost_usd,
            }
        )
        return brief.id
    finally:
        await db.disconnect()


async def _log_run(
    url: str,
    video_id: str | None,
    status: str,
    error: str | None,
    triggered_by: str,
    started_at: datetime,
) -> int:
    db = Prisma()
    await db.connect()
    try:
        run = await db.run.create(
            data={
                "url": url, "videoId": video_id, "status": status,
                "error": error, "triggeredBy": triggered_by,
                "startedAt": started_at, "finishedAt": datetime.now(),
            }
        )
        return run.id
    finally:
        await db.disconnect()


async def _find_matching_brief(
    video_id: str, model: str, language: str, fmt: str
) -> Any:
    db = Prisma()
    await db.connect()
    try:
        return await db.brief.find_first(
            where={
                "videoId": video_id, "model": model,
                "language": language, "format": fmt,
            },
            order={"createdAt": "desc"},
        )
    finally:
        await db.disconnect()


async def _query_video(video_id: str, with_segments: bool = False) -> Any:
    db = Prisma()
    await db.connect()
    try:
        return await db.video.find_unique(
            where={"id": video_id},
            include={
                "briefs": True,
                "segments": {"order_by": {"index": "asc"}} if with_segments else False,
            },
        )
    finally:
        await db.disconnect()


# ─── Sync entrypoints — callers don't need to know about asyncio ─────

def save_transcript(transcript) -> None:
    asyncio.run(_upsert_video_and_segments(transcript))


def save_brief(video_id, model, language, fmt, content_md, content_json, usage) -> tuple[int, float | None]:
    cost = calculate_cost(model, usage)
    brief_id = asyncio.run(_insert_brief(
        video_id, model, language, fmt, content_md, content_json, usage, cost
    ))
    return brief_id, cost


def log_run(url: str, video_id: str | None, status: str, error: str | None,
            triggered_by: str, started_at: datetime) -> int:
    return asyncio.run(_log_run(url, video_id, status, error, triggered_by, started_at))


def get_video(video_id: str, with_segments: bool = False):
    return asyncio.run(_query_video(video_id, with_segments=with_segments))


def find_matching_brief(video_id: str, model: str, language: str, fmt: str):
    return asyncio.run(_find_matching_brief(video_id, model, language, fmt))


# ─── Phase 1: live streams ─────────────────────────────────────────

async def _create_stream(data: dict):
    db = Prisma()
    await db.connect()
    try:
        return await db.livestream.create(data=data)
    finally:
        await db.disconnect()


async def _list_streams(status: str | None = None):
    db = Prisma()
    await db.connect()
    try:
        where = {"status": status} if status else {}
        return await db.livestream.find_many(
            where=where,
            order={"createdAt": "desc"},
            # Pull all chunks so we can compute latest + status counts client-side.
            # Cheap for typical workloads (1-2 streams × 30-300 chunks).
            include={"chunks": {"order_by": {"index": "desc"}}},
        )
    finally:
        await db.disconnect()


async def _get_stream(stream_id: str, with_chunks: bool = False):
    db = Prisma()
    await db.connect()
    try:
        return await db.livestream.find_unique(
            where={"id": stream_id},
            include={
                "chunks": {"order_by": {"index": "asc"}} if with_chunks else False,
                "newsItems": True,
                "streamBriefs": {"order_by": {"createdAt": "desc"}},
            },
        )
    finally:
        await db.disconnect()


async def _update_stream_status(stream_id: str, status: str):
    db = Prisma()
    await db.connect()
    try:
        await db.livestream.update(where={"id": stream_id}, data={"status": status})
    finally:
        await db.disconnect()


async def _update_stream_fields(stream_id: str, data: dict):
    """Update arbitrary mutable fields. Ignores unknown keys."""
    db = Prisma()
    await db.connect()
    try:
        return await db.livestream.update(where={"id": stream_id}, data=data)
    finally:
        await db.disconnect()


async def _next_chunk_index(stream_id: str) -> int:
    db = Prisma()
    await db.connect()
    try:
        last = await db.chunk.find_first(
            where={"streamId": stream_id},
            order={"index": "desc"},
        )
        return (last.index + 1) if last else 0
    finally:
        await db.disconnect()


async def _save_chunk(stream_id: str, index: int, started_at: datetime,
                      duration_sec: float, audio_path: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.chunk.create(data={
            "streamId": stream_id, "index": index, "startedAt": started_at,
            "durationSec": duration_sec, "audioPath": audio_path,
            "status": "pending",
        })
    finally:
        await db.disconnect()


async def _update_chunk(chunk_id: str, **fields):
    db = Prisma()
    await db.connect()
    try:
        return await db.chunk.update(where={"id": chunk_id}, data=fields)
    finally:
        await db.disconnect()


async def _get_pending_chunks(status: str, limit: int = 10):
    db = Prisma()
    await db.connect()
    try:
        return await db.chunk.find_many(
            where={"status": status},
            order={"createdAt": "asc"},
            take=limit,
            include={"stream": True},
        )
    finally:
        await db.disconnect()


async def _insert_news_items(
    stream_id: str, chunk_id: str, items: list[dict], attribution: str
):
    db = Prisma()
    await db.connect()
    try:
        if not items:
            return 0
        await db.newsitem.create_many(data=[
            {
                "streamId": stream_id, "chunkId": chunk_id,
                "headline": it["headline"], "quote": it["quote"],
                "category": it.get("category"),
                "offsetSec": float(it.get("offset_sec", 0)),
                "confidence": float(it.get("confidence", 0.5)),
                "tags": json.dumps(it.get("tags", []), ensure_ascii=False),
                "attribution": attribution,
                "status": "draft",
            }
            for it in items
        ])
        return len(items)
    finally:
        await db.disconnect()


async def _list_news_items(
    stream_id: str | None = None, status: str | None = None, limit: int = 200
):
    db = Prisma()
    await db.connect()
    try:
        where: dict = {}
        if stream_id:
            where["streamId"] = stream_id
        if status:
            where["status"] = status
        return await db.newsitem.find_many(
            where=where, order={"createdAt": "desc"}, take=limit,
            include={"stream": True},
        )
    finally:
        await db.disconnect()


async def _update_news_item_status(item_id: int, status: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.newsitem.update(
            where={"id": item_id}, data={"status": status}
        )
    finally:
        await db.disconnect()


# Note: SQLite LIKE is case-insensitive only for ASCII. Cyrillic search is
# case-sensitive. For the Editor workspace that's acceptable (users can search
# by keyword in either case) — full unicode-insensitive search needs raw SQL
# with LOWER() or switching to Postgres with mode="insensitive".
async def _search_news_items(stream_id: str | None, status: str | None, q: str | None, limit: int):
    db = Prisma()
    await db.connect()
    try:
        where: dict = {}
        if stream_id:
            where["streamId"] = stream_id
        if status:
            where["status"] = status
        if q:
            # Prisma supports OR with contains (case-insensitive). SQLite LIKE is
            # case-insensitive for ASCII by default; mode="insensitive" is Postgres-only.
            # Use startswith/contains on lowercase for simplicity — good enough for UI search.
            # NB: Prisma Python doesn't support mode= for sqlite; use basic `contains`.
            where["OR"] = [
                {"headline": {"contains": q}},
                {"quote":    {"contains": q}},
            ]
        items = await db.newsitem.find_many(
            where=where,
            include={"stream": True},
            order={"createdAt": "desc"},
            take=limit,
        )
        return items
    finally:
        await db.disconnect()


# ─── Sync wrappers for Phase 1 ─────────────────────────────────────

def create_stream(**kwargs):
    return asyncio.run(_create_stream(kwargs))


def list_streams(status: str | None = None):
    return asyncio.run(_list_streams(status))


def get_stream(stream_id: str, with_chunks: bool = False):
    return asyncio.run(_get_stream(stream_id, with_chunks=with_chunks))


def update_stream_status(stream_id: str, status: str):
    return asyncio.run(_update_stream_status(stream_id, status))


def update_stream_fields(stream_id: str, data: dict):
    return asyncio.run(_update_stream_fields(stream_id, data))


def next_chunk_index(stream_id: str) -> int:
    return asyncio.run(_next_chunk_index(stream_id))


def save_chunk(stream_id: str, index: int, started_at, duration_sec: float, audio_path: str):
    return asyncio.run(_save_chunk(stream_id, index, started_at, duration_sec, audio_path))


def update_chunk(chunk_id: str, **fields):
    return asyncio.run(_update_chunk(chunk_id, **fields))


def get_pending_chunks(status: str, limit: int = 10):
    return asyncio.run(_get_pending_chunks(status, limit))


def insert_news_items(stream_id: str, chunk_id: str, items: list[dict], attribution: str):
    return asyncio.run(_insert_news_items(stream_id, chunk_id, items, attribution))


def list_news_items(stream_id: str | None = None, status: str | None = None, limit: int = 200):
    return asyncio.run(_list_news_items(stream_id, status, limit))


def search_news_items(stream_id: str | None, status: str | None, q: str | None, limit: int = 200):
    return asyncio.run(_search_news_items(stream_id, status, q, limit))


def update_news_item_status(item_id: int, status: str):
    return asyncio.run(_update_news_item_status(item_id, status))


# ─── App settings (key/value) ──────────────────────────────────────

# Defaults baked in. UI loads these, user overrides persist in DB.
DEFAULT_SETTINGS: dict[str, Any] = {
    # Form defaults
    "default_brief_model":          "",               # "" = env CLAUDE_MODEL or sonnet-4-6
    "default_brief_lang":           "ru",
    "default_brief_format":         "markdown",
    "default_backend":              "auto",
    "default_whisper_model":        "medium",
    "default_stream_interval_min":  5,
    "default_brief_template":       "news",
    "default_auto_brief_on_stop":   False,
    # Retention policy (0 = forever, > 0 = days to keep)
    "retain_chunk_audio_days":        7,   # MP3 files in ./chunks/
    "retain_chunk_row_days":          0,   # Chunk DB rows (with transcripts) — default forever
    "retain_news_item_days":          0,   # forever
    "retain_stream_brief_days":       0,   # forever
    "retain_video_output_files_days": 30,  # ./output/ files from single-video briefs
    "retain_failed_chunks_days":      3,   # aggressive cleanup of failed chunks
    "cleanup_auto_enabled":           False,  # master switch for periodic cleaner
    "cleanup_interval_hours":         6,
    # Semantic dedup of news items (fastembed local | ollama | openai-compat | none)
    "dedup_enabled":                True,
    "dedup_embedding_provider":     "fastembed",
    "dedup_embedding_model":        "",        # provider default if empty
    "dedup_window_hours":           24,
    "dedup_similarity_threshold":   0.85,
    # News enrichment (expanded text + illustration via OpenAI + stock photos)
    "enrich_text_model":            "gpt-4o-mini",     # gpt-4o | gpt-4o-mini | gpt-4-turbo | o1-mini
    "enrich_image_model":           "dall-e-3",        # dall-e-3 | dall-e-3-hd | gpt-image-1 | dall-e-2
    "enrich_image_source":          "hybrid",          # generate (pure AI) | stock (Pexels) | hybrid (stock+AI edit)
    "enrich_auto_on_approve":       False,             # auto-enrich when a draft is approved
    "enrich_image_enabled":         True,              # can disable image-gen to save $
    "enrich_image_dedup_threshold": 0.88,              # cosine for concept-phrase reuse
    # AI Assistant (operational helper — answers "how to configure X", executes tools)
    "assistant_provider":           "openai",          # openai | anthropic | ollama
    "assistant_model":              "gpt-4o",          # gpt-4o | gpt-4o-mini | claude-sonnet-4-6 | llama3.1:8b | ...
    "assistant_cache_enabled":      True,              # reuse prior answers via fastembed similarity
    "assistant_cache_similarity":   0.85,              # threshold 0.0-1.0
    "assistant_auto_confirm_writes": False,            # if True, writes don't require explicit user OK (risky)
    # Local LLM (Ollama) — used by spec-expansion in the brief view
    "local_llm_model":              "qwen2.5:7b",      # any installed Ollama tag; pick from /local-llm/models
    "local_llm_num_ctx":            32768,             # qwen2.5 native cap; default 2048 truncates brief+transcript
    "local_llm_temperature":        0.3,               # lower = more focused/deterministic specs
    "local_llm_max_transcript_chars": 60000,           # raw transcript clip before stuffing into prompt
    # Research-stage external verification (Anthropic server-side web search).
    "research_web_search_enabled":  True,              # off → research degrades to "не проверено"
    "research_web_search_max_uses": 5,                 # searches per research run; 0 disables
}


async def _get_all_settings() -> dict:
    db = Prisma()
    await db.connect()
    try:
        rows = await db.appsetting.find_many()
        result = dict(DEFAULT_SETTINGS)
        for r in rows:
            try:
                result[r.key] = json.loads(r.value)
            except json.JSONDecodeError:
                result[r.key] = r.value
        return result
    finally:
        await db.disconnect()


async def _set_settings(updates: dict) -> dict:
    """Upsert each key. Unknown keys are accepted to remain flexible."""
    db = Prisma()
    await db.connect()
    try:
        for key, value in updates.items():
            encoded = json.dumps(value, ensure_ascii=False)
            await db.appsetting.upsert(
                where={"key": key},
                data={"create": {"key": key, "value": encoded}, "update": {"value": encoded}},
            )
        return await _get_all_settings_inner(db)
    finally:
        await db.disconnect()


async def _get_all_settings_inner(db: Prisma) -> dict:
    """Reuse-friendly version that takes an open connection."""
    rows = await db.appsetting.find_many()
    result = dict(DEFAULT_SETTINGS)
    for r in rows:
        try:
            result[r.key] = json.loads(r.value)
        except json.JSONDecodeError:
            result[r.key] = r.value
    return result


def get_all_settings() -> dict:
    return asyncio.run(_get_all_settings())


def set_settings(updates: dict) -> dict:
    return asyncio.run(_set_settings(updates))


# ─── Expansions (local-LLM extended sections) ──────────────────────

async def _get_expansion(video_id: str, mode: str):
    """Current artifact = highest version, whatever its status. An errored
    latest version must stay visible; the UI relies on seeing it."""
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.find_first(
            where={"videoId": video_id, "mode": mode},
            order={"version": "desc"},
        )
    finally:
        await db.disconnect()


async def _get_expansion_version(video_id: str, mode: str, version: int):
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.find_unique(
            where={"videoId_mode_version": {
                "videoId": video_id, "mode": mode, "version": version}},
        )
    finally:
        await db.disconnect()


async def _list_expansion_versions(video_id: str, mode: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.find_many(
            where={"videoId": video_id, "mode": mode},
            order={"version": "desc"},
        )
    finally:
        await db.disconnect()


async def _list_expansions(video_id: str):
    """One row per mode — the current version. The UI pre-fills its modal from
    this, so older versions must not show up as extra artifacts."""
    db = Prisma()
    await db.connect()
    try:
        rows = await db.expansion.find_many(
            where={"videoId": video_id},
            order={"version": "desc"},
        )
        latest: dict[str, Any] = {}
        for r in rows:
            if r.mode not in latest:
                latest[r.mode] = r
        return sorted(latest.values(), key=lambda r: r.updatedAt, reverse=True)
    finally:
        await db.disconnect()


async def _start_expansion(
    *, video_id: str, mode: str, source_title: str, source_md: str,
    context_mode: str, model: str, num_ctx: int, input_chars: int,
    _max_attempts: int = 5,
):
    """Append a new running version. Never overwrites: two clients generating
    the same mode concurrently produce two versions instead of racing for one
    row. The read-then-write (max version -> create version+1) is not
    atomic, so a concurrent caller can create the same next version first;
    that loses the race with a UniqueViolationError on (videoId, mode,
    version), which we retry against a fresh read rather than let propagate."""
    db = Prisma()
    await db.connect()
    try:
        for attempt in range(_max_attempts):
            last = await db.expansion.find_first(
                where={"videoId": video_id, "mode": mode}, order={"version": "desc"},
            )
            version = (last.version + 1) if last else 1
            try:
                return await db.expansion.create(data={
                    "videoId": video_id, "mode": mode, "version": version,
                    "fromVersion": last.version if last else None,
                    "sourceTitle": source_title, "sourceMd": source_md,
                    "contextMode": context_mode, "model": model, "numCtx": num_ctx,
                    "contentMd": "", "inputChars": input_chars, "elapsedMs": 0,
                    "status": "running", "error": None,
                })
            except UniqueViolationError:
                if attempt == _max_attempts - 1:
                    raise
                continue
    finally:
        await db.disconnect()


async def _finish_expansion(
    *, video_id: str, mode: str, content_md: str, elapsed_ms: int,
    verdict: str | None = None,
):
    db = Prisma()
    await db.connect()
    try:
        last = await db.expansion.find_first(
            where={"videoId": video_id, "mode": mode}, order={"version": "desc"},
        )
        if not last:
            return None
        return await db.expansion.update(
            where={"id": last.id},
            data={"contentMd": content_md, "elapsedMs": elapsed_ms,
                  "status": "done", "error": None, "verdict": verdict},
        )
    finally:
        await db.disconnect()


async def _fail_expansion(*, video_id: str, mode: str, error: str):
    db = Prisma()
    await db.connect()
    try:
        last = await db.expansion.find_first(
            where={"videoId": video_id, "mode": mode}, order={"version": "desc"},
        )
        if not last:
            return None
        return await db.expansion.update(
            where={"id": last.id},
            data={"status": "error", "error": error[:2000]},
        )
    finally:
        await db.disconnect()


async def _sweep_running_expansions() -> int:
    """Mark orphaned running rows (from a crash/restart) as error. Returns count."""
    db = Prisma()
    await db.connect()
    try:
        return await db.expansion.update_many(
            where={"status": "running"},
            data={"status": "error", "error": "прервано рестартом"},
        )
    finally:
        await db.disconnect()


def start_expansion(**kwargs):
    return asyncio.run(_start_expansion(**kwargs))


def finish_expansion(**kwargs):
    return asyncio.run(_finish_expansion(**kwargs))


def fail_expansion(**kwargs):
    return asyncio.run(_fail_expansion(**kwargs))


def sweep_running_expansions() -> int:
    return asyncio.run(_sweep_running_expansions())


def get_expansion(video_id: str, mode: str):
    return asyncio.run(_get_expansion(video_id, mode))


def get_expansion_version(video_id: str, mode: str, version: int):
    return asyncio.run(_get_expansion_version(video_id, mode, version))


def list_expansion_versions(video_id: str, mode: str):
    return asyncio.run(_list_expansion_versions(video_id, mode))


def list_expansions(video_id: str):
    return asyncio.run(_list_expansions(video_id))


# ─── Transcript edits (version history) ────────────────────────────

async def _create_transcript_edit(
    *, video_id: str, content_md: str, op: str, instruction: str,
    from_version: int | None, model: str, input_chars: int, elapsed_ms: int,
    kind: str = "transcript",
):
    db = Prisma()
    await db.connect()
    try:
        last = await db.transcriptedit.find_first(
            where={"videoId": video_id, "kind": kind}, order={"version": "desc"},
        )
        version = (last.version + 1) if last else 1
        return await db.transcriptedit.create(data={
            "videoId": video_id, "kind": kind, "version": version, "contentMd": content_md,
            "op": op, "instruction": instruction, "fromVersion": from_version,
            "model": model, "inputChars": input_chars, "elapsedMs": elapsed_ms,
        })
    finally:
        await db.disconnect()


async def _list_transcript_edits(video_id: str, kind: str = "transcript"):
    db = Prisma()
    await db.connect()
    try:
        return await db.transcriptedit.find_many(
            where={"videoId": video_id, "kind": kind}, order={"version": "desc"},
        )
    finally:
        await db.disconnect()


async def _get_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    db = Prisma()
    await db.connect()
    try:
        return await db.transcriptedit.find_unique(
            where={"videoId_kind_version": {
                "videoId": video_id, "kind": kind, "version": version}},
        )
    finally:
        await db.disconnect()


async def _rollback_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    db = Prisma()
    await db.connect()
    try:
        src = await db.transcriptedit.find_unique(
            where={"videoId_kind_version": {
                "videoId": video_id, "kind": kind, "version": version}},
        )
        if not src:
            return None
        last = await db.transcriptedit.find_first(
            where={"videoId": video_id, "kind": kind}, order={"version": "desc"},
        )
        new_version = (last.version + 1) if last else 1
        return await db.transcriptedit.create(data={
            "videoId": video_id, "kind": kind, "version": new_version,
            "contentMd": src.contentMd, "op": "rollback", "instruction": "",
            "fromVersion": version, "model": src.model,
            "inputChars": src.inputChars, "elapsedMs": 0,
        })
    finally:
        await db.disconnect()


async def _upsert_stage_gate(*, video_id: str, stage: str, items: list, assessed: bool):
    import json as _json
    from datetime import datetime, timezone
    db = Prisma()
    await db.connect()
    try:
        payload = _json.dumps(items, ensure_ascii=False)
        assessed_at = datetime.now(timezone.utc) if assessed else None
        return await db.stagegate.upsert(
            where={"videoId_stage": {"videoId": video_id, "stage": stage}},
            data={
                "create": {"videoId": video_id, "stage": stage, "items": payload,
                           "assessedAt": assessed_at},
                "update": {"items": payload, **({"assessedAt": assessed_at} if assessed else {})},
            },
        )
    finally:
        await db.disconnect()


async def _get_stage_gate(video_id: str, stage: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.stagegate.find_unique(
            where={"videoId_stage": {"videoId": video_id, "stage": stage}})
    finally:
        await db.disconnect()


async def _list_stage_gates(video_id: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.stagegate.find_many(where={"videoId": video_id})
    finally:
        await db.disconnect()


# ─── Doc drafts (unsaved generations, one per video+kind) ──────────

async def _upsert_transcript_draft(*, video_id, kind, content_md, op,
                                   instruction, from_version, model, elapsed_ms):
    db = Prisma()
    await db.connect()
    try:
        fields = {
            "contentMd": content_md, "op": op, "instruction": instruction or "",
            "fromVersion": from_version, "model": model, "elapsedMs": elapsed_ms,
        }
        return await db.transcriptdraft.upsert(
            where={"videoId_kind": {"videoId": video_id, "kind": kind}},
            data={"create": {"videoId": video_id, "kind": kind, **fields},
                  "update": fields},
        )
    finally:
        await db.disconnect()


async def _get_transcript_draft(video_id: str, kind: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.transcriptdraft.find_unique(
            where={"videoId_kind": {"videoId": video_id, "kind": kind}})
    finally:
        await db.disconnect()


async def _delete_transcript_draft(video_id: str, kind: str):
    db = Prisma()
    await db.connect()
    try:
        # delete_many so a missing draft is a no-op (unique delete would raise).
        return await db.transcriptdraft.delete_many(
            where={"videoId": video_id, "kind": kind})
    finally:
        await db.disconnect()


# ─── Screenshot-references ─────────────────────────────────────────

async def _replace_screenshots(video_id: str, items: list[dict]):
    """Drop existing screenshot rows for the video, then insert `items`
    (each: timestamp, segment_index?, file_path, caption, reason?, model?)."""
    db = Prisma()
    await db.connect()
    try:
        await db.screenshot.delete_many(where={"videoId": video_id})
        rows = []
        for it in items:
            rows.append(await db.screenshot.create(data={
                "videoId": video_id,
                "timestamp": float(it["timestamp"]),
                "segmentIndex": it.get("segment_index"),
                "filePath": it["file_path"],
                "caption": it.get("caption", ""),
                "reason": it.get("reason", ""),
                "model": it.get("model", ""),
            }))
        return rows
    finally:
        await db.disconnect()


async def _list_screenshots(video_id: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.screenshot.find_many(
            where={"videoId": video_id}, order={"timestamp": "asc"})
    finally:
        await db.disconnect()


async def _delete_screenshot(screenshot_id: int):
    db = Prisma()
    await db.connect()
    try:
        return await db.screenshot.delete_many(where={"id": screenshot_id})
    finally:
        await db.disconnect()


def create_transcript_edit(**kwargs):
    return asyncio.run(_create_transcript_edit(**kwargs))


def upsert_stage_gate(**kwargs):
    return asyncio.run(_upsert_stage_gate(**kwargs))


def get_stage_gate(video_id: str, stage: str):
    return asyncio.run(_get_stage_gate(video_id, stage))


def list_stage_gates(video_id: str):
    return asyncio.run(_list_stage_gates(video_id))


def upsert_transcript_draft(**kwargs):
    return asyncio.run(_upsert_transcript_draft(**kwargs))


def get_transcript_draft(video_id: str, kind: str):
    return asyncio.run(_get_transcript_draft(video_id, kind))


def delete_transcript_draft(video_id: str, kind: str):
    return asyncio.run(_delete_transcript_draft(video_id, kind))


def replace_screenshots(video_id: str, items: list[dict]):
    return asyncio.run(_replace_screenshots(video_id, items))


def list_screenshots(video_id: str):
    return asyncio.run(_list_screenshots(video_id))


def delete_screenshot(screenshot_id: int):
    return asyncio.run(_delete_screenshot(screenshot_id))


def list_transcript_edits(video_id: str, kind: str = "transcript"):
    return asyncio.run(_list_transcript_edits(video_id, kind=kind))


def get_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    return asyncio.run(_get_transcript_edit(video_id, version, kind=kind))


def rollback_transcript_edit(video_id: str, version: int, kind: str = "transcript"):
    return asyncio.run(_rollback_transcript_edit(video_id, version, kind=kind))


# ─── Stream briefs ─────────────────────────────────────────────────

async def _save_stream_brief(
    stream_id: str, template: str, content_md: str, content_json: str | None,
    model: str, usage: dict, cost_usd: float | None, chunks_covered: int,
):
    db = Prisma()
    await db.connect()
    try:
        return await db.streambrief.create(data={
            "streamId": stream_id, "template": template,
            "contentMd": content_md, "contentJson": content_json,
            "model": model,
            "inputTokens": usage.get("input_tokens", 0),
            "outputTokens": usage.get("output_tokens", 0),
            "cacheReadTokens": usage.get("cache_read_input_tokens", 0),
            "cacheWriteTokens": usage.get("cache_creation_input_tokens", 0),
            "costUsd": cost_usd,
            "chunksCovered": chunks_covered,
        })
    finally:
        await db.disconnect()


async def _list_stream_briefs(stream_id: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.streambrief.find_many(
            where={"streamId": stream_id},
            order={"createdAt": "desc"},
        )
    finally:
        await db.disconnect()


def save_stream_brief(stream_id: str, template: str, content_md: str, content_json: str | None,
                     model: str, usage: dict, cost_usd: float | None, chunks_covered: int):
    return asyncio.run(_save_stream_brief(
        stream_id, template, content_md, content_json, model, usage, cost_usd, chunks_covered
    ))


def list_stream_briefs(stream_id: str):
    return asyncio.run(_list_stream_briefs(stream_id))


# ─── Dedup support ─────────────────────────────────────────────────

async def _list_items_by_chunk(chunk_id: str):
    db = Prisma()
    await db.connect()
    try:
        return await db.newsitem.find_many(
            where={"chunkId": chunk_id}, order={"id": "desc"}
        )
    finally:
        await db.disconnect()


def _list_chunk_news_items(chunk_id: str):
    return asyncio.run(_list_items_by_chunk(chunk_id))


async def _load_recent_embeddings(
    stream_id: str | None, hours: int
) -> list[tuple[int, list[float]]]:
    """Fetch (id, embedding) pairs from news items within `hours` window, optionally
    scoped to one stream. Skips rows without embeddings."""
    db = Prisma()
    await db.connect()
    try:
        cutoff = datetime.now() - timedelta(hours=max(1, hours))
        where: dict = {"createdAt": {"gt": cutoff}, "NOT": [{"embedding": None}]}
        if stream_id:
            where["streamId"] = stream_id
        rows = await db.newsitem.find_many(where=where)
        out: list[tuple[int, list[float]]] = []
        for r in rows:
            if not r.embedding:
                continue
            try:
                vec = json.loads(r.embedding)
                if isinstance(vec, list) and vec:
                    out.append((r.id, vec))
            except json.JSONDecodeError:
                continue
        return out
    finally:
        await db.disconnect()


async def _update_news_item_dedup_link(
    item_id: int, embedding: list[float] | None,
    duplicate_of_id: int | None, similarity: float | None,
):
    db = Prisma()
    await db.connect()
    try:
        data = {
            "embedding": json.dumps(embedding) if embedding is not None else None,
            "duplicateOfId": duplicate_of_id,
            "duplicateSim": similarity,
        }
        await db.newsitem.update(where={"id": item_id}, data=data)
    finally:
        await db.disconnect()


def load_recent_embeddings(stream_id: str | None, hours: int):
    return asyncio.run(_load_recent_embeddings(stream_id, hours))


def update_news_item_dedup(item_id: int, embedding, duplicate_of_id, similarity):
    return asyncio.run(
        _update_news_item_dedup_link(item_id, embedding, duplicate_of_id, similarity)
    )


# ─── Enrichment (expanded text + image) ───────────────────────────

async def _get_news_item(item_id: int):
    db = Prisma()
    await db.connect()
    try:
        return await db.newsitem.find_unique(
            where={"id": item_id},
            include={"image": True, "stream": True},
        )
    finally:
        await db.disconnect()


async def _find_similar_image(
    concept_embedding: list[float], threshold: float
) -> dict | None:
    db = Prisma()
    await db.connect()
    try:
        images = await db.newsimage.find_many()
        best: tuple[float, Any] | None = None
        for img in images:
            if not img.conceptEmbedding:
                continue
            try:
                emb = json.loads(img.conceptEmbedding)
            except json.JSONDecodeError:
                continue
            if not isinstance(emb, list) or not emb:
                continue
            # Cosine similarity inline (avoids importing from dedup here)
            import math
            dot = sum(a * b for a, b in zip(concept_embedding, emb))
            na = math.sqrt(sum(a * a for a in concept_embedding))
            nb = math.sqrt(sum(b * b for b in emb))
            sim = (dot / (na * nb)) if na and nb else 0.0
            if sim >= threshold and (best is None or sim > best[0]):
                best = (sim, img)
        if best:
            sim, img = best
            return {"id": img.id, "path": img.filePath, "sim": sim,
                    "concept": img.conceptPhrase}
        return None
    finally:
        await db.disconnect()


async def _create_news_image(
    concept: str, embedding: list[float], prompt: str,
    file_path: str, model: str, cost_usd: float,
) -> int:
    db = Prisma()
    await db.connect()
    try:
        img = await db.newsimage.create(data={
            "conceptPhrase": concept,
            "conceptEmbedding": json.dumps(embedding) if embedding else None,
            "prompt": prompt,
            "filePath": file_path,
            "model": model,
            "costUsd": cost_usd,
        })
        return img.id
    finally:
        await db.disconnect()


async def _increment_image_reuse(image_id: int):
    db = Prisma()
    await db.connect()
    try:
        img = await db.newsimage.find_unique(where={"id": image_id})
        if img:
            await db.newsimage.update(
                where={"id": image_id},
                data={"reuseCount": (img.reuseCount or 0) + 1},
            )
    finally:
        await db.disconnect()


async def _update_news_item_enrichment(
    item_id: int, expanded_text: str, expanded_model: str,
    expanded_cost: float, image_id: int | None,
):
    db = Prisma()
    await db.connect()
    try:
        await db.newsitem.update(
            where={"id": item_id},
            data={
                "expandedText": expanded_text,
                "expandedModel": expanded_model,
                "expandedCostUsd": expanded_cost,
                "imageId": image_id,
            },
        )
    finally:
        await db.disconnect()


async def _get_news_image(image_id: int):
    db = Prisma()
    await db.connect()
    try:
        return await db.newsimage.find_unique(where={"id": image_id})
    finally:
        await db.disconnect()


async def _list_news_images(limit: int = 100):
    db = Prisma()
    await db.connect()
    try:
        return await db.newsimage.find_many(
            order={"createdAt": "desc"}, take=limit,
        )
    finally:
        await db.disconnect()


def get_news_item(item_id: int):
    return asyncio.run(_get_news_item(item_id))


def get_news_image(image_id: int):
    return asyncio.run(_get_news_image(image_id))


def find_similar_image(concept_embedding: list[float], threshold: float):
    return asyncio.run(_find_similar_image(concept_embedding, threshold))


def create_news_image(concept, embedding, prompt, file_path, model, cost_usd):
    return asyncio.run(_create_news_image(concept, embedding, prompt, file_path, model, cost_usd))


def increment_image_reuse(image_id: int):
    return asyncio.run(_increment_image_reuse(image_id))


def update_news_item_enrichment(item_id, expanded_text, expanded_model, expanded_cost, image_id):
    return asyncio.run(_update_news_item_enrichment(
        item_id, expanded_text, expanded_model, expanded_cost, image_id,
    ))


def list_news_images(limit: int = 100):
    return asyncio.run(_list_news_images(limit))


async def _apply_editor_change(item_id: int, field: str, value, tool_call_id: str):
    """Async inner: update one NewsItem field."""
    allowed = {"headline", "quote", "expandedText", "imageId", "tags"}
    if field not in allowed:
        raise ValueError(f"field {field!r} not in allowed set {sorted(allowed)}")
    if field == "tags" and not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)

    db = Prisma()
    await db.connect()
    try:
        existing = await db.newsitem.find_unique(where={"id": item_id})
        if existing is None:
            raise LookupError(f"NewsItem {item_id} not found")
        return await db.newsitem.update(
            where={"id": item_id},
            data={field: value},
        )
    finally:
        await db.disconnect()


def apply_editor_change(item_id: int, field: str, value, tool_call_id: str):
    """Sync wrapper for the editor-apply endpoint. Returns the updated row."""
    return asyncio.run(_apply_editor_change(item_id, field, value, tool_call_id))
