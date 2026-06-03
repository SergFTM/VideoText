"""Versioned doc-edits generalized by `kind` (transcript | brief | essence)."""
import pytest
import store


@pytest.mark.asyncio
async def test_versions_are_isolated_per_kind(db):
    await db.video.create(data={"id": "vid1", "url": "u", "source": "test"})

    t1 = await store._create_transcript_edit(
        video_id="vid1", kind="transcript", content_md="T1", op="improve",
        instruction="", from_version=None, model="m", input_chars=2, elapsed_ms=1,
    )
    b1 = await store._create_transcript_edit(
        video_id="vid1", kind="brief", content_md="B1", op="improve",
        instruction="", from_version=None, model="m", input_chars=2, elapsed_ms=1,
    )
    t2 = await store._create_transcript_edit(
        video_id="vid1", kind="transcript", content_md="T2", op="clean",
        instruction="", from_version=1, model="m", input_chars=2, elapsed_ms=1,
    )

    # Each kind numbers from 1 independently.
    assert (t1.version, t2.version) == (1, 2)
    assert b1.version == 1

    tx = await store._list_transcript_edits("vid1", kind="transcript")
    assert [r.version for r in tx] == [2, 1]
    br = await store._list_transcript_edits("vid1", kind="brief")
    assert [r.contentMd for r in br] == ["B1"]


@pytest.mark.asyncio
async def test_rollback_is_per_kind(db):
    await db.video.create(data={"id": "vid2", "url": "u", "source": "test"})
    await store._create_transcript_edit(
        video_id="vid2", kind="essence", content_md="E1", op="seed",
        instruction="", from_version=None, model="m", input_chars=2, elapsed_ms=1,
    )
    await store._create_transcript_edit(
        video_id="vid2", kind="essence", content_md="E2", op="chat",
        instruction="", from_version=1, model="m", input_chars=2, elapsed_ms=1,
    )
    v3 = await store._rollback_transcript_edit("vid2", kind="essence", version=1)
    assert v3.version == 3
    assert v3.contentMd == "E1"
    assert v3.op == "rollback"
    assert v3.fromVersion == 1
    assert v3.kind == "essence"


import server


def _row(version, content):
    # Minimal stand-in for a TranscriptEdit row (newest-first lists).
    from types import SimpleNamespace
    return SimpleNamespace(version=version, contentMd=content)


def test_pick_current_prefers_latest_edit_else_original():
    assert server._pick_current("ORIG", []) == "ORIG"
    assert server._pick_current("ORIG", [_row(2, "V2"), _row(1, "V1")]) == "V2"


from httpx import ASGITransport, AsyncClient
from server import app


@pytest.mark.asyncio
async def test_get_docs_reports_original_per_kind(db):
    v = await db.video.create(data={"id": "vid3", "url": "u", "source": "test"})
    await db.segment.create(data={"videoId": v.id, "index": 0, "start": 0, "end": 1, "text": "hello"})
    await db.brief.create(data={
        "videoId": v.id, "model": "m", "language": "ru", "format": "markdown",
        "contentMd": "BRIEF BODY", "inputTokens": 0, "outputTokens": 0,
    })
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        rt = (await ac.get(f"/videos/{v.id}/docs/transcript")).json()
        rb = (await ac.get(f"/videos/{v.id}/docs/brief")).json()
        re = (await ac.get(f"/videos/{v.id}/docs/essence")).json()
        bad = await ac.get(f"/videos/{v.id}/docs/bogus")
    assert rt["has_original"] and rt["original_md"] == "hello"
    assert rb["has_original"] and rb["original_md"] == "BRIEF BODY"
    assert re["has_original"] is False and re["original_md"] == ""
    assert bad.status_code == 422


def test_assemble_essence_section_included_when_present():
    # Pure helper: builds the optional "## Суть" block fed into expand prompts.
    assert server._essence_section("") == ""
    block = server._essence_section("ядро материала")
    assert "Суть" in block and "ядро материала" in block


def test_seed_inputs_ok_strips_each_source_before_or():
    # The bug guarded against: ("   " or "content").strip() short-circuits to
    # the whitespace-only first operand, falsely rejecting a real second source.
    assert server._seed_inputs_ok("", "") is False
    assert server._seed_inputs_ok("   ", "") is False
    assert server._seed_inputs_ok("", "  \n  ") is False
    assert server._seed_inputs_ok("   ", "real brief") is True   # the bug case
    assert server._seed_inputs_ok("real transcript", "") is True
    assert server._seed_inputs_ok("tx", "br") is True
    # Defensive against `None` (defaults on missing keys).
    assert server._seed_inputs_ok(None, None) is False
    assert server._seed_inputs_ok(None, "x") is True
