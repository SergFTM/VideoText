"""Durable-expansion store lifecycle: start(running) -> finish(done) | fail(error),
and the startup sweep that clears orphaned 'running' rows."""
import pytest
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
