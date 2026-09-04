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
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import re                                # noqa: E402
from pydantic import BaseModel, HttpUrl, field_validator

load_dotenv(override=True)

import orchestrator                     # noqa: E402
from cleanup import get_storage_stats, run_cleanup  # noqa: E402
from export import news_items_to_json, news_items_to_pdf  # noqa: E402
from main import process_url            # noqa: E402
from store import (                     # noqa: E402
    create_news_image, create_stream, create_transcript_edit, fail_expansion,
    find_similar_image, finish_expansion, get_all_settings, get_expansion,
    get_expansion_version, get_news_item, get_stream, get_transcript_edit, get_video, increment_image_reuse,
    list_expansions, list_expansion_versions, list_news_images, list_news_items, list_stream_briefs,
    get_stage_gate, list_stage_gates, list_streams, list_transcript_edits,
    rollback_transcript_edit, search_news_items, set_settings, start_expansion,
    sweep_running_expansions, update_news_item_enrichment, update_news_item_status,
    update_stream_fields, upsert_stage_gate,
    upsert_transcript_draft, get_transcript_draft, delete_transcript_draft,
    replace_screenshots, list_screenshots, delete_screenshot,
)
import local_llm                        # noqa: E402
import pipeline                         # noqa: E402
import screenshot as screenshot_mod     # noqa: E402

from prisma import Prisma               # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    await orchestrator.startup()
    try:
        swept = await asyncio.to_thread(sweep_running_expansions)
        if swept:
            print(f"[expand] swept {swept} orphaned 'running' expansion(s) -> error")
    except Exception as e:
        print(f"[expand] sweep failed: {e}")
    try:
        yield
    finally:
        await orchestrator.shutdown()


app = FastAPI(title="VideoText", version="0.4.0", lifespan=lifespan)


# Log 422 request-validation failures with the exact field + bad input, so a
# rejected /briefs (transcription never starts) is diagnosable from the console.
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse             # noqa: E402


@app.exception_handler(RequestValidationError)
async def _log_validation_error(request, exc: RequestValidationError):
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", []))
        print(f"[422] {request.method} {request.url.path} :: {loc}: "
              f"{e.get('msg')} (input={e.get('input')!r})", flush=True)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


import threading                        # noqa: E402
_expansion_jobs: set[tuple[str, str]] = set()
_expansion_jobs_lock = threading.Lock()


def _run_expansion_job(*, video_id, mode, system, user, model, num_ctx, temperature):
    """Runs in a daemon thread. Streams the LLM fully, then persists. Never tied
    to the HTTP request, so client disconnect/navigation cannot abort it."""
    import time
    started = time.monotonic()
    try:
        chunks = list(local_llm.stream_chat(
            system=system, user=user, model=model,
            num_ctx=num_ctx, temperature=temperature,
        ))
        full_text = "".join(chunks).strip()
        if full_text:
            finish_expansion(video_id=video_id, mode=mode, content_md=full_text,
                             elapsed_ms=int((time.monotonic() - started) * 1000),
                             verdict=pipeline.parse_verdict(full_text) if mode == "report" else None)
        else:
            fail_expansion(video_id=video_id, mode=mode, error="пустой ответ модели")
    except Exception as e:
        fail_expansion(video_id=video_id, mode=mode, error=f"{type(e).__name__}: {e}")
    finally:
        with _expansion_jobs_lock:
            _expansion_jobs.discard((video_id, mode))


_screenshot_jobs: set[str] = set()
_screenshot_jobs_lock = threading.Lock()


def _run_screenshot_job(video_id: str, model: str | None):
    """Daemon thread: detect visual-reference moments in the transcript, grab a
    frame per moment (one low-res video download + local ffmpeg cuts), and
    persist. Replaces prior screenshots only when new frames are captured — a
    run that finds nothing leaves existing references untouched."""
    try:
        v = get_video(video_id, with_segments=True)
        if not v:
            return
        segments = v.segments or []
        moments = screenshot_mod.detect_reference_moments(segments, model=model)
        by_index = {s.index: s for s in segments}
        prepared = []
        for m in moments:
            seg = by_index.get(m["segment_index"])
            if not seg:
                continue
            prepared.append({
                # base timestamp; the vision window searches around it for the
                # frame that actually shows the described screen
                "ts": max(0.0, float(seg.start)),
                "segment_index": m["segment_index"],
                "caption": m["caption"], "reason": m["reason"],
                "model": m.get("model", ""),
            })
        captured = (screenshot_mod.capture_frames_vision(
            v.url, prepared, video_id, vision_model="claude-opus-4-8")
            if prepared else [])
        rows = [{
            "timestamp": c["ts"], "segment_index": c.get("segment_index"),
            "file_path": c["file_path"], "caption": c.get("caption", ""),
            "reason": c.get("reason", ""), "model": c.get("model", ""),
        } for c in captured]
        if rows:
            for old in (list_screenshots(video_id) or []):
                try:
                    Path(old.filePath).unlink(missing_ok=True)
                except Exception:
                    pass
            replace_screenshots(video_id, rows)
        print(f"[screenshots] {video_id}: {len(moments)} moments → {len(rows)} frames",
              flush=True)
    except Exception as e:
        print(f"[screenshots] job FAILED {video_id}: {type(e).__name__}: {e}", flush=True)
    finally:
        with _screenshot_jobs_lock:
            _screenshot_jobs.discard(video_id)


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

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, v):
        """Be forgiving about how the URL is pasted — HttpUrl otherwise 422s on
        anything without a scheme. Accepts a bare 11-char YouTube id and a
        scheme-less host (`www.youtube.com/...`, `youtu.be/...`)."""
        if not isinstance(v, str):
            return v
        s = v.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
            return f"https://www.youtube.com/watch?v={s}"
        if s and "://" not in s:
            return f"https://{s}"
        return s


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


