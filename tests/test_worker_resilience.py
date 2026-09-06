"""Background workers must survive a transient error, and a dead capture must
not keep reporting itself as running.

Both loops held their try/except around only the expensive inner call, so an
error from the polling query — or from touching chunk.stream — escaped the
`while` and killed the task for the lifetime of the process. Nothing printed:
the task object lives in a module global, so it is never garbage-collected and
Python's "Task exception was never retrieved" warning never fires.
"""
import asyncio

import pytest

import orchestrator


async def test_transcribe_worker_survives_a_failing_poll(monkeypatch):
    calls = {"n": 0}

    def flaky_poll(status, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("временный сбой БД")
        orchestrator._global_stop.set()
        return []

    monkeypatch.setattr(orchestrator, "get_pending_chunks", flaky_poll)
    monkeypatch.setattr(orchestrator, "_global_stop", asyncio.Event())

    await asyncio.wait_for(orchestrator._transcribe_worker(), timeout=10)
    assert calls["n"] >= 2, "воркер умер на первой же ошибке опроса"


async def test_extract_worker_survives_a_failing_poll(monkeypatch):
    calls = {"n": 0}

    def flaky_poll(status, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("временный сбой БД")
        orchestrator._global_stop.set()
        return []

    monkeypatch.setattr(orchestrator, "get_pending_chunks", flaky_poll)
    monkeypatch.setattr(orchestrator, "_global_stop", asyncio.Event())

    await asyncio.wait_for(orchestrator._extract_worker(), timeout=10)
    assert calls["n"] >= 2, "воркер умер на первой же ошибке опроса"


async def test_finished_capture_stops_being_reported_as_active(monkeypatch):
    """capture_loop breaks out after 5 consecutive failures, but its key stayed
    in _stream_tasks, so the UI kept showing a live green dot on dead recording."""
    orchestrator._stream_tasks.clear()

    async def dead_capture(**kw):
        return  # what capture_loop does after giving up

    monkeypatch.setattr(orchestrator, "capture_loop", dead_capture)
    monkeypatch.setattr(orchestrator, "CHUNK_ROOT", orchestrator.CHUNK_ROOT)

    await orchestrator._start_stream_workers("vidX", "http://example", 5)
    entry = orchestrator._stream_tasks.get("vidX")
    assert entry is not None
    await asyncio.wait_for(entry["task"], timeout=10)
    await asyncio.sleep(0)

    assert "vidX" not in orchestrator.active_stream_ids(), \
        "мёртвая запись всё ещё числится активной"
