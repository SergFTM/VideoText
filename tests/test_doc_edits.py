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