# ─── Local LLM (Ollama) — spec expansion ─────────────────────────

@app.get("/local-llm/models")
def local_llm_models() -> dict:
    """List models installed on the local Ollama daemon."""
    import local_llm
    models = local_llm.list_installed_models()
    return {
        "endpoint": local_llm._endpoint(),
        "reachable": bool(models),
        "models": [
            {
                "name": m.get("name"),
                "size_bytes": m.get("size"),
                "parameter_size": (m.get("details") or {}).get("parameter_size"),
                "quantization": (m.get("details") or {}).get("quantization_level"),
                "family": (m.get("details") or {}).get("family"),
            }
            for m in models
        ],
    }


# Output mode — which deliverable the local model should produce. Defined once
# and reused by the expand request body AND the expansion-accessor routes so the
# allowed set never drifts between writing and reading an expansion.
#   "spec"          → expanded technical specification (default for spec section)
#   "research"      → analytical research report (default for non-spec sections)
#   "report"        → executive summary / action-item report
#   "ai_skills"     → reusable AI-skill definitions derived from the material
#   "ai_algorithms" → step-by-step action algorithms for an AI agent
ExpandMode = Literal["spec", "research", "report", "uiux", "ai_skills", "ai_algorithms"]


class ExpandSpecRequest(BaseModel):
    section_md: str = ""                 # optional: source is the whole video now
    section_title: str = "бриф"
    mode: ExpandMode = "spec"
    model: str | None = None
    # Context source: which parts of the video go into the prompt.
    context: Literal["brief", "transcript", "both"] | None = None
    include_transcript: bool = True


@app.post("/videos/{video_id}/expand-spec")
def expand_spec(video_id: str, req: ExpandSpecRequest) -> dict:
    """Fire-and-forget: start a durable background generation and return immediately.
    The result is persisted to Expansion regardless of whether the client stays
    connected (navigation-proof). The UI polls GET /videos/{id}/expansions/{mode}."""
    video = get_video(video_id, with_segments=True)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    if not video.briefs:
        raise HTTPException(status_code=400, detail="У видео нет брифа")

    key = (video_id, req.mode)
    existing = get_expansion(video_id, req.mode)
    if existing and existing.status == "running":
        return {"status": "running", "mode": req.mode, "already": True}
    with _expansion_jobs_lock:
        if key in _expansion_jobs:
            return {"status": "running", "mode": req.mode, "already": True}
        _expansion_jobs.add(key)

    latest = video.briefs[-1]
    settings = get_all_settings()
    model = (req.model or settings.get("local_llm_model") or local_llm.DEFAULT_MODEL).strip()
    num_ctx = int(settings.get("local_llm_num_ctx") or local_llm.DEFAULT_NUM_CTX)
    temperature = float(settings.get("local_llm_temperature") or 0.3)
    max_tx_chars = int(settings.get("local_llm_max_transcript_chars") or local_llm.MAX_TRANSCRIPT_CHARS)

    ctx_mode = req.context or ("both" if req.include_transcript else "brief")
    use_brief = ctx_mode in ("brief", "both")
    use_transcript = ctx_mode in ("transcript", "both")

    sb_json = None
    if use_brief and latest.contentJson:
        try:
            sb_json = (json.loads(latest.contentJson) if isinstance(latest.contentJson, str)
                       else latest.contentJson).get("software_brief")
        except (json.JSONDecodeError, AttributeError):
            sb_json = None

    # Curated source: prefer the edited blocks over raw segments / raw brief.
    transcript_excerpt = ""
    if use_transcript:
        transcript_excerpt = _current_doc_text(video_id, "transcript")
        local_llm.MAX_TRANSCRIPT_CHARS = max_tx_chars
    curated_brief = _current_doc_text(video_id, "brief") if use_brief else ""
    essence_block = _essence_section(_current_doc_text(video_id, "essence"))

    # Передача: feed predecessor stages' outputs into the prompt (pipeline graph).
    upstream: dict[str, str] = {}
    for dep in pipeline.UPSTREAM.get(req.mode, []):
        de = get_expansion(video_id, dep)
        if de and getattr(de, "status", "done") == "done" and de.contentMd:
            upstream[dep] = de.contentMd

    system, user = local_llm.build_expand_prompt(
        mode=req.mode, video_title=video.title or video_id,
        section_title=req.section_title, section_md=req.section_md,
        software_brief_json=sb_json,
        full_brief_md=(essence_block + curated_brief) if use_brief else essence_block,
        transcript_excerpt=transcript_excerpt,
        upstream=upstream,
    )

    # Mark running (preserves any previous content) BEFORE launching the thread.
    start_expansion(
        video_id=video_id, mode=req.mode, source_title=req.section_title,
        source_md=req.section_md, context_mode=ctx_mode, model=model,
        num_ctx=num_ctx, input_chars=len(user),
    )
    threading.Thread(
        target=_run_expansion_job, daemon=True,
        kwargs={"video_id": video_id, "mode": req.mode, "system": system,
                "user": user, "model": model, "num_ctx": num_ctx,
                "temperature": temperature},
    ).start()
    return {"status": "running", "mode": req.mode, "context_mode": ctx_mode}


