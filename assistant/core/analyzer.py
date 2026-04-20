"""Lightweight text analysis — no AI calls, no sklearn.

Used by context_builder to rank KB entries and DB items against a user question.

Two kinds of scoring:
  1. score_kb_chunk   — static knowledge (setup guides, error patterns, code docs)
  2. score_db_item    — live items (news, streams, errors) with recency + relevance

Both are cheap heuristics. We don't need perfect ranking — we need fast filtering
from ~100 candidates down to ~5-10 that fit in the model context.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Iterable


# ── Russian + English stop words. Cherry-picked for operational queries ──
_STOP = frozenset(
    "и в не на с что это как по из за для то но а о к у же бы ли так"
    " он она они мы вы я его её их все всё был была было были"
    " этот эта эти тот та те который которая которое которые"
    " свой своё свою наш ваш такой такая можно нужно надо может будет есть"
    " при уже ещё также тоже очень более менее через после перед между до без про"
    " чтобы если когда где так вот тут там только потому поэтому кроме вместе вместо"
    " мне меня тебя себя"
    " the a an is are was were be been being to of in it for on at by with from"
    " this that these those which who what how why when where why can could"
    " should would will may might do does did has have had get got"
    " and or not but if then than so up down out off over under".split()
)


_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, drop stop words and 1-2 char tokens."""
    if not text:
        return []
    words = _TOKEN_RE.findall(text.lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def keyword_overlap(question_tokens: list[str], doc_tokens: list[str]) -> float:
    """How much of the question's vocabulary appears in the doc? (0-1)"""
    if not question_tokens:
        return 0.0
    qset = set(question_tokens)
    dset = set(doc_tokens)
    return len(qset & dset) / len(qset)


def tf_idf_keywords(docs: list[list[str]], top_n: int = 30) -> list[tuple[str, float]]:
    """Top TF-IDF keywords across a corpus. Used for KB summary generation."""
    n = len(docs)
    if n == 0:
        return []
    df = Counter()
    tf = Counter()
    for d in docs:
        df.update(set(d))
        tf.update(d)
    total_tf = sum(tf.values()) or 1
    scores: dict[str, float] = {}
    for word, freq in tf.items():
        if df[word] < 2:
            continue
        idf = math.log(n / df[word])
        scores[word] = (freq / total_tf) * idf
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_n]


# ─── Cherry-pick scoring ──────────────────────────────────────────────


def score_kb_chunk(
    question_tokens: list[str],
    chunk_tokens: list[str],
    *,
    kind: str,  # "error_pattern" | "setup_guide" | "code_module" | "schema"
    title_tokens: list[str] | None = None,
) -> float:
    """Score a static KB chunk for relevance to the user's question.

    Returns a float — higher = more relevant. No normalization; used only
    for sorting within the same question context.
    """
    overlap = keyword_overlap(question_tokens, chunk_tokens)
    title_overlap = keyword_overlap(question_tokens, title_tokens or [])

    # Base = body overlap + title overlap (title hits weigh more — they're curated labels)
    score = overlap * 1.0 + title_overlap * 2.5

    # Kind priors — tweak empirically. Operational queries usually hit error
    # patterns first, then setup guides, then schema/code docs.
    kind_boost = {
        "error_pattern": 1.3,
        "setup_guide": 1.1,
        "schema": 0.9,
        "code_module": 0.8,
    }.get(kind, 1.0)

    return score * kind_boost


def score_db_item(
    question_tokens: list[str],
    item_tokens: list[str],
    *,
    created_ts: float,
    has_error: bool = False,
    status: str | None = None,
    now_ts: float | None = None,
) -> float:
    """Score a live DB item (news, stream, chunk, error) against the question.

    Parameters
    ----------
    question_tokens : tokenized user question
    item_tokens     : tokenized item text (headline, transcript, error msg, ...)
    created_ts      : unix timestamp of the item
    has_error       : True if item has a non-null error field
    status          : item status string ('draft', 'approved', 'failed', ...)
    now_ts          : current unix timestamp (for tests; defaults to time.time())

    # ───────────────────────── USER TODO ─────────────────────────
    # The 4 weights below control what the assistant "sees" first.
    # Default values are reasonable but you know your usage patterns —
    # tune these after a few real conversations:
    #
    #   W_RELEVANCE  — how much keyword overlap with the question matters
    #   W_RECENCY    — how steeply to decay older items (half-life in hours)
    #   W_ERROR      — boost for items with errors when question mentions problems
    #   W_ACTIVE     — boost for items in actionable states (draft, pending, failed)
    #
    # Example tunings:
    #   Want assistant to focus on "what happened last 30min" → lower W_RECENCY.
    #   Missing old but relevant items → raise W_RELEVANCE, lower W_RECENCY.
    #   Ignoring failed items → raise W_ERROR.
    # ─────────────────────────────────────────────────────────────
    """
    W_RELEVANCE = 3.0     # keyword overlap weight
    HALF_LIFE_HOURS = 6.0 # items this old score half as high on recency alone
    W_ERROR = 2.0         # boost when item has error AND question asks about problems
    W_ACTIVE = 0.8        # boost for actionable statuses

    if now_ts is None:
        now_ts = time.time()

    # 1. Relevance (0-1) × weight
    relevance = keyword_overlap(question_tokens, item_tokens) * W_RELEVANCE

    # 2. Recency — exponential decay. Recent items > old ones.
    age_hours = max(0.0, (now_ts - created_ts) / 3600.0)
    recency = 0.5 ** (age_hours / HALF_LIFE_HOURS)

    # 3. Error boost — only when question has problem-related keywords
    problem_keywords = {"ошибк", "error", "fail", "упал", "сломал", "не работает",
                        "проблем", "почему", "why", "broken", "не", "not", "crash"}
    question_lower = " ".join(question_tokens).lower()
    is_problem_q = any(kw in question_lower for kw in problem_keywords)
    error_boost = W_ERROR if (has_error and is_problem_q) else 0.0

    # 4. Active status boost — user usually asks about things in flight
    active_statuses = {"draft", "pending", "failed", "active", "transcribing", "extracting"}
    active_boost = W_ACTIVE if status in active_statuses else 0.0

    return relevance + recency + error_boost + active_boost


# ─── Dedup helpers ────────────────────────────────────────────────────


def dedup_by_jaccard(
    items: list[tuple[str, list[str]]],
    threshold: float = 0.7,
) -> list[tuple[str, list[str]]]:
    """Drop near-duplicate items. Compares each against the last 30 keepers
    (O(n·30) — same pattern as the Telegram reference)."""
    keep: list[tuple[str, list[str]]] = []
    for item_id, toks in items:
        if any(jaccard(toks, k_toks) > threshold for _, k_toks in keep[-30:]):
            continue
        keep.append((item_id, toks))
    return keep
