"""News enrichment: expand context + generate/reuse illustration.

Flow per item:
  1. Expand text via chat-completion (gpt-4o-mini by default) — 200-400 word article.
  2. Extract a short English "visual concept" phrase (same call or separate).
  3. Embed the concept; search NewsImage table for cosine sim >= threshold.
  4. If match — reuse file, increment reuseCount.
     Else — call images.generate, save PNG to ./images/, create NewsImage row.
  5. Attach NewsItem.imageId and persist expandedText.

Image dedup key insight: we embed the CONCEPT PHRASE, not the image pixels.
Works for conceptual matches ("нефть" → "oil barrel") without vision models.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from dedup import cosine, embed


IMAGES_DIR = Path("./images")


# ─── Pricing (for cost tracking) ───────────────────────────────────

_CHAT_PRICING = {  # per 1M tokens: (input, output)
    "gpt-4o":              (2.50, 10.00),
    "gpt-4o-mini":         (0.15,  0.60),
    "gpt-4-turbo":         (10.00, 30.00),
    "o1-mini":             (3.00, 12.00),
}
_IMAGE_PRICING = {  # USD per image (standard 1024x1024)
    "dall-e-3":            0.08,
    "dall-e-3-hd":         0.12,
    "gpt-image-1":         0.04,   # avg, can be $0.011-$0.222 depending on size/quality
    "dall-e-2":            0.02,
}


def _chat_cost(model: str, in_tok: int, out_tok: int) -> float:
    p = _CHAT_PRICING.get(model, (0, 0))
    return (in_tok * p[0] + out_tok * p[1]) / 1_000_000


def _image_cost(model: str) -> float:
    return _IMAGE_PRICING.get(model, 0.0)


# ─── Text expansion ────────────────────────────────────────────────

_EXPAND_SYSTEM = """Ты — редактор ленты новостей. На вход дан короткий news-item: \
заголовок, прямая цитата, атрибуция (канал и время), теги, категория.

Задача: написать расширенную версию новости — читабельную заметку на 150-300 слов, \
на ТОМ ЖЕ ЯЗЫКЕ, что и headline.

Структура ответа:
- Первый абзац (2-3 предложения): ключевой факт, контекст где произошло, кто ключевой спикер.
- Средние абзацы (2-3): разворот — почему это важно, какие последствия, связи с другими событиями.
- Последний абзац: что следить дальше, открытые вопросы.

Принципы:
- НЕ ВЫДУМЫВАЙ конкретные факты, числа, имена, цитаты, которых нет в исходных данных.
- Можно добавлять общеизвестный фоновый контекст (например, «НАТО — военный альянс из 32 стран»).
- Не повторяй дословно headline в первом предложении — перефразируй, разворачивай.
- Тон: нейтральный новостной, без кликбейта, без эмоций.
- Markdown отключён. Чистый текст с абзацами через двойной перенос."""


def expand_text(
    headline: str, quote: str, attribution: str,
    category: str | None, tags: list[str],
    model: str = "gpt-4o-mini",
) -> tuple[str, dict]:
    """Returns (expanded_text, usage_dict)."""
    client = OpenAI()
    user_msg = (
        f"Заголовок: {headline}\n"
        f"Цитата: «{quote}»\n"
        f"Атрибуция: {attribution}\n"
        f"Категория: {category or '—'}\n"
        f"Теги: {', '.join(tags) if tags else '—'}\n\n"
        "Напиши расширенную версию новости."
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _EXPAND_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=600,
        temperature=0.5,
    )
    text = (resp.choices[0].message.content or "").strip()
    usage = {
        "input_tokens":  resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        "cost_usd":      _chat_cost(model, resp.usage.prompt_tokens, resp.usage.completion_tokens),
    }
    return text, usage


# ─── Visual concept extraction ────────────────────────────────────

_CONCEPT_SYSTEM = """Определи, какой ВИЗУАЛЬНЫЙ СИМВОЛ лучше всего иллюстрирует эту новость. \
Ответь короткой фразой на АНГЛИЙСКОМ (2-5 слов), описывающей ОДИН универсальный объект/сцену.

Примеры:
- Новость про подорожание нефти → "crude oil barrel"
- Новость про падение акций Tesla → "stock market red chart"
- Новость про саммит НАТО → "NATO flags summit hall"
- Новость про ИИ-прорыв → "futuristic AI brain circuit"
- Интервью CEO компании → "business executive podium"