# ─── Expansion accessors (for UI preload + downloads) ─────────────

def _expansion_to_dict(e) -> dict:
    return {
        "id": e.id,
        "video_id": e.videoId,
        "mode": e.mode,
        "version": e.version,
        "from_version": e.fromVersion,
        "verdict": getattr(e, "verdict", None),
        "source_title": e.sourceTitle,
        "source_md": e.sourceMd,
        "context_mode": e.contextMode,
        "model": e.model,
        "num_ctx": e.numCtx,
        "content_md": e.contentMd,
        "input_chars": e.inputChars,
        "elapsed_ms": e.elapsedMs,
        "status": getattr(e, "status", "done"),
        "error": getattr(e, "error", None),
        "created_at": e.createdAt.isoformat(),
        "updated_at": e.updatedAt.isoformat(),
    }


@app.get("/videos/{video_id}/expansions")
def read_expansions(video_id: str) -> list[dict]:
    """All saved expansions for a video. Used by UI to pre-fill modal on open."""
    rows = list_expansions(video_id)
    return [_expansion_to_dict(e) for e in rows]


# ─── Layer 3: pipeline stage gates ─────────────────────────────────

def _stage_gate_to_dict(g) -> dict:
    return {
        "stage": g.stage,
        "items": json.loads(g.items),
        "assessed_at": g.assessedAt.isoformat() if g.assessedAt else None,
        "updated_at": g.updatedAt.isoformat(),
    }


class StageGatePutRequest(BaseModel):
    items: list[dict]


@app.get("/videos/{video_id}/stage-gates")
def read_stage_gates(video_id: str) -> dict:
    """Checklist state per gated stage, keyed by stage. Empty if none yet."""
    return {g.stage: _stage_gate_to_dict(g) for g in list_stage_gates(video_id)}


@app.put("/videos/{video_id}/stage-gates/{stage}")
def put_stage_gate(video_id: str, stage: str, req: StageGatePutRequest) -> dict:
    """Persist manual checklist overrides."""
    if stage not in pipeline.CHECKLISTS:
        raise HTTPException(status_code=400, detail=f"Этап {stage} без чеклиста")
    row = upsert_stage_gate(video_id=video_id, stage=stage, items=req.items, assessed=False)
    return _stage_gate_to_dict(row)


@app.post("/videos/{video_id}/stage-assess/{stage}")
def assess_stage(video_id: str, stage: str) -> dict:
    """AI-assess the stage's artifact against its checklist (Claude, JSON)."""
    if stage not in pipeline.CHECKLISTS:
        raise HTTPException(status_code=400, detail=f"Этап {stage} без чеклиста")
    e = get_expansion(video_id, stage)
    if not e or not (e.contentMd or "").strip():
        raise HTTPException(status_code=400, detail="Нет артефакта этапа для оценки")
    try:
        items = pipeline.assess_checklist(stage, e.contentMd)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"AI-оценка не удалась: {ex}")
    row = upsert_stage_gate(video_id=video_id, stage=stage, items=items, assessed=True)
    return _stage_gate_to_dict(row)


