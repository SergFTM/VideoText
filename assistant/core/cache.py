"""Fuzzy Q&A cache — reuse past answers for similar questions.

Unlike the Telegram reference (Counter-cosine on tokens), we reuse the fastembed
model that's already loaded for news dedup. Semantic match >> lexical match —
"как поставить pexels?" matches "где взять api key pexels" even without shared words.

Degradation: if fastembed isn't available, falls back to tokenized Jaccard.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from prisma import Prisma

from assistant.core.analyzer import jaccard, tokenize


# Threshold: start at 0.82 for fastembed (empirical — similar to news dedup).
# Lower = more cache hits but higher risk of stale/wrong answers.
# Tunable via AppSetting "assistant_cache_similarity".
DEFAULT_SIMILARITY = 0.82


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed(text: str) -> list[float] | None:
    """Best-effort embedding via fastembed. Returns None if unavailable."""
    try:
        from dedup import _fastembed_embed
        return _fastembed_embed(text, "")
    except Exception:
        return None


async def find_cached_answer(
    db: Prisma,
    question: str,
    *,
    persona: str = "platform",
    threshold: float = DEFAULT_SIMILARITY,
) -> dict | None:
    """Return {question, answer, similarity} or None."""
    if not question.strip():
        return None

    rows = await db.assistantcache.find_many(
        where={"persona": persona},
        order={"lastUsedAt": "desc"},
        take=500,
    )
    if not rows:
        return None

    q_emb = _embed(question)
    q_tokens = tokenize(question)

    best = None
    best_sim = 0.0

    for r in rows:
        sim = 0.0
        if q_emb and r.embedding:
            try:
                stored_emb = json.loads(r.embedding)
                sim = _cosine(q_emb, stored_emb)
            except (json.JSONDecodeError, TypeError):
                sim = 0.0
        else:
            # Fallback: Jaccard on tokens
            stored_tokens = tokenize(r.question)
            sim = jaccard(q_tokens, stored_tokens)

        if sim > best_sim:
            best_sim = sim
            best = r

    if best and best_sim >= threshold:
        # Bump usage counter + timestamp
        await db.assistantcache.update(
            where={"id": best.id},
            data={"usedCount": best.usedCount + 1, "lastUsedAt": datetime.utcnow()},
        )
        return {
            "question": best.question,
            "answer": best.answer,
            "similarity": best_sim,
            "used_count": best.usedCount + 1,
        }

    return None


async def save_qa(db: Prisma, question: str, answer: str, persona: str = "platform") -> None:
    """Persist a new Q&A pair with its embedding."""
    emb = _embed(question)
    emb_json = json.dumps(emb) if emb else None
    await db.assistantcache.create(
        data={
            "question": question,
            "answer": answer,
            "embedding": emb_json,
            "persona": persona,
        }
    )
    # Trim cache: keep last 500 entries
    total = await db.assistantcache.count()
    if total > 500:
        overflow = total - 500
        oldest = await db.assistantcache.find_many(
            order={"lastUsedAt": "asc"}, take=overflow
        )
        for r in oldest:
            await db.assistantcache.delete(where={"id": r.id})


async def list_cache(db: Prisma, limit: int = 50) -> list[dict]:
    rows = await db.assistantcache.find_many(
        order={"lastUsedAt": "desc"}, take=limit,
    )
    return [
        {
            "id": r.id,
            "question": r.question,
            "answer": r.answer[:200] + ("…" if len(r.answer) > 200 else ""),
            "used_count": r.usedCount,
            "last_used": r.lastUsedAt.isoformat(),
        }
        for r in rows
    ]


async def clear_cache(db: Prisma) -> int:
    count = await db.assistantcache.count()
    await db.assistantcache.delete_many()
    return count