Критерии:
- Универсально — то же изображение подойдёт для ДРУГОЙ похожей новости.
- Конкретно — не "politics" или "business news", а живая образная сцена/объект.
- Без брендов, без лиц конкретных людей (generic «business leader», не «Elon Musk»).
- Не повторяй название ньюсмейкера.

Ответь ТОЛЬКО фразой, без кавычек, без пояснений."""


def extract_visual_concept(
    headline: str, category: str | None, tags: list[str],
    model: str = "gpt-4o-mini",
) -> tuple[str, dict]:
    client = OpenAI()
    user_msg = (
        f"Headline: {headline}\n"
        f"Category: {category or '—'}\n"
        f"Tags: {', '.join(tags) if tags else '—'}"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _CONCEPT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=40,
        temperature=0.3,
    )
    phrase = (resp.choices[0].message.content or "").strip().strip('"\'').lower()
    # strip extra whitespace and limit length
    phrase = re.sub(r"\s+", " ", phrase)[:80]
    usage = {
        "input_tokens":  resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        "cost_usd":      _chat_cost(model, resp.usage.prompt_tokens, resp.usage.completion_tokens),
    }
    return phrase, usage


# ─── Image generation ──────────────────────────────────────────────

def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())[:max_len].strip("-")
    return s or "news"


def _save_bytes(img_bytes: bytes, concept: str, salt: str = "") -> Path:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256((concept + salt + str(os.getpid())).encode()).hexdigest()[:6]
    path = IMAGES_DIR / f"{_slug(concept)}-{h}.png"
    path.write_bytes(img_bytes)
    return path


def generate_image_dalle(
    concept: str, model: str = "dall-e-3", size: str = "1024x1024",
) -> tuple[Path, str]:
    """Pure text-to-image generation via DALL-E / gpt-image-1."""
    import base64
    client = OpenAI()
    final_prompt = (
        f"Editorial news illustration: {concept}. "
        "Clean, minimalist, professional newsroom style. Muted palette, subtle lighting, "
        "no text or logos, no faces of specific real people."
    )
    resp = client.images.generate(
        model=model, prompt=final_prompt, size=size, n=1,
        response_format="b64_json" if model != "gpt-image-1" else None,
    )
    img_b64 = resp.data[0].b64_json
    if not img_b64:
        raise RuntimeError("Image API returned no b64 payload")
    return _save_bytes(base64.b64decode(img_b64), concept, salt="dalle"), final_prompt


def fetch_pexels_photo(concept: str) -> tuple[bytes, str] | None:
    """Search Pexels by concept keyword, download the first landscape photo.
    Returns (png_bytes, page_url) or None if key missing / no results.
    """
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        return None
    import urllib.request
    import urllib.parse
    query = urllib.parse.quote(concept)
    url = f"https://api.pexels.com/v1/search?query={query}&per_page=5&orientation=landscape"
    req = urllib.request.Request(url, headers={"Authorization": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"[enrich] Pexels search failed: {e}", file=sys.stderr)
        return None
    photos = data.get("photos") or []
    if not photos:
        return None
    photo = photos[0]
    # Pick a reasonable size — "large" is ~940×650, "large2x" is ~1880×1300
    img_url = photo.get("src", {}).get("large") or photo.get("src", {}).get("original")
    if not img_url:
        return None
    try:
        with urllib.request.urlopen(img_url, timeout=20) as r:
            return r.read(), photo.get("url", "")
    except Exception as e:
        print(f"[enrich] Pexels download failed: {e}", file=sys.stderr)
        return None


def generate_image_stock(concept: str) -> tuple[Path, str]:
    """Pure stock photo via Pexels. No AI. Zero cost beyond bandwidth."""
    result = fetch_pexels_photo(concept)
    if not result:
        raise RuntimeError("Pexels returned no usable photo — check PEXELS_API_KEY and concept")
    img_bytes, source_url = result
    path = _save_bytes(img_bytes, concept, salt="pexels")
    return path, f"Pexels stock: {source_url}"


def generate_image_hybrid(concept: str, model: str = "gpt-image-1") -> tuple[Path, str]:
    """Stock photo + light AI editorial processing via gpt-image-1.

    Falls back to pure DALL-E generation if Pexels is unavailable.
    """
    result = fetch_pexels_photo(concept)
    if not result:
        # Graceful fallback
        return generate_image_dalle(concept, model="dall-e-3")

    img_bytes, source_url = result
    base_path = _save_bytes(img_bytes, concept, salt="pexels-base")

    edit_prompt = (
        f"Enhance this stock photo for editorial news use: {concept}. "
        "Apply subtle color grading toward a muted professional palette, soft lighting, "
        "newsroom-appropriate mood. Preserve the subject. No added text or logos."
    )
    try:
        client = OpenAI()
        with open(base_path, "rb") as f:
            resp = client.images.edit(
                model=model, image=f, prompt=edit_prompt, size="1024x1024",
            )
        import base64
        img_b64 = resp.data[0].b64_json
        if img_b64:
            edited = _save_bytes(base64.b64decode(img_b64), concept, salt="edited")
            try: base_path.unlink()  # drop the raw stock file
            except OSError: pass
            return edited, f"Pexels+{model} edit of: {source_url}"
    except Exception as e:
        print(f"[enrich] Hybrid edit failed ({e}) — keeping raw stock", file=sys.stderr)

    return base_path, f"Pexels stock (edit failed): {source_url}"


def generate_image(
    concept: str,
    source: str = "generate",     # "generate" | "stock" | "hybrid"
    model: str = "dall-e-3",
    size: str = "1024x1024",
) -> tuple[Path, str]:
    """Dispatch to the requested image source. Returns (file_path, prompt_or_note)."""
    if source == "stock":
        return generate_image_stock(concept)
    if source == "hybrid":
        return generate_image_hybrid(concept, model=("gpt-image-1" if model != "gpt-image-1" else model))
    return generate_image_dalle(concept, model=model, size=size)


# ─── Public orchestrator ──────────────────────────────────────────

@dataclass
class EnrichResult:
    expanded_text: str
    expanded_usage: dict
    image_id: int | None
    image_path: str | None
    image_reused: bool
    image_concept: str | None
    image_cost_usd: float
    total_cost_usd: float


def enrich_item(
    item,                          # NewsItem Prisma obj
    text_model: str,
    image_model: str,
    image_source: str,              # "generate" | "stock" | "hybrid"
    dedup_threshold: float,
    image_provider_for_embed: str,  # reuse dedup provider config
    embed_model: str,
    find_existing_image_fn,         # callable(concept_embedding: list[float]) -> (image_id, sim) or None
    create_image_fn,                # callable(concept, embedding, prompt, file_path, model, cost) -> id
    increment_reuse_fn,             # callable(image_id: int)
    generate_image_enabled: bool = True,
) -> EnrichResult:
    """Full enrichment. Callers wire in DB accessors (keeps enrich.py pure)."""
    total_cost = 0.0

    # 1. Text expansion
    tags = json.loads(item.tags) if item.tags else []
    expanded, usage_ex = expand_text(
        item.headline, item.quote, item.attribution,
        item.category, tags, model=text_model,
    )
    total_cost += usage_ex["cost_usd"]

    image_id: int | None = None
    image_path: str | None = None
    image_reused = False
    concept: str | None = None
    image_cost = 0.0

    if generate_image_enabled:
        # 2. Concept phrase
        concept, usage_c = extract_visual_concept(
            item.headline, item.category, tags, model=text_model,
        )
        total_cost += usage_c["cost_usd"]

        # 3. Concept embedding
        concept_vec = embed(concept, image_provider_for_embed, embed_model)

        # 4. Look up similar existing image
        if concept_vec:
            existing = find_existing_image_fn(concept_vec, dedup_threshold)
            if existing is not None:
                image_id = existing["id"]
                image_path = existing["path"]
                image_reused = True
                increment_reuse_fn(image_id)

        if image_id is None:
            # 5. Generate new image + store (via selected source)
            file_path, final_prompt = generate_image(
                concept, source=image_source, model=image_model,
            )
            # Cost depends on source
            if image_source == "stock":
                image_cost = 0.0
                model_tag = "pexels-stock"
            elif image_source == "hybrid":
                image_cost = _image_cost("gpt-image-1")
                model_tag = "pexels+gpt-image-1"
            else:
                image_cost = _image_cost(image_model)
                model_tag = image_model
            image_id = create_image_fn(
                concept=concept, embedding=concept_vec or [],
                prompt=final_prompt, file_path=str(file_path),
                model=model_tag, cost_usd=image_cost,
            )
            image_path = str(file_path)
            total_cost += image_cost

    return EnrichResult(
        expanded_text=expanded,
        expanded_usage=usage_ex,
        image_id=image_id,
        image_path=image_path,
        image_reused=image_reused,
        image_concept=concept,
        image_cost_usd=image_cost,
        total_cost_usd=total_cost,
    )