@app.get("/videos/{video_id}/expansions/{mode}/versions")
def read_expansion_versions(video_id: str, mode: ExpandMode) -> dict:
    """Version list for the artifact selector. Bodies are omitted on purpose —
    the UI fetches one version's text only when the user picks it."""
    rows = list_expansion_versions(video_id, mode)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No '{mode}' expansion for {video_id}")
    return {
        "mode": mode,
        "versions": [{
            "version": r.version,
            "model": r.model,
            "status": getattr(r, "status", "done"),
            "verdict": getattr(r, "verdict", None),
            "chars": len(r.contentMd or ""),
            "elapsed_ms": r.elapsedMs,
            "created_at": r.createdAt.isoformat(),
        } for r in rows],
    }


# NOTE: the `.pdf` route MUST be declared before the bare `{mode}` route.
# A path param matches dots, so `{mode}` would otherwise capture "spec.pdf"
# and shadow this route (Starlette matches in declaration order).
@app.get("/videos/{video_id}/expansions/{mode}.pdf")
def export_expansion_pdf(video_id: str, mode: ExpandMode):
    """Render a saved expansion as a PDF. Available for all modes, but the UI
    only surfaces the button for research/report (ТЗ / AI-артефакты остаются
    markdown — их скармливают обратно в Cursor/Claude)."""
    from export import markdown_to_pdf
    e = get_expansion(video_id, mode)
    if not e:
        raise HTTPException(status_code=404, detail=f"No '{mode}' expansion for {video_id}")
    title_map = {
        "spec": "Расширенное ТЗ", "research": "Исследование", "report": "Репорт",
        "ai_skills": "AI-скиллы", "ai_algorithms": "Алгоритмы для AI",
    }
    title = f"{title_map.get(mode, 'Расширение')}: {e.sourceTitle}"
    pdf_bytes = markdown_to_pdf(e.contentMd, title=title)
    filename = f"{mode}-{video_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/videos/{video_id}/expansions/{mode}")
def read_expansion(video_id: str, mode: ExpandMode, version: int | None = None) -> dict:
    """Current artifact, or a specific version when `?version=N` is given."""
    e = (get_expansion_version(video_id, mode, version) if version is not None
         else get_expansion(video_id, mode))
    if not e:
        detail = (f"No '{mode}' v{version} for {video_id}" if version is not None
                  else f"No '{mode}' expansion for {video_id}")
        raise HTTPException(status_code=404, detail=detail)
    return _expansion_to_dict(e)


# ─── Transcript AI-editor ──────────────────────────────────────────

def _original_transcript_text(video_id: str) -> tuple[str, Any]:
    """(joined original transcript text, video). Raises 404/400 as needed."""
    v = get_video(video_id, with_segments=True)
    if not v:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    if not v.segments:
        raise HTTPException(status_code=400, detail="У видео нет расшифровки")
    text = "\n".join(s.text for s in v.segments if s.text)
    return text, v


def _edit_to_dict(e) -> dict:
    return {
        "id": e.id, "video_id": e.videoId, "kind": e.kind, "version": e.version,
        "content_md": e.contentMd, "op": e.op, "instruction": e.instruction,
        "from_version": e.fromVersion, "model": e.model,
        "input_chars": e.inputChars, "elapsed_ms": e.elapsedMs,
        "created_at": e.createdAt.isoformat(),
    }


_DOC_KINDS = ("transcript", "brief", "essence")


def _pick_current(original_md: str, edits: list) -> str:
    """Latest edited text if any (edits are newest-first), else the original."""
    return edits[0].contentMd if edits else original_md


def _original_doc_text(video_id: str, kind: str) -> tuple[str, bool]:
    """(original text for this kind, has_original). Raises 404 for unknown video."""
    v = get_video(video_id, with_segments=True)
    if not v:
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    if kind == "transcript":
        text = "\n".join(s.text for s in (v.segments or []) if s.text)
        return text, bool(text)
    if kind == "brief":
        text = v.briefs[-1].contentMd if v.briefs else ""
        return text, bool(text)
    if kind == "essence":
        return "", False  # no source of truth; v1 comes from `seed`
    raise HTTPException(status_code=422, detail=f"Unknown doc kind: {kind}")


def _current_doc_text(video_id: str, kind: str) -> str:
    """Current working text for a kind = latest edit, else original."""
    original, _has = _original_doc_text(video_id, kind)
    return _pick_current(original, list_transcript_edits(video_id, kind=kind))


def _doc_download_text(video_id: str, kind: str, which: str) -> str:
    """Text for the download buttons. `which`:
      - "current" (default) → latest edit if any, else original ("улучшенный" view);
        for essence this is the generated суть.
      - "original" → raw source text ("оригинал" view).
    404 if the requested view is empty (e.g. essence has no original yet)."""
    if which == "original":
        text, _has = _original_doc_text(video_id, kind)
    else:
        text = _current_doc_text(video_id, kind)
    if not (text or "").strip():
        raise HTTPException(status_code=404, detail=f"Нет текста для {kind}/{which}")
    return text


