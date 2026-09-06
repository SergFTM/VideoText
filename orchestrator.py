"""Orchestrates live-stream workers.

Model:
  - 1 capture worker per active stream (each writes its own MP3 chunks)
  - 1 GLOBAL transcribe worker (single GPU, can't parallelize cheaply)
  - 1 GLOBAL news-extract worker (Claude API calls, latency-tolerant)

Workers are asyncio tasks running inside the uvicorn process. Chunks are the
handoff queue — written by capture, read by transcribe (status=pending), read
by extract (status=transcribed). Everything is durable in SQLite, so a server
restart resumes: we just re-pick up `status=pending/transcribed` chunks.

A note on blocking work: transcribe & news-extract do CPU/GPU/API work that
blocks. They're wrapped in `asyncio.to_thread` so the event loop stays
responsive for API requests.
"""
import asyncio
import sys
from pathlib import Path

from capture import capture_loop
from dedup import DedupConfig, embed, find_duplicate
from news_extractor import build_attribution, extract_news_items
from store import (
    _list_chunk_news_items, get_pending_chunks, insert_news_items, list_streams,
    load_recent_embeddings, update_chunk,
    update_news_item_dedup, update_stream_status,
)
from transcribe import transcribe_file


# Per-stream state: stream_id → {task, stop_event}
_stream_tasks: dict[str, dict] = {}


# Shared worker state
_global_stop: asyncio.Event | None = None
_transcribe_task: asyncio.Task | None = None
_extract_task: asyncio.Task | None = None
_cleanup_task: asyncio.Task | None = None

CHUNK_ROOT = Path("./chunks")


