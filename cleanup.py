"""Retention & storage cleanup primitives.

Design:
  - Pure functions — no auto-scheduling here. Orchestrator wires it on a timer.
  - Dry-run by default: every function returns a report of what WOULD be done;
    pass `commit=True` to actually delete.
  - Conservative: removes audio files before rows, rows only after audio gone,
    respects `retain_*` settings from DB.
  - Never touches video/segment/brief tables from the single-video pipeline
    unless explicitly asked (they have their own retention policy).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from prisma import Prisma
import asyncio


# ─── Stats ─────────────────────────────────────────────────────────

@dataclass
class StorageStats:
    disk_chunks_mb: float
    disk_output_mb: float
    disk_db_mb: float
    row_counts: dict[str, int]
    oldest: dict[str, str | None]          # ISO timestamps per table
    chunks_without_audio: int               # already cleaned
    chunks_failed: int
    retention_policy: dict                  # current active settings

    def as_dict(self) -> dict:
        return {
            "disk": {
                "chunks_mb": round(self.disk_chunks_mb, 2),
                "output_mb": round(self.disk_output_mb, 2),
                "db_mb": round(self.disk_db_mb, 2),
                "total_mb": round(self.disk_chunks_mb + self.disk_output_mb + self.disk_db_mb, 2),
            },
            "rows": self.row_counts,
            "oldest": self.oldest,
            "chunks_without_audio": self.chunks_without_audio,
            "chunks_failed": self.chunks_failed,
            "retention_policy": self.retention_policy,
        }


@dataclass
class CleanupReport:
    audio_files_deleted: int = 0
    audio_bytes_freed: int = 0
    chunk_rows_deleted: int = 0
    news_items_deleted: int = 0
    stream_briefs_deleted: int = 0
    output_files_deleted: int = 0
    output_bytes_freed: int = 0
    failed_chunks_deleted: int = 0
    committed: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "audio_files_deleted":   self.audio_files_deleted,
            "audio_mb_freed":        round(self.audio_bytes_freed / 1024 / 1024, 2),
            "chunk_rows_deleted":    self.chunk_rows_deleted,
            "news_items_deleted":    self.news_items_deleted,
            "stream_briefs_deleted": self.stream_briefs_deleted,
            "output_files_deleted":  self.output_files_deleted,
            "output_mb_freed":       round(self.output_bytes_freed / 1024 / 1024, 2),
            "failed_chunks_deleted": self.failed_chunks_deleted,
            "committed":             self.committed,
            "errors":                self.errors,
        }


# ─── Helpers ───────────────────────────────────────────────────────

def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _ago(days: int) -> datetime:
    return datetime.now() - timedelta(days=days)


# ─── Stats ─────────────────────────────────────────────────────────

async def _collect_stats(db: Prisma, retention: dict) -> StorageStats:
    chunks_dir = Path("./chunks")
    output_dir = Path("./output")
    db_file    = Path("./prisma/videotext.db")

    tables = ["video", "segment", "brief", "run", "livestream",
              "chunk", "newsitem", "streambrief", "appsetting"]
    row_counts: dict[str, int] = {}
    for t in tables:
        row_counts[t] = await getattr(db, t).count()

    oldest: dict[str, str | None] = {}
    for t in ("chunk", "newsitem", "streambrief", "video", "brief"):
        rec = await getattr(db, t).find_first(order={"createdAt": "asc"})
        oldest[t] = rec.createdAt.isoformat() if rec else None

    chunks_without_audio = await db.chunk.count(where={"audioPath": None})
    chunks_failed        = await db.chunk.count(where={"status": "failed"})

    return StorageStats(
        disk_chunks_mb = _dir_size_bytes(chunks_dir) / 1024 / 1024,
        disk_output_mb = _dir_size_bytes(output_dir) / 1024 / 1024,
        disk_db_mb     = (db_file.stat().st_size if db_file.exists() else 0) / 1024 / 1024,
        row_counts = row_counts,
        oldest = oldest,
        chunks_without_audio = chunks_without_audio,
        chunks_failed = chunks_failed,
        retention_policy = retention,
    )


def get_storage_stats(retention: dict) -> dict:
    async def _run():
        db = Prisma(); await db.connect()
        try:
            return (await _collect_stats(db, retention)).as_dict()
        finally:
            await db.disconnect()
    return asyncio.run(_run())


# ─── Cleanup steps ─────────────────────────────────────────────────

async def _purge_audio_files(db: Prisma, days: int, report: CleanupReport, commit: bool) -> None:
    if days <= 0:
        return
    cutoff = _ago(days)
    # Find chunks where audio still present AND older than cutoff
    chunks = await db.chunk.find_many(
        where={
            "createdAt": {"lt": cutoff},
            "NOT": [{"audioPath": None}],
        },
    )
    for c in chunks:
        if not c.audioPath:
            continue
        p = Path(c.audioPath)
        size = 0
        if p.exists():
            try: size = p.stat().st_size
            except OSError: pass
        report.audio_files_deleted += 1
        report.audio_bytes_freed += size
        if commit:
            try:
                if p.exists():
                    p.unlink()
                await db.chunk.update(
                    where={"id": c.id},
                    data={"audioPath": None, "audioDeletedAt": datetime.now()},
                )
            except Exception as e:
                report.errors.append(f"audio delete failed {c.id}: {e}")


async def _purge_old_chunks(db: Prisma, days: int, report: CleanupReport, commit: bool) -> None:
    if days <= 0:
        return
    cutoff = _ago(days)
    chunks = await db.chunk.find_many(where={"createdAt": {"lt": cutoff}})
    report.chunk_rows_deleted += len(chunks)
    if commit:
        for c in chunks:
            # Delete audio file if still present
            if c.audioPath and Path(c.audioPath).exists():
                try: Path(c.audioPath).unlink()
                except OSError: pass
        await db.chunk.delete_many(where={"createdAt": {"lt": cutoff}})


async def _purge_news_items(db: Prisma, days: int, report: CleanupReport, commit: bool) -> None:
    if days <= 0:
        return
    cutoff = _ago(days)
    n = await db.newsitem.count(where={"createdAt": {"lt": cutoff}})
    report.news_items_deleted += n
    if commit:
        await db.newsitem.delete_many(where={"createdAt": {"lt": cutoff}})


async def _purge_stream_briefs(db: Prisma, days: int, report: CleanupReport, commit: bool) -> None:
    if days <= 0:
        return
    cutoff = _ago(days)
    n = await db.streambrief.count(where={"createdAt": {"lt": cutoff}})
    report.stream_briefs_deleted += n
    if commit:
        await db.streambrief.delete_many(where={"createdAt": {"lt": cutoff}})


async def _purge_failed_chunks(db: Prisma, days: int, report: CleanupReport, commit: bool) -> None:
    if days <= 0:
        return
    cutoff = _ago(days)
    chunks = await db.chunk.find_many(
        where={"status": "failed", "createdAt": {"lt": cutoff}},
    )
    report.failed_chunks_deleted += len(chunks)
    if commit:
        for c in chunks:
            if c.audioPath and Path(c.audioPath).exists():
                try: Path(c.audioPath).unlink()
                except OSError: pass
        await db.chunk.delete_many(
            where={"status": "failed", "createdAt": {"lt": cutoff}},
        )


def _purge_output_dir(days: int, report: CleanupReport, commit: bool) -> None:
    if days <= 0:
        return
    output = Path("./output")
    if not output.exists():
        return
    cutoff_ts = (_ago(days)).timestamp()
    for p in output.iterdir():
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff_ts:
                size = p.stat().st_size
                report.output_files_deleted += 1
                report.output_bytes_freed += size
                if commit:
                    p.unlink()
        except OSError as e:
            report.errors.append(f"output delete failed {p}: {e}")


# ─── Orchestration ─────────────────────────────────────────────────

async def _run_cleanup(retention: dict, commit: bool) -> CleanupReport:
    report = CleanupReport(committed=commit)
    db = Prisma(); await db.connect()
    try:
        # Order matters: failed chunks first (aggressive), then audio (keep rows),
        # then news + briefs, then old chunks entirely, then output/ files.
        await _purge_failed_chunks(db, retention.get("retain_failed_chunks_days", 0) or 0, report, commit)
        await _purge_audio_files(db, retention.get("retain_chunk_audio_days", 0) or 0, report, commit)
        await _purge_news_items(db, retention.get("retain_news_item_days", 0) or 0, report, commit)
        await _purge_stream_briefs(db, retention.get("retain_stream_brief_days", 0) or 0, report, commit)
        await _purge_old_chunks(db, retention.get("retain_chunk_row_days", 0) or 0, report, commit)
    finally:
        await db.disconnect()
    _purge_output_dir(retention.get("retain_video_output_files_days", 0) or 0, report, commit)
    return report


def run_cleanup(retention: dict, commit: bool = False) -> dict:
    """Sync entrypoint. Pass `commit=True` to actually delete; default is dry-run."""
    return asyncio.run(_run_cleanup(retention, commit)).as_dict()