def _essence_section(essence_md: str) -> str:
    """Optional '## Суть' block prepended to the expand user-prompt source."""
    essence_md = (essence_md or "").strip()
    return f"## Суть\n{essence_md}\n\n" if essence_md else ""


def _seed_inputs_ok(transcript_text: str, brief_md: str) -> bool:
    """True iff at least one source has non-whitespace content. The strip() must
    happen BEFORE the `or` — `("   " or br).strip()` short-circuits to the
    whitespace-only first operand and falsely rejects a non-empty brief."""
    return bool((transcript_text or "").strip() or (brief_md or "").strip())


@app.get("/videos/{video_id}/transcript")
def read_transcript(video_id: str) -> dict:
    text, v = _original_transcript_text(video_id)
    edits = list_transcript_edits(video_id)
    return {
        "video_id": video_id,
        "title": v.title,
        "original_md": text,
        "original_chars": len(text),
        "versions": len(edits),
        "latest_version": edits[0].version if edits else 0,
    }


@app.get("/videos/{video_id}/transcript/edits")
def read_transcript_edits(video_id: str) -> list[dict]:
    return [_edit_to_dict(e) for e in list_transcript_edits(video_id)]


@app.get("/videos/{video_id}/transcript/edits/{version}.pdf")
def export_transcript_edit_pdf(video_id: str, version: int):
    from export import markdown_to_pdf
    e = get_transcript_edit(video_id, version)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    pdf_bytes = markdown_to_pdf(e.contentMd, title=f"Расшифровка v{version}")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="transcript-v{version}-{video_id}.pdf"'},
    )


@app.get("/videos/{video_id}/transcript/edits/{version}.md")
def export_transcript_edit_md(video_id: str, version: int):
    e = get_transcript_edit(video_id, version)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    return Response(
        content=e.contentMd, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="transcript-v{version}-{video_id}.md"'},
    )


@app.get("/videos/{video_id}/transcript/edits/{version}")
def read_transcript_edit(video_id: str, version: int) -> dict:
    e = get_transcript_edit(video_id, version)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    return _edit_to_dict(e)


class TranscriptEditRequest(BaseModel):
    op: Literal["improve", "structure", "clean", "chat", "expand_idea"] = "improve"
    instruction: str = ""
    model: str | None = None        # None/empty -> Claude default
    base_version: int | None = None  # which version we edit from (None = original)


class TranscriptApplyRequest(BaseModel):
    op: str
    instruction: str = ""
    content_md: str
    model: str = ""
    from_version: int | None = None
    elapsed_ms: int = 0


@app.post("/videos/{video_id}/transcript/edit")
def transcript_edit_preview(video_id: str, req: TranscriptEditRequest):
    """Stream a proposed new full text (SSE). Does NOT persist."""
    import transcript_edit

    original, _v = _original_transcript_text(video_id)
    # Edit from the requested base version if given, else the original text.
    current = original
    if req.base_version:
        base = get_transcript_edit(video_id, req.base_version)
        if not base:
            raise HTTPException(status_code=404, detail=f"No v{req.base_version}")
        current = base.contentMd

    settings = get_all_settings()
    model = (req.model or settings.get("editor_transcript_model") or "claude-sonnet-4-6").strip()
    num_ctx = int(settings.get("local_llm_num_ctx") or 32768)
    temperature = float(settings.get("local_llm_temperature") or 0.3)

    def event_stream():
        import time
        started = time.monotonic()
        try:
            yield f"data: {json.dumps({'type': 'meta', 'model': model, 'op': req.op, 'base_version': req.base_version, 'current_chars': len(current)}, ensure_ascii=False)}\n\n"
            for piece in transcript_edit.stream_edit(
                op=req.op, current_text=current, instruction=req.instruction,
                model=model, num_ctx=num_ctx, temperature=temperature,
            ):
                yield f"data: {json.dumps({'type': 'delta', 'text': piece}, ensure_ascii=False)}\n\n"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'op': req.op, 'elapsed_ms': elapsed_ms}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/videos/{video_id}/transcript/edits/apply")
def transcript_edit_apply(video_id: str, req: TranscriptApplyRequest) -> dict:
    """Persist a new version from the previewed text."""
    if not (req.content_md or "").strip():
        raise HTTPException(status_code=400, detail="Пустой текст — нечего сохранять")
    row = create_transcript_edit(
        video_id=video_id, content_md=req.content_md, op=req.op,
        instruction=req.instruction, from_version=req.from_version,
        model=req.model or "claude-sonnet-4-6",
        input_chars=len(req.content_md), elapsed_ms=req.elapsed_ms,
    )
    return _edit_to_dict(row)