async def _start_stream_workers(stream_id: str, url: str, interval_min: int) -> None:
    if stream_id in _stream_tasks:
        return  # already running
    stop = asyncio.Event()
    chunk_dir = CHUNK_ROOT / stream_id

    async def on_chunk_ready(chunk):
        # Nothing extra — transcribe worker polls DB for new pending chunks.
        pass

    async def _wrapped():
        """Surface exceptions from capture_loop — bare tasks swallow them."""
        try:
            await capture_loop(
                stream_id=stream_id, youtube_url=url, chunk_dir=chunk_dir,
                interval_min=interval_min, stop_event=stop,
                on_chunk_ready=on_chunk_ready,
            )
            print(f"[orchestrator] capture_loop exited cleanly for {stream_id}", file=sys.stderr)
        except asyncio.CancelledError:
            print(f"[orchestrator] capture_loop cancelled for {stream_id}", file=sys.stderr)
            raise
        except Exception as e:
            print(
                f"[orchestrator] capture_loop CRASHED for {stream_id}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            import traceback
            traceback.print_exc(file=sys.stderr)
        finally:
            # capture_loop also returns normally after giving up on 5 consecutive
            # failures. Leaving the key here made active_stream_ids() report a
            # dead recording as live, so the UI showed a green dot forever.
            if _stream_tasks.get(stream_id, {}).get("stop_event") is stop:
                _stream_tasks.pop(stream_id, None)
                print(f"[orchestrator] capture for {stream_id} is no longer active",
                      file=sys.stderr)

    task = asyncio.create_task(_wrapped(), name=f"capture:{stream_id}")
    _stream_tasks[stream_id] = {"task": task, "stop_event": stop}
    print(f"[orchestrator] started capture for {stream_id}", file=sys.stderr)


async def _stop_stream_workers(stream_id: str) -> None:
    entry = _stream_tasks.pop(stream_id, None)
    if not entry:
        return
    entry["stop_event"].set()
    try:
        await asyncio.wait_for(entry["task"], timeout=10)
    except asyncio.TimeoutError:
        entry["task"].cancel()
    print(f"[orchestrator] stopped capture for {stream_id}", file=sys.stderr)


async def _sleep_or_stop(seconds: float) -> None:
    """Back off, but wake immediately if the server is shutting down."""
    assert _global_stop is not None
    try:
        await asyncio.wait_for(_global_stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _transcribe_worker() -> None:
    """Pulls `status=pending` chunks, transcribes, writes text back."""
    assert _global_stop is not None
    while not _global_stop.is_set():
        # The poll and the chunk.stream access below sit OUTSIDE the inner try
        # that guards transcription. An error here used to escape the while loop
        # and kill this task for the life of the process — silently, because the
        # task is held in a module global and so is never garbage-collected,
        # which is what would otherwise print "Task exception was never retrieved".
        try:
            pending = await asyncio.to_thread(get_pending_chunks, "pending", 1)
        except Exception as e:
            print(f"[transcribe] poll failed, retrying: {e}", file=sys.stderr)
            await _sleep_or_stop(5)
            continue
        if not pending:
            try:
                await asyncio.wait_for(_global_stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            chunk = pending[0]
            stream = chunk.stream
            model_name = stream.whisperModel if stream else "medium"
        except Exception as e:
            print(f"[transcribe] bad chunk row, skipping: {e}", file=sys.stderr)
            await _sleep_or_stop(5)
            continue
        try:
            result = await transcribe_file(chunk.audioPath, model_name=model_name)
            await asyncio.to_thread(
                update_chunk, chunk.id,
                transcriptText=result.text, status="transcribed",
            )
            print(
                f"[transcribe] chunk {chunk.index} ({stream.id if stream else '?'}): "
                f"{len(result.text)} chars, lang={result.language}",
                file=sys.stderr,
            )
        except Exception as e:
            await asyncio.to_thread(
                update_chunk, chunk.id, status="failed", error=f"transcribe: {e}"
            )
            print(f"[transcribe] FAILED chunk {chunk.id}: {e}", file=sys.stderr)


async def _cleanup_worker() -> None:
    """Periodic retention cleaner. Disabled by default, enabled via AppSetting.

    Checks `cleanup_auto_enabled` each tick — lets you toggle without restart.
    Interval comes from `cleanup_interval_hours` (default 6).
    """
    assert _global_stop is not None
    # Lazy imports to avoid circular concerns
    from cleanup import run_cleanup
    from store import get_all_settings

    while not _global_stop.is_set():
        try:
            settings = await asyncio.to_thread(get_all_settings)
        except Exception as e:
            print(f"[cleanup] settings load failed: {e}", file=sys.stderr)
            settings = {}

        enabled = bool(settings.get("cleanup_auto_enabled", False))
        interval_h = max(1, int(settings.get("cleanup_interval_hours", 6) or 6))
        if enabled:
            try:
                report = await asyncio.to_thread(run_cleanup, settings, True)
                if (report.get("audio_files_deleted", 0)
                        or report.get("chunk_rows_deleted", 0)
                        or report.get("output_files_deleted", 0)
                        or report.get("failed_chunks_deleted", 0)):
                    print(
                        f"[cleanup] auto-run: freed {report.get('audio_mb_freed', 0)}MB audio, "
                        f"{report.get('output_mb_freed', 0)}MB output, "
                        f"rows deleted — chunks={report.get('chunk_rows_deleted', 0)} "
                        f"news={report.get('news_items_deleted', 0)} "
                        f"briefs={report.get('stream_briefs_deleted', 0)}",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(f"[cleanup] auto-run failed: {e}", file=sys.stderr)

        # Sleep interval_h hours OR until global stop
        try:
            await asyncio.wait_for(_global_stop.wait(), timeout=interval_h * 3600)
        except asyncio.TimeoutError:
            pass


async def _extract_worker() -> None:
    """Pulls `status=transcribed` chunks, calls Claude, writes NewsItems."""
    assert _global_stop is not None
    while not _global_stop.is_set():
        try:
            pending = await asyncio.to_thread(get_pending_chunks, "transcribed", 1)
        except Exception as e:
            print(f"[extract] poll failed, retrying: {e}", file=sys.stderr)
            await _sleep_or_stop(5)
            continue
        if not pending:
            try:
                await asyncio.wait_for(_global_stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            chunk = pending[0]
            stream = chunk.stream
        except Exception as e:
            print(f"[extract] bad chunk row, skipping: {e}", file=sys.stderr)
            await _sleep_or_stop(5)
            continue
        if not stream:
            await asyncio.to_thread(update_chunk, chunk.id, status="failed", error="stream missing")
            continue

        try:
            # Target output language comes from user settings — news items
            # and their quotes are emitted in THIS language regardless of source.
            from store import get_all_settings
            settings = await asyncio.to_thread(get_all_settings)
            target_lang = settings.get("default_brief_lang", "ru") or "ru"

            attribution = build_attribution(
                channel_name=stream.channelName,
                chunk_started_at_iso=chunk.startedAt.isoformat(timespec="seconds"),
                speaker_default=stream.speakerDefault,
            )
            items, usage = await asyncio.to_thread(
                extract_news_items,
                chunk.transcriptText or "",
                stream.channelName,
                chunk.startedAt.isoformat(timespec="seconds"),
                "auto",
                None,
                target_lang,
            )
            items_dict = [
                {
                    "headline": it.headline, "quote": it.quote,
                    "category": it.category, "offset_sec": it.offset_sec,
                    "confidence": it.confidence, "tags": it.tags,
                }
                for it in items
            ]
            n = await asyncio.to_thread(
                insert_news_items, stream.id, chunk.id, items_dict, attribution
            )
            await asyncio.to_thread(update_chunk, chunk.id, status="extracted")

            # Dedup pass: for each freshly-inserted item, compute embedding,
            # compare against recent items (within window), link as duplicate if sim > threshold.
            dedup_cfg = DedupConfig.from_settings(settings)
            dupes_found = 0
            if dedup_cfg.enabled and n > 0:
                try:
                    candidates = await asyncio.to_thread(
                        load_recent_embeddings, stream.id, dedup_cfg.window_hours,
                    )
                    # Fetch just-inserted items (newest N for this chunk)
                    fresh = await asyncio.to_thread(
                        _list_chunk_news_items, chunk.id,
                    )
                    for fitem in fresh:
                        vec = await asyncio.to_thread(
                            embed, fitem.headline, dedup_cfg.provider, dedup_cfg.model,
                        )
                        dup_id, sim = (None, 0.0)
                        if vec:
                            dup_id, sim = find_duplicate(
                                vec, candidates, dedup_cfg.similarity_threshold,
                            )
                        await asyncio.to_thread(
                            update_news_item_dedup,
                            fitem.id, vec, dup_id, sim if vec else None,
                        )
                        if dup_id:
                            dupes_found += 1
                        # Append to candidates so next item in same chunk can match too
                        if vec:
                            candidates.append((fitem.id, vec))
                except Exception as e:
                    print(f"[dedup] pass failed: {e}", file=sys.stderr)

            print(
                f"[extract] chunk {chunk.index}: {n} items ({dupes_found} dupes), "
                f"tokens in={usage['input_tokens']} out={usage['output_tokens']}",
                file=sys.stderr,
            )
        except Exception as e:
            await asyncio.to_thread(
                update_chunk, chunk.id, status="failed", error=f"extract: {e}"
            )
            print(f"[extract] FAILED chunk {chunk.id}: {e}", file=sys.stderr)


# ─── Public API ───────────────────────────────────────────────────

async def startup() -> None:
    """Called from FastAPI lifespan on server start."""
    global _global_stop, _transcribe_task, _extract_task
    CHUNK_ROOT.mkdir(parents=True, exist_ok=True)
    _global_stop = asyncio.Event()
    global _cleanup_task
    _transcribe_task = asyncio.create_task(_transcribe_worker(), name="transcribe-worker")
    _extract_task    = asyncio.create_task(_extract_worker(),    name="extract-worker")
    _cleanup_task    = asyncio.create_task(_cleanup_worker(),    name="cleanup-worker")
    # Resume streams that were active when the server was previously stopped
    streams = await asyncio.to_thread(list_streams, "active")
    for s in streams:
        await _start_stream_workers(s.id, s.url, s.intervalMin)
    print(
        f"[orchestrator] startup complete. "
        f"Resumed {len(streams)} active stream(s). Global workers up.",
        file=sys.stderr,
    )


async def shutdown() -> None:
    """Called from FastAPI lifespan on server stop."""
    if _global_stop:
        _global_stop.set()
    for sid in list(_stream_tasks.keys()):
        await _stop_stream_workers(sid)
    for task in (_transcribe_task, _extract_task, _cleanup_task):
        if task:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
    print("[orchestrator] shutdown complete", file=sys.stderr)


async def start_stream(stream_id: str, url: str, interval_min: int) -> None:
    await _start_stream_workers(stream_id, url, interval_min)
    await asyncio.to_thread(update_stream_status, stream_id, "active")


async def pause_stream(stream_id: str) -> None:
    await _stop_stream_workers(stream_id)
    await asyncio.to_thread(update_stream_status, stream_id, "paused")


async def stop_stream(stream_id: str) -> None:
    await _stop_stream_workers(stream_id)
    await asyncio.to_thread(update_stream_status, stream_id, "stopped")
    # Auto-brief on stop, if enabled
    from store import get_stream
    s = await asyncio.to_thread(get_stream, stream_id)
    if s and s.makeSummaryBrief and s.autoBriefOnStop:
        try:
            await trigger_stream_brief(stream_id, template=s.briefTemplate)
        except Exception as e:
            print(f"[orchestrator] auto-brief failed for {stream_id}: {e}", file=sys.stderr)


async def trigger_stream_brief(stream_id: str, template: str = "news") -> dict:
    """Run summarize_stream and persist StreamBrief. Can also be called on-demand."""
    from brief import summarize_stream
    from store import calculate_cost, get_all_settings, save_stream_brief

    settings = await asyncio.to_thread(get_all_settings)
    language = settings.get("default_brief_lang", "ru") or "ru"

    result = await asyncio.to_thread(
        summarize_stream, stream_id, template, language, "markdown", None,
    )
    cost = calculate_cost(result["model"], result["usage"])
    brief = await asyncio.to_thread(
        save_stream_brief,
        stream_id, template, result["content_md"], result.get("content_json"),
        result["model"], result["usage"], cost, result["chunks_covered"],
    )
    print(
        f"[orchestrator] stream brief saved (#{brief.id}, {template}, "
        f"{result['chunks_covered']} chunks, ${cost or 0:.4f})",
        file=sys.stderr,
    )
    return {"id": brief.id, "chunks_covered": result["chunks_covered"], "cost_usd": cost}


def active_stream_ids() -> list[str]:
    return list(_stream_tasks.keys())
