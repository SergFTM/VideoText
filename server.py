"""FastAPI app — webhook API + static frontend.

Run:
    .venv/Scripts/python.exe -m uvicorn server:app --reload --port 8000

Then open:
    http://localhost:8000/        → UI
    http://localhost:8000/docs    → Swagger for the raw API

Endpoints:
    GET  /                    → frontend (static/index.html)
    GET  /health              → liveness + backend availability
    GET  /config              → API keys status (masked, never full values)
    POST /config/test         → smoke-test a connector (?provider=supadata|anthropic)
    GET  /videos              → list videos with brief counts + total cost
    GET  /videos/{id}         → single video detail (optional segments=true)
    POST /briefs              → run the full pipeline (transcript + brief)

Auth:
    Set WEBHOOK_TOKEN in .env to require header `X-Webhook-Token` on POST /briefs.
    Unset = open (fine for localhost only).
"""
import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

load_dotenv(override=True)

import orchestrator                     # noqa: E402
from cleanup import get_storage_stats, run_cleanup  # noqa: E402
from export import news_items_to_json, news_items_to_pdf  # noqa: E402
from main import process_url            # noqa: E402
from store import (                     # noqa: E402
    create_news_image, create_stream, find_similar_image, get_all_settings,
    get_news_item, get_stream, get_video, increment_image_reuse, list_news_images,
    list_news_items, list_stream_briefs, list_streams, set_settings,
    update_news_item_enrichment, update_news_item_status, update_stream_fields,
)

from prisma import Prisma               # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    await orchestrator.startup()
    try:
        yield
    finally:
        await orchestrator.shutdown()


app = FastAPI(title="VideoText", version="0.4.0", lifespan=lifespan)

_ROOT = Path(__file__).parent
_STATIC = _ROOT / "static"
_OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
_WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

_IMAGES_DIR = _ROOT / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=str(_IMAGES_DIR)), name="images")


# ─── Helpers ──────────────────────────────────────────────────────

def _check_auth(x_webhook_token: str | None) -> None:
    if _WEBHOOK_TOKEN and x_webhook_token != _WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid webhook token")


def _mask(key: str | None) -> str:
    if not key:
        return ""
    if len(key) < 12:
        return "***"
    return key[:6] + "…" + key[-4:]


# ─── Schemas ──────────────────────────────────────────────────────

class BriefRequest(BaseModel):
    url: HttpUrl
    brief_lang: Literal["ru", "en"] = "ru"
    format: Literal["markdown", "json"] = "markdown"
    model: str | None = None
    backend: Literal["auto", "supadata", "ytdlp"] = "auto"
    no_brief: bool = False
    force_refresh: bool = False


# ─── Frontend entry ───────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


# ─── Status + config ──────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "backends": {
            "supadata": bool(os.getenv("SUPADATA_API_KEY")),
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        },
        "auth_required": bool(_WEBHOOK_TOKEN),
    }


