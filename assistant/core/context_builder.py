"""Cherry-pick relevant context for the assistant.

Pipeline per user question:
  1. Load user-provided UI context (current tab, focused entity) — always goes in.
  2. Always-include: settings snapshot + integration key status (small, high-value).
  3. Match errors.yaml patterns against question — direct regex hits bypass scoring.
  4. Score KB chunks (setup_guide / code_module / schema) and take top-N.
  5. Score live DB items (recent news / active streams / recent errors) and take top-N.
  6. Assemble into a compact system-prompt section.

The final context is ~3-8KB instead of 200KB of raw data. Same idea as the
Telegram reference — but tuned for *operational* queries, not content analysis.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Any

import yaml
from prisma import Prisma

from assistant.core.analyzer import score_db_item, score_kb_chunk, tokenize
from assistant.core.knowledge_base import ASSISTANT_DIR, load_kb

# Caps for individual sections — prevents runaway prompts
MAX_KB_CHUNKS = 6
MAX_NEWS = 5
MAX_STREAMS = 5
MAX_ERRORS = 8
MAX_BODY_CHARS = 1200  # per KB chunk


# ─── Error pattern matching (bypass scoring) ──────────────────────────


def _match_error_patterns(question: str) -> list[dict]:
    """Match question against errors.yaml regex patterns. Direct hits bypass ranking."""
    path = ASSISTANT_DIR / "errors.yaml"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    hits = []
    for err in data:
        pat = err.get("pattern", "")
        if not pat:
            continue
        try:
            if re.search(pat, question, flags=re.IGNORECASE):
                hits.append(err)
        except re.error:
            continue
    return hits


# ─── Always-include: settings + integrations status ───────────────────


async def _settings_snapshot_text(db: Prisma) -> str:
    """Compact human-readable snapshot of current config state."""
    rows = await db.appsetting.find_many()
    settings = {}
    for r in rows:
        try:
            settings[r.key] = json.loads(r.value)
        except (json.JSONDecodeError, TypeError):
            settings[r.key] = r.value
    keys_configured = {
        "supadata": bool(os.getenv("SUPADATA_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "pexels": bool(os.getenv("PEXELS_API_KEY")),
    }
    lines = ["API ключи:"]
    for k, v in keys_configured.items():
        lines.append(f"  - {k}: {'✓ configured' if v else '✗ missing'}")
    lines.append("Ключевые настройки:")
    picks = [
        "transcript_source", "default_brief_model", "default_brief_lang",
        "dedup_embedding_provider", "dedup_similarity_threshold",
        "enrich_image_source", "enrich_image_model",
        "assistant_provider", "assistant_model",
    ]
    for k in picks:
        if k in settings:
            lines.append(f"  - {k}: {settings[k]}")
    return "\n".join(lines)


# ─── Live DB snapshot ─────────────────────────────────────────────────


async def _recent_news(db: Prisma, hours: int = 48) -> list[dict]:
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = await db.newsitem.find_many(
        where={"createdAt": {"gte": since}},
        order={"createdAt": "desc"},
        take=50,
    )
    return [
        {
            "id": r.id,
            "headline": r.headline,
            "category": r.category,
            "status": r.status,
            "has_error": False,
            "created_ts": r.createdAt.timestamp(),
            "text": f"{r.headline} {r.quote[:200]}",
        }
        for r in rows
    ]


async def _active_streams(db: Prisma) -> list[dict]:
    rows = await db.livestream.find_many(
        where={"status": {"in": ["active", "paused"]}},
        order={"createdAt": "desc"},
        take=20,
    )
    return [
        {
            "id": r.id,
            "channel": r.channelName,
            "status": r.status,
            "url": r.url,
            "has_error": False,
            "created_ts": r.createdAt.timestamp(),
            "text": f"{r.channelName} {r.url} status={r.status}",
        }
        for r in rows
    ]


async def _recent_errors(db: Prisma, minutes: int = 60) -> list[dict]:
    since = datetime.utcnow() - timedelta(minutes=minutes)
    runs = await db.run.find_many(
        where={"status": "error", "startedAt": {"gte": since}},
        order={"startedAt": "desc"},
        take=20,
    )
    chunks = await db.chunk.find_many(
        where={"status": "failed", "createdAt": {"gte": since}},
        order={"createdAt": "desc"},
        take=20,
    )
    out = []
    for r in runs:
        out.append({
            "id": f"run:{r.id}",
            "kind": "run_error",
            "error": r.error or "",
            "has_error": True,
            "status": "failed",
            "created_ts": r.startedAt.timestamp(),
            "text": f"run error: {r.url} — {r.error or ''}",
        })
    for c in chunks:
        out.append({
            "id": f"chunk:{c.id}",
            "kind": "chunk_error",
            "error": c.error or "",
            "has_error": True,
            "status": "failed",
            "created_ts": c.createdAt.timestamp(),
            "text": f"chunk error stream={c.streamId} idx={c.index} — {c.error or ''}",
        })
    return out


# ─── Main entry point ─────────────────────────────────────────────────


async def build_context(
    db: Prisma,
    question: str,
    *,
    persona=None,
    ui_context: dict | None = None,
) -> str:
    """Build the system-prompt context block for a given question.

    `persona` optional persona object — when supplied, sections are gated by
    `persona.kb_source_keys`.  When None (legacy callers), all sections are
    included (backward-compat).

    `ui_context` optional dict from the frontend — {tab, selected_id, error_visible,
    item_id, stream_id}.
    """
    q_tokens = tokenize(question)
    sections: list[str] = []

    # Compute gating set — None means "no filter" (full context, legacy behaviour)
    kb_keys: set[str] | None = set(persona.kb_source_keys) if persona is not None else None

    # ── Section 1: UI context — always include ────────────────────────
    if ui_context:
        ui_lines = []
        if ui_context.get("tab"):
            ui_lines.append(f"Открыта вкладка: {ui_context['tab']}")
        if ui_context.get("selected"):
            ui_lines.append(f"Выделен объект: {json.dumps(ui_context['selected'], ensure_ascii=False)}")
        if ui_context.get("visible_error"):
            ui_lines.append(f"Пользователь видит ошибку на экране: {ui_context['visible_error']}")
        if ui_lines:
            sections.append("## Контекст UI\n" + "\n".join(ui_lines))

    # ── Section 2: Settings snapshot — gated by "settings_db" ────────
    if kb_keys is None or "settings_db" in kb_keys:
        sections.append("## Состояние системы\n" + await _settings_snapshot_text(db))

    # ── Section 3: Error-pattern hits — gated by "platform_docs" ─────
    if kb_keys is None or "platform_docs" in kb_keys:
        err_hits = _match_error_patterns(question)
        if err_hits:
            lines = ["## Прямое совпадение с каталогом ошибок"]
            for err in err_hits[:3]:
                lines.append(f"### {err.get('title_ru') or err['id']} [{err.get('category', '')}]")
                lines.extend(f"- {s}" for s in err.get("fix_steps", []))
                if err.get("auto_fix"):
                    lines.append(f"Auto-fix: `{err['auto_fix']}`")
                if err.get("docs_url"):
                    lines.append(f"Docs: {err['docs_url']}")
            sections.append("\n".join(lines))

    # ── Section 4: KB chunks — gated by project_docs / saved_kb / platform_docs
    _kb_allowed = kb_keys is None or bool(
        kb_keys & {"project_docs", "saved_kb", "platform_docs"}
    )
    if _kb_allowed:
        kb_entries = await load_kb(db)
        if kb_entries and q_tokens:
            scored = [
                (
                    score_kb_chunk(
                        q_tokens, e["tokens"], kind=e["kind"],
                        title_tokens=tokenize(e["title"]),
                    ),
                    e,
                )
                for e in kb_entries
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            top_kb = [e for score, e in scored[:MAX_KB_CHUNKS] if score > 0.05]
            if top_kb:
                lines = ["## Релевантные разделы knowledge base"]
                for e in top_kb:
                    body = e["body"][:MAX_BODY_CHARS]
                    if len(e["body"]) > MAX_BODY_CHARS:
                        body += "…"
                    lines.append(f"### [{e['kind']}] {e['title']}\n{body}")
                sections.append("\n".join(lines))

    # ── Section 5: content_db — specific item + stream ledger ─────────
    if kb_keys is not None and "content_db" in kb_keys:
        if ui_context and ui_context.get("item_id"):
            item = await db.newsitem.find_unique(where={"id": ui_context["item_id"]})
            if item:
                sections.append(
                    f"## Текущая карточка\n"
                    f"- ID: #{item.id}\n"
                    f"- Заголовок: {item.headline}\n"
                    f"- Цитата: {item.quote}\n"
                    f"- Атрибуция: {item.attribution}\n"
                    f"- Теги: {item.tags}\n"
                )

        if ui_context and ui_context.get("stream_id"):
            recent = await db.newsitem.find_many(
                where={"streamId": ui_context["stream_id"]},
                order={"createdAt": "desc"},
                take=10,
            )
            if recent:
                lines = ["## Последние items в стриме"]
                for n in recent:
                    lines.append(f"- #{n.id} {n.headline}")
                sections.append("\n".join(lines))

    # ── Section 5 (legacy / platform path): Live DB scored items ──────
    # For each sub-kind respect kb_keys gating; when persona=None include all.
    _news_allowed   = kb_keys is None or "content_db" in kb_keys
    _streams_allowed = kb_keys is None or "settings_db" in kb_keys
    _errors_allowed  = kb_keys is None or "settings_db" in kb_keys

    news    = await _recent_news(db)    if _news_allowed    else []
    streams = await _active_streams(db) if _streams_allowed else []
    errors  = await _recent_errors(db)  if _errors_allowed  else []

    all_items = news + streams + errors
    if all_items and q_tokens:
        now_ts = datetime.utcnow().timestamp()
        scored = [
            (
                score_db_item(
                    q_tokens, tokenize(it["text"]),
                    created_ts=it["created_ts"],
                    has_error=it.get("has_error", False),
                    status=it.get("status"),
                    now_ts=now_ts,
                ),
                it,
            )
            for it in all_items
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        # Split by kind with per-kind caps
        top_news, top_streams, top_errors = [], [], []
        for _, it in scored:
            if "headline" in it and len(top_news) < MAX_NEWS:
                top_news.append(it)
            elif it.get("kind") in ("run_error", "chunk_error") and len(top_errors) < MAX_ERRORS:
                top_errors.append(it)
            elif "channel" in it and len(top_streams) < MAX_STREAMS:
                top_streams.append(it)

        if top_errors:
            lines = ["## Свежие ошибки"]
            for e in top_errors:
                when = datetime.fromtimestamp(e["created_ts"]).strftime("%H:%M")
                lines.append(f"- [{when}] {e['kind']}: {e['error'][:200]}")
            sections.append("\n".join(lines))

        if top_streams:
            lines = ["## Активные стримы (релевантные запросу)"]
            for s in top_streams:
                lines.append(f"- `{s['id']}` — {s['channel']} ({s['status']}) {s['url']}")
            sections.append("\n".join(lines))

        if top_news:
            lines = ["## Недавние новости (релевантные запросу)"]
            for n in top_news:
                when = datetime.fromtimestamp(n["created_ts"]).strftime("%m-%d %H:%M")
                lines.append(f"- #{n['id']} [{when}] [{n['status']}] {n['headline']}")
            sections.append("\n".join(lines))

    return "\n\n".join(sections)
