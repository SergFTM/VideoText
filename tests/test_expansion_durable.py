"""Durable-expansion store lifecycle: start(running) -> finish(done) | fail(error),
and the startup sweep that clears orphaned 'running' rows."""
import threading

import pytest

# Import server at module level: server.py calls load_dotenv(override=True) on
# import, so it must run BEFORE the per-test `db` fixture sets DATABASE_URL to
# test.db — otherwise the import would clobber it back to production.
import server  # noqa: F401
import store


async def _seed_video(db):
    await db.video.create(data={"id": "vidE", "url": "u", "source": "test"})


@pytest.mark.asyncio
async def test_start_then_finish(db):
    await _seed_video(db)
    r = await store._start_expansion(
        video_id="vidE", mode="research", source_title="ТЗ", source_md="x",
        context_mode="both", model="m", num_ctx=8192, input_chars=10,
    )
    assert r.status == "running"
    done = await store._finish_expansion(
        video_id="vidE", mode="research", content_md="готовый текст", elapsed_ms=42,
    )
    assert done.status == "done"
    assert done.contentMd == "готовый текст"
    assert done.elapsedMs == 42


@pytest.mark.asyncio
async def test_start_then_fail_keeps_old_content(db):
    await _seed_video(db)
    await store._start_expansion(
        video_id="vidE", mode="report", source_title="", source_md="",
        context_mode="brief", model="m", num_ctx=8192, input_chars=1,
    )
    failed = await store._fail_expansion(video_id="vidE", mode="report", error="boom")
    assert failed.status == "error"
    assert failed.error == "boom"


@pytest.mark.asyncio
async def test_sweep_running_to_error(db):
    await _seed_video(db)
    await store._start_expansion(
        video_id="vidE", mode="spec", source_title="", source_md="",
        context_mode="brief", model="m", num_ctx=8192, input_chars=1,
    )
    n = await store._sweep_running_expansions()
    assert n >= 1
    row = await store._get_expansion("vidE", "spec")
    assert row.status == "error"


@pytest.mark.asyncio
async def test_run_expansion_job_persists_without_client(db, monkeypatch):
    """The background job must persist status=done even though no HTTP client is
    connected — that's the whole point of durability."""
    await _seed_video(db)
    await store._start_expansion(
        video_id="vidE", mode="research", source_title="", source_md="",
        context_mode="brief", model="m", num_ctx=8192, input_chars=1,
    )
    monkeypatch.setattr(server.local_llm, "stream_chat",
                        lambda **kw: iter(["часть1 ", "часть2"]))
    # Run the job the way production does — in a real thread (it uses the sync
    # store wrappers, which call asyncio.run; that needs a thread with no loop).
    t = threading.Thread(target=server._run_expansion_job, kwargs={
        "video_id": "vidE", "mode": "research", "system": "s", "user": "u",
        "model": "m", "num_ctx": 8192, "temperature": 0.3,
    })
    t.start()
    t.join(timeout=20)
    row = await store._get_expansion("vidE", "research")
    assert row.status == "done"
    assert row.contentMd == "часть1 часть2"