@app.post("/videos/{video_id}/transcript/edits/{version}/rollback")
def transcript_edit_rollback(video_id: str, version: int) -> dict:
    row = rollback_transcript_edit(video_id, version)
    if not row:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}")
    return _edit_to_dict(row)


# ─── Generalized doc editor (transcript | brief | essence) ─────────

def _require_kind(kind: str) -> str:
    if kind not in _DOC_KINDS:
        raise HTTPException(status_code=422, detail=f"Unknown doc kind: {kind}")
    return kind


def _doc_title(kind: str) -> str:
    return {"transcript": "Расшифровка", "brief": "Бриф", "essence": "Суть"}[kind]


@app.get("/videos/{video_id}/docs/{kind}")
def read_doc(video_id: str, kind: str) -> dict:
    _require_kind(kind)
    original, has_original = _original_doc_text(video_id, kind)
    edits = list_transcript_edits(video_id, kind=kind)
    return {
        "video_id": video_id, "kind": kind,
        "original_md": original, "original_chars": len(original),
        "has_original": has_original,
        "versions": len(edits),
        "latest_version": edits[0].version if edits else 0,
    }


@app.get("/videos/{video_id}/docs/{kind}/edits")
def read_doc_edits(video_id: str, kind: str) -> list[dict]:
    _require_kind(kind)
    return [_edit_to_dict(e) for e in list_transcript_edits(video_id, kind=kind)]


@app.get("/videos/{video_id}/docs/{kind}/edits/{version}.pdf")
def export_doc_edit_pdf(video_id: str, kind: str, version: int):
    _require_kind(kind)
    from export import markdown_to_pdf
    e = get_transcript_edit(video_id, version, kind=kind)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    pdf_bytes = markdown_to_pdf(e.contentMd, title=f"{_doc_title(kind)} v{version}")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{kind}-v{version}-{video_id}.pdf"'},
    )


@app.get("/videos/{video_id}/docs/{kind}/edits/{version}.md")
def export_doc_edit_md(video_id: str, kind: str, version: int):
    _require_kind(kind)
    e = get_transcript_edit(video_id, version, kind=kind)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    return Response(
        content=e.contentMd, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}-v{version}-{video_id}.md"'},
    )


@app.get("/videos/{video_id}/docs/{kind}/download.pdf")
def export_doc_current_pdf(video_id: str, kind: str, which: str = "current"):
    """Download the whole doc block (расшифровка / бриф / суть) as one PDF.
    `which=current` (default) exports the улучшенный / generated text; `which=original`
    exports the raw source. Per-version PDFs live under /edits/{version}.pdf."""
    _require_kind(kind)
    from export import markdown_to_pdf
    text = _doc_download_text(video_id, kind, which)
    suffix = " (оригинал)" if which == "original" else ""
    pdf_bytes = markdown_to_pdf(text, title=f"{_doc_title(kind)}{suffix}")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{kind}-{which}-{video_id}.pdf"'},
    )


@app.get("/videos/{video_id}/docs/{kind}/download.md")
def export_doc_current_md(video_id: str, kind: str, which: str = "current"):
    """Markdown counterpart of the doc-block download (see export_doc_current_pdf)."""
    _require_kind(kind)
    text = _doc_download_text(video_id, kind, which)
    return Response(
        content=text, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}-{which}-{video_id}.md"'},
    )


@app.get("/videos/{video_id}/docs/{kind}/edits/{version}")
def read_doc_edit(video_id: str, kind: str, version: int) -> dict:
    _require_kind(kind)
    e = get_transcript_edit(video_id, version, kind=kind)
    if not e:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    return _edit_to_dict(e)


class DocEditRequest(BaseModel):
    op: Literal["improve", "structure", "clean", "chat", "expand_idea", "seed"] = "improve"
    instruction: str = ""
    model: str | None = None
    base_version: int | None = None


