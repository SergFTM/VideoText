"""Semantic deduplication for news items.

Providers (switchable via AppSetting `dedup_embedding_provider`):
  * `fastembed`  — local ONNX model via the fastembed library (default)
                   Multilingual MiniLM, 384 dim, ~5ms/item after warmup.
  * `ollama`     — HTTP to a running ollama instance, uses /api/embeddings
                   with e.g. `nomic-embed-text` or any embedding model.
  * `openai`     — OpenAI-compatible /v1/embeddings (e.g. LM Studio, llama.cpp
                   server). Works with any endpoint that follows the spec.
  * `none`       — disable embedding computation (still lets exact-hash dedup run).

The interface is uniform: `embed(text) -> list[float]`. Consumers don't care
about the source. To swap providers at runtime, change the setting and new
extractions will use the new provider. Old embeddings stay compatible only if
dimensionality matches — mixing providers across an index is NOT recommended.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request
from dataclasses import dataclass


# ─── Normalization (used for cheap exact-hash dedup as fallback) ─────

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\wёЁ\s]", re.UNICODE)


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace. Cheap canonical form."""
    t = (text or "").lower()
    t = _PUNCT.sub("", t)
    t = _WHITESPACE.sub(" ", t).strip()
    return t


# ─── Cosine similarity (small & fast for our sizes) ─────────────────

def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return (dot / (na * nb)) if na and nb else 0.0


# ─── fastembed provider (ONNX, local) ───────────────────────────────

_fastembed_cache: dict[str, object] = {}


def _fastembed_embed(text: str, model_name: str) -> list[float]:
    from fastembed import TextEmbedding
    name = model_name or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    if name not in _fastembed_cache:
        print(f"[dedup] loading fastembed model {name}", file=sys.stderr)
        _fastembed_cache[name] = TextEmbedding(name)
    model = _fastembed_cache[name]
    return list(next(iter(model.embed([text]))))


# ─── ollama provider (HTTP to localhost:11434 by default) ───────────

def _ollama_embed(text: str, model_name: str) -> list[float]:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/embeddings"
    model = model_name or "nomic-embed-text"
    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    emb = data.get("embedding") or []
    if not emb:
        raise RuntimeError(f"ollama returned empty embedding (model {model}): {data}")
    return list(emb)


# ─── OpenAI-compatible provider (LM Studio, llama.cpp server, etc.) ─

def _openai_compat_embed(text: str, model_name: str) -> list[float]:
    """Works with api.openai.com AND any compatible local endpoint (LM Studio, llama.cpp).

    Resolution order:
      * base URL  — `EMBED_OPENAI_URL` (explicit), else `https://api.openai.com` if
                    `OPENAI_API_KEY` is set, else `http://localhost:1234`.
      * api key   — `OPENAI_API_KEY` (preferred), else `EMBED_OPENAI_KEY`, else "lm-studio".
    """
    has_openai_key = bool(os.getenv("OPENAI_API_KEY"))
    default_base = "https://api.openai.com" if has_openai_key else "http://localhost:1234"
    base = os.getenv("EMBED_OPENAI_URL", default_base).rstrip("/")
    url = base + "/v1/embeddings"
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("EMBED_OPENAI_KEY") or "lm-studio"
    model = model_name or "text-embedding-3-small"
    body = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return list(data["data"][0]["embedding"])


# ─── Unified interface ──────────────────────────────────────────────

@dataclass
class DedupConfig:
    enabled: bool = True
    provider: str = "fastembed"       # fastembed | ollama | openai | none
    model: str = ""                    # provider-specific; empty = provider default
    window_hours: int = 24
    similarity_threshold: float = 0.85

    @classmethod
    def from_settings(cls, s: dict) -> "DedupConfig":
        return cls(
            enabled              = bool(s.get("dedup_enabled", True)),
            provider             = str(s.get("dedup_embedding_provider", "fastembed") or "fastembed"),
            model                = str(s.get("dedup_embedding_model", "") or ""),
            window_hours         = int(s.get("dedup_window_hours", 24) or 24),
            similarity_threshold = float(s.get("dedup_similarity_threshold", 0.85) or 0.85),
        )


def embed(text: str, provider: str, model: str = "") -> list[float] | None:
    """Compute embedding using the chosen provider. Returns None on provider='none'."""
    if provider == "none":
        return None
    try:
        if provider == "fastembed":
            return _fastembed_embed(text, model)
        if provider == "ollama":
            return _ollama_embed(text, model)
        if provider == "openai":
            return _openai_compat_embed(text, model)
        raise ValueError(f"unknown dedup provider: {provider}")
    except Exception as e:
        print(f"[dedup] provider {provider!r} failed: {e}", file=sys.stderr)
        return None


def find_duplicate(
    new_embedding: list[float],
    candidates: list[tuple[int, list[float]]],
    threshold: float,
) -> tuple[int | None, float]:
    """Return (best_candidate_id, similarity). If best sim < threshold — id is None."""
    best_id = None
    best_sim = 0.0
    for cid, emb in candidates:
        sim = cosine(new_embedding, emb)
        if sim > best_sim:
            best_sim = sim
            best_id = cid
    if best_sim < threshold:
        return None, best_sim
    return best_id, best_sim
