"""Unit tests for transcript-edit prompt routing.

`build_edit_prompt` is pure (no DB/network), so we assert op → system-prompt
wiring directly. Guards against an op silently falling back to the wrong prompt.
"""
import transcript_edit as te

_OPS = ["improve", "structure", "clean", "chat"]


def _build(op):
    return te.build_edit_prompt(
        op=op,
        current_text="Приветствую, уважаемые трейдеры. Робот строит спред.",
        instruction="разбей по темам",
    )


def test_all_ops_registered():
    for op in _OPS:
        assert op in te.SYSTEM_PROMPTS, f"{op} missing from SYSTEM_PROMPTS"


def test_ops_select_their_own_system_prompt():
    for op in _OPS:
        system, _user = _build(op)
        assert system == te.SYSTEM_PROMPTS[op]


def test_user_message_carries_text_and_instruction():
    _system, user = _build("structure")
    assert "Приветствую, уважаемые трейдеры" in user
    assert "разбей по темам" in user


def test_unknown_op_falls_back_to_improve():
    system, _ = te.build_edit_prompt(op="bogus", current_text="x", instruction="")
    assert system == te.SYSTEM_PROMPTS["improve"]


def test_is_claude_recognises_claude_ids_and_aliases():
    assert te._is_claude("claude-sonnet-4-6")
    assert te._is_claude("sonnet")        # alias from brief._MODEL_ALIASES
    assert te._is_claude("")              # empty -> default Claude
    assert not te._is_claude("qwen2.5:7b")
    assert not te._is_claude("nomic-embed-text:latest")


import pytest
import store


@pytest.mark.asyncio
async def test_version_numbering_and_rollback(db):
    # Seed a video (FK target for TranscriptEdit).
    await db.video.create(data={"id": "vid1", "url": "u", "source": "test"})

    v1 = await store._create_transcript_edit(
        video_id="vid1", content_md="text v1", op="improve",
        instruction="", from_version=None, model="claude-sonnet-4-6",
        input_chars=10, elapsed_ms=5,
    )
    assert v1.version == 1

    v2 = await store._create_transcript_edit(
        video_id="vid1", content_md="text v2", op="structure",
        instruction="разбей", from_version=1, model="claude-sonnet-4-6",
        input_chars=12, elapsed_ms=6,
    )
    assert v2.version == 2

    versions = await store._list_transcript_edits("vid1")
    assert [r.version for r in versions] == [2, 1]  # newest first

    # Rollback to v1 -> new v3 cloned from v1.
    v3 = await store._rollback_transcript_edit("vid1", 1)
    assert v3.version == 3
    assert v3.contentMd == "text v1"
    assert v3.op == "rollback"
    assert v3.fromVersion == 1