@app.post("/videos/{video_id}/docs/{kind}/edit")
def doc_edit_preview(video_id: str, kind: str, req: DocEditRequest):
    """Stream a proposed new full text (SSE). Does NOT persist."""
    import transcript_edit

    _require_kind(kind)
    settings = get_all_settings()
    model = (req.model or settings.get("editor_transcript_model") or "claude-sonnet-4-6").strip()
    num_ctx = int(settings.get("local_llm_num_ctx") or 32768)
    temperature = float(settings.get("local_llm_temperature") or 0.3)

    if kind == "essence" and req.op == "seed":
        tx = _current_doc_text(video_id, "transcript")
        br = _current_doc_text(video_id, "brief")
        if not _seed_inputs_ok(tx, br):
            raise HTTPException(status_code=400, detail="Нет исходного текста для сути")
        system, user = transcript_edit.build_seed_prompt(transcript_text=tx, brief_md=br)
    else:
        if req.base_version:
            base = get_transcript_edit(video_id, req.base_version, kind=kind)
            if not base:
                raise HTTPException(status_code=404, detail=f"No v{req.base_version}")
            current = base.contentMd
        else:
            current = _current_doc_text(video_id, kind)
        system, user = transcript_edit.build_edit_prompt(
            op=req.op, current_text=current, instruction=req.instruction, kind=kind)

    def event_stream():
        import time
        started = time.monotonic()
        full = ""
        try:
            yield f"data: {json.dumps({'type': 'meta', 'model': model, 'op': req.op, 'kind': kind, 'current_chars': len(user)}, ensure_ascii=False)}\n\n"
            if transcript_edit._is_claude(model):
                pieces = transcript_edit._stream_claude(system, user, model)
            else:
                pieces = local_llm.stream_chat(system=system, user=user, model=model,
                                                num_ctx=num_ctx, temperature=temperature)
            for piece in pieces:
                full += piece
                yield f"data: {json.dumps({'type': 'delta', 'text': piece}, ensure_ascii=False)}\n\n"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # Tokens are spent by the time we get here — persist the result as a
            # draft so a browser refresh restores the preview instead of
            # re-generating. One draft per (video, kind); this UPSERTs it.
            saved = False
            if full.strip():
                try:
                    upsert_transcript_draft(
                        video_id=video_id, kind=kind, content_md=full, op=req.op,
                        instruction=req.instruction, from_version=req.base_version,
                        model=model, elapsed_ms=elapsed_ms)
                    saved = True
                except Exception as e:
                    print(f"[edit] draft save FAILED {video_id}/{kind}: "
                          f"{type(e).__name__}: {e}", flush=True)
            print(f"[edit] {video_id}/{kind} model={model} op={req.op} "
                  f"chars={len(full)} elapsed={elapsed_ms}ms draft_saved={saved}",
                  flush=True)
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'op': req.op, 'elapsed_ms': elapsed_ms, 'draft_saved': saved}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class DocApplyRequest(BaseModel):
    op: str
    instruction: str = ""
    content_md: str
    model: str = ""
    from_version: int | None = None
    elapsed_ms: int = 0


@app.post("/videos/{video_id}/docs/{kind}/edits/apply")
def doc_edit_apply(video_id: str, kind: str, req: DocApplyRequest) -> dict:
    _require_kind(kind)
    if not (req.content_md or "").strip():
        raise HTTPException(status_code=400, detail="Пустой текст — нечего сохранять")
    row = create_transcript_edit(
        video_id=video_id, kind=kind, content_md=req.content_md, op=req.op,
        instruction=req.instruction, from_version=req.from_version,
        model=req.model or "claude-sonnet-4-6",
        input_chars=len(req.content_md), elapsed_ms=req.elapsed_ms,
    )
    delete_transcript_draft(video_id, kind)  # applied → the draft is now a version
    return _edit_to_dict(row)


def _draft_to_dict(d) -> dict:
    return {
        "video_id": d.videoId, "kind": d.kind, "content_md": d.contentMd,
        "op": d.op, "instruction": d.instruction, "from_version": d.fromVersion,
        "model": d.model, "elapsed_ms": d.elapsedMs,
        "updated_at": d.updatedAt.isoformat(),
    }


@app.get("/videos/{video_id}/docs/{kind}/draft")
def read_doc_draft(video_id: str, kind: str):
    """The unsaved generation for this doc block, or null. Restores the preview
    after a browser refresh without re-spending tokens."""
    _require_kind(kind)
    d = get_transcript_draft(video_id, kind)
    return _draft_to_dict(d) if d else None


@app.get("/videos/{video_id}/docs/{kind}/draft/download.pdf")
def export_doc_draft_pdf(video_id: str, kind: str):
    _require_kind(kind)
    from export import markdown_to_pdf
    d = get_transcript_draft(video_id, kind)
    if not d or not (d.contentMd or "").strip():
        raise HTTPException(status_code=404, detail=f"Нет черновика для {kind}")
    pdf_bytes = markdown_to_pdf(d.contentMd, title=f"{_doc_title(kind)} (черновик)")
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{kind}-draft-{video_id}.pdf"'},
    )


@app.get("/videos/{video_id}/docs/{kind}/draft/download.md")
def export_doc_draft_md(video_id: str, kind: str):
    _require_kind(kind)
    d = get_transcript_draft(video_id, kind)
    if not d or not (d.contentMd or "").strip():
        raise HTTPException(status_code=404, detail=f"Нет черновика для {kind}")
    return Response(
        content=d.contentMd, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}-draft-{video_id}.md"'},
    )