@app.get("/config")
def config() -> dict:
    """Non-sensitive config summary for the frontend. Never returns full keys."""
    settings = get_all_settings()
    return {
        "supadata": bool(os.getenv("SUPADATA_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "supadata_hint": _mask(os.getenv("SUPADATA_API_KEY")),
        "anthropic_hint": _mask(os.getenv("ANTHROPIC_API_KEY")),
        "default_model": os.getenv("CLAUDE_MODEL") or "claude-sonnet-4-6",
        "auth_required": bool(_WEBHOOK_TOKEN),
        "settings": settings,
    }


@app.get("/settings")
def read_settings() -> dict:
    return get_all_settings()


@app.post("/settings")
def write_settings(updates: dict) -> dict:
    """Upsert any subset of settings. Returns the full resulting state."""
    return set_settings(updates)


def _fastembed_installed() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _ollama_available() -> tuple[bool, list[str]]:
    """Returns (reachable, list_of_installed_models)."""
    import urllib.request
    import urllib.error
    try:
        url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=2) as r:
            data = json.loads(r.read())
            return True, [m.get("name", "") for m in (data.get("models") or [])]
    except Exception:
        return False, []


@app.get("/config/integrations")
def integrations_status() -> dict:
    """Unified status of all 5 integrations — cloud keys + local runners."""
    ollama_reachable, ollama_models = _ollama_available()
    return {
        "supadata": {
            "name": "Supadata", "kind": "cloud",
            "description": "Транскрипт YouTube (готовые видео)",
            "env_key": "SUPADATA_API_KEY",
            "key_masked": _mask(os.getenv("SUPADATA_API_KEY")),
            "configured": bool(os.getenv("SUPADATA_API_KEY")),
            "website": "https://supadata.ai",
            "docs": "https://docs.supadata.ai",
        },
        "anthropic": {
            "name": "Anthropic (Claude)", "kind": "cloud",
            "description": "Генерация брифов + извлечение news items",
            "env_key": "ANTHROPIC_API_KEY",
            "key_masked": _mask(os.getenv("ANTHROPIC_API_KEY")),
            "configured": bool(os.getenv("ANTHROPIC_API_KEY")),
            "website": "https://console.anthropic.com",
            "model": os.getenv("CLAUDE_MODEL") or "claude-sonnet-4-6",
        },
        "openai": {
            "name": "OpenAI (ChatGPT)", "kind": "cloud",
            "description": "Embeddings для dedup и альтернативный LLM",
            "env_key": "OPENAI_API_KEY",
            "key_masked": _mask(os.getenv("OPENAI_API_KEY")),
            "configured": bool(os.getenv("OPENAI_API_KEY")),
            "website": "https://platform.openai.com/api-keys",
            "model": "text-embedding-3-small",
        },
        "fastembed": {
            "name": "fastembed", "kind": "local",
            "description": "Локальные ONNX-embeddings, 0 токенов, 0 сеть",
            "configured": _fastembed_installed(),
            "website": "https://github.com/qdrant/fastembed",
            "install_cmd": "pip install fastembed",
            "install_hint": "Уже в requirements.txt. Первый запуск скачает модель ~300 MB из HuggingFace.",
        },
        "ollama": {
            "name": "Ollama", "kind": "local",
            "description": "Локальный LLM-runner (embeddings + полноценные модели)",
            "configured": ollama_reachable,
            "website": "https://ollama.ai",
            "install_cmd": "winget install Ollama.Ollama",
            "install_hint": "После установки: запусти ollama, потом `ollama pull nomic-embed-text`",
            "models": ollama_models,
            "endpoint": os.getenv("OLLAMA_URL", "http://localhost:11434"),
        },
        "pexels": {
            "name": "Pexels", "kind": "cloud",
            "description": "Стоковые фото для иллюстраций новостей (200 req/hour бесплатно)",
            "env_key": "PEXELS_API_KEY",
            "key_masked": _mask(os.getenv("PEXELS_API_KEY")),
            "configured": bool(os.getenv("PEXELS_API_KEY")),
            "website": "https://www.pexels.com/api/",
        },
    }


class SaveKeyRequest(BaseModel):
    integration: Literal["supadata", "anthropic", "openai", "pexels"]
    key: str


@app.post("/config/keys")
def save_key(req: SaveKeyRequest) -> dict:
    """Persist API key to .env and update os.environ for the running process."""
    from dotenv import set_key as dotenv_set_key
    env_names = {
        "supadata":  "SUPADATA_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "pexels":    "PEXELS_API_KEY",
    }
    env_key = env_names[req.integration]
    key_val = (req.key or "").strip()
    if not key_val:
        raise HTTPException(400, "Empty key")
    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")
    try:
        dotenv_set_key(str(env_path), env_key, key_val, quote_mode="never")
        os.environ[env_key] = key_val
    except Exception as e:
        raise HTTPException(500, f"Failed to save key: {e}")
    return {"ok": True, "integration": req.integration, "configured": True, "masked": _mask(key_val)}


@app.get("/system/gpu")
def gpu_status() -> dict:
    """Best-effort GPU snapshot via `nvidia-smi`.

    Returns {available, util_pct, mem_used_mb, mem_total_mb, name} on success,
    or {available: false, reason: ...} when nvidia-smi is missing / fails.
    Cheap to call (~50ms); the UI polls it every 10s.
    """
    import shutil, subprocess
    nvsmi = shutil.which("nvidia-smi")
    if not nvsmi:
        return {"available": False, "reason": "nvidia-smi not in PATH"}
    try:
        out = subprocess.run(
            [nvsmi, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return {"available": False, "reason": (out.stderr or "").strip()[:200]}
        first = (out.stdout or "").splitlines()[0].split(",")
        if len(first) < 4:
            return {"available": False, "reason": "unexpected nvidia-smi output"}
        return {
            "available": True,
            "name":         first[0].strip(),
            "util_pct":     int(first[1].strip()),
            "mem_used_mb":  int(first[2].strip()),
            "mem_total_mb": int(first[3].strip()),
        }
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


@app.post("/config/cookies")
async def upload_cookies(file: UploadFile = File(...)) -> dict:
    """Save uploaded cookies.txt for yt-dlp.

    Stored at ./cookies.txt in the project root. yt-dlp picks it up via
    YTDLP_COOKIES_PATH env var (also set in os.environ for the live process).
    Tiny file (<100KB typically), no streaming needed.
    """
    contents = await file.read()
    if len(contents) > 1_000_000:  # 1MB sanity ceiling
        raise HTTPException(400, "cookies file too large (>1MB)")
    target = Path("cookies.txt")
    target.write_bytes(contents)
    os.environ["YTDLP_COOKIES_PATH"] = str(target.resolve())
    return {"ok": True, "path": str(target.resolve()), "size_bytes": len(contents)}


@app.post("/config/test")
def config_test(
    provider: Literal["supadata", "anthropic", "openai", "fastembed", "ollama", "pexels"] = Query(...)
) -> dict:
    """Smoke-test a connector: tiny request/op to verify the integration works."""
    try:
        if provider == "supadata":
            key = os.getenv("SUPADATA_API_KEY")
            if not key:
                return {"ok": False, "msg": "SUPADATA_API_KEY не задан"}
            from supadata import Supadata
            client = Supadata(api_key=key)
            resp = client.youtube.transcript(video_id="jNQXAC9IVRw", text=True)
            sample = (getattr(resp, "content", "") or "")[:60]
            return {"ok": True, "msg": f'Supadata OK: "{sample}…"'}

        if provider == "anthropic":
            key = os.getenv("ANTHROPIC_API_KEY")
            if not key:
                return {"ok": False, "msg": "ANTHROPIC_API_KEY не задан"}
            import anthropic
            c = anthropic.Anthropic()
            msg = c.messages.create(
                model="claude-haiku-4-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "say ok"}],
            )
            text = next((b.text for b in msg.content if b.type == "text"), "")
            return {"ok": True, "msg": f'Claude Haiku OK: "{text.strip()}"'}

        if provider == "openai":
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                return {"ok": False, "msg": "OPENAI_API_KEY не задан"}
            from dedup import _openai_compat_embed
            vec = _openai_compat_embed("smoke test", "")
            return {"ok": True, "msg": f"OpenAI OK: embedding {len(vec)}-dim (text-embedding-3-small)"}

        if provider == "fastembed":
            if not _fastembed_installed():
                return {"ok": False, "msg": "fastembed не установлен. pip install fastembed"}
            from dedup import _fastembed_embed
            vec = _fastembed_embed("smoke test", "")
            return {"ok": True, "msg": f"fastembed OK: embedding {len(vec)}-dim (локально)"}

        if provider == "ollama":
            reachable, models = _ollama_available()
            if not reachable:
                return {"ok": False, "msg": "ollama не отвечает на localhost:11434. Запущен ли сервис?"}
            if not models:
                return {"ok": True, "msg": "Ollama работает, но нет моделей. ollama pull nomic-embed-text"}
            return {"ok": True, "msg": f"Ollama OK. Модели: {', '.join(models[:5])}"}

        if provider == "pexels":
            key = os.getenv("PEXELS_API_KEY")
            if not key:
                return {"ok": False, "msg": "PEXELS_API_KEY не задан"}
            from enrich import fetch_pexels_photo
            result = fetch_pexels_photo("oil barrel")
            if not result:
                return {"ok": False, "msg": "Pexels не вернул фото (проверь ключ и квоту)"}
            img_bytes, source_url = result
            return {"ok": True, "msg": f"Pexels OK: получено {len(img_bytes) // 1024} KB с {source_url}"}

        return {"ok": False, "msg": f"unknown provider: {provider}"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ─── Videos ───────────────────────────────────────────────────────

async def _list_videos() -> list[dict]:
    db = Prisma()
    await db.connect()
    try:
        videos = await db.video.find_many(
            include={"briefs": True},
            order={"createdAt": "desc"},
            take=50,
        )
        return [
            {
                "id": v.id,
                "title": v.title,
                "duration": v.duration,
                "language": v.language,
                "source": v.source,
                "created_at": v.createdAt.isoformat(),
                "briefs_count": len(v.briefs or []),
                "total_cost": sum(b.costUsd or 0 for b in (v.briefs or [])) or None,
            }
            for v in videos
        ]
    finally:
        await db.disconnect()


@app.get("/videos")
def list_videos() -> list[dict]:
    return asyncio.run(_list_videos())


@app.get("/videos/{video_id}")
def read_video(video_id: str, segments: bool = False) -> dict:
    v = get_video(video_id, with_segments=segments)
    if not v:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    latest_brief = v.briefs[-1] if v.briefs else None
    return {
        "id": v.id,
        "title": v.title,
        "duration": v.duration,
        "language": v.language,
        "source": v.source,
        "created_at": v.createdAt.isoformat(),
        "segments": [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in (v.segments or [])
        ] if segments else None,
        "brief": {
            "id": latest_brief.id,
            "model": latest_brief.model,
            "format": latest_brief.format,
            "language": latest_brief.language,
            "content_md": latest_brief.contentMd,
            "content_json": latest_brief.contentJson,
            "input_tokens": latest_brief.inputTokens,
            "output_tokens": latest_brief.outputTokens,
            "cache_read_tokens": latest_brief.cacheReadTokens,
            "cache_write_tokens": latest_brief.cacheWriteTokens,
            "cost_usd": latest_brief.costUsd,
            "created_at": latest_brief.createdAt.isoformat(),
        } if latest_brief else None,
    }


# ─── Pipeline ─────────────────────────────────────────────────────

@app.post("/briefs")
def create_brief(
    req: BriefRequest,
    x_webhook_token: str | None = Header(default=None),
) -> dict:
    _check_auth(x_webhook_token)
    try:
        result = process_url(
            str(req.url), languages=("ru", "en"),
            output_dir=_OUTPUT_DIR,
            no_brief=req.no_brief, brief_lang=req.brief_lang,
            fmt=req.format, model=req.model, backend=req.backend,
            force_refresh=req.force_refresh,
            triggered_by="webhook",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return result


# ─── Live streams ────────────────────────────────────────────────

class StreamCreateRequest(BaseModel):
    url: HttpUrl
    channel_name: str
    speaker_default: str | None = None
    interval_min: int = 5
    whisper_model: str = "medium"
    make_summary_brief: bool = False
    brief_template: Literal["news", "full"] = "news"
    auto_brief_on_stop: bool = False


class StreamUpdateRequest(BaseModel):
    """All optional — only fields actually provided will be updated."""
    url: HttpUrl | None = None
    channel_name: str | None = None
    speaker_default: str | None = None
    interval_min: int | None = None
    whisper_model: str | None = None
    make_summary_brief: bool | None = None
    brief_template: Literal["news", "full"] | None = None
    auto_brief_on_stop: bool | None = None


def _stream_to_dict(s, active_ids: set[str] | None = None) -> dict:
    active_ids = active_ids or set()
    chunks_total = len(getattr(s, "chunks", None) or [])
    return {
        "id": s.id,
        "url": s.url,
        "channel_name": s.channelName,
        "speaker_default": s.speakerDefault,
        "interval_min": s.intervalMin,
        "status": s.status,
        "running": s.id in active_ids,
        "whisper_model": s.whisperModel,
        "make_summary_brief": s.makeSummaryBrief,
        "brief_template": s.briefTemplate,
        "auto_brief_on_stop": s.autoBriefOnStop,
        "chunks_total": chunks_total,
        "news_items_count": len(getattr(s, "newsItems", None) or []),
        "created_at": s.createdAt.isoformat(),
        "updated_at": s.updatedAt.isoformat(),
    }


@app.post("/streams")
async def create_live_stream(req: StreamCreateRequest) -> dict:
    """Create a new live-stream monitor and kick off capture."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY not set — news extraction won't work")
    s = await asyncio.to_thread(
        create_stream,
        url=str(req.url),
        channelName=req.channel_name,
        speakerDefault=req.speaker_default,
        intervalMin=req.interval_min,
        whisperModel=req.whisper_model,
        makeSummaryBrief=req.make_summary_brief,
        briefTemplate=req.brief_template,
        autoBriefOnStop=req.auto_brief_on_stop,
        status="active",
    )
    await orchestrator.start_stream(s.id, s.url, s.intervalMin)
    return _stream_to_dict(s, set(orchestrator.active_stream_ids()))


@app.get("/streams")
async def list_live_streams(status: str | None = None) -> list[dict]:
    streams = await asyncio.to_thread(list_streams, status)
    active = set(orchestrator.active_stream_ids())
    return [_stream_to_dict(s, active) for s in streams]


@app.get("/streams/{stream_id}")
async def get_live_stream(stream_id: str, with_chunks: bool = False) -> dict:
    s = await asyncio.to_thread(get_stream, stream_id, with_chunks)
    if not s:
        raise HTTPException(404, f"Stream {stream_id} not found")
    active = set(orchestrator.active_stream_ids())
    out = _stream_to_dict(s, active)
    if with_chunks:
        out["chunks"] = [
            {
                "id": c.id, "index": c.index,
                "started_at": c.startedAt.isoformat(),
                "duration_sec": c.durationSec,
                "status": c.status,
                "transcript_chars": len(c.transcriptText) if c.transcriptText else 0,
                "error": c.error,
            }
            for c in (s.chunks or [])
        ]
    return out


@app.post("/streams/{stream_id}/pause")
async def pause_live_stream(stream_id: str) -> dict:
    s = await asyncio.to_thread(get_stream, stream_id)
    if not s:
        raise HTTPException(404, f"Stream {stream_id} not found")
    await orchestrator.pause_stream(stream_id)
    return {"ok": True, "status": "paused"}


@app.post("/streams/{stream_id}/resume")
async def resume_live_stream(stream_id: str) -> dict:
    s = await asyncio.to_thread(get_stream, stream_id)
    if not s:
        raise HTTPException(404, f"Stream {stream_id} not found")
    await orchestrator.start_stream(stream_id, s.url, s.intervalMin)
    return {"ok": True, "status": "active"}


@app.post("/streams/{stream_id}/stop")
async def stop_live_stream(stream_id: str) -> dict:
    s = await asyncio.to_thread(get_stream, stream_id)
    if not s:
        raise HTTPException(404, f"Stream {stream_id} not found")
    await orchestrator.stop_stream(stream_id)
    return {"ok": True, "status": "stopped"}


@app.patch("/streams/{stream_id}")
async def update_live_stream(stream_id: str, req: StreamUpdateRequest) -> dict:
    """Update mutable fields. URL change takes effect only after pause/resume."""
    s = await asyncio.to_thread(get_stream, stream_id)
    if not s:
        raise HTTPException(404, f"Stream {stream_id} not found")

    # Map request field names → Prisma column names
    updates: dict = {}
    payload = req.model_dump(exclude_unset=True)
    field_map = {
        "url":                 "url",
        "channel_name":        "channelName",
        "speaker_default":     "speakerDefault",
        "interval_min":        "intervalMin",
        "whisper_model":       "whisperModel",
        "make_summary_brief":  "makeSummaryBrief",
        "brief_template":      "briefTemplate",
        "auto_brief_on_stop":  "autoBriefOnStop",
    }
    for src, dst in field_map.items():
        if src in payload:
            val = payload[src]
            if src == "url" and val is not None:
                val = str(val)
            updates[dst] = val

    if not updates:
        raise HTTPException(400, "No fields to update")

    updated = await asyncio.to_thread(update_stream_fields, stream_id, updates)
    active = set(orchestrator.active_stream_ids())
    return _stream_to_dict(updated, active)


@app.delete("/streams/{stream_id}")
async def delete_live_stream(stream_id: str) -> dict:
    s = await asyncio.to_thread(get_stream, stream_id)
    if not s:
        raise HTTPException(404, f"Stream {stream_id} not found")
    # Stop first, then cascade-delete via Prisma, then clean up chunk files
    await orchestrator.stop_stream(stream_id)

    async def _delete_row():
        db = Prisma()
        await db.connect()
        try:
            await db.livestream.delete(where={"id": stream_id})
        finally:
            await db.disconnect()
    await _delete_row()

    chunk_dir = Path("./chunks") / stream_id
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir, ignore_errors=True)
    return {"ok": True}


# ─── Stream briefs ─────────────────────────────────────────────

class StreamBriefRequest(BaseModel):
    template: Literal["news", "full"] = "news"


@app.post("/streams/{stream_id}/brief")
async def make_stream_brief(stream_id: str, req: StreamBriefRequest) -> dict:
    s = await asyncio.to_thread(get_stream, stream_id)
    if not s:
        raise HTTPException(404, f"Stream {stream_id} not found")
    try:
        return await orchestrator.trigger_stream_brief(stream_id, template=req.template)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/streams/{stream_id}/briefs")
async def read_stream_briefs(stream_id: str) -> list[dict]:
    briefs = await asyncio.to_thread(list_stream_briefs, stream_id)
    return [
        {
            "id": b.id,
            "template": b.template,
            "content_md": b.contentMd,
            "content_json": b.contentJson,
            "model": b.model,
            "input_tokens": b.inputTokens,
            "output_tokens": b.outputTokens,
            "cache_read_tokens": b.cacheReadTokens,
            "cache_write_tokens": b.cacheWriteTokens,
            "cost_usd": b.costUsd,
            "chunks_covered": b.chunksCovered,
            "created_at": b.createdAt.isoformat(),
        }
        for b in briefs
    ]


# ─── News items ─────────────────────────────────────────────────

@app.get("/news-items")
async def list_items(
    stream_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict]:
    items = await asyncio.to_thread(list_news_items, stream_id, status, limit)
    return [
        {
            "id": i.id,
            "stream_id": i.streamId,
            "stream_name": getattr(i.stream, "channelName", None),
            "chunk_id": i.chunkId,
            "headline": i.headline,
            "quote": i.quote,
            "category": i.category,
            "offset_sec": i.offsetSec,
            "confidence": i.confidence,
            "tags": json.loads(i.tags) if i.tags else [],
            "status": i.status,
            "attribution": i.attribution,
            "duplicate_of_id": i.duplicateOfId,
            "duplicate_sim": i.duplicateSim,
            "created_at": i.createdAt.isoformat(),
        }
        for i in items
    ]


@app.post("/news-items/{item_id}/approve")
async def approve_item(item_id: int) -> dict:
    await asyncio.to_thread(update_news_item_status, item_id, "approved")
    return {"ok": True}


@app.post("/news-items/{item_id}/reject")
async def reject_item(item_id: int) -> dict:
    await asyncio.to_thread(update_news_item_status, item_id, "rejected")
    return {"ok": True}


@app.get("/news-items/{item_id}")
async def read_news_item(item_id: int) -> dict:
    item = await asyncio.to_thread(get_news_item, item_id)
    if not item:
        raise HTTPException(404, f"NewsItem {item_id} not found")
    img = getattr(item, "image", None)
    image_url = None
    if img and img.filePath:
        # Resolve relative path to URL served by /images mount
        p = Path(img.filePath).name
        image_url = f"/images/{p}"
    return {
        "id": item.id,
        "stream_id": item.streamId,
        "stream_name": getattr(item.stream, "channelName", None),
        "chunk_id": item.chunkId,
        "headline": item.headline,
        "quote": item.quote,
        "category": item.category,
        "offset_sec": item.offsetSec,
        "confidence": item.confidence,
        "tags": json.loads(item.tags) if item.tags else [],
        "status": item.status,
        "attribution": item.attribution,
        "duplicate_of_id": item.duplicateOfId,
        "duplicate_sim": item.duplicateSim,
        "expanded_text": item.expandedText,
        "expanded_model": item.expandedModel,
        "expanded_cost_usd": item.expandedCostUsd,
        "image": {
            "id": img.id,
            "url": image_url,
            "concept": img.conceptPhrase,
            "prompt": img.prompt,
            "model": img.model,
            "reuse_count": img.reuseCount,
            "cost_usd": img.costUsd,
        } if img else None,
        "created_at": item.createdAt.isoformat(),
    }


@app.post("/news-items/{item_id}/enrich")
async def enrich_news_item(item_id: int) -> dict:
    """Generate expanded text + illustration (with concept-based dedup)."""
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(400, "OPENAI_API_KEY не задан в .env")
    item = await asyncio.to_thread(get_news_item, item_id)
    if not item:
        raise HTTPException(404, f"NewsItem {item_id} not found")

    settings = await asyncio.to_thread(get_all_settings)
    text_model   = str(settings.get("enrich_text_model", "gpt-4o-mini") or "gpt-4o-mini")
    image_model  = str(settings.get("enrich_image_model", "dall-e-3") or "dall-e-3")
    image_source = str(settings.get("enrich_image_source", "hybrid") or "hybrid")
    image_enabled = bool(settings.get("enrich_image_enabled", True))
    dedup_thr = float(settings.get("enrich_image_dedup_threshold", 0.88) or 0.88)
    embed_provider = str(settings.get("dedup_embedding_provider", "fastembed") or "fastembed")
    embed_model = str(settings.get("dedup_embedding_model", "") or "")

    from enrich import enrich_item

    def _find_existing(vec, threshold):
        return find_similar_image(vec, threshold)

    def _create_img(concept, embedding, prompt, file_path, model, cost_usd):
        return create_news_image(concept, embedding, prompt, file_path, model, cost_usd)

    try:
        result = await asyncio.to_thread(
            enrich_item, item,
            text_model, image_model, image_source, dedup_thr,
            embed_provider, embed_model,
            _find_existing, _create_img, increment_image_reuse,
            image_enabled,
        )
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")

    await asyncio.to_thread(
        update_news_item_enrichment,
        item.id, result.expanded_text, text_model,
        result.expanded_usage.get("cost_usd", 0), result.image_id,
    )

    return {
        "ok": True,
        "item_id": item.id,
        "expanded_text": result.expanded_text,
        "image_id": result.image_id,
        "image_path": result.image_path,
        "image_reused": result.image_reused,
        "image_concept": result.image_concept,
        "cost_usd": round(result.total_cost_usd, 4),
    }


@app.get("/news-images")
async def list_images() -> list[dict]:
    images = await asyncio.to_thread(list_news_images, 100)
    return [
        {
            "id": i.id, "concept": i.conceptPhrase,
            "model": i.model, "cost_usd": i.costUsd,
            "reuse_count": i.reuseCount,
            "url": f"/images/{Path(i.filePath).name}",
            "created_at": i.createdAt.isoformat(),
        }
        for i in images
    ]


# ─── Export ─────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    format: Literal["json", "pdf"] = "json"
    stream_id: str | None = None
    status: str | None = None
    ids: list[int] | None = None


def _fetch_items_for_export(req: ExportRequest) -> list[dict]:
    """Resolve filter → list[dict] matching the shape used by /news-items."""
    items = list_news_items(req.stream_id, req.status, 1000)
    if req.ids:
        id_set = set(req.ids)
        items = [i for i in items if i.id in id_set]
    return [
        {
            "id": i.id,
            "stream_id": i.streamId,
            "stream_name": getattr(i.stream, "channelName", None),
            "headline": i.headline,
            "quote": i.quote,
            "category": i.category,
            "offset_sec": i.offsetSec,
            "confidence": i.confidence,
            "tags": json.loads(i.tags) if i.tags else [],
            "status": i.status,
            "attribution": i.attribution,
            "created_at": i.createdAt.isoformat(),
        }
        for i in items
    ]


@app.post("/export")
async def export_news_items(req: ExportRequest) -> Response:
    """Export filtered (or id-selected) news items as JSON or PDF file download."""
    items = await asyncio.to_thread(_fetch_items_for_export, req)
    ts = datetime_stamp()
    if req.format == "json":
        payload = news_items_to_json(items)
        return Response(
            content=payload, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="videotext-{ts}.json"'},
        )
    # pdf
    payload = news_items_to_pdf(items, title=f"VideoText — {len(items)} items")
    return Response(
        content=payload, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="videotext-{ts}.pdf"'},
    )


def datetime_stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ─── Storage / retention ────────────────────────────────────────

@app.get("/storage/stats")
async def storage_stats() -> dict:
    """Disk usage, row counts, oldest records, current retention policy."""
    settings = await asyncio.to_thread(get_all_settings)
    return await asyncio.to_thread(get_storage_stats, settings)


class CleanupRequest(BaseModel):
    commit: bool = False  # default dry-run — show what would be deleted
    policy_override: dict | None = None  # optional one-shot override of retain_* keys


# ─── AI Assistant ───────────────────────────────────────────────

from fastapi.responses import StreamingResponse  # noqa: E402


class AssistantChatRequest(BaseModel):
    question: str
    session_id: str | None = None
    ui_context: dict | None = None  # {tab, selected, visible_error}
    auto_confirm: bool = False      # user pre-approved write actions for this turn
    provider: Literal["openai", "anthropic", "ollama"] | None = None
    model: str | None = None


@app.post("/chat/platform")
@app.post("/assistant/chat")  # legacy alias — remove in Phase 3
async def chat_platform(req: AssistantChatRequest):
    """Platform persona chat — handles settings, integrations, errors."""
    from assistant.core.chat import Assistant
    from assistant.personas.platform import PlatformPersona

    settings = await asyncio.to_thread(get_all_settings)
    provider = req.provider or settings.get("assistant_provider") or "openai"
    model = req.model or settings.get("assistant_model") or (
        "gpt-4o" if provider == "openai"
        else "claude-sonnet-4-6" if provider == "anthropic"
        else "llama3.1:8b"
    )
    cache_thr = float(settings.get("assistant_cache_similarity", 0.85))
    use_cache = bool(settings.get("assistant_cache_enabled", True))

    assistant = Assistant(
        PlatformPersona(),
        provider=provider, model=model,
        use_cache=use_cache, cache_threshold=cache_thr,
    )

    async def event_stream():
        db = Prisma()
        await db.connect()
        try:
            final_answer_parts: list[str] = []
            async for ev in assistant.ask_stream(
                db, req.question,
                session_id=req.session_id,
                ui_context=req.ui_context,
                auto_confirm=req.auto_confirm,
            ):
                if ev["type"] == "text":
                    final_answer_parts.append(ev.get("delta", ""))
                if ev["type"] == "cache_hit":
                    final_answer_parts.append(ev.get("answer", ""))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

            # Persist Q&A to cache (only if we actually generated a new answer)
            answer_text = "".join(final_answer_parts).strip()
            if answer_text and not any(e for e in [] if False):  # naive — future: skip if cache_hit
                from assistant.core.cache import save_qa
                try:
                    await save_qa(db, req.question, answer_text, persona="platform")
                except Exception:
                    pass
        finally:
            await db.disconnect()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class EditorChatRequest(BaseModel):
    question: str
    session_id: str | None = None
    item_id: int | None = None
    stream_id: str | None = None
    auto_confirm: bool = False
    provider: Literal["openai", "anthropic", "ollama"] | None = None
    model: str | None = None


@app.post("/chat/editor")
async def chat_editor(req: EditorChatRequest):
    """Editor persona chat — handles news item content (headlines, quotes, images)."""
    from assistant.core.chat import Assistant
    from assistant.personas.editor import EditorPersona

    settings = await asyncio.to_thread(get_all_settings)
    provider = req.provider or settings.get("assistant_provider") or "openai"
    model = req.model or settings.get("assistant_model") or (
        "gpt-4o" if provider == "openai"
        else "claude-sonnet-4-6" if provider == "anthropic"
        else "llama3.1:8b"
    )
    cache_thr = float(settings.get("assistant_cache_similarity", 0.85))
    use_cache = bool(settings.get("assistant_cache_enabled", True))

    assistant = Assistant(
        persona=EditorPersona(),
        provider=provider, model=model,
        use_cache=use_cache, cache_threshold=cache_thr,
    )

    async def event_stream():
        db = Prisma()
        await db.connect()
        try:
            final_answer_parts: list[str] = []
            ui_context = {"item_id": req.item_id, "stream_id": req.stream_id}
            async for ev in assistant.ask_stream(
                db, req.question,
                session_id=req.session_id,
                ui_context=ui_context,
                auto_confirm=req.auto_confirm,
            ):
                if ev["type"] == "text":
                    final_answer_parts.append(ev.get("delta", ""))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

            answer_text = "".join(final_answer_parts).strip()
            if answer_text:
                from assistant.core.cache import save_qa
                try:
                    await save_qa(db, req.question, answer_text, persona="editor")
                except Exception:
                    pass
        finally:
            await db.disconnect()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class EditorApplyRequest(BaseModel):
    item_id: int
    field: Literal["headline", "quote", "expandedText", "imageId", "tags"]
    value: Any
    tool_call_id: str  # for audit; not validated, but logged


@app.post("/chat/editor/apply")
async def chat_editor_apply(req: EditorApplyRequest):
    """Commit a previously-previewed edit to the news item."""
    from store import apply_editor_change
    try:
        updated = await asyncio.to_thread(
            apply_editor_change, req.item_id, req.field, req.value, req.tool_call_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {"ok": True, "item_id": updated.id, "updated_field": req.field}


@app.post("/assistant/refresh-kb")
async def assistant_refresh_kb():
    """Rebuild the AssistantKB from errors.yaml + kb_static.md + codebase AST."""
    from assistant.core.knowledge_base import rebuild_kb
    counts = await rebuild_kb()
    return {"ok": True, "counts": counts}


@app.get("/assistant/cache")
async def assistant_list_cache(limit: int = 50):
    """Show recent cached Q&A pairs (for debugging / transparency)."""
    from assistant.core.cache import list_cache
    db = Prisma()
    await db.connect()
    try:
        return await list_cache(db, limit)
    finally:
        await db.disconnect()


@app.delete("/assistant/cache")
async def assistant_clear_cache():
    from assistant.core.cache import clear_cache
    db = Prisma()
    await db.connect()
    try:
        n = await clear_cache(db)
        return {"ok": True, "cleared": n}
    finally:
        await db.disconnect()


@app.get("/assistant/sessions")
async def assistant_list_sessions(limit: int = 50):
    """List past chat sessions (sidebar history)."""
    db = Prisma()
    await db.connect()
    try:
        sess = await db.assistantsession.find_many(
            order={"updatedAt": "desc"}, take=limit,
            include={"messages": {"take": 1, "order_by": {"createdAt": "asc"}}},
        )
        return [
            {
                "id": s.id,
                "title": s.title or (s.messages[0].content[:60] if s.messages else "Без темы"),
                "updated_at": s.updatedAt.isoformat(),
            }
            for s in sess
        ]
    finally:
        await db.disconnect()


@app.get("/assistant/sessions/{session_id}")
async def assistant_get_session(session_id: str):
    db = Prisma()
    await db.connect()
    try:
        s = await db.assistantsession.find_unique(
            where={"id": session_id},
            include={"messages": {"order_by": {"createdAt": "asc"}}},
        )
        if not s:
            raise HTTPException(404, "session not found")
        return {
            "id": s.id,
            "title": s.title,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "model": m.model,
                    "cache_hit": m.cacheHit,
                    "created_at": m.createdAt.isoformat(),
                }
                for m in (s.messages or [])
            ],
        }
    finally:
        await db.disconnect()


@app.post("/storage/cleanup")
async def storage_cleanup(req: CleanupRequest) -> dict:
    """Run retention cleanup. Default is dry-run; pass commit=true to actually delete.

    Pass `policy_override` to test a different retention policy without changing settings.
    """
    settings = await asyncio.to_thread(get_all_settings)
    if req.policy_override:
        settings = {**settings, **req.policy_override}
    report = await asyncio.to_thread(run_cleanup, settings, req.commit)
    return {
        "retention_policy": {k: v for k, v in settings.items() if k.startswith(("retain_", "cleanup_"))},
        "report": report,
    }