@app.delete("/videos/{video_id}/docs/{kind}/draft")
def discard_doc_draft(video_id: str, kind: str) -> dict:
    """Drop the unsaved generation (user hit «отмена»)."""
    _require_kind(kind)
    delete_transcript_draft(video_id, kind)
    return {"ok": True}


# ─── Screenshot-references ─────────────────────────────────────────

def _screenshot_to_dict(r) -> dict:
    return {
        "id": r.id, "video_id": r.videoId, "timestamp": r.timestamp,
        "segment_index": r.segmentIndex,
        "url": "/" + r.filePath.replace("\\", "/"),
        "caption": r.caption, "reason": r.reason, "model": r.model,
    }


class ScreenshotExtractRequest(BaseModel):
    model: str | None = None


@app.post("/videos/{video_id}/screenshots/extract")
def extract_screenshots(video_id: str, req: ScreenshotExtractRequest) -> dict:
    """Kick off (in the background) detection + frame-grabbing of visual
    references for this video. Poll GET /screenshots for progress + results."""
    if not get_video(video_id):
        raise HTTPException(status_code=404, detail=f"Video {video_id} not found")
    with _screenshot_jobs_lock:
        if video_id in _screenshot_jobs:
            return {"status": "running"}
        _screenshot_jobs.add(video_id)
    threading.Thread(target=_run_screenshot_job, args=(video_id, req.model),
                     daemon=True).start()
    return {"status": "started"}


@app.get("/videos/{video_id}/screenshots")
def read_screenshots(video_id: str) -> dict:
    with _screenshot_jobs_lock:
        running = video_id in _screenshot_jobs
    rows = list_screenshots(video_id) or []
    return {"running": running,
            "screenshots": [_screenshot_to_dict(r) for r in rows]}


@app.delete("/videos/{video_id}/screenshots/{screenshot_id}")
def remove_screenshot(video_id: str, screenshot_id: int) -> dict:
    for r in (list_screenshots(video_id) or []):
        if r.id == screenshot_id:
            try:
                Path(r.filePath).unlink(missing_ok=True)
            except Exception:
                pass
    delete_screenshot(screenshot_id)
    return {"ok": True}


@app.post("/videos/{video_id}/docs/{kind}/edits/{version}/rollback")
def doc_edit_rollback(video_id: str, kind: str, version: int) -> dict:
    _require_kind(kind)
    row = rollback_transcript_edit(video_id, version, kind=kind)
    if not row:
        raise HTTPException(status_code=404, detail=f"No v{version} for {video_id}/{kind}")
    return _edit_to_dict(row)


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
    q: str | None = None,
    limit: int = 200,
) -> list[dict]:
    items = await asyncio.to_thread(search_news_items, stream_id, status, q, limit)
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
    if req.field == "imageId":
        # value is the concept phrase; actually generate + attach via enrich
        concept = req.value if isinstance(req.value, str) else (req.value or {}).get("concept") or ""
        if not concept:
            raise HTTPException(400, "imageId apply requires concept string in value")
        try:
            from enrich import enrich_item
            from store import (
                create_news_image, find_similar_image, get_news_item,
                increment_image_reuse, update_news_item_enrichment,
            )
            settings = await asyncio.to_thread(get_all_settings)
            text_model   = str(settings.get("enrich_text_model", "gpt-4o-mini"))
            image_model  = str(settings.get("enrich_image_model", "dall-e-3"))
            image_source = str(settings.get("enrich_image_source", "hybrid"))
            dedup_thr    = float(settings.get("enrich_image_dedup_threshold", 0.88))
            embed_prov   = str(settings.get("dedup_embedding_provider", "fastembed"))
            embed_model  = str(settings.get("dedup_embedding_model", ""))
            item = await asyncio.to_thread(get_news_item, req.item_id)
            if not item:
                raise HTTPException(404, f"NewsItem {req.item_id} not found")
            result = await asyncio.to_thread(
                enrich_item, item,
                text_model, image_model, image_source, dedup_thr,
                embed_prov, embed_model,
                find_similar_image, create_news_image, increment_image_reuse,
                True,  # image_enabled
            )
            await asyncio.to_thread(
                update_news_item_enrichment,
                item.id, result.expanded_text, text_model,
                result.expanded_usage.get("cost_usd", 0), result.image_id,
            )
            return {"ok": True, "item_id": req.item_id, "updated_field": "imageId", "image_id": result.image_id}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"{type(e).__name__}: {e}")

    # Default path for headline/quote/expandedText/tags
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


@app.get("/chat/{persona}/sessions")
async def chat_sessions(persona: Literal["platform", "editor"], limit: int = 50):
    """List chat sessions for a given persona (sidebar history)."""
    db = Prisma()
    await db.connect()
    try:
        sess = await db.assistantsession.find_many(
            where={"persona": persona},
            order={"updatedAt": "desc"},
            take=limit,
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
